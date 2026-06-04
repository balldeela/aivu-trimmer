# AIVU Trimmer

A lightweight macOS tool for **lossless trimming of Apple Immersive Video (`.aivu`) files**.

Set in/out points on a visual player with a timecode display, then export a trimmed
copy — without re-encoding the footage and while preserving the Apple-specific
immersive metadata (AIME / VenueDescriptor / spatial audio).

![timeline screenshot placeholder](docs/screenshot.png)

## Features

- **Native playback** of `.aivu` files via AVFoundation / AVKit
- **Timecode display** (HH:MM:SS:FF at 45 fps)
- **Visual in/out points** — drag the **yellow** bar for the in point and the
  **red** bar for the out point on the timeline
- **Sample-accurate scrubbing** (zero-tolerance seeking, no keyframe snapping)
- **Scroll-wheel nudging** of the playhead (±1 second per tick)
- **Zoom in/out** of the preview to inspect detail
- **Lossless export** using `AVAssetExportSession` with passthrough — writes a real
  `.aivu` that opens in Apple Immersive Video Utility

## Requirements

- **macOS 26 (Tahoe) or newer** — required for AVFoundation's
  `com.apple.immersive-video` export support. The app will run on older macOS but
  exporting `.aivu` will fail.
- **Python 3.10+**
- The PyObjC frameworks listed in [`requirements.txt`](requirements.txt)

## Installation

```bash
git clone https://github.com/balldeela/aivu-trimmer.git
cd aivu-trimmer
python3 -m pip install -r requirements.txt
```

## Usage

```bash
python3 aivu_trimmer.py
```

1. Pick an `.aivu` file when prompted (or use **Open…**).
2. Scrub the timeline / use the scroll wheel to find your in point, press **Set In**
   (or drag the yellow bar).
3. Find your out point, press **Set Out** (or drag the red bar).
4. Press **Export Trimmed…** and choose where to save.

### Controls

| Action | How |
|---|---|
| Play / pause | **Play** button |
| Set in point | **Set In** button, or drag the yellow bar |
| Set out point | **Set Out** button, or drag the red bar |
| Seek | Click / drag on the timeline |
| Nudge playhead ±1s | Scroll wheel over the timeline |
| Zoom preview | **Zoom +** / **Zoom −** / **Fit** buttons |

## How the lossless trim works

Export uses `AVAssetExportSession` with the **passthrough** preset and an output
file type of `com.apple.immersive-video`. This copies the compressed MV-HEVC
bitstream directly (no re-encode) and lets AVFoundation preserve the proprietary
AIVU boxes, so the result opens correctly in Apple Immersive Video Utility.

> **Note on cut accuracy:** like any lossless trim on any format, the start of the
> output snaps to the nearest keyframe in the source. Your exact in point may shift
> by up to one GOP interval. Avoiding this would require re-encoding.

## License

[MIT](LICENSE)
