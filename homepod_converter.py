#!/usr/bin/env python3
"""
HomePod Converter — a local web app that converts video files to QuickTime-friendly
MP4s optimized for AirPlay playback on a stereo HomePod pair.

Pick a file with the built-in browser (no uploads — the app reads it in place),
choose exactly which audio/subtitle tracks you want, and convert.

  • Video : copied bit-for-bit when QuickTime can play it (H.264/HEVC) — zero quality
            loss; HEVC gets the hvc1 tag. Re-encodes only formats QuickTime can't open.
  • Audio : copies AAC; transcodes EAC3/AC3/DTS to AAC. Downmixes surround to clean
            stereo for the HomePod pair, with optional center-channel dialogue boost.
  • Subs  : text subs (SRT/ASS) → mov_text; image subs (PGS/VOBSUB) can't go in MP4.
  • Meta  : strips junk titles, KEEPS language tags so QuickTime shows real languages.

RUN:    python3 homepod_converter.py     (opens in your browser)
NEEDS:  Python 3.7+ + ffmpeg/ffprobe
          macOS:   brew install ffmpeg
          Linux:   sudo apt install ffmpeg
          Windows: https://ffmpeg.org/download.html
"""

import json
import os
import platform
import re
import shutil
import subprocess
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse, parse_qs

HOST = "127.0.0.1"
PORT = 8723

JOBS = {}
JOBS_LOCK = threading.Lock()

TEXT_SUB_CODECS = ("subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text")
VIDEO_EXTS = {
    ".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".webm", ".wmv",
    ".flv", ".mpg", ".mpeg", ".m2ts", ".vob", ".ogv", ".3gp", ".divx",
}

_OS = platform.system()  # "Darwin", "Windows", "Linux"

# Cached encoder choice — detected once at first use
_ENCODER = None
_ENCODER_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Encoder detection
# ---------------------------------------------------------------------------
def _detect_encoder():
    """Return (encoder_name, extra_flags). Uses hardware accel on macOS if available."""
    if _OS == "Darwin":
        try:
            r = subprocess.run(
                ["ffmpeg", "-hide_banner", "-encoders"],
                capture_output=True, text=True, timeout=10,
            )
            if "h264_videotoolbox" in r.stdout:
                return ("h264_videotoolbox", ["-b:v", "12000k"])
        except Exception:
            pass
    return ("libx264", ["-preset", "fast", "-crf", "20"])


def get_encoder():
    global _ENCODER
    with _ENCODER_LOCK:
        if _ENCODER is None:
            _ENCODER = _detect_encoder()
        return _ENCODER


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------
def have_tools():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        return json.loads(out.stdout) if out.returncode == 0 else None
    except Exception:
        return None


def analyze(path):
    data = probe(path)
    if not data or "streams" not in data:
        return {"ok": False, "error": "Could not read this file. It may be corrupt "
                                      "or an unsupported format."}

    duration = float((data.get("format") or {}).get("duration") or 0)
    video, audio, subs = [], [], []
    ai = si = 0
    for s in data["streams"]:
        codec = s.get("codec_name", "?")
        ctype = s.get("codec_type")
        tags = s.get("tags") or {}
        lang = tags.get("language", "und")
        title = tags.get("title", "")
        if ctype == "video":
            if (s.get("disposition") or {}).get("attached_pic"):
                continue
            video.append({"codec": codec, "width": s.get("width"),
                          "height": s.get("height")})
        elif ctype == "audio":
            audio.append({"ti": ai, "codec": codec,
                          "channels": s.get("channels") or 2,
                          "lang": lang, "title": title})
            ai += 1
        elif ctype == "subtitle":
            subs.append({"ti": si, "codec": codec, "lang": lang,
                         "title": title, "text": codec in TEXT_SUB_CODECS})
            si += 1

    if not video:
        return {"ok": False, "error": "No video stream found in this file."}

    v = video[0]
    vc = v["codec"]
    if vc == "h264":
        v_plan = "Copy as-is (no quality loss)"
    elif vc == "hevc":
        v_plan = "Copy as-is + hvc1 tag (no quality loss)"
    else:
        enc, _ = get_encoder()
        v_plan = f"Re-encode {vc} → H.264 via {enc} (needed for QuickTime)"

    return {
        "ok": True, "duration": duration, "video": video,
        "audio": audio, "subs": subs, "video_plan": v_plan,
        "resolution": f'{v.get("width", "?")}x{v.get("height", "?")}',
    }


def build_command(path, output, analysis, opts):
    bitrate = str(opts.get("bitrate", "256k"))
    downmix = bool(opts.get("downmix", True))
    dialogue = bool(opts.get("dialogue", False))
    force_h264 = bool(opts.get("force_h264", False))

    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", path, "-map", "0:v"]

    vcodec = analysis["video"][0]["codec"]
    if force_h264 or vcodec not in ("h264", "hevc"):
        enc, enc_flags = get_encoder()
        cmd += ["-c:v", enc] + enc_flags + ["-tag:v", "avc1"]
    elif vcodec == "hevc":
        cmd += ["-c:v", "copy", "-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", "copy"]

    by_ti = {a["ti"]: a for a in analysis["audio"]}
    for out_idx, ti in enumerate(opts.get("audio") or []):
        a = by_ti.get(int(ti))
        if a is None:
            continue
        cmd += ["-map", f"0:a:{ti}"]
        ch = a["channels"]
        if a["codec"] == "aac" and (not downmix or ch <= 2):
            cmd += [f"-c:a:{out_idx}", "copy"]
        else:
            cmd += [f"-c:a:{out_idx}", "aac", f"-b:a:{out_idx}", bitrate]
            if downmix and ch > 2:
                if dialogue:
                    cmd += [f"-filter:a:{out_idx}",
                            "pan=stereo|FL=FC+0.30*FL+0.30*BL|FR=FC+0.30*FR+0.30*BR"]
                else:
                    cmd += [f"-ac:a:{out_idx}", "2"]

    sub_by_ti = {s["ti"]: s for s in analysis["subs"]}
    chosen_subs = [ti for ti in (opts.get("subs") or [])
                   if ti in sub_by_ti and sub_by_ti[ti]["text"]]
    for ti in chosen_subs:
        cmd += ["-map", f"0:s:{ti}"]
    if chosen_subs:
        cmd += ["-c:s", "mov_text"]

    cmd += ["-metadata", "title=", "-metadata:s", "title=",
            "-movflags", "+faststart", output]
    return cmd


