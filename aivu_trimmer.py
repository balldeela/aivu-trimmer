#!/usr/bin/env python3
"""
AIVU Trimmer — set in/out points on a visual player and export a trimmed copy.

Controls:
  Space        play / pause
  I            set in point at current position
  O            set out point at current position
"""

import os
import subprocess
import threading
import time

import objc
from Foundation import NSObject, NSURL
from AppKit import (
    NSApplication, NSWindow, NSView, NSButton, NSTextField,
    NSColor, NSFont, NSMakeRect, NSBezierPath,
    NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
    NSAlert, NSSavePanel, NSOpenPanel,
    NSBezelStyleRounded, NSTextAlignmentLeft,
    NSBackingStoreBuffered, NSApplicationActivationPolicyRegular,
    NSProgressIndicator, NSProgressIndicatorStyleBar,
    NSViewWidthSizable, NSViewHeightSizable, NSViewMaxYMargin,
    NSViewMinXMargin, NSPopUpButton, NSSlider,
)
import Quartz
from AVFoundation import (
    AVPlayer, AVPlayerItem, AVAsset,
    AVPlayerTimeControlStatusPlaying,
    AVMutableVideoComposition,
)
from AVKit import AVPlayerView
from Foundation import NSData
from Quartz import CIFilter
import CoreMedia
from CoreMedia import CMTimeMakeWithSeconds, CMTimeGetSeconds, kCMTimeZero

ASSUMED_FPS = 45.0

# Rectilinear (mono) export geometry, derived from the stereoscopic ST map.
EYE_SIZE = 4320          # source single-eye square the ST map was authored for
RECTI_SQUARE = 2048      # remap output (per eye) before cropping
RECTI_OUT_W = 2048
RECTI_OUT_H = 1152       # 16:9 crop of the square
RECTI_CROP_Y = (RECTI_SQUARE - RECTI_OUT_H) // 2

# Default location of the Blackmagic immersive ST map EXR (user-provided).
DEFAULT_STMAP = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/Downloads/"
    "URSA_Immersive_Stereoscopic_v01_STMap.0000.exr"
)

# Neutral grade (no-op)
GRADE_NEUTRAL = {"contrast": 1.0, "gamma": 1.0, "gain": 1.0}


def grade_is_neutral(g):
    return (abs(g["contrast"] - 1.0) < 1e-3 and
            abs(g["gamma"] - 1.0) < 1e-3 and
            abs(g["gain"] - 1.0) < 1e-3)


def ffmpeg_grade_expr(g):
    """A single lutrgb expression applying gain -> contrast -> gamma, or None."""
    if grade_is_neutral(g):
        return None
    K, C, G = g["gain"], g["contrast"], g["gamma"]
    return ("clip(pow(clip(((val/maxval)*%s-0.5)*%s+0.5,0,1),1/%s)*maxval,0,maxval)"
            % (K, C, G))


