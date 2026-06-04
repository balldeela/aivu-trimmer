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
    NSViewMinXMargin,
)
import Quartz
from AVFoundation import (
    AVPlayer, AVPlayerItem, AVAsset,
    AVPlayerTimeControlStatusPlaying,
)
from AVKit import AVPlayerView
import CoreMedia
from CoreMedia import CMTimeMakeWithSeconds, CMTimeGetSeconds, kCMTimeZero

ASSUMED_FPS = 45.0


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
        self._zoom = 1.0
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
            NSMakeRect(100, 100, 1000, 620), style, NSBackingStoreBuffered, False,
        )
        win.setTitle_("AIVU Trimmer")
        win.setMinSize_((860, 500))
        c = win.contentView()

        # Player view — grows with the window (width + height flexible),
        # bottom edge pinned at y=160 so it never overlaps the timeline.
        pv = AVPlayerView.alloc().initWithFrame_(NSMakeRect(0, 160, 1000, 420))
        pv.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        pv.setControlsStyle_(0)
        pv.setWantsLayer_(True)
        pv.layer().setMasksToBounds_(True)
        c.addSubview_(pv)
        self._player_view = pv

        # Timeline — fixed height, pinned a fixed distance above the bottom
        # controls (flexible TOP margin so the gap above it absorbs resizing).
        tv = TimelineView.alloc().initWithFrame_(NSMakeRect(0, 110, 1000, 50))
        tv.setup(self)
        tv.setAutoresizingMask_(NSViewWidthSizable | NSViewMaxYMargin)
        c.addSubview_(tv)
        self._timeline_view = tv

        # Timecode label
        tc = self.makeTextField_rect_font_color_(
            "00:00:00:00",
            NSMakeRect(10, 76, 220, 28),
            NSFont.monospacedDigitSystemFontOfSize_weight_(18, 0),
            NSColor.whiteColor(),
        )
        c.addSubview_(tc)
        self._tc_label = tc

        # In label
        in_lbl = self.makeTextField_rect_font_color_(
            "IN:  00:00:00:00",
            NSMakeRect(240, 76, 220, 28),
            NSFont.monospacedDigitSystemFontOfSize_weight_(14, 0),
            NSColor.yellowColor(),
        )
        c.addSubview_(in_lbl)
        self._in_label = in_lbl

        # Out label
        out_lbl = self.makeTextField_rect_font_color_(
            "OUT: 00:00:00:00",
            NSMakeRect(470, 76, 220, 28),
            NSFont.monospacedDigitSystemFontOfSize_weight_(14, 0),
            NSColor.redColor(),
        )
        c.addSubview_(out_lbl)
        self._out_label = out_lbl

        # Zoom controls (right side of the label row, tracks right edge)
        zout = self.addButton_title_rect_action_(c, "Zoom −", NSMakeRect(800, 74, 60, 30), "zoomOut:")
        zin  = self.addButton_title_rect_action_(c, "Zoom +", NSMakeRect(863, 74, 60, 30), "zoomIn:")
        zfit = self.addButton_title_rect_action_(c, "Fit",    NSMakeRect(926, 74, 54, 30), "zoomReset:")
        for b in (zout, zin, zfit):
            b.setAutoresizingMask_(NSViewMinXMargin)  # stick to right edge

        # Buttons row
        self.addButton_title_rect_action_(c, "▶  Play",  NSMakeRect(10,  35, 80, 32), "togglePlayPause:")
        self.addButton_title_rect_action_(c, "🟡 Set In",  NSMakeRect(98,  35, 92, 32), "setInPoint:")
        self.addButton_title_rect_action_(c, "🔴 Set Out", NSMakeRect(198, 35, 98, 32), "setOutPoint:")
        self.addButton_title_rect_action_(c, "Open…",     NSMakeRect(304, 35, 70, 32), "openFile:")

        exp_btn = self.addButton_title_rect_action_(c, "Export .aivu…", NSMakeRect(382, 35, 150, 32), "exportTrimmed:")
        exp_btn.setEnabled_(False)
        self._export_btn = exp_btn

        sbs_btn = self.addButton_title_rect_action_(c, "Export SBS MP4 (Quest)…", NSMakeRect(540, 35, 210, 32), "exportSBS:")
        sbs_btn.setEnabled_(False)
        self._sbs_btn = sbs_btn

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

        if self._player is None:
            player = AVPlayer.playerWithPlayerItem_(item)
            self._player = player
            self._player_view.setPlayer_(player)
        else:
            self._player.replaceCurrentItemWithPlayerItem_(item)

        self.startTimeObserver()

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

        base, ext = os.path.splitext(self._source_path)
        suggested = os.path.basename(base + "_trimmed" + ext)

        panel = NSSavePanel.savePanel()
        panel.setAllowedFileTypes_(["aivu", "mov", "mp4", "m4v"])
        panel.setNameFieldStringValue_(suggested)
        panel.setDirectoryURL_(NSURL.fileURLWithPath_(os.path.dirname(self._source_path)))
        if panel.runModal() != 1:
            return

        out_path = str(panel.URL().path())
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

            # Wait for completion (runs on AVFoundation's internal queue)
            for _ in range(1200):  # up to 10 min
                if result_box[0] is not None:
                    break
                time.sleep(0.5)

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
        self._beginExportUI_("Exporting side-by-side MP4 (this can take a few minutes)…")
        src = self._source_path

        def do_export():
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass

            # Decode both MV-HEVC eye views, place them side-by-side, scale to
            # an 8K-wide frame (≤ Quest 3's HEVC decode limit), drop 90→60 fps
            # without changing speed (the fps filter resamples by timestamp),
            # and encode with Apple's hardware HEVC encoder.
            cmd = [
                ffmpeg, "-hide_banner", "-v", "error", "-y",
                "-ss", f"{start:.6f}", "-t", f"{duration:.6f}",
                "-i", src,
                "-filter_complex",
                "[0:v:view:0][0:v:view:1]hstack=inputs=2,scale=7680:3840,fps=60[v]",
                "-map", "[v]", "-map", "0:a:0?",
                "-c:v", "hevc_videotoolbox", "-b:v", "60M", "-tag:v", "hvc1",
                "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart",
                out_path,
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
                ok = result.returncode == 0 and os.path.exists(out_path)
                msg = (result.stderr or result.stdout).strip() or "Done."
            except subprocess.TimeoutExpired:
                ok, msg = False, "Export timed out (over 60 minutes)."
            except Exception as e:
                ok, msg = False, str(e)

            def finish():
                self._endExportUI()
                if ok:
                    self.showAlert_message_(
                        "SBS MP4 export complete",
                        f"Saved 7680×3840 side-by-side HEVC at 60 fps to:\n{out_path}",
                    )
                else:
                    self.showAlert_message_("SBS export failed", msg[-800:])

            self.performSelectorOnMainThread_withObject_waitUntilDone_(
                "runCallback:", finish, False
            )

        threading.Thread(target=do_export, daemon=True).start()

    # ------------------------------------------------------------------ #
    #  Shared export-UI helpers                                            #
    # ------------------------------------------------------------------ #

    def _beginExportUI_(self, status_text):
        self._export_btn.setEnabled_(False)
        self._sbs_btn.setEnabled_(False)
        self._status_label.setStringValue_(status_text)
        self._progress.setHidden_(False)
        self._progress.startAnimation_(None)

    def _endExportUI(self):
        self._progress.stopAnimation_(None)
        self._progress.setHidden_(True)
        self._status_label.setStringValue_("")
        self._export_btn.setEnabled_(True)
        self._sbs_btn.setEnabled_(True)

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
