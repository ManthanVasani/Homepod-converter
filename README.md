# HomePod Converter

A tiny local web app that converts video files into **QuickTime-friendly MP4s**
optimized for AirPlay playback on a stereo HomePod pair.

Pick a file in the built-in browser (nothing is uploaded — files are read in place),
choose exactly which audio/subtitle tracks you want, and convert.

## What it does

| Stream | Behaviour |
| ------ | --------- |
| **Video** | Copied bit-for-bit when QuickTime can play it (H.264/HEVC) — zero quality loss. HEVC gets the `hvc1` tag. Only re-encodes formats QuickTime can't open. |
| **Audio** | Copies AAC as-is; transcodes EAC3/AC3/DTS to AAC. Optionally downmixes surround to clean stereo for the HomePod pair, with an optional center-channel **dialogue boost**. |
| **Subtitles** | Text subs (SRT/ASS) → `mov_text`. Image subs (PGS/VOBSUB) can't live in MP4 and are skipped. |
| **Metadata** | Strips junk titles, **keeps language tags** so QuickTime shows real language names. |

The output `.mp4` is written next to the original and never overwrites an existing file.

## Requirements

- **Python 3.7+**
- **ffmpeg** and **ffprobe** on your `PATH`

| OS | Install ffmpeg |
| -- | -------------- |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` (or your distro's package manager) |
| Windows | <https://ffmpeg.org/download.html> |

## Run

```bash
python3 homepod_converter.py
```

It starts a local server on `http://127.0.0.1:8723/` and opens your browser
automatically. Press `Control-C` in the terminal to stop.

## Using it on a HomePod

1. Convert your video.
2. Open the resulting `.mp4` in **QuickTime Player**.
3. Pick your HomePod pair from **Control Center → Sound** (or the AirPlay icon).

## Notes on privacy & security

- The server binds to **loopback only** (`127.0.0.1`) and validates the `Host`
  header to defeat DNS-rebinding, so other devices on your network can't reach it.
- Files are processed **locally** — nothing leaves your machine.

## Browser & platform support

Works in all modern browsers (Chrome, Safari, Firefox, Edge) and on macOS, Linux,
and Windows. The UI is keyboard-navigable and screen-reader friendly. On macOS it
uses hardware-accelerated `h264_videotoolbox` for re-encodes; elsewhere it falls
back to `libx264`.