# ---------------------------------------------------------------------------
# File browser
# ---------------------------------------------------------------------------
def list_dir(path):
    if not path:
        path = os.path.expanduser("~")
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        path = os.path.expanduser("~")

    folders, files = [], []
    try:
        for name in sorted(os.listdir(path), key=str.lower):
            if name.startswith("."):
                continue
            full = os.path.join(path, name)
            try:
                if os.path.isdir(full):
                    folders.append({"name": name, "path": full, "dir": True})
                else:
                    ext = os.path.splitext(name)[1].lower()
                    if ext in VIDEO_EXTS:
                        try:
                            size = os.path.getsize(full)
                        except OSError:
                            size = 0
                        files.append({"name": name, "path": full,
                                      "dir": False, "size": size})
            except OSError:
                continue
    except PermissionError:
        return {
            "path": path,
            "parent": None if _is_fs_root(path) else os.path.dirname(path),
            "entries": [], "shortcuts": _shortcuts(), "os": _OS,
            "error": "Permission denied for this folder.",
        }

    parent = None if _is_fs_root(path) else os.path.dirname(path)
    return {
        "path": path, "parent": parent, "os": _OS,
        "shortcuts": _shortcuts(), "entries": folders + files,
    }


def _is_fs_root(path):
    if _OS == "Windows":
        drive, tail = os.path.splitdrive(path)
        return bool(drive) and tail in ("", "\\", "/")
    return path == "/"


def _shortcuts():
    home = os.path.expanduser("~")
    candidates = [
        ("Home", home),
        ("Downloads", os.path.join(home, "Downloads")),
        ("Movies", os.path.join(home, "Movies")),
        ("Desktop", os.path.join(home, "Desktop")),
    ]
    if _OS == "Darwin":
        candidates.append(("Volumes", "/Volumes"))
    elif _OS == "Windows":
        for letter in "DCEFGHIJKLMNOPQRSTUVWXYZ":
            d = f"{letter}:\\"
            if os.path.isdir(d):
                candidates.append((f"Drive {letter}:", d))
    return [{"label": lbl, "path": p} for lbl, p in candidates if os.path.isdir(p)]


# ---------------------------------------------------------------------------
# Conversion worker
# ---------------------------------------------------------------------------
TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def _unique_output(path):
    """Pick an .mp4 output path that never overwrites the input or any existing file."""
    base, _ = os.path.splitext(path)
    candidate = base + ".mp4"
    src = os.path.abspath(path)
    # If the natural name is free (and isn't the source itself), use it.
    if os.path.abspath(candidate) != src and not os.path.exists(candidate):
        return candidate
    # Otherwise fall back to a non-colliding ".converted" variant.
    candidate = base + ".converted.mp4"
    n = 1
    while os.path.exists(candidate) or os.path.abspath(candidate) == src:
        candidate = f"{base}.converted ({n}).mp4"
        n += 1
    return candidate


def run_job(job_id, path, opts):
    analysis = analyze(path)
    if not analysis["ok"]:
        _update(job_id, status="error", log=analysis["error"])
        return

    output = _unique_output(path)

    cmd = build_command(path, output, analysis, opts)
    total = analysis["duration"] or 1
    _update(job_id, status="running", output=output, duration=total,
            log="Starting…\n$ " + " ".join(_q(c) for c in cmd) + "\n\n")

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
        with JOBS_LOCK:
            JOBS[job_id]["proc"] = proc

        for line in proc.stdout:
            m = TIME_RE.search(line)
            if m:
                secs = (int(m.group(1)) * 3600
                        + int(m.group(2)) * 60
                        + float(m.group(3)))
                _update(job_id, progress=max(0.0, min(100.0, secs / total * 100)))

        proc.wait()

        with JOBS_LOCK:
            was_cancelling = JOBS.get(job_id, {}).get("status") == "cancelling"

        if proc.returncode == 0:
            _update(job_id, status="done", progress=100,
                    log_append=f"\n✓ Finished: {output}\n")
        elif was_cancelling:
            _update(job_id, status="cancelled",
                    log_append="\n✗ Cancelled by user.\n")
        else:
            _update(job_id, status="error",
                    log_append=f"\n✗ ffmpeg exited with code {proc.returncode}\n")
    except Exception as e:
        _update(job_id, status="error", log_append=f"\n✗ {e}\n")
    finally:
        with JOBS_LOCK:
            JOBS[job_id].pop("proc", None)


def _q(s):
    return f'"{s}"' if any(c in s for c in (' ', '|', '&', ';', '(', ')')) else s


def _update(job_id, **kw):
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {
            "progress": 0, "status": "queued",
            "log": "", "output": "", "duration": 0,
        })
        if "log_append" in kw:
            job["log"] += kw.pop("log_append")
        if "log" in kw:
            job["log"] = kw.pop("log")
        job.update(kw)


_FINISHED = ("done", "error", "cancelled")
_MAX_JOBS = 50