def seconds_to_tc(secs: float) -> str:
    """Convert seconds to HH:MM:SS:FF timecode (45 fps)."""
    if secs < 0:
        secs = 0.0
    total_frames = int(round(secs * ASSUMED_FPS))
    fps = int(ASSUMED_FPS)
    frames = total_frames % fps
    total_secs = total_frames // fps
    ss = total_secs % 60
    mm = (total_secs // 60) % 60
    hh = total_secs // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{frames:02d}"


# ---------------------------------------------------------------------------
# Custom timeline NSView
# ---------------------------------------------------------------------------

class TimelineView(NSView):

    @objc.python_method
    def setup(self, app_delegate):
        self._app = app_delegate
        self._dragging = None

    def isFlipped(self):
        return False

    def drawRect_(self, rect):
        app = self._app
        h = self.bounds().size.height

        NSColor.colorWithRed_green_blue_alpha_(0.15, 0.15, 0.15, 1).set()
        NSBezierPath.fillRect_(self.bounds())

        if app._duration <= 0:
            return

        w = self.bounds().size.width

        def x_for(secs):
            return (secs / app._duration) * w

        cur = CMTimeGetSeconds(app._player.currentTime()) if app._player else 0.0

        # Played region
        NSColor.colorWithRed_green_blue_alpha_(0.2, 0.5, 0.5, 1).set()
        NSBezierPath.fillRect_(NSMakeRect(0, 0, x_for(cur), h))

        # In/Out highlight
        in_x = x_for(app._in_point)
        out_x = x_for(app._out_point)
        NSColor.colorWithRed_green_blue_alpha_(0.4, 0.4, 0.0, 0.35).set()
        NSBezierPath.fillRect_(NSMakeRect(in_x, 0, out_x - in_x, h))

        # In marker — yellow
        NSColor.yellowColor().set()
        NSBezierPath.fillRect_(NSMakeRect(in_x - 2, 0, 4, h))

        # Out marker — red
        NSColor.redColor().set()
        NSBezierPath.fillRect_(NSMakeRect(out_x - 2, 0, 4, h))

        # Playhead — white
        NSColor.whiteColor().set()
        NSBezierPath.fillRect_(NSMakeRect(x_for(cur) - 1, 0, 2, h))

    def mouseDown_(self, event):
        self._start_drag(event)

    def mouseDragged_(self, event):
        self._continue_drag(event)

    def mouseUp_(self, event):
        self._dragging = None

    @objc.python_method
    def _fraction_from_event(self, event):
        loc = self.convertPoint_fromView_(event.locationInWindow(), None)
        w = self.bounds().size.width
        if w <= 0:
            return 0.0
        return max(0.0, min(loc.x / w, 1.0))

    def scrollWheel_(self, event):
        app = self._app
        if app._player is None or app._duration <= 0:
            return
        # Each scroll tick nudges ±1 second
        delta = event.scrollingDeltaY()
        if delta == 0:
            delta = -event.deltaY()
        nudge = -1.0 if delta > 0 else 1.0
        cur = CMTimeGetSeconds(app._player.currentTime())
        app.seekToSeconds_(cur + nudge)
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _start_drag(self, event):
        app = self._app
        if app._duration <= 0:
            return
        frac = self._fraction_from_event(event)
        w = self.bounds().size.width
        in_x = (app._in_point / app._duration) * w
        out_x = (app._out_point / app._duration) * w
        px = frac * w
        if abs(px - in_x) < 12:
            self._dragging = 'in'
        elif abs(px - out_x) < 12:
            self._dragging = 'out'
        else:
            self._dragging = 'seek'
        self._continue_drag(event)

    @objc.python_method
    def _continue_drag(self, event):
        app = self._app
        if app._duration <= 0:
            return
        frac = self._fraction_from_event(event)
        secs = frac * app._duration
        if self._dragging == 'in':
            app._in_point = min(secs, app._out_point - 0.05)
            app.refreshLabels()
        elif self._dragging == 'out':
            app._out_point = max(secs, app._in_point + 0.05)
            app.refreshLabels()
        else:
            app.seekToSeconds_(secs)
        self.setNeedsDisplay_(True)

    def acceptsFirstResponder(self):
        return True


# ---------------------------------------------------------------------------
# App delegate
# ---------------------------------------------------------------------------

class AivuTrimmerApp(NSObject):

    def init(self):
        self = objc.super(AivuTrimmerApp, self).init()
        if self is None:
            return None
        self._duration = 0.0
        self._in_point = 0.0
        self._out_point = 0.0
        self._player = None
        self._player_item = None
        self._time_observer = None
        self._source_path = None
        self._window = None
        self._player_view = None
        self._timeline_view = None
        self._tc_label = None
        self._in_label = None
        self._out_label = None
        self._play_btn = None
        self._export_btn = None
        self._sbs_btn = None
        self._status_label = None
        self._progress = None
        self._lut_popup = None
        self._luts = []        # list of (label, path); first entry is "No LUT"
        self._status_base = ""
        self._asset = None     # current AVAsset (for preview composition)
        self._cube_cache = {}  # path -> (size, NSData) parsed .cube
        self._zoom = 1.0
        # Color grade
        self._grade = dict(GRADE_NEUTRAL)
        self._recti_btn = None
        self._sliders = {}     # name -> NSSlider
        self._slider_vals = {} # name -> NSTextField (value readout)
        self._stmap_path = DEFAULT_STMAP if os.path.isfile(DEFAULT_STMAP) else None
        return self

    # ------------------------------------------------------------------ #
    #  App lifecycle                                                       #
    # ------------------------------------------------------------------ #

    def applicationDidFinishLaunching_(self, notification):
        self.buildWindow()
        # Allow launching with a file path: `python3 aivu_trimmer.py movie.aivu`
        import sys
        args = [a for a in sys.argv[1:] if os.path.isfile(a)]
        if args:
            self.loadFile_(args[0])
        else:
            self.promptOpenFile()

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return True

    # ------------------------------------------------------------------ #
    #  Window                                                              #
    # ------------------------------------------------------------------ #

    def buildWindow(self):
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable |
                 NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(100, 100, 1000, 720), style, NSBackingStoreBuffered, False,
        )
        win.setTitle_("AIVU Trimmer")
        win.setMinSize_((900, 560))
        c = win.contentView()

        # Player view — grows with the window; bottom pinned at y=228.
        pv = AVPlayerView.alloc().initWithFrame_(NSMakeRect(0, 228, 1000, 484))
        pv.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        pv.setControlsStyle_(0)
        pv.setWantsLayer_(True)
        pv.layer().setMasksToBounds_(True)
        c.addSubview_(pv)
        self._player_view = pv

        # Timeline — fixed height, fixed distance above the bottom controls.
        tv = TimelineView.alloc().initWithFrame_(NSMakeRect(0, 176, 1000, 46))
        tv.setup(self)
        tv.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        c.addSubview_(tv)
        self._timeline_view = tv

        # --- Grade sliders row (y≈118) ---
        self.addSlider_name_x_y_width_lo_hi_val_caption_(
            c, "contrast", 10, 122, 230, 0.5, 2.0, 1.0, "Contrast")
        self.addSlider_name_x_y_width_lo_hi_val_caption_(
            c, "gamma", 256, 122, 230, 0.4, 2.5, 1.0, "Gamma")
        self.addSlider_name_x_y_width_lo_hi_val_caption_(
            c, "gain", 502, 122, 230, 0.5, 2.0, 1.0, "Gain")
        reset = self.addButton_title_rect_action_(
            c, "Reset", NSMakeRect(742, 120, 64, 26), "resetGrade:")
        reset.setToolTip_("Reset contrast / gamma / gain to neutral.")

        # Zoom controls (far right of the grade row, tracks right edge)
        zout = self.addButton_title_rect_action_(c, "Zoom −", NSMakeRect(818, 120, 56, 26), "zoomOut:")
        zin  = self.addButton_title_rect_action_(c, "Zoom +", NSMakeRect(878, 120, 56, 26), "zoomIn:")
        zfit = self.addButton_title_rect_action_(c, "Fit",    NSMakeRect(938, 120, 50, 26), "zoomReset:")
        for b in (zout, zin, zfit):
            b.setAutoresizingMask_(NSViewMinXMargin)

        # --- Timecode / IN / OUT row (y≈82) + LUT selector on the right ---
        tc = self.makeTextField_rect_font_color_(
            "00:00:00:00", NSMakeRect(10, 82, 220, 28),
            NSFont.monospacedDigitSystemFontOfSize_weight_(18, 0), NSColor.whiteColor())
        c.addSubview_(tc)
        self._tc_label = tc

        in_lbl = self.makeTextField_rect_font_color_(
            "IN:  00:00:00:00", NSMakeRect(238, 82, 210, 28),
            NSFont.monospacedDigitSystemFontOfSize_weight_(14, 0), NSColor.yellowColor())
        c.addSubview_(in_lbl)
        self._in_label = in_lbl

        out_lbl = self.makeTextField_rect_font_color_(
            "OUT: 00:00:00:00", NSMakeRect(452, 82, 210, 28),
            NSFont.monospacedDigitSystemFontOfSize_weight_(14, 0), NSColor.redColor())
        c.addSubview_(out_lbl)
        self._out_label = out_lbl

        lut_cap = self.makeTextField_rect_font_color_(
            "Color LUT (preview + export):", NSMakeRect(718, 104, 272, 14),
            NSFont.systemFontOfSize_(10), NSColor.lightGrayColor())
        lut_cap.setAutoresizingMask_(NSViewMinXMargin)
        c.addSubview_(lut_cap)

        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(718, 80, 272, 26), False)
        popup.setAutoresizingMask_(NSViewMinXMargin)
        self._luts = self.discoverLUTs()
        for label, _ in self._luts:
            popup.addItemWithTitle_(label)
        popup.setToolTip_("Previews a 3D LUT on the player and bakes it into the "
                          "MP4 exports.")
        popup.setTarget_(self)
        popup.setAction_("lutChanged:")
        c.addSubview_(popup)
        self._lut_popup = popup

        # --- Buttons row (y=42) ---
        self.addButton_title_rect_action_(c, "▶  Play",  NSMakeRect(10,  42, 64, 32), "togglePlayPause:")
        self.addButton_title_rect_action_(c, "🟡 Set In",  NSMakeRect(78,  42, 84, 32), "setInPoint:")
        self.addButton_title_rect_action_(c, "🔴 Set Out", NSMakeRect(166, 42, 90, 32), "setOutPoint:")
        self.addButton_title_rect_action_(c, "Open…",     NSMakeRect(260, 42, 64, 32), "openFile:")

        exp_btn = self.addButton_title_rect_action_(c, "Export .aivu…", NSMakeRect(330, 42, 128, 32), "exportTrimmed:")
        exp_btn.setEnabled_(False)
        self._export_btn = exp_btn

        sbs_btn = self.addButton_title_rect_action_(c, "Export SBS MP4 (Quest)…", NSMakeRect(464, 42, 196, 32), "exportSBS:")
        sbs_btn.setEnabled_(False)
        self._sbs_btn = sbs_btn

        recti_btn = self.addButton_title_rect_action_(c, "Export Rectilinear 16:9…", NSMakeRect(666, 42, 200, 32), "exportRectilinear:")
        recti_btn.setEnabled_(False)
        self._recti_btn = recti_btn

        # Keep a ref to play button to update its title
        for sub in c.subviews():
            if hasattr(sub, 'title') and sub.title() == "▶  Play":
                self._play_btn = sub
                break

        # Status label (shows export progress text)
        status = self.makeTextField_rect_font_color_(
            "", NSMakeRect(10, 8, 700, 18),
            NSFont.systemFontOfSize_(11), NSColor.lightGrayColor(),
        )
        c.addSubview_(status)
        self._status_label = status

        # Progress bar (own row along the bottom)
        prog = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(720, 7, 270, 18))
        prog.setStyle_(NSProgressIndicatorStyleBar)
        prog.setIndeterminate_(True)
        prog.setHidden_(True)
        prog.setAutoresizingMask_(NSViewMinXMargin)
        c.addSubview_(prog)
        self._progress = prog

        # Dark bg
        c.setWantsLayer_(True)
        c.layer().setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.1, 0.1, 0.1, 1.0).CGColor()
        )

        win.setDelegate_(self)
        win.makeKeyAndOrderFront_(None)
        self._window = win

    def makeTextField_rect_font_color_(self, text, rect, font, color):
        f = NSTextField.alloc().initWithFrame_(rect)
        f.setStringValue_(text)
        f.setFont_(font)
        f.setTextColor_(color)
        f.setBackgroundColor_(NSColor.clearColor())
        f.setBezeled_(False)
        f.setEditable_(False)
        f.setAlignment_(NSTextAlignmentLeft)
        return f

    def addButton_title_rect_action_(self, parent, title, rect, action):
        btn = NSButton.alloc().initWithFrame_(rect)
        btn.setTitle_(title)
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setTarget_(self)
        btn.setAction_(action)
        parent.addSubview_(btn)
        return btn

    def addSlider_name_x_y_width_lo_hi_val_caption_(
            self, parent, name, x, y, width, lo, hi, val, caption):
        cap = self.makeTextField_rect_font_color_(
            caption, NSMakeRect(x, y + 22, width, 13),
            NSFont.systemFontOfSize_(10), NSColor.lightGrayColor())
        parent.addSubview_(cap)

        s = NSSlider.alloc().initWithFrame_(NSMakeRect(x, y, width - 50, 20))
        s.setMinValue_(lo)
        s.setMaxValue_(hi)
        s.setDoubleValue_(val)
        s.setContinuous_(True)
        s.setTarget_(self)
        s.setAction_("gradeChanged:")
        parent.addSubview_(s)
        self._sliders[name] = s

        vlbl = self.makeTextField_rect_font_color_(
            "%.2f" % val, NSMakeRect(x + width - 46, y, 44, 18),
            NSFont.monospacedDigitSystemFontOfSize_weight_(11, 0), NSColor.whiteColor())
        parent.addSubview_(vlbl)
        self._slider_vals[name] = vlbl
        return s

    # ------------------------------------------------------------------ #
    #  File open                                                           #
    # ------------------------------------------------------------------ #

    def promptOpenFile(self):
        panel = NSOpenPanel.openPanel()
        panel.setAllowedFileTypes_(["aivu", "mov", "mp4", "m4v"])
        panel.setTitle_("Open AIVU File")
        if panel.runModal() == 1:
            self.loadFile_(str(panel.URL().path()))

    def openFile_(self, sender):
        self.promptOpenFile()

    def loadFile_(self, path):
        self._source_path = path
        self._window.setTitle_(f"AIVU Trimmer — {os.path.basename(path)}")

        url = NSURL.fileURLWithPath_(path)
        asset = AVAsset.assetWithURL_(url)
        item = AVPlayerItem.playerItemWithAsset_(asset)
        self._player_item = item
        self._asset = asset

        if self._player is None:
            player = AVPlayer.playerWithPlayerItem_(item)
            self._player = player
            self._player_view.setPlayer_(player)
        else:
            self._player.replaceCurrentItemWithPlayerItem_(item)

        self.startTimeObserver()
        self.applyPreviewProcessing()   # carry grade + LUT onto the new item

        def poll_duration():
            for _ in range(100):
                dur = CMTimeGetSeconds(asset.duration())
                if dur and dur > 0:
                    break
                time.sleep(0.1)
            else:
                dur = 0.0
            self._duration = dur
            self._in_point = 0.0
            self._out_point = dur
            self.refreshLabels()
            self._timeline_view.setNeedsDisplay_(True)
            self._export_btn.setEnabled_(True)
            self._sbs_btn.setEnabled_(True)
            self._recti_btn.setEnabled_(True)

        threading.Thread(target=poll_duration, daemon=True).start()

    def startTimeObserver(self):
        if self._time_observer and self._player:
            self._player.removeTimeObserver_(self._time_observer)
            self._time_observer = None

        interval = CMTimeMakeWithSeconds(1.0 / ASSUMED_FPS, 90000)

        def on_time(cm_time):
            secs = CMTimeGetSeconds(cm_time)
            self._tc_label.setStringValue_(seconds_to_tc(secs))
            self._timeline_view.setNeedsDisplay_(True)

        self._time_observer = self._player.addPeriodicTimeObserverForInterval_queue_usingBlock_(
            interval, None, on_time
        )

    # ------------------------------------------------------------------ #
    #  Zoom                                                                #
    # ------------------------------------------------------------------ #

    def zoomIn_(self, sender):
        self._zoom = min(self._zoom * 1.25, 8.0)
        self.applyZoom()

    def zoomOut_(self, sender):
        self._zoom = max(self._zoom / 1.25, 1.0)
        self.applyZoom()

    def zoomReset_(self, sender):
        self._zoom = 1.0
        self.applyZoom()

    def applyZoom(self):
        pv = self._player_view
        if pv is None:
            return
        layer = pv.layer()
        if layer is None:
            return
        b = pv.bounds().size
        z = self._zoom
        # Scale the video content around the center of the player view.
        t = Quartz.CATransform3DMakeTranslation(b.width / 2.0, b.height / 2.0, 0)
        t = Quartz.CATransform3DScale(t, z, z, 1.0)
        t = Quartz.CATransform3DTranslate(t, -b.width / 2.0, -b.height / 2.0, 0)
        layer.setSublayerTransform_(t)

    # NSWindowDelegate — reapply zoom so it stays centered after a resize.
    def windowDidResize_(self, notification):
        self.applyZoom()

    # ------------------------------------------------------------------ #
    #  Playback                                                            #
    # ------------------------------------------------------------------ #

    def togglePlayPause_(self, sender):
        if self._player is None:
            return
        if self._player.timeControlStatus() == AVPlayerTimeControlStatusPlaying:
            self._player.pause()
            if self._play_btn:
                self._play_btn.setTitle_("▶  Play")
        else:
            self._player.play()
            if self._play_btn:
                self._play_btn.setTitle_("⏸  Pause")

    def setInPoint_(self, sender):
        if self._player is None:
            return
        self._in_point = CMTimeGetSeconds(self._player.currentTime())
        if self._in_point >= self._out_point:
            self._out_point = min(self._in_point + 1.0, self._duration)
        self.refreshLabels()
        self._timeline_view.setNeedsDisplay_(True)

    def setOutPoint_(self, sender):
        if self._player is None:
            return
        self._out_point = CMTimeGetSeconds(self._player.currentTime())
        if self._out_point <= self._in_point:
            self._in_point = max(self._out_point - 1.0, 0.0)
        self.refreshLabels()
        self._timeline_view.setNeedsDisplay_(True)

    def seekToSeconds_(self, secs):
        if self._player is None or self._duration <= 0:
            return
        secs = max(0.0, min(secs, self._duration))
        # Use zero tolerance for sample-accurate scrubbing — the default
        # seekToTime_ snaps to the nearest keyframe (only a handful exist
        # in MV-HEVC), causing the playhead to jump in large chunks.
        t = CMTimeMakeWithSeconds(secs, 90000)
        self._player.seekToTime_toleranceBefore_toleranceAfter_(
            t, kCMTimeZero, kCMTimeZero
        )

    def refreshLabels(self):
        self._in_label.setStringValue_(f"IN:  {seconds_to_tc(self._in_point)}")
        self._out_label.setStringValue_(f"OUT: {seconds_to_tc(self._out_point)}")

    # ------------------------------------------------------------------ #
    #  Export                                                              #
    # ------------------------------------------------------------------ #

    def exportTrimmed_(self, sender):
        if not self._source_path:
            return
        trim_dur = self._out_point - self._in_point
        if trim_dur <= 0:
            self.showAlert_message_("Invalid range", "Out point must be after in point.")
            return

        graded = self.processingActive()
        base, ext = os.path.splitext(self._source_path)

        if graded:
            # A grade/LUT is active: the only way to bake it in is to re-encode,
            # which flattens to mono and drops the immersive metadata.
            self.showAlert_message_(
                "Re-encoding graded video",
                "A color grade or LUT is active. The exported file will be "
                "re-encoded with the look baked in — which makes it a mono, "
                "standard (non-immersive) movie.\n\nFor a true lossless immersive "
                ".aivu, reset the grade and set the LUT to “No LUT”.")
            suggested = os.path.basename(base + "_graded.mov")
            allowed = ["mov", "mp4", "m4v"]
        else:
            suggested = os.path.basename(base + "_trimmed" + ext)
            allowed = ["aivu", "mov", "mp4", "m4v"]

        panel = NSSavePanel.savePanel()
        panel.setAllowedFileTypes_(allowed)
        panel.setNameFieldStringValue_(suggested)
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(os.path.dirname(self._source_path)))
        if panel.runModal() != 1:
            return

        out_path = str(panel.URL().path())
        if graded:
            self.runGradedExport_start_duration_(out_path, self._in_point, trim_dur)
        else:
            self.runExport_start_duration_(out_path, self._in_point, trim_dur)

    def runExport_start_duration_(self, out_path, start, duration):
        self._beginExportUI_("Exporting lossless .aivu…")
        src = self._source_path

        def do_export():
            from AVFoundation import (
                AVAssetExportSession, AVAssetExportPresetPassthrough,
                AVAssetExportSessionStatusCompleted,
            )
            from CoreMedia import CMTimeRangeMake

            url = NSURL.fileURLWithPath_(src)
            asset = AVAsset.assetWithURL_(url)
            out_url = NSURL.fileURLWithPath_(out_path)

            if os.path.exists(out_path):
                os.remove(out_path)

            session = AVAssetExportSession.alloc().initWithAsset_presetName_(
                asset, AVAssetExportPresetPassthrough
            )
            session.setOutputURL_(out_url)
            session.setOutputFileType_("com.apple.immersive-video")

            start_cm = CMTimeMakeWithSeconds(start, 90000)
            dur_cm = CMTimeMakeWithSeconds(duration, 90000)
            session.setTimeRange_(CMTimeRangeMake(start_cm, dur_cm))

            result_box = [None]  # [ok, msg]

            def on_complete():
                ok = session.status() == AVAssetExportSessionStatusCompleted
                err = session.error()
                msg = str(err) if err else "Done."
                result_box[0] = (ok, msg)

            session.exportAsynchronouslyWithCompletionHandler_(on_complete)

            # Wait for completion (runs on AVFoundation's internal queue),
            # polling the session's progress to drive the progress bar.
            for _ in range(1200):  # up to 10 min
                if result_box[0] is not None:
                    break
                try:
                    self.setProgressFraction_(session.progress())
                except Exception:
                    pass
                time.sleep(0.25)
            self.setProgressFraction_(1.0)

            ok, msg = result_box[0] if result_box[0] else (False, "Export timed out.")

            def finish():
                self._endExportUI()
                if ok:
                    self.showAlert_message_("Export complete", f"Saved to:\n{out_path}")
                else:
                    self.showAlert_message_("Export failed", msg[:500])

            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "runCallback:", finish, False
            )

        threading.Thread(target=do_export, daemon=True).start()

    def runGradedExport_start_duration_(self, out_path, start, duration):
        """Re-encode the trimmed range with the grade + LUT baked in (mono,
        non-immersive) via AVAssetExportSession + the Core Image composition."""
        self._beginExportUI_("Exporting graded video (re-encode)…")
        src = self._source_path

        def do_export():
            from AVFoundation import (
                AVAssetExportSession, AVAssetExportPresetHEVCHighestQuality,
                AVAssetExportSessionStatusCompleted,
            )
            from CoreMedia import CMTimeRangeMake

            asset = AVAsset.assetWithURL_(NSURL.fileURLWithPath_(src))
            comp = self.makeProcessingComposition_(asset)
            out_url = NSURL.fileURLWithPath_(out_path)
            if os.path.exists(out_path):
                os.remove(out_path)

            session = AVAssetExportSession.alloc().initWithAsset_presetName_(
                asset, AVAssetExportPresetHEVCHighestQuality)
            session.setOutputURL_(out_url)
            session.setOutputFileType_("com.apple.quicktime-movie")
            if comp is not None:
                session.setVideoComposition_(comp)
            session.setTimeRange_(CMTimeRangeMake(
                CMTimeMakeWithSeconds(start, 90000),
                CMTimeMakeWithSeconds(duration, 90000)))

            result_box = [None]

            def on_complete():
                ok = session.status() == AVAssetExportSessionStatusCompleted
                err = session.error()
                result_box[0] = (ok, str(err) if err else "Done.")

            session.exportAsynchronouslyWithCompletionHandler_(on_complete)
            for _ in range(2400):  # up to 20 min
                if result_box[0] is not None:
                    break
                try:
                    self.setProgressFraction_(session.progress())
                except Exception:
                    pass
                time.sleep(0.25)
            self.setProgressFraction_(1.0)
            ok, msg = result_box[0] if result_box[0] else (False, "Export timed out.")

            def finish():
                self._endExportUI()
                if ok:
                    self.showAlert_message_(
                        "Graded export complete",
                        f"Saved graded mono (non-immersive) movie to:\n{out_path}")
                else:
                    self.showAlert_message_("Graded export failed", msg[:500])

            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "runCallback:", finish, False)

        threading.Thread(target=do_export, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Side-by-side MP4 export (Meta Quest 3 compatible)                   #
    # ------------------------------------------------------------------ #

    def exportSBS_(self, sender):
        if not self._source_path:
            return
        trim_dur = self._out_point - self._in_point
        if trim_dur <= 0:
            self.showAlert_message_("Invalid range", "Out point must be after in point.")
            return

        ffmpeg = self.findFFmpeg()
        if not ffmpeg:
            self.showAlert_message_(
                "FFmpeg not found",
                "Side-by-side MP4 export needs FFmpeg (with the hevc_videotoolbox "
                "encoder) on your PATH.\n\nInstall it with:\n"
                "    conda install -c conda-forge ffmpeg\nor:\n"
                "    brew install ffmpeg",
            )
            return

        base, _ = os.path.splitext(self._source_path)
        suggested = os.path.basename(base + "_SBS_60fps.mp4")

        panel = NSSavePanel.savePanel()
        panel.setAllowedFileTypes_(["mp4"])
        panel.setNameFieldStringValue_(suggested)
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(os.path.dirname(self._source_path)))
        if panel.runModal() != 1:
            return

        out_path = str(panel.URL().path())
        self.runSBSExport_start_duration_ffmpeg_(out_path, self._in_point, trim_dur, ffmpeg)

    def discoverLUTs(self):
        """Return [(label, path|None)] — 'No LUT' first, then discovered .cube files.

        Prefer LUTs bundled in the local ``luts/`` folder; if none are present
        (e.g. a fresh clone where they were not redistributed), fall back to the
        Gen 5 Rec709 LUTs from a DaVinci Resolve installation.
        """
        def cubes_in(d):
            if not os.path.isdir(d):
                return []
            try:
                return sorted(n for n in os.listdir(d) if n.lower().endswith(".cube"))
            except OSError:
                return []

        luts = [("No LUT (passthrough)", None)]
        here = os.path.dirname(os.path.abspath(__file__))
        bundled = os.path.join(here, "luts")

        names = cubes_in(bundled)
        if names:
            for name in names:
                luts.append((os.path.splitext(name)[0], os.path.join(bundled, name)))
            return luts

        # Fallback: Blackmagic's Gen 5 -> Rec709 LUTs from DaVinci Resolve
        resolve = ("/Library/Application Support/Blackmagic Design/"
                   "DaVinci Resolve/LUT/Blackmagic Design")
        for name in cubes_in(resolve):
            if "gen 5" not in name.lower():
                continue
            luts.append((os.path.splitext(name)[0], os.path.join(resolve, name)))
        return luts

    def selectedLUTPath(self):
        if self._lut_popup is None:
            return None
        idx = self._lut_popup.indexOfSelectedItem()
        if 0 <= idx < len(self._luts):
            return self._luts[idx][1]
        return None

    def ffmpegLookFilters(self):
        """FFmpeg filter strings for the current look: grade (lutrgb) then LUT
        (lut3d). Empty list if nothing is active."""
        parts = []
        expr = ffmpeg_grade_expr(self._grade)
        if expr:
            parts.append("lutrgb=r='%s':g='%s':b='%s'" % (expr, expr, expr))
        lut_path = self.selectedLUTPath()
        if lut_path:
            esc = lut_path.replace("\\", "\\\\").replace("'", "\\'")
            parts.append("lut3d=file='%s'" % esc)
        return parts

    def processingActive(self):
        return bool(self.ffmpegLookFilters())

    # ------------------------------------------------------------------ #
    #  Live LUT preview (Core Image color cube on the player)             #
    # ------------------------------------------------------------------ #

    def lutChanged_(self, sender):
        self.applyPreviewProcessing()

    def gradeChanged_(self, sender):
        for name, s in self._sliders.items():
            v = float(s.doubleValue())
            self._grade[name] = v
            lbl = self._slider_vals.get(name)
            if lbl is not None:
                lbl.setStringValue_("%.2f" % v)
        self.applyPreviewProcessing()

    def resetGrade_(self, sender):
        for name, s in self._sliders.items():
            s.setDoubleValue_(GRADE_NEUTRAL[name])
            self._grade[name] = GRADE_NEUTRAL[name]
            lbl = self._slider_vals.get(name)
            if lbl is not None:
                lbl.setStringValue_("%.2f" % GRADE_NEUTRAL[name])
        self.applyPreviewProcessing()

    def loadCube_(self, path):
        """Parse a 3D .cube LUT into (size, NSData of RGBA float32). Cached."""
        if path in self._cube_cache:
            return self._cube_cache[path]
        import array
        size = None
        data = array.array('f')
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    up = s.upper()
                    if up.startswith("LUT_3D_SIZE"):
                        size = int(s.split()[-1])
                        continue
                    if up.startswith("LUT_1D_SIZE"):
                        self._cube_cache[path] = None   # 1D not supported here
                        return None
                    if up[0].isalpha():                 # TITLE/DOMAIN_*/etc.
                        continue
                    parts = s.split()
                    if len(parts) >= 3:
                        try:
                            r, g, b = float(parts[0]), float(parts[1]), float(parts[2])
                        except ValueError:
                            continue
                        data.extend((r, g, b, 1.0))
        except OSError:
            self._cube_cache[path] = None
            return None

        if not size or len(data) != size * size * size * 4:
            self._cube_cache[path] = None
            return None

        raw = data.tobytes()
        nsdata = NSData.dataWithBytes_length_(raw, len(raw))
        result = (size, nsdata)
        self._cube_cache[path] = result
        return result

    def buildGradeFilters(self):
        """Return an ordered list of CIFilters for the current grade (gain ->
        contrast -> gamma), matching the FFmpeg lutrgb math. Empty if neutral."""
        filters = []
        g = self._grade
        K, C, G = g["gain"], g["contrast"], g["gamma"]

        if abs(K - 1.0) >= 1e-3:
            m = CIFilter.filterWithName_("CIColorMatrix")
            from Quartz import CIVector
            m.setValue_forKey_(CIVector.vectorWithX_Y_Z_W_(K, 0, 0, 0), "inputRVector")
            m.setValue_forKey_(CIVector.vectorWithX_Y_Z_W_(0, K, 0, 0), "inputGVector")
            m.setValue_forKey_(CIVector.vectorWithX_Y_Z_W_(0, 0, K, 0), "inputBVector")
            filters.append(m)

        if abs(C - 1.0) >= 1e-3:
            cc = CIFilter.filterWithName_("CIColorControls")
            cc.setValue_forKey_(C, "inputContrast")
            filters.append(cc)

        if abs(G - 1.0) >= 1e-3:
            ga = CIFilter.filterWithName_("CIGammaAdjust")
            ga.setValue_forKey_(1.0 / G, "inputPower")  # ffmpeg gamma G == in^(1/G)
            filters.append(ga)

        return filters

    def buildProcessingFilters(self):
        """Full preview/export chain as CIFilters: grade first, then the LUT."""
        filters = self.buildGradeFilters()
        path = self.selectedLUTPath()
        if path:
            cube = self.loadCube_(path)
            if cube:
                size, nsdata = cube
                lut = CIFilter.filterWithName_("CIColorCube")
                if lut is not None:
                    lut.setValue_forKey_(size, "inputCubeDimension")
                    lut.setValue_forKey_(nsdata, "inputCubeData")
                    filters.append(lut)
        return filters

    def makeProcessingComposition_(self, asset):
        """Build an AVMutableVideoComposition applying the current grade + LUT
        (Core Image) to the given asset, or None if nothing is active."""
        filters = self.buildProcessingFilters()
        if not filters or asset is None:
            return None

        def handler(request):
            try:
                img = request.sourceImage()
                extent = img.extent()
                for f in filters:
                    f.setValue_forKey_(img, "inputImage")
                    out = f.outputImage()
                    if out is not None:
                        img = out
                img = img.imageByCroppingToRect_(extent)
                request.finishWithImage_context_(img, None)
            except Exception:
                request.finishWithImage_context_(request.sourceImage(), None)

        try:
            return AVMutableVideoComposition.videoCompositionWithAsset_applyingCIFiltersWithHandler_(
                asset, handler)
        except Exception:
            return None

    def applyPreviewProcessing(self):
        """Apply the current grade + LUT live on the player. Approximate
        preview; exports re-render exactly."""
        item = self._player_item
        if item is None:
            return
        item.setVideoComposition_(self.makeProcessingComposition_(self._asset))

    def findFFmpeg(self):
        import shutil
        found = shutil.which("ffmpeg")
        if found:
            return found
        # Common locations when launched from Finder (minimal PATH)
        candidates = [
            os.path.expanduser("~/miniconda3/bin/ffmpeg"),
            os.path.expanduser("~/anaconda3/bin/ffmpeg"),
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
        ]
        for c in candidates:
            if os.path.isfile(c) and os.access(c, os.X_OK):
                return c
        return None

    def runSBSExport_start_duration_ffmpeg_(self, out_path, start, duration, ffmpeg):
        lut_path = self.selectedLUTPath()
        self._beginExportUI_("Exporting side-by-side MP4…")
        src = self._source_path

        # Build the filtergraph: both eye views -> side-by-side -> 8K-wide ->
        # optional grade + 3D LUT -> 60 fps (frame-drop, no speed change).
        chain = "[0:v:view:0][0:v:view:1]hstack=inputs=2,scale=7680:3840"
        for f in self.ffmpegLookFilters():
            chain += "," + f
        chain += ",fps=60[v]"

        def do_export():
            import tempfile
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass

            cmd = [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats",
                "-progress", "pipe:1", "-y",
                "-ss", f"{start:.6f}", "-t", f"{duration:.6f}",
                "-i", src,
                "-filter_complex", chain,
                "-map", "[v]", "-map", "0:a:0?",
                "-c:v", "hevc_videotoolbox", "-b:v", "60M", "-tag:v", "hvc1",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                out_path,
            ]

            errf = tempfile.NamedTemporaryFile(delete=False, suffix=".log",
                                               mode="w+", encoding="utf-8")
            ok, msg = False, "Done."
            last_pct = -1.0
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf,
                                        text=True)
                for line in proc.stdout:
                    line = line.strip()
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        val = line.split("=", 1)[1]
                        try:
                            us = int(val)
                        except ValueError:
                            continue
                        frac = max(0.0, min((us / 1e6) / duration, 1.0))
                        pct = frac * 100.0
                        if pct - last_pct >= 0.5:   # throttle UI updates
                            last_pct = pct
                            self.setProgressFraction_(frac)
                proc.wait()
                ok = proc.returncode == 0 and os.path.exists(out_path)
                errf.flush(); errf.seek(0)
                err_text = errf.read().strip()
                msg = err_text or "Done."
            except Exception as e:
                ok, msg = False, str(e)
            finally:
                try:
                    errf.close(); os.remove(errf.name)
                except OSError:
                    pass

            def finish():
                self._endExportUI()
                if ok:
                    extra = ("\nLUT: %s" % os.path.basename(lut_path)) if lut_path else ""
                    self.showAlert_message_(
                        "SBS MP4 export complete",
                        f"Saved 7680×3840 side-by-side HEVC at 60 fps to:\n{out_path}{extra}",
                    )
                else:
                    self.showAlert_message_("SBS export failed", msg[-800:])

            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "runCallback:", finish, False
            )

        threading.Thread(target=do_export, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Rectilinear (flat mono 16:9) export via ST-map remap               #
    # ------------------------------------------------------------------ #

    def exportRectilinear_(self, sender):
        if not self._source_path:
            return
        trim_dur = self._out_point - self._in_point
        if trim_dur <= 0:
            self.showAlert_message_("Invalid range", "Out point must be after in point.")
            return

        ffmpeg = self.findFFmpeg()
        if not ffmpeg:
            self.showAlert_message_(
                "FFmpeg not found",
                "Rectilinear export needs FFmpeg.\nInstall with:\n"
                "    conda install -c conda-forge ffmpeg",
            )
            return

        # Ensure we have the precomputed remap pixel maps (need the ST map EXR
        # the first time, to derive them).
        try:
            maps = self.ensureRectiMaps()
        except Exception as e:
            self.showAlert_message_("Could not build ST map", str(e))
            return

        if maps is None:
            # Prompt the user to locate the ST map EXR, then retry.
            panel = NSOpenPanel.openPanel()
            panel.setAllowedFileTypes_(["exr"])
            panel.setTitle_("Locate the immersive ST map (.exr)")
            if panel.runModal() != 1:
                return
            self._stmap_path = str(panel.URL().path())
            try:
                maps = self.ensureRectiMaps()
            except Exception as e:
                self.showAlert_message_("Could not build ST map", str(e))
                return
            if maps is None:
                return

        base, _ = os.path.splitext(self._source_path)
        suggested = os.path.basename(base + "_rectilinear_16x9.mp4")
        panel = NSSavePanel.savePanel()
        panel.setAllowedFileTypes_(["mp4"])
        panel.setNameFieldStringValue_(suggested)
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(os.path.dirname(self._source_path)))
        if panel.runModal() != 1:
            return

        out_path = str(panel.URL().path())
        self.runRectiExport_start_duration_ffmpeg_maps_(
            out_path, self._in_point, trim_dur, ffmpeg, maps)

    def ensureRectiMaps(self):
        """Return (xmap_png, ymap_png) for the mono left-eye remap, generating
        them from the ST map EXR (cached) if needed."""
        here = os.path.dirname(os.path.abspath(__file__))
        cache = os.path.join(here, "cache")
        xm = os.path.join(cache, "recti_mono_x.png")
        ym = os.path.join(cache, "recti_mono_y.png")
        if os.path.isfile(xm) and os.path.isfile(ym):
            return (xm, ym)

        exr = self._stmap_path
        if not exr or not os.path.isfile(exr):
            return None   # caller will prompt for the EXR

        try:
            import numpy as np
            from PIL import Image
        except ImportError:
            raise RuntimeError(
                "Generating the ST map needs numpy + Pillow:\n"
                "    pip install numpy pillow\n"
                "(or drop precomputed recti_mono_x.png / _y.png into cache/).")

        ffmpeg = self.findFFmpeg()
        if not ffmpeg:
            raise RuntimeError("FFmpeg is required to read the EXR ST map.")

        os.makedirs(cache, exist_ok=True)
        import tempfile
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".raw")
        tmp.close()
        try:
            subprocess.run(
                [ffmpeg, "-hide_banner", "-v", "error", "-i", exr,
                 "-vf", "format=gbrpf32le", "-f", "rawvideo", tmp.name, "-y"],
                check=True, capture_output=True)
            n = os.path.getsize(tmp.name) // 4
            pixels = n // 3
            H = int(round((pixels / 2) ** 0.5))   # ST map is 2:1
            W = 2 * H
            a = np.fromfile(tmp.name, dtype="<f4").reshape(3, H, W)
            G, B, R = a[0], a[1], a[2]
            half = W // 2
            u = R[:, :half] * 2.0                 # left eye -> [0,1]
            v = G[:, :half]
            xmap = np.clip(np.rint(u * (EYE_SIZE - 1)), 0, EYE_SIZE - 1).astype(np.uint16)
            ymap = np.clip(np.rint((1.0 - v) * (EYE_SIZE - 1)), 0, EYE_SIZE - 1).astype(np.uint16)
            Image.fromarray(xmap, mode="I;16").save(xm)
            Image.fromarray(ymap, mode="I;16").save(ym)
        finally:
            try:
                os.remove(tmp.name)
            except OSError:
                pass
        return (xm, ym)

    def runRectiExport_start_duration_ffmpeg_maps_(self, out_path, start, duration, ffmpeg, maps):
        xm, ym = maps
        self._beginExportUI_("Exporting rectilinear 16:9…")
        src = self._source_path

        # Left eye -> remap (de-fisheye) -> 16:9 crop -> look -> 60 fps.
        chain = ("[0:v:view:0]scale=%d:%d[e];"
                 "[e][1][2]remap=format=color:fill=black,"
                 "crop=%d:%d:0:%d"
                 % (EYE_SIZE, EYE_SIZE, RECTI_OUT_W, RECTI_OUT_H, RECTI_CROP_Y))
        for f in self.ffmpegLookFilters():
            chain += "," + f
        chain += ",fps=60[o]"

        cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats",
            "-progress", "pipe:1", "-y",
            "-ss", f"{start:.6f}", "-t", f"{duration:.6f}",
            "-i", src, "-i", xm, "-i", ym,
            "-filter_complex", chain,
            "-map", "[o]", "-map", "0:a:0?",
            "-c:v", "h264_videotoolbox", "-b:v", "24M",
            "-pix_fmt", "yuv420p", "-tag:v", "avc1",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            out_path,
        ]

        def do_export():
            ok, msg = self.runFFmpeg_duration_(cmd, duration)

            def finish():
                self._endExportUI()
                if ok:
                    self.showAlert_message_(
                        "Rectilinear export complete",
                        f"Saved 2048×1152 flat 16:9 (mono) to:\n{out_path}")
                else:
                    self.showAlert_message_("Rectilinear export failed", msg[-800:])

            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "runCallback:", finish, False)

        threading.Thread(target=do_export, daemon=True).start()

    def runFFmpeg_duration_(self, cmd, duration):
        """Run an ffmpeg command (with -progress pipe:1) updating the progress
        bar. Returns (ok, message). Call from a background thread."""
        import tempfile
        errf = tempfile.NamedTemporaryFile(delete=False, suffix=".log",
                                           mode="w+", encoding="utf-8")
        ok, msg = False, "Done."
        last_pct = -1.0
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True)
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    frac = max(0.0, min((us / 1e6) / duration, 1.0))
                    pct = frac * 100.0
                    if pct - last_pct >= 0.5:
                        last_pct = pct
                        self.setProgressFraction_(frac)
            proc.wait()
            ok = proc.returncode == 0 and os.path.exists(cmd[-1])
            errf.flush(); errf.seek(0)
            msg = errf.read().strip() or "Done."
        except Exception as e:
            ok, msg = False, str(e)
        finally:
            try:
                errf.close(); os.remove(errf.name)
            except OSError:
                pass
        return ok, msg

    # ------------------------------------------------------------------ #
    #  Shared export-UI helpers                                            #
    # ------------------------------------------------------------------ #

    def _beginExportUI_(self, status_text):
        self._export_btn.setEnabled_(False)
        self._sbs_btn.setEnabled_(False)
        if self._recti_btn is not None:
            self._recti_btn.setEnabled_(False)
        self._status_base = status_text
        self._status_label.setStringValue_(status_text + "  0%")
        self._progress.setIndeterminate_(False)
        self._progress.setMinValue_(0.0)
        self._progress.setMaxValue_(100.0)
        self._progress.setDoubleValue_(0.0)
        self._progress.setHidden_(False)

    def _endExportUI(self):
        self._progress.setDoubleValue_(0.0)
        self._progress.setHidden_(True)
        self._status_label.setStringValue_("")
        self._export_btn.setEnabled_(True)
        self._sbs_btn.setEnabled_(True)
        if self._recti_btn is not None:
            self._recti_btn.setEnabled_(True)

    def setProgressFraction_(self, frac):
        """Thread-safe: update the determinate progress bar + status percentage."""
        try:
            f = float(frac)
        except (TypeError, ValueError):
            return

        def apply():
            pct = max(0.0, min(f * 100.0, 100.0))
            self._progress.setDoubleValue_(pct)
            base = getattr(self, "_status_base", "")
            if base:
                self._status_label.setStringValue_("%s  %d%%" % (base, int(pct)))

        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "runCallback:", apply, False
        )

    def runCallback_(self, block):
        block()

    def showAlert_message_(self, title, message):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(message)
        alert.runModal()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    delegate = AivuTrimmerApp.alloc().init()
    app.setDelegate_(delegate)
    app.activateIgnoringOtherApps_(True)
    app.run()


if __name__ == "__main__":
    main()
