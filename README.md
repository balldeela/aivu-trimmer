# AIVU Trimmer

A lightweight macOS tool for **trimming Apple Immersive Video (`.aivu`) files** —
with two export modes:

1. **Lossless `.aivu`** — a trimmed copy with no re-encoding, preserving the
   Apple-specific immersive metadata (AIME / VenueDescriptor / spatial audio).
2. **Side-by-side MP4 for Meta Quest 3** — a stereoscopic SBS H.265 clip at 60 fps,
   ready to drop onto a Quest 3 and play in a VR video player.

Set in/out points on a visual player with a timecode display, then export.

![AIVU Trimmer screenshot](docs/screenshot.png)

## Features

- **Native playback** of `.aivu` files via AVFoundation / AVKit
- **Timecode display** (HH:MM:SS:FF at 45 fps)
- **Visual in/out points** — drag the **yellow** bar for the in point and the
  **red** bar for the out point on the timeline
- **Sample-accurate scrubbing** (zero-tolerance seeking, no keyframe snapping)
- **Scroll-wheel nudging** of the playhead (±1 second per tick)
- **Zoom in/out** of the preview to inspect detail
- **Lossless `.aivu` export** using `AVAssetExportSession` with passthrough — writes
  a real `.aivu` that opens in Apple Immersive Video Utility
- **Side-by-side MP4 export** (Meta Quest 3): decodes both MV-HEVC eye views, packs
  them side-by-side at 7680×3840, drops 90→60 fps **without changing speed**, and
  encodes HEVC with Apple's hardware encoder (`hevc_videotoolbox`)
- **Color LUT** option — preview a `.cube` 3D LUT live on the player and bake it
  into the SBS export (e.g. Blackmagic Gen 5 Film → Rec709). See [`luts/`](luts/)
- **Live progress bar** with percentage during both exports

## Requirements

- **macOS 26 (Tahoe) or newer** — required for AVFoundation's
  `com.apple.immersive-video` export support. The app will run on older macOS but
  exporting `.aivu` will fail.
- **Python 3.10+**
- The PyObjC frameworks listed in [`requirements.txt`](requirements.txt)
- **FFmpeg** (only for the side-by-side MP4 export) with the `hevc_videotoolbox`
  encoder — the standard macOS builds include it:
  ```bash
  conda install -c conda-forge ffmpeg     # or: brew install ffmpeg
  ```
  The lossless `.aivu` export does **not** need FFmpeg.

## Installation

```bash
git clone https://github.com/balldeela/aivu-trimmer.git
cd aivu-trimmer
python3 -m pip install -r requirements.txt
```

## Usage

```bash
python3 aivu_trimmer.py                 # opens a file picker
python3 aivu_trimmer.py movie.aivu      # or open a file directly
```

1. Pick an `.aivu` file when prompted (or use **Open…**).
2. Scrub the timeline / use the scroll wheel to find your in point, press **Set In**
   (or drag the yellow bar).
3. Find your out point, press **Set Out** (or drag the red bar).
4. Export:
   - **Export .aivu…** for a lossless trimmed Apple Immersive Video, or
   - **Export SBS MP4 (Quest)…** for a side-by-side stereo clip for Meta Quest 3.

### Controls

| Action | How |
|---|---|
| Play / pause | **Play** button |
| Set in point | **Set In** button, or drag the yellow bar |
| Set out point | **Set Out** button, or drag the red bar |
| Seek | Click / drag on the timeline |
| Nudge playhead ±1s | Scroll wheel over the timeline |
| Zoom preview | **Zoom +** / **Zoom −** / **Fit** buttons |
| Export lossless `.aivu` | **Export .aivu…** button |
| Export side-by-side MP4 | **Export SBS MP4 (Quest)…** button |
| Apply a color LUT (SBS only) | **Color LUT** dropdown |

## How the lossless `.aivu` trim works

Export uses `AVAssetExportSession` with the **passthrough** preset and an output
file type of `com.apple.immersive-video`. This copies the compressed MV-HEVC
bitstream directly (no re-encode) and lets AVFoundation preserve the proprietary
AIVU boxes, so the result opens correctly in Apple Immersive Video Utility.

> **Note on cut accuracy:** like any lossless trim on any format, the start of the
> output snaps to the nearest keyframe in the source. Your exact in point may shift
> by up to one GOP interval. Avoiding this would require re-encoding.

## How the side-by-side MP4 export works

For Meta Quest 3 the app re-encodes the trimmed range with FFmpeg:

```
ffmpeg -ss <in> -t <dur> -i input.aivu \
  -filter_complex "[0:v:view:0][0:v:view:1]hstack=inputs=2,scale=7680:3840,fps=60[v]" \
  -map "[v]" -map "0:a:0?" \
  -c:v hevc_videotoolbox -b:v 60M -tag:v hvc1 -c:a aac -b:a 192k \
  -movflags +faststart output_SBS_60fps.mp4
```

- Both MV-HEVC eye views are decoded (`view:0` / `view:1`) and placed
  **side-by-side** (left eye | right eye).
- Output is **7680×3840** — under the Quest 3's HEVC decode ceiling. (Full-res SBS
  would be 8640 px wide, beyond HEVC's 8192 limit, so it's scaled to 8K width.)
- The `fps=60` filter resamples 90→60 fps **by timestamp**, i.e. it drops frames
  without altering playback speed.
- Encoded as HEVC (H.265) — H.264 can't exceed 4096 px, so it isn't an option at
  this resolution.

On the Quest 3, copy the `.mp4` over and open it in a VR video player
(DeoVR, Skybox, etc.), then select a **side-by-side / 180° stereo** viewing mode to
match the source projection.

## Color LUTs

The **Color LUT** dropdown previews a `.cube` 3D LUT **live on the player** (via a
Core Image `CIColorCube` video composition) and bakes it into the side-by-side MP4
export (via FFmpeg's `lut3d` filter). It's intended for converting **Blackmagic
Cine immersive footage (Gen 5 Film color science) to Rec709**.

> The on-screen preview is a fast Core Image approximation (and flattens to a single
> eye while filtering); the exported file uses FFmpeg's exact `lut3d` result.

- The app discovers `.cube` files from the [`luts/`](luts/) folder, and if that's
  empty, falls back to the Gen 5 Rec709 LUTs that ship with **DaVinci Resolve**.
- The Blackmagic `.cube` files are **not redistributed** in this repo — they're
  Blackmagic's. Install DaVinci Resolve (free) or drop your own LUTs in `luts/`.
- LUTs apply **only** to the SBS MP4 export. The lossless `.aivu` export copies the
  original bitstream untouched, so there's nothing to color-transform.

> **Only apply a Film→Rec709 LUT to log/Film-gamma footage.** If your `.aivu` is
> already display-ready (graded Rec709), the LUT will double-correct it. Leave the
> dropdown on **No LUT** when in doubt.

## License

[MIT](LICENSE)