def _prune_jobs():
    """Drop the oldest finished jobs so the JOBS dict can't grow without bound.
    Must be called while holding JOBS_LOCK."""
    if len(JOBS) <= _MAX_JOBS:
        return
    for jid in list(JOBS):
        if len(JOBS) <= _MAX_JOBS:
            break
        if JOBS[jid].get("status") in _FINISHED:
            del JOBS[jid]


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark">
  <title>HomePod Converter</title>
  <style>
    :root {
      --bg: #0b0d12; --panel: #151922; --panel2: #1c212d; --line: #2a3140;
      --txt: #e6e9ef; --muted: #8b94a7; --accent: #5b8cff; --accent2: #37d39b;
      --warn: #ffb454; --err: #ff6b6b;
    }
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
    html { scroll-behavior: smooth; }
    body {
      font: 15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
            "Helvetica Neue", Arial, sans-serif;
      background: var(--bg); color: var(--txt); min-height: 100vh;
    }

    /* ── Skip link ── */
    .skip-link {
      position: absolute; top: -100%; left: 16px;
      background: var(--accent); color: #fff;
      padding: 8px 16px; border-radius: 8px;
      font-weight: 600; z-index: 9999; text-decoration: none;
    }
    .skip-link:focus { top: 16px; }

    /* ── Focus ring — visible only for keyboard users ── */
    :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    :focus:not(:focus-visible) { outline: none; }

    /* ── Layout ── */
    .wrap { max-width: 820px; margin: 0 auto; padding: 32px 20px 100px; }
    header { margin-bottom: 28px; }
    h1 { font-size: 24px; font-weight: 700; letter-spacing: -.4px; margin-bottom: 4px; }
    .sub { color: var(--muted); font-size: 14px; }

    /* ── Cards ── */
    .card {
      background: var(--panel); border: 1px solid var(--line);
      border-radius: 14px; padding: 22px; margin-bottom: 16px;
      animation: fadeIn .2s ease;
    }
    @keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; } }
    .card-title {
      font-size: 11px; text-transform: uppercase; letter-spacing: .8px;
      color: var(--muted); font-weight: 600; margin-bottom: 16px;
    }

    /* ── File browser ── */
    .shortcut-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
    .shortcut {
      background: var(--panel2); border: 1px solid var(--line); color: var(--txt);
      border-radius: 8px; padding: 5px 12px; font-size: 13px; cursor: pointer;
      font-family: inherit; transition: border-color .15s, background .15s;
    }
    .shortcut:hover, .shortcut:focus-visible { border-color: var(--accent); background: #1e2535; }

    .path-row { display: flex; gap: 8px; margin-bottom: 10px; }
    .path-input {
      flex: 1; background: var(--panel2); border: 1px solid var(--line);
      color: var(--txt); border-radius: 8px; padding: 7px 12px; font-size: 13px;
      font-family: ui-monospace, "Cascadia Code", Menlo, "Courier New", monospace;
      transition: border-color .15s;
    }
    .path-input:focus { border-color: var(--accent); outline: none; }
    .path-input::placeholder { color: var(--muted); }
    .btn-go {
      background: var(--panel2); border: 1px solid var(--line); color: var(--txt);
      border-radius: 8px; padding: 7px 14px; font-size: 13px; cursor: pointer;
      font-family: inherit; transition: border-color .15s; white-space: nowrap;
    }
    .btn-go:hover, .btn-go:focus-visible { border-color: var(--accent); }

    .browser-err { padding: 10px 14px; color: var(--err); font-size: 13px; }

    .browser {
      border: 1px solid var(--line); border-radius: 10px;
      max-height: 340px; overflow-y: auto; background: #0e1219;
      scroll-behavior: smooth;
    }
    .browser::-webkit-scrollbar { width: 6px; }
    .browser::-webkit-scrollbar-track { background: transparent; }
    .browser::-webkit-scrollbar-thumb { background: var(--line); border-radius: 3px; }
    .browser::-webkit-scrollbar-thumb:hover { background: var(--muted); }

    .item {
      display: flex; align-items: center; gap: 11px;
      padding: 10px 14px; cursor: pointer;
      border-bottom: 1px solid #1a1f2b; font-size: 14px;
      transition: background .1s; user-select: none;
    }
    .item:last-child { border-bottom: none; }
    .item:hover, .item[aria-selected="true"] { background: #161d2b; }
    .item .ic { width: 20px; text-align: center; flex-shrink: 0; }
    .item .nm { flex: 1; word-break: break-all; }
    .item .sz { color: var(--muted); font-size: 12px; flex-shrink: 0; }
    .item.up { color: var(--muted); }
    .item.file .nm { color: var(--accent2); }
    .empty { padding: 20px 14px; color: var(--muted); font-size: 13px; text-align: center; }

    /* ── Selected file ── */
    .sel-file {
      display: flex; align-items: center; gap: 10px;
      background: var(--panel2); border: 1px solid var(--accent);
      border-radius: 10px; padding: 11px 14px; font-size: 14px;
    }
    .sel-name { flex: 1; word-break: break-all; font-weight: 500; }
    .btn-clear {
      background: none; border: none; color: var(--muted);
      cursor: pointer; font-size: 20px; line-height: 1;
      padding: 2px 4px; border-radius: 4px; flex-shrink: 0;
      transition: color .15s;
    }
    .btn-clear:hover, .btn-clear:focus-visible { color: var(--err); }

    /* ── Analysis pills ── */
    .pill-row { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
    .pill {
      display: flex; align-items: center; gap: 6px;
      background: var(--panel2); border: 1px solid var(--line);
      border-radius: 999px; padding: 5px 12px; font-size: 13px;
    }
    .pill b { color: var(--accent2); font-weight: 600; }
    .plan {
      background: var(--panel2); border: 1px solid var(--line);
      border-radius: 10px; padding: 12px 14px; font-size: 13.5px;
    }
    .plan .muted { color: var(--muted); }

    /* ── Track checkboxes ── */
    .track {
      display: flex; align-items: flex-start; gap: 11px;
      padding: 11px 0; border-top: 1px solid var(--line); cursor: pointer;
    }
    .track:first-child { border-top: none; }
    .track input[type="checkbox"] {
      margin-top: 3px; width: 16px; height: 16px;
      accent-color: var(--accent); flex-shrink: 0; cursor: pointer;
    }
    .track .t { font-size: 14px; line-height: 1.4; }
    .track .d { color: var(--muted); font-size: 12.5px; margin-top: 2px; }
    .track-disabled { opacity: .55; cursor: default; }
    .track-disabled input { cursor: not-allowed; }

    .badge {
      display: inline-block; font-size: 11px; padding: 1px 7px;
      border-radius: 5px; background: #243049; color: #9db4ff;
      margin-left: 5px; font-weight: 500;
    }
    .badge.warn { background: #3a2d16; color: var(--warn); }

    /* ── Options ── */
    .opt {
      display: flex; align-items: flex-start; gap: 11px;
      padding: 12px 0; cursor: pointer; border-top: 1px solid var(--line);
    }
    .opt:first-child { border-top: none; }
    .opt input[type="checkbox"] {
      margin-top: 3px; width: 16px; height: 16px;
      accent-color: var(--accent); flex-shrink: 0;
    }
    .opt .ot { font-size: 14px; font-weight: 500; }
    .opt .od { color: var(--muted); font-size: 13px; margin-top: 2px; }

    .field { margin: 14px 0; }
    .field-lbl { font-size: 13px; color: var(--muted); margin-bottom: 8px; font-weight: 500; }
    .seg { display: flex; gap: 6px; }
    .seg-btn {
      flex: 1; background: var(--panel2); border: 1px solid var(--line);
      color: var(--muted); border-radius: 8px; padding: 8px; font-size: 13px;
      cursor: pointer; font-weight: 500; transition: .15s; font-family: inherit;
    }
    .seg-btn:hover { border-color: var(--muted); }
    .seg-btn.on { background: var(--accent); color: #fff; border-color: var(--accent); }
    .seg-btn[aria-checked="true"] { background: var(--accent); color: #fff; border-color: var(--accent); }

    .cmd-preview { font-size: 12px; color: var(--muted); margin-top: 10px; line-height: 1.6; }

    /* ── Buttons ── */
    .btn-primary {
      display: block; width: 100%; background: var(--accent); color: #fff;
      border: none; border-radius: 10px; padding: 13px 20px; font-size: 15px;
      font-weight: 600; cursor: pointer; transition: filter .15s, opacity .15s;
      font-family: inherit; margin-top: 18px;
    }
    .btn-primary:hover:not(:disabled) { filter: brightness(1.1); }
    .btn-primary:disabled { opacity: .45; cursor: not-allowed; }

    .btn-secondary {
      display: inline-flex; align-items: center; gap: 6px;
      background: var(--panel2); border: 1px solid var(--line); color: var(--txt);
      border-radius: 8px; padding: 8px 14px; font-size: 13px; font-weight: 500;
      cursor: pointer; transition: border-color .15s; font-family: inherit;
      text-decoration: none;
    }
    .btn-secondary:hover, .btn-secondary:focus-visible { border-color: var(--accent); }

    .btn-danger {
      background: none; border: 1px solid var(--err); color: var(--err);
      border-radius: 8px; padding: 8px 14px; font-size: 13px;
      cursor: pointer; font-family: inherit; transition: background .15s;
    }
    .btn-danger:hover { background: rgba(255,107,107,.12); }

    /* ── Progress ── */
    .prog-bar-wrap {
      height: 10px; background: var(--panel2); border-radius: 999px;
      overflow: hidden; margin: 14px 0 6px;
    }
    .prog-bar {
      height: 100%; width: 0%;
      background: linear-gradient(90deg, var(--accent), var(--accent2));
      border-radius: 999px; transition: width .3s ease;
    }
    .prog-meta { display: flex; justify-content: space-between; margin-bottom: 14px; }
    .prog-pct, .prog-elapsed { font-size: 13px; color: var(--muted); }
    .status-txt { font-size: 15px; margin-bottom: 6px; }

    .log-wrap {
      background: #0a0c11; border: 1px solid var(--line);
      border-radius: 10px; padding: 14px; max-height: 260px;
      overflow-y: auto; margin-top: 14px;
    }
    .log-wrap::-webkit-scrollbar { width: 6px; }
    .log-wrap::-webkit-scrollbar-track { background: transparent; }
    .log-wrap::-webkit-scrollbar-thumb { background: var(--line); border-radius: 3px; }
    pre {
      font-size: 12px; white-space: pre-wrap; color: #aeb6c6;
      font-family: ui-monospace, "Cascadia Code", Menlo, "Courier New", monospace;
    }

    .prog-actions { display: flex; gap: 10px; margin-top: 16px; flex-wrap: wrap; align-items: center; }

    /* ── Toast notifications ── */
    .toast-wrap {
      position: fixed; bottom: 24px; right: 24px; z-index: 9000;
      display: flex; flex-direction: column; gap: 10px; pointer-events: none;
    }
    .toast {
      background: var(--panel); border: 1px solid var(--line);
      border-radius: 10px; padding: 12px 16px; font-size: 14px;
      box-shadow: 0 4px 24px rgba(0,0,0,.4); pointer-events: auto;
      animation: toastIn .25s ease; max-width: 340px;
    }
    @keyframes toastIn { from { opacity:0; transform: translateY(12px); } to { opacity:1; } }
    .toast.ok { border-color: var(--accent2); }
    .toast.err { border-color: var(--err); }

    /* ── Utilities ── */
    .hide { display: none !important; }
    .muted { color: var(--muted); }
    .ok { color: var(--accent2); }
    .err { color: var(--err); }
    .warn { color: var(--warn); }

    /* ── Responsive ── */
    @media (max-width: 500px) {
      .wrap { padding: 16px 12px 80px; }
      h1 { font-size: 20px; }
      .seg { flex-direction: column; }
      .pill-row { gap: 6px; }
      .toast-wrap { left: 12px; right: 12px; bottom: 12px; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#main">Skip to main content</a>
  <div class="toast-wrap" id="toasts" aria-live="assertive" aria-atomic="false" role="status"></div>

  <div class="wrap">
    <header>
      <h1>HomePod Converter</h1>
      <p class="sub">Convert any video to a QuickTime-ready MP4 — optimised for your stereo HomePod pair.</p>
    </header>

    <main id="main">

      <!-- Step 1: File browser -->
      <section id="pickCard" class="card" aria-label="File browser">
        <div class="card-title" aria-hidden="true">Choose a video file</div>
        <div class="shortcut-row" id="shortcuts" role="list" aria-label="Quick-access folders"></div>
        <div class="path-row">
          <input class="path-input" id="pathInput" type="text"
            placeholder="Or type / paste a folder path…" aria-label="Folder path">
          <button class="btn-go" id="pathGo" type="button">Go</button>
        </div>
        <div id="browserErr" class="browser-err hide" role="alert" aria-live="polite"></div>
        <div class="browser" id="browser" role="listbox"
          aria-label="Files and folders — use arrow keys to navigate"
          tabindex="0"></div>
        <p class="muted" style="font-size:12px;margin-top:10px">
          Files are read in-place on your machine — nothing is uploaded.
        </p>
      </section>

      <!-- Step 2: Selected file -->
      <section id="selCard" class="card hide" aria-label="Selected file">
        <div class="card-title" aria-hidden="true">Selected file</div>
        <div class="sel-file">
          <span aria-hidden="true">🎬</span>
          <span class="sel-name" id="selName"></span>
          <button class="btn-clear" id="clearSel" type="button"
            aria-label="Clear selection and choose a different file">&#x2715;</button>
        </div>
      </section>

      <!-- Step 3: Analysis -->
      <section id="analysisCard" class="card hide" aria-label="File analysis" aria-live="polite"></section>

      <!-- Step 4: Audio tracks -->
      <section id="audioCard" class="card hide" aria-label="Audio tracks">
        <div class="card-title" aria-hidden="true">Audio tracks</div>
        <fieldset style="border:none;padding:0">
          <legend class="hide">Select audio tracks to include</legend>
          <div id="audioList"></div>
        </fieldset>
      </section>

      <!-- Step 5: Subtitle tracks -->
      <section id="subCard" class="card hide" aria-label="Subtitle tracks">
        <div class="card-title" aria-hidden="true">Subtitle tracks</div>
        <fieldset style="border:none;padding:0">
          <legend class="hide">Select subtitle tracks to include</legend>
          <div id="subList"></div>
        </fieldset>
      </section>

      <!-- Step 6: Output options -->
      <section id="optsCard" class="card hide" aria-label="Output options">
        <div class="card-title" aria-hidden="true">Output options</div>

        <div class="field">
          <div class="field-lbl" id="brLabel">Audio quality (AAC bitrate)</div>
          <div class="seg" id="brSeg" role="radiogroup" aria-labelledby="brLabel">
            <button class="seg-btn" data-br="192k" role="radio" aria-checked="false" type="button">192k</button>
            <button class="seg-btn on" data-br="256k" role="radio" aria-checked="true" type="button">256k</button>
            <button class="seg-btn" data-br="320k" role="radio" aria-checked="false" type="button">320k</button>
          </div>
        </div>

        <fieldset style="border:none;padding:0;margin-top:4px">
          <legend class="hide">Encoding options</legend>

          <label class="opt">
            <input type="checkbox" id="downmix" checked aria-describedby="downmix-d">
            <div>
              <div class="ot">Downmix surround to stereo</div>
              <div class="od" id="downmix-d">Recommended for a HomePod stereo pair. Turn off only if you use a receiver.</div>
            </div>
          </label>

          <label class="opt">
            <input type="checkbox" id="dialogue" aria-describedby="dialogue-d">
            <div>
              <div class="ot">Dialogue boost</div>
              <div class="od" id="dialogue-d">Raises the center channel so speech cuts through music and effects. Surround tracks only.</div>
            </div>
          </label>

          <label class="opt">
            <input type="checkbox" id="forceh264" aria-describedby="forceh264-d">
            <div>
              <div class="ot">Force H.264 re-encode</div>
              <div class="od" id="forceh264-d">Only needed if HEVC won&apos;t play on an older Mac. Much slower — leave off to keep full quality.</div>
            </div>
          </label>
        </fieldset>

        <div class="cmd-preview" id="cmdPreview" aria-live="polite"></div>
        <button class="btn-primary" id="go" type="button" disabled>Convert</button>
      </section>

      <!-- Step 7: Progress -->
      <section id="progCard" class="card hide" aria-label="Conversion progress">
        <div class="card-title" aria-hidden="true">Converting</div>
        <div id="statusTxt" class="status-txt muted" aria-live="polite" aria-atomic="true">Starting&#x2026;</div>
        <div class="prog-bar-wrap">
          <div class="prog-bar" id="fill"
            role="progressbar" aria-valuenow="0" aria-valuemin="0" aria-valuemax="100"
            aria-label="Conversion progress"></div>
        </div>
        <div class="prog-meta">
          <span class="prog-pct" id="pct" aria-hidden="true">0%</span>
          <span class="prog-elapsed" id="elapsed"></span>
        </div>
        <div class="log-wrap" id="logWrap">
          <pre id="log" aria-label="Conversion log output"></pre>
        </div>
        <div class="prog-actions">
          <button class="btn-danger" id="cancelBtn" type="button">Cancel</button>
          <button class="btn-secondary hide" id="revealBtn" type="button">&#x1F4C2; Show in Finder</button>
          <button class="btn-secondary hide" id="anotherBtn" type="button">Convert another file</button>
        </div>
      </section>

    </main>
  </div>

<script>
"use strict";

// ── Utilities ──────────────────────────────────────────────────────────────
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

function esc(s) {
  // Escape HTML special chars before inserting into innerHTML
  return String(s || "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtSize(b) {
  if (!b) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0, n = b;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n < 10 && i > 0 ? 1 : 0) + " " + u[i];
}

function fmtDur(s) {
  s = Math.round(s);
  const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
  return (h ? h + "h " : "") + (m ? m + "m " : "") + x + "s";
}

function fmtElapsed(ms) {
  const s = Math.floor(ms / 1000), m = Math.floor(s / 60);
  return m > 0 ? m + "m " + (s % 60) + "s" : s + "s";
}

function toast(msg, type) {
  const el = document.createElement("div");
  el.className = "toast" + (type ? " " + type : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4500);
}

function show(id) { $(id).classList.remove("hide"); }
function hide(id) { $(id).classList.add("hide"); }

function scrollTo(id) {
  const el = $(id);
  if (el && !el.classList.contains("hide"))
    setTimeout(() => el.scrollIntoView({ behavior: "smooth", block: "nearest" }), 60);
}

// ── Language display names ─────────────────────────────────────────────────
const LANG = {
  hin:"Hindi", eng:"English", tam:"Tamil", tel:"Telugu", und:"Unknown",
  spa:"Spanish", fre:"French", fra:"French", ger:"German", deu:"German",
  jpn:"Japanese", kor:"Korean", chi:"Chinese", zho:"Chinese",
  ben:"Bengali", mar:"Marathi", guj:"Gujarati", pan:"Punjabi",
  kan:"Kannada", mal:"Malayalam", ara:"Arabic", rus:"Russian",
  por:"Portuguese", ita:"Italian", nld:"Dutch", tur:"Turkish",
  pol:"Polish", tha:"Thai", vie:"Vietnamese", swe:"Swedish",
  nor:"Norwegian", dan:"Danish", fin:"Finnish", heb:"Hebrew",
  ron:"Romanian", ces:"Czech", hrv:"Croatian", ukr:"Ukrainian",
  cat:"Catalan", ind:"Indonesian", may:"Malay", msa:"Malay",
};
const langName = c => LANG[c] || (c ? c.toUpperCase() : "Unknown");

// ── App state ──────────────────────────────────────────────────────────────
let currentPath = null;
let analysis = null;
let jobId = null;
let pollTimer = null;
let elapsedTimer = null;
let jobStart = null;
let bitrate = "256k";
let serverOS = null;

// Browser item list — maintained separately from DOM to avoid re-parsing data-* attrs
let browserItems = [];   // [{path, name, isDir}, ...]
let focusedIdx = -1;

// Label for the reveal-in-file-manager button, set once we know the server OS.
function revealLabel() {
  if (serverOS === "Windows") return "\u{1F4C2} Show in Explorer";
  if (serverOS === "Darwin")  return "\u{1F4C2} Show in Finder";
  return "\u{1F4C2} Open containing folder";
}

// ── File browser ───────────────────────────────────────────────────────────
async function browse(path) {
  focusedIdx = -1;
  $("#browser").innerHTML = '<div class="empty" aria-busy="true">Loading…</div>';

  let d;
  try {
    const r = await fetch("/browse?path=" + encodeURIComponent(path || ""));
    if (!r.ok) throw new Error("HTTP " + r.status);
    d = await r.json();
  } catch (e) {
    $("#browser").innerHTML = '<div class="empty err">Could not load directory.</div>';
    return;
  }

  // Sync path input to actual resolved path
  $("#pathInput").value = d.path;
  if (d.os) { serverOS = d.os; $("#revealBtn").textContent = revealLabel(); }

  // Error banner (e.g. permission denied)
  const errEl = $("#browserErr");
  if (d.error) {
    errEl.textContent = d.error;
    errEl.classList.remove("hide");
  } else {
    errEl.textContent = "";
    errEl.classList.add("hide");
  }

  // Shortcut buttons
  $("#shortcuts").innerHTML = (d.shortcuts || []).map(s =>
    `<button class="shortcut" role="listitem" data-path="${esc(s.path)}" type="button">${esc(s.label)}</button>`
  ).join("");

  // Build item list + DOM
  browserItems = [];
  let rows = "";

  if (d.parent) {
    browserItems.push({ path: d.parent, name: ".. (up a level)", isDir: true });
    rows += `<div class="item up" role="option" tabindex="-1" data-idx="0"
      aria-label="Go up to parent folder" aria-selected="false">
      <span class="ic" aria-hidden="true">⬆</span>
      <span class="nm">.. (up a level)</span>
    </div>`;
  }

  for (const e of d.entries) {
    const idx = browserItems.length;
    browserItems.push({ path: e.path, name: e.name, isDir: e.dir });
    if (e.dir) {
      rows += `<div class="item" role="option" tabindex="-1" data-idx="${idx}"
        aria-label="Folder: ${esc(e.name)}" aria-selected="false">
        <span class="ic" aria-hidden="true">📁</span>
        <span class="nm">${esc(e.name)}</span>
      </div>`;
    } else {
      rows += `<div class="item file" role="option" tabindex="-1" data-idx="${idx}"
        aria-label="Video: ${esc(e.name)}, ${fmtSize(e.size)}" aria-selected="false">
        <span class="ic" aria-hidden="true">🎬</span>
        <span class="nm">${esc(e.name)}</span>
        <span class="sz">${fmtSize(e.size)}</span>
      </div>`;
    }
  }

  if (!d.entries.length) {
    rows += `<div class="empty">${d.parent ? "No video files in this folder — go up or open a subfolder." : "No files or folders found here."}</div>`;
  }

  $("#browser").innerHTML = rows;
}

// Keyboard navigation inside the browser listbox
$("#browser").addEventListener("keydown", e => {
  const items = $$("#browser [data-idx]");
  if (!items.length) return;

  const total = items.length;
  let next = focusedIdx;

  switch (e.key) {
    case "ArrowDown":  e.preventDefault(); next = Math.min(focusedIdx + 1, total - 1); break;
    case "ArrowUp":    e.preventDefault(); next = Math.max(focusedIdx - 1, 0); break;
    case "Home":       e.preventDefault(); next = 0; break;
    case "End":        e.preventDefault(); next = total - 1; break;
    case "Enter":
    case " ":
      e.preventDefault();
      if (focusedIdx >= 0 && focusedIdx < browserItems.length)
        activateItem(browserItems[focusedIdx]);
      return;
    case "Backspace":
      // Navigate up a level
      if (browserItems[0] && browserItems[0].isDir && browserItems[0].name === ".. (up a level)")
        browse(browserItems[0].path);
      return;
    default: return;
  }

  focusedIdx = next;
  items.forEach((el, i) => {
    const active = i === focusedIdx;
    el.setAttribute("aria-selected", String(active));
    if (active) { el.focus(); el.scrollIntoView({ block: "nearest" }); }
  });
});

// Give the browser container an initial focused state when tabbed into
$("#browser").addEventListener("focus", e => {
  if (e.target === $("#browser") && focusedIdx === -1) {
    const first = $("#browser [data-idx]");
    if (first) {
      focusedIdx = 0;
      first.setAttribute("aria-selected", "true");
      first.focus();
    }
  }
});

// Click delegation — browser items, shortcuts
document.addEventListener("click", e => {
  const sc = e.target.closest(".shortcut[data-path]");
  if (sc) { browse(sc.dataset.path); return; }

  const item = e.target.closest("#browser [data-idx]");
  if (item) {
    const idx = parseInt(item.dataset.idx, 10);
    if (!isNaN(idx) && idx < browserItems.length) activateItem(browserItems[idx]);
    return;
  }
});

function activateItem(item) {
  if (item.isDir) browse(item.path);
  else selectFile(item.path, item.name);
}

// Path input box
$("#pathGo").addEventListener("click", () => browse($("#pathInput").value));
$("#pathInput").addEventListener("keydown", e => {
  if (e.key === "Enter") browse($("#pathInput").value);
});

function selectFile(path, name) {
  currentPath = path;
  $("#selName").textContent = name;
  show("#selCard");
  hide("#pickCard");
  runAnalyze();
}

$("#clearSel").addEventListener("click", () => {
  currentPath = null;
  analysis = null;
  stopPolling();
  ["#selCard", "#analysisCard", "#audioCard", "#subCard", "#optsCard", "#progCard"].forEach(hide);
  show("#pickCard");
  browse($("#pathInput").value || "");
});

// ── Analysis ───────────────────────────────────────────────────────────────
async function runAnalyze() {
  ["#audioCard", "#subCard", "#optsCard", "#progCard"].forEach(hide);
  show("#analysisCard");
  $("#analysisCard").innerHTML = '<span class="muted">Analysing file…</span>';

  let res;
  try {
    const r = await fetch("/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath }),
    });
    res = await r.json();
  } catch {
    $("#analysisCard").innerHTML = '<span class="err">Network error — is the server still running?</span>';
    return;
  }

  analysis = res;
  if (!analysis.ok) {
    $("#analysisCard").innerHTML = `<span class="err">${esc(analysis.error)}</span>`;
    return;
  }

  renderAll();
  scrollTo("#analysisCard");
}

function renderAll() {
  const a = analysis;
  const v = a.video[0];

  $("#analysisCard").innerHTML = `
    <div class="pill-row">
      <span class="pill"><span aria-hidden="true">📏</span> <b>${esc(a.resolution)}</b></span>
      <span class="pill"><span aria-hidden="true">⏱</span> <b>${fmtDur(a.duration)}</b></span>
      <span class="pill"><span aria-hidden="true">🎬</span> <b>${esc(v.codec.toUpperCase())}</b></span>
      <span class="pill"><span aria-hidden="true">🔊</span> <b>${a.audio.length}</b> audio</span>
      <span class="pill"><span aria-hidden="true">💬</span> <b>${a.subs.length}</b> subs</span>
    </div>
    <div class="plan"><b>Video:</b> <span class="muted">${esc(a.video_plan)}</span></div>`;

  // Audio
  if (a.audio.length) {
    $("#audioList").innerHTML = a.audio.map(x => `
      <label class="track">
        <input type="checkbox" class="aud" value="${x.ti}" checked
          aria-label="${esc(langName(x.lang))} audio — ${esc(x.codec.toUpperCase())} ${x.channels}ch">
        <div>
          <div class="t">${esc(langName(x.lang))}
            <span class="badge">${esc(x.codec.toUpperCase())} ${x.channels}ch</span>
            ${x.channels > 2 ? '<span class="badge">→ stereo</span>' : ''}
          </div>
          ${x.title ? `<div class="d">${esc(x.title)}</div>` : ""}
        </div>
      </label>`).join("");
  } else {
    $("#audioList").innerHTML = '<div class="muted">No audio tracks found.</div>';
  }
  show("#audioCard");

  // Subtitles
  if (a.subs.length) {
    $("#subList").innerHTML = a.subs.map(x => x.text
      ? `<label class="track">
          <input type="checkbox" class="sub" value="${x.ti}" checked
            aria-label="${esc(langName(x.lang))} subtitles — ${esc(x.codec)}">
          <div>
            <div class="t">${esc(langName(x.lang))} <span class="badge">${esc(x.codec)}</span></div>
            ${x.title ? `<div class="d">${esc(x.title)}</div>` : ""}
          </div>
        </label>`
      : `<div class="track track-disabled" aria-label="${esc(langName(x.lang))} image subtitles — not supported in MP4">
          <input type="checkbox" disabled aria-hidden="true" tabindex="-1">
          <div>
            <div class="t">${esc(langName(x.lang))} <span class="badge warn">${esc(x.codec)} · image-based</span></div>
            <div class="d">Image subtitles cannot be stored in MP4 — this track will be skipped.</div>
          </div>
        </div>`
    ).join("");
    show("#subCard");
  } else {
    hide("#subCard");
  }

  show("#optsCard");
  updatePreview();
  scrollTo("#audioCard");
}

// ── Options ────────────────────────────────────────────────────────────────
$("#brSeg").addEventListener("click", e => {
  const b = e.target.closest(".seg-btn");
  if (!b || !b.dataset.br) return;
  bitrate = b.dataset.br;
  $$("#brSeg .seg-btn").forEach(btn => {
    const on = btn === b;
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-checked", String(on));
  });
  updatePreview();
});

["#downmix", "#dialogue", "#forceh264"].forEach(s => $(s).addEventListener("change", updatePreview));
document.addEventListener("change", e => {
  if (e.target.classList.contains("aud") || e.target.classList.contains("sub")) updatePreview();
});

function selected(cls) { return $$("." + cls + ":checked").map(c => parseInt(c.value, 10)); }

function updatePreview() {
  if (!analysis) return;
  const au = selected("aud"), su = selected("sub");
  const vc = analysis.video[0].codec;
  let vDesc = "copy";
  if ($("#forceh264").checked || !["h264", "hevc"].includes(vc)) vDesc = "H.264 re-encode";
  else if (vc === "hevc") vDesc = "copy + hvc1 tag";

  const dm = $("#downmix").checked, dl = $("#dialogue").checked;
  let s = `Video: ${vDesc} · ${au.length} audio track${au.length !== 1 ? "s" : ""} → AAC ${bitrate}`;
  if (dm) s += " stereo";
  if (dl) s += " (dialogue boost)";
  if (su.length) s += ` · ${su.length} subtitle${su.length !== 1 ? "s" : ""} → mov_text`;
  s += " · languages preserved.";

  $("#cmdPreview").textContent = s;
  $("#go").disabled = au.length === 0;
}

// ── Conversion ─────────────────────────────────────────────────────────────
$("#go").addEventListener("click", async () => {
  const opts = {
    audio: selected("aud"),
    subs: selected("sub"),
    bitrate,
    downmix: $("#downmix").checked,
    dialogue: $("#dialogue").checked,
    force_h264: $("#forceh264").checked,
  };

  $("#go").disabled = true;
  show("#progCard");
  $("#statusTxt").textContent = "Starting…";
  $("#statusTxt").className = "status-txt muted";
  $("#fill").style.width = "0%";
  $("#fill").setAttribute("aria-valuenow", "0");
  $("#pct").textContent = "0%";
  $("#elapsed").textContent = "";
  $("#log").textContent = "";
  show("#cancelBtn");
  hide("#revealBtn");
  hide("#anotherBtn");
  scrollTo("#progCard");

  let res;
  try {
    const r = await fetch("/convert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: currentPath, opts }),
    });
    res = await r.json();
  } catch {
    $("#statusTxt").textContent = "Network error — could not start conversion.";
    $("#statusTxt").className = "status-txt err";
    $("#go").disabled = false;
    return;
  }

  if (!res.ok) {
    $("#statusTxt").textContent = res.error || "Failed to start.";
    $("#statusTxt").className = "status-txt err";
    $("#go").disabled = false;
    return;
  }

  jobId = res.job_id;
  jobStart = Date.now();
  elapsedTimer = setInterval(() => {
    if (jobStart) $("#elapsed").textContent = "Elapsed: " + fmtElapsed(Date.now() - jobStart);
  }, 1000);
  pollTimer = setInterval(checkJob, 700);
});

async function checkJob() {
  let j;
  try {
    const r = await fetch("/job?id=" + jobId);
    j = await r.json();
  } catch { return; } // transient error — keep polling

  const pct = Math.round(j.progress || 0);
  $("#fill").style.width = pct + "%";
  $("#fill").setAttribute("aria-valuenow", String(pct));
  $("#pct").textContent = pct + "%";
  if (j.log) { $("#log").textContent = j.log; $("#logWrap").scrollTop = $("#logWrap").scrollHeight; }

  if (j.status === "done") {
    stopPolling();
    $("#go").disabled = false;
    hide("#cancelBtn");
    show("#anotherBtn");
    $("#statusTxt").innerHTML = '<span class="ok">✓ Done! Saved next to the original. Open the MP4 in QuickTime, then choose your HomePod pair via Control Center → Sound.</span>';
    $("#statusTxt").className = "status-txt";
    if (j.output) {
      $("#revealBtn").dataset.path = j.output;
      show("#revealBtn");
    }
    toast("Conversion complete!", "ok");

  } else if (j.status === "error") {
    stopPolling();
    $("#go").disabled = false;
    hide("#cancelBtn");
    show("#anotherBtn");
    $("#statusTxt").innerHTML = '<span class="err">✗ Conversion failed — see log below.</span>';
    $("#statusTxt").className = "status-txt";
    toast("Conversion failed.", "err");

  } else if (j.status === "cancelled") {
    stopPolling();
    $("#go").disabled = false;
    hide("#cancelBtn");
    show("#anotherBtn");
    $("#statusTxt").innerHTML = '<span class="warn">Cancelled.</span>';
    $("#statusTxt").className = "status-txt";

  } else {
    $("#statusTxt").textContent = "Converting…";
    $("#statusTxt").className = "status-txt muted";
  }
}

function stopPolling() {
  clearInterval(pollTimer); pollTimer = null;
  clearInterval(elapsedTimer); elapsedTimer = null;
  jobStart = null;
}

$("#cancelBtn").addEventListener("click", async () => {
  if (!jobId) return;
  try {
    await fetch("/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId }),
    });
  } catch {}
  $("#statusTxt").textContent = "Cancelling…";
  // Let polling pick up the cancelled status
});

$("#revealBtn").addEventListener("click", async () => {
  const path = $("#revealBtn").dataset.path;
  if (!path) return;
  try {
    const r = await fetch("/reveal", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    const j = await r.json();
    if (!j.ok) toast(j.error || "Could not open the folder.", "err");
  } catch {
    toast("Could not open the folder.", "err");
  }
});

$("#anotherBtn").addEventListener("click", () => {
  jobId = null;
  hide("#progCard");
  show("#optsCard");
  $("#go").disabled = false;
});

// ── Boot ───────────────────────────────────────────────────────────────────
browse("");
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------
ALLOWED_HOSTS = {
    f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}",
    "127.0.0.1", "localhost",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass  # suppress request logs

    def _host_ok(self):
        """Reject requests whose Host header isn't a loopback address.
        Defeats DNS-rebinding attacks against this localhost-only server."""
        host = (self.headers.get("Host") or "").strip().lower()
        return host in ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
        except (TypeError, ValueError):
            return {}
        if n <= 0 or n > 1_000_000:  # cap body size — these are tiny JSON payloads
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if not self._host_ok():
            self._send(403, {"error": "forbidden"})
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")

        elif parsed.path == "/browse":
            qs = parse_qs(parsed.query)
            path = unquote(qs.get("path", [""])[0])
            self._send(200, list_dir(path))

        elif parsed.path == "/job":
            qs = parse_qs(parsed.query)
            qid = qs.get("id", [""])[0]
            with JOBS_LOCK:
                job = JOBS.get(qid, {"status": "unknown", "progress": 0, "log": ""})
                self._send(200, {k: v for k, v in job.items() if k != "proc"})

        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            self._send(403, {"error": "forbidden"})
            return
        body = self._read_json()

        if self.path == "/analyze":
            path = os.path.expanduser((body.get("path") or "").strip())
            if not path or not os.path.isfile(path):
                self._send(200, {"ok": False, "error": "File not found at that path."})
                return
            self._send(200, analyze(path))

        elif self.path == "/convert":
            path = os.path.expanduser((body.get("path") or "").strip())
            if not path or not os.path.isfile(path):
                self._send(200, {"ok": False, "error": "File not found."})
                return
            opts = body.get("opts") or {}
            job_id = uuid.uuid4().hex[:12]
            with JOBS_LOCK:
                _prune_jobs()
            _update(job_id, status="queued", progress=0, log="")
            threading.Thread(target=run_job, args=(job_id, path, opts),
                             daemon=True).start()
            self._send(200, {"ok": True, "job_id": job_id})

        elif self.path == "/cancel":
            job_id = (body.get("job_id") or "").strip()
            proc = None
            with JOBS_LOCK:
                job = JOBS.get(job_id, {})
                proc = job.get("proc")
                if proc:
                    job["status"] = "cancelling"
            if proc:
                try:
                    proc.terminate()
                except Exception:
                    pass
            self._send(200, {"ok": True})

        elif self.path == "/reveal":
            path = (body.get("path") or "").strip()
            if not path or not os.path.exists(path):
                self._send(200, {"ok": False, "error": "Path not found."})
                return
            try:
                if _OS == "Darwin":
                    subprocess.Popen(["open", "-R", path])
                elif _OS == "Windows":
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                elif shutil.which("xdg-open"):
                    subprocess.Popen(["xdg-open", os.path.dirname(path)])
                else:
                    self._send(200, {"ok": False,
                                     "error": "Reveal not supported on this system."})
                    return
                self._send(200, {"ok": True})
            except Exception as e:
                self._send(200, {"ok": False, "error": str(e)})

        else:
            self._send(404, {"error": "not found"})


def main():
    if not have_tools():
        hints = {
            "Darwin": "brew install ffmpeg",
            "Linux": "sudo apt install ffmpeg  (or your distro's package manager)",
            "Windows": "https://ffmpeg.org/download.html",
        }
        print("ERROR: ffmpeg/ffprobe not found.")
        print("Install with: " + hints.get(_OS, "https://ffmpeg.org/download.html"))
        return

    url = f"http://{HOST}:{PORT}/"

    # Bind first so we can report "port in use" cleanly before opening a browser.
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as e:
        if e.errno in (48, 98, 10048):  # EADDRINUSE on macOS / Linux / Windows
            print(f"Port {PORT} is already in use.")
            print(f"HomePod Converter may already be running — open:  {url}")
            try:
                webbrowser.open(url)
            except Exception:
                pass
        else:
            print(f"ERROR: could not start server: {e}")
        return

    print("─" * 56)
    print("  HomePod Converter is running.")
    print(f"  Open:  {url}")
    print("  Press Control-C to stop.")
    print("─" * 56)
    try:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
