#!/usr/bin/env python3
"""
HomePod Converter — a local web app that converts video files to QuickTime-friendly
MP4s optimized for AirPlay playback on a stereo HomePod pair.

Pick a file with the built-in browser (no uploads — the app reads it in place on your
Mac), choose exactly which audio/subtitle tracks you want, and convert.

  • Video : copied bit-for-bit when QuickTime can play it (H.264/HEVC) — zero quality
            loss; HEVC gets the hvc1 tag. Re-encodes only formats QuickTime can't open.
  • Audio : copies AAC; transcodes EAC3/AC3/DTS to AAC. Downmixes surround to clean
            stereo for the HomePod pair, with optional center-channel dialogue boost.
  • Subs  : text subs (SRT/ASS) → mov_text; image subs (PGS/VOBSUB) can't go in MP4.
  • Meta  : strips junk titles, KEEPS language tags so QuickTime shows real languages.

RUN:    python3 homepod_converter.py     (opens in your browser)
NEEDS:  macOS Python 3 (built in) + ffmpeg/ffprobe  ->  brew install ffmpeg
"""

import json
import os
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
VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".ts", ".webm", ".wmv",
              ".flv", ".mpg", ".mpeg", ".m2ts", ".vob", ".ogv", ".3gp", ".divx"}


# ----------------------------------------------------------------------------
# ffmpeg / ffprobe helpers
# ----------------------------------------------------------------------------
def have_tools():
    return shutil.which("ffmpeg") and shutil.which("ffprobe")


def probe(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", path],
            capture_output=True, text=True, timeout=60,
        )
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except Exception:
        return None


def analyze(path):
    data = probe(path)
    if not data or "streams" not in data:
        return {"ok": False, "error": "Could not read this file. It may be corrupt "
                                      "or an unsupported format."}

    duration = float(data.get("format", {}).get("duration", 0) or 0)
    video, audio, subs = [], [], []
    ai = si = 0
    for s in data["streams"]:
        codec = s.get("codec_name", "?")
        ctype = s.get("codec_type")
        lang = s.get("tags", {}).get("language", "und")
        title = s.get("tags", {}).get("title", "")
        if ctype == "video":
            if s.get("disposition", {}).get("attached_pic"):
                continue
            video.append({"codec": codec, "width": s.get("width"),
                          "height": s.get("height")})
        elif ctype == "audio":
            audio.append({"ti": ai, "codec": codec,
                          "channels": s.get("channels", 2) or 2,
                          "lang": lang, "title": title})
            ai += 1
        elif ctype == "subtitle":
            subs.append({"ti": si, "codec": codec, "lang": lang, "title": title,
                         "text": codec in TEXT_SUB_CODECS})
            si += 1

    if not video:
        return {"ok": False, "error": "No video stream found."}

    v = video[0]
    if v["codec"] == "h264":
        v_plan = "Copy as-is (no quality loss)"
    elif v["codec"] == "hevc":
        v_plan = "Copy as-is + hvc1 tag (no quality loss)"
    else:
        v_plan = f"Re-encode {v['codec']} → H.264 (needed for QuickTime)"

    return {"ok": True, "duration": duration, "video": video, "audio": audio,
            "subs": subs, "video_plan": v_plan,
            "resolution": f'{v.get("width","?")}×{v.get("height","?")}'}


def build_command(path, output, analysis, opts):
    bitrate = opts.get("bitrate", "256k")
    downmix = opts.get("downmix", True)
    dialogue = opts.get("dialogue", False)
    force_h264 = opts.get("force_h264", False)

    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", path, "-map", "0:v"]

    vcodec = analysis["video"][0]["codec"]
    if force_h264 or vcodec not in ("h264", "hevc"):
        cmd += ["-c:v", "h264_videotoolbox", "-b:v", "12000k", "-tag:v", "avc1"]
    elif vcodec == "hevc":
        cmd += ["-c:v", "copy", "-tag:v", "hvc1"]
    else:
        cmd += ["-c:v", "copy"]

    by_ti = {a["ti"]: a for a in analysis["audio"]}
    for out_idx, ti in enumerate(opts.get("audio", [])):
        a = by_ti.get(ti)
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
                            "pan=stereo|FL=FC+0.30*FL+0.30*BL|"
                            "FR=FC+0.30*FR+0.30*BR"]
                else:
                    cmd += [f"-ac:a:{out_idx}", "2"]

    sub_by_ti = {s["ti"]: s for s in analysis["subs"]}
    chosen_subs = [ti for ti in opts.get("subs", [])
                   if ti in sub_by_ti and sub_by_ti[ti]["text"]]
    for ti in chosen_subs:
        cmd += ["-map", f"0:s:{ti}"]
    if chosen_subs:
        cmd += ["-c:s", "mov_text"]

    cmd += ["-metadata", "title=", "-metadata:s", "title=",
            "-movflags", "+faststart", output]
    return cmd


# ----------------------------------------------------------------------------
# Server-side file browser (localhost only — reads files in place, no upload)
# ----------------------------------------------------------------------------
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
                        files.append({"name": name, "path": full, "dir": False,
                                      "size": size})
            except OSError:
                continue
    except PermissionError:
        return {"path": path, "parent": os.path.dirname(path),
                "entries": [], "error": "Permission denied for this folder."}

    parent = os.path.dirname(path) if path != "/" else None

    # handy shortcuts
    home = os.path.expanduser("~")
    shortcuts = []
    for label, p in [("Home", home),
                     ("Downloads", os.path.join(home, "Downloads")),
                     ("Movies", os.path.join(home, "Movies")),
                     ("Desktop", os.path.join(home, "Desktop")),
                     ("Volumes", "/Volumes")]:
        if os.path.isdir(p):
            shortcuts.append({"label": label, "path": p})

    return {"path": path, "parent": parent, "shortcuts": shortcuts,
            "entries": folders + files}


# ----------------------------------------------------------------------------
# Conversion worker
# ----------------------------------------------------------------------------
TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")


def run_job(job_id, path, opts):
    analysis = analyze(path)
    if not analysis["ok"]:
        _update(job_id, status="error", log=analysis["error"])
        return

    base, _ = os.path.splitext(path)
    output = base + ".mp4"
    if os.path.abspath(output) == os.path.abspath(path):
        output = base + ".converted.mp4"

    cmd = build_command(path, output, analysis, opts)
    total = analysis["duration"] or 1
    _update(job_id, status="running", output=output, duration=total,
            log="Starting…\n$ " + " ".join(_q(c) for c in cmd) + "\n\n")

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            m = TIME_RE.search(line)
            if m:
                secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                _update(job_id, progress=max(0, min(100, secs / total * 100)))
        proc.wait()
        if proc.returncode == 0:
            _update(job_id, status="done", progress=100,
                    log_append=f"\n✓ Finished: {output}\n")
        else:
            _update(job_id, status="error",
                    log_append=f"\n✗ ffmpeg exited with code {proc.returncode}\n")
    except Exception as e:
        _update(job_id, status="error", log_append=f"\n✗ {e}\n")


def _q(s):
    return f'"{s}"' if (" " in s or "|" in s) else s


def _update(job_id, **kw):
    with JOBS_LOCK:
        job = JOBS.setdefault(job_id, {"progress": 0, "status": "queued",
                                       "log": "", "output": "", "duration": 0})
        if "log_append" in kw:
            job["log"] += kw.pop("log_append")
        if "log" in kw:
            job["log"] = kw.pop("log")
        job.update(kw)


# ----------------------------------------------------------------------------
# Web UI
# ----------------------------------------------------------------------------
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HomePod Converter</title>
<style>
  :root{--bg:#0b0d12;--panel:#151922;--panel2:#1c212d;--line:#2a3140;--txt:#e6e9ef;
        --muted:#8b94a7;--accent:#5b8cff;--accent2:#37d39b;--warn:#ffb454;--err:#ff6b6b;}
  *{box-sizing:border-box}
  body{margin:0;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       background:var(--bg);color:var(--txt);min-height:100vh}
  .wrap{max-width:800px;margin:0 auto;padding:32px 20px 80px}
  h1{font-size:22px;margin:0 0 4px;letter-spacing:-.3px}
  .sub{color:var(--muted);margin:0 0 28px;font-size:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:18px}
  .card h2{font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin:0 0 14px;font-weight:600}
  /* file browser */
  .crumbs{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
  .shortcut{background:var(--panel2);border:1px solid var(--line);color:var(--txt);border-radius:8px;
            padding:6px 12px;font-size:13px;cursor:pointer}
  .shortcut:hover{border-color:var(--accent)}
  .curpath{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--muted);
           margin-bottom:10px;word-break:break-all}
  .browser{border:1px solid var(--line);border-radius:10px;max-height:340px;overflow:auto;background:#0e1219}
  .item{display:flex;align-items:center;gap:11px;padding:10px 14px;cursor:pointer;border-bottom:1px solid #1a1f2b;font-size:14px}
  .item:last-child{border-bottom:none}
  .item:hover{background:#161d2b}
  .item .ic{width:20px;text-align:center;flex-shrink:0}
  .item .nm{flex:1;word-break:break-all}
  .item .sz{color:var(--muted);font-size:12px;flex-shrink:0}
  .item.up{color:var(--muted)}
  .item.file .nm{color:var(--accent2)}
  .empty{padding:18px 14px;color:var(--muted);font-size:13px}
  /* analysis + options */
  .row{display:flex;gap:10px;flex-wrap:wrap;margin:0 0 14px}
  .pill{display:flex;align-items:center;gap:7px;background:var(--panel2);border:1px solid var(--line);border-radius:999px;padding:6px 13px;font-size:13px}
  .pill b{color:var(--accent2);font-weight:600}
  .plan{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:13px 15px;font-size:13.5px;margin-bottom:6px}
  .plan b{color:var(--txt)} .plan .muted{color:var(--muted)}
  .track{display:flex;align-items:flex-start;gap:11px;padding:10px 0;border-top:1px solid var(--line)}
  .track:first-of-type{border-top:none}
  .track input{margin-top:3px;accent-color:var(--accent)}
  .track .t{font-size:14px}
  .track .d{color:var(--muted);font-size:12.5px;margin-top:1px}
  .badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:6px;background:#243049;color:#9db4ff;margin-left:6px}
  .badge.warn{background:#3a2d16;color:var(--warn)}
  label.opt{display:flex;align-items:flex-start;gap:11px;padding:11px 0;cursor:pointer;border-top:1px solid var(--line)}
  label.opt:first-of-type{border-top:none}
  label.opt input{margin-top:3px;accent-color:var(--accent)}
  .field{margin:14px 0}.field .lbl{font-size:13px;color:var(--muted);margin-bottom:2px}
  .seg{display:flex;gap:6px;margin-top:6px}
  .seg button{flex:1;background:var(--panel2);border:1px solid var(--line);color:var(--muted);border-radius:8px;padding:8px;font-size:13px;cursor:pointer;font-weight:500}
  .seg button.on{background:var(--accent);color:#fff;border-color:var(--accent)}
  button.primary{background:var(--accent);color:#fff;border:0;border-radius:10px;padding:13px 20px;font-size:15px;font-weight:600;cursor:pointer;width:100%;transition:.15s}
  button.primary:hover{filter:brightness(1.08)} button.primary:disabled{opacity:.5;cursor:not-allowed}
  .bar{height:10px;background:var(--panel2);border-radius:999px;overflow:hidden;margin:14px 0 8px}
  .bar i{display:block;height:100%;width:0;background:linear-gradient(90deg,var(--accent),var(--accent2));transition:width .3s}
  pre{background:#0a0c11;border:1px solid var(--line);border-radius:10px;padding:14px;font-size:12px;max-height:260px;overflow:auto;white-space:pre-wrap;color:#aeb6c6}
  .hide{display:none}.muted{color:var(--muted)}.ok{color:var(--accent2)}.err{color:var(--err)}.warn{color:var(--warn)}
  .mini{font-size:12px;color:var(--muted);margin-top:8px}
  .selfile{display:flex;align-items:center;gap:10px;background:var(--panel2);border:1px solid var(--accent);border-radius:10px;padding:11px 14px;margin-bottom:6px;font-size:13.5px}
  .selfile .x{margin-left:auto;color:var(--muted);cursor:pointer;font-size:18px;line-height:1}
</style></head>
<body><div class="wrap">
  <h1>HomePod Converter</h1>
  <p class="sub">Pick a video → choose which tracks you want → get a QuickTime-ready MP4 tuned for your stereo HomePod pair.</p>

  <div id="pickCard" class="card"><h2>Choose a file</h2>
    <div class="crumbs" id="shortcuts"></div>
    <div class="curpath" id="curpath"></div>
    <div class="browser" id="browser"><div class="empty">Loading…</div></div>
  </div>

  <div id="selCard" class="card hide"><h2>Selected file</h2>
    <div class="selfile"><span>🎬</span><span id="selName"></span><span class="x" id="clearSel" title="Choose a different file">✕</span></div>
  </div>

  <div id="analysis" class="card hide"></div>
  <div id="audioCard" class="card hide"><h2>Audio tracks</h2><div id="audioList"></div></div>
  <div id="subCard" class="card hide"><h2>Subtitle tracks</h2><div id="subList"></div></div>

  <div id="optsCard" class="card hide"><h2>Output options</h2>
    <div class="field"><div class="lbl">Audio quality (AAC bitrate)</div>
      <div class="seg" id="brSeg">
        <button data-br="192k">192k</button>
        <button data-br="256k" class="on">256k</button>
        <button data-br="320k">320k</button></div></div>
    <label class="opt"><input type="checkbox" id="downmix" checked>
      <div><div class="t">Downmix surround to stereo</div>
        <div class="d">Best for a HomePod stereo pair. Recommended ON.</div></div></label>
    <label class="opt"><input type="checkbox" id="dialogue">
      <div><div class="t">Dialogue boost</div>
        <div class="d">Raises the center channel so speech sits above music/effects. Surround tracks only.</div></div></label>
    <label class="opt"><input type="checkbox" id="forceh264">
      <div><div class="t">Force H.264 re-encode</div>
        <div class="d">Only if HEVC won't play on an older Mac. Slower; otherwise leave OFF to keep full quality.</div></div></label>
    <div style="margin-top:16px"><button class="primary" id="go">Convert</button></div>
    <div class="mini" id="cmdPreview"></div>
  </div>

  <div id="progCard" class="card hide">
    <div id="status" class="muted">Working…</div>
    <div class="bar"><i id="fill"></i></div>
    <div id="pct" class="muted" style="font-size:13px">0%</div>
    <pre id="log"></pre>
  </div>
</div>
<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
let currentPath=null, analysis=null, jobId=null, poll=null, bitrate="256k";

// ---------- file browser ----------
function fmtSize(b){if(!b)return"";const u=["B","KB","MB","GB"];let i=0,n=b;
  while(n>=1024&&i<u.length-1){n/=1024;i++;}return n.toFixed(n<10&&i>0?1:0)+" "+u[i];}

async function browse(path){
  const r=await fetch("/browse?path="+encodeURIComponent(path||""));
  const d=await r.json();
  $("#curpath").textContent=d.path;
  // shortcuts
  $("#shortcuts").innerHTML=(d.shortcuts||[]).map(s=>
    `<button class="shortcut" data-p="${encodeURIComponent(s.path)}">${s.label}</button>`).join("");
  // list
  let rows="";
  if(d.parent) rows+=`<div class="item up" data-dir="${encodeURIComponent(d.parent)}"><span class="ic">⬆</span><span class="nm">.. (up a level)</span></div>`;
  if(d.entries.length===0 && !d.parent) rows+=`<div class="empty">No video files or folders here.</div>`;
  for(const e of d.entries){
    if(e.dir) rows+=`<div class="item" data-dir="${encodeURIComponent(e.path)}"><span class="ic">📁</span><span class="nm">${e.name}</span></div>`;
    else rows+=`<div class="item file" data-file="${encodeURIComponent(e.path)}" data-name="${encodeURIComponent(e.name)}"><span class="ic">🎬</span><span class="nm">${e.name}</span><span class="sz">${fmtSize(e.size)}</span></div>`;
  }
  if(d.entries.length===0 && d.parent) rows+=`<div class="empty">No video files in this folder. Open a subfolder or go up.</div>`;
  $("#browser").innerHTML=rows;
}

document.addEventListener("click",e=>{
  const sc=e.target.closest(".shortcut");
  if(sc){browse(decodeURIComponent(sc.dataset.p));return;}
  const dir=e.target.closest("[data-dir]");
  if(dir){browse(decodeURIComponent(dir.dataset.dir));return;}
  const f=e.target.closest("[data-file]");
  if(f){selectFile(decodeURIComponent(f.dataset.file),decodeURIComponent(f.dataset.name));return;}
});

function selectFile(path,name){
  currentPath=path;
  $("#selName").textContent=name;
  $("#selCard").classList.remove("hide");
  $("#pickCard").classList.add("hide");
  runAnalyze();
}
$("#clearSel").onclick=()=>{
  currentPath=null; analysis=null;
  ["#selCard","#analysis","#audioCard","#subCard","#optsCard","#progCard"].forEach(s=>$(s).classList.add("hide"));
  $("#pickCard").classList.remove("hide");
};

// ---------- analysis ----------
const LANG={hin:"Hindi",eng:"English",tam:"Tamil",tel:"Telugu",und:"Unknown",
  spa:"Spanish",fre:"French",fra:"French",ger:"German",deu:"German",jpn:"Japanese",
  kor:"Korean",chi:"Chinese",zho:"Chinese",ben:"Bengali",mar:"Marathi",guj:"Gujarati",
  pan:"Punjabi",kan:"Kannada",mal:"Malayalam",ara:"Arabic",rus:"Russian",por:"Portuguese",
  ita:"Italian",nld:"Dutch",tur:"Turkish",pol:"Polish",tha:"Thai",vie:"Vietnamese"};
const langName=c=>LANG[c]||(c?c.toUpperCase():"Unknown");

async function runAnalyze(){
  ["#analysis","#audioCard","#subCard","#optsCard"].forEach(s=>$(s).classList.add("hide"));
  $("#analysis").classList.remove("hide");
  $("#analysis").innerHTML='<span class="muted">Analyzing…</span>';
  try{
    const r=await fetch("/analyze",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({path:currentPath})});
    analysis=await r.json();
  }catch(e){$("#analysis").innerHTML='<span class="err">Server error.</span>';return;}
  if(!analysis.ok){$("#analysis").innerHTML='<span class="err">'+analysis.error+'</span>';return;}
  renderAll();
}

function fmtDur(s){s=Math.round(s);const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (h?h+"h ":"")+(m?m+"m ":"")+x+"s";}

function renderAll(){
  const a=analysis;
  $("#analysis").innerHTML=`<div class="row">
    <span class="pill">📐 <b>${a.resolution}</b></span>
    <span class="pill">⏱ <b>${fmtDur(a.duration)}</b></span>
    <span class="pill">🎬 <b>${a.video[0].codec.toUpperCase()}</b></span>
    <span class="pill">🔊 <b>${a.audio.length}</b> audio</span>
    <span class="pill">💬 <b>${a.subs.length}</b> subs</span></div>
    <div class="plan"><b>Video:</b> <span class="muted">${a.video_plan}</span></div>`;

  $("#audioList").innerHTML=a.audio.map(x=>`
    <label class="track"><input type="checkbox" class="aud" value="${x.ti}" checked>
      <div><div class="t">${langName(x.lang)}
        <span class="badge">${x.codec.toUpperCase()} ${x.channels}ch</span>
        ${x.channels>2?'<span class="badge">→ stereo</span>':''}</div>
        ${x.title?`<div class="d">${x.title}</div>`:''}</div></label>`).join("")
        || '<div class="muted">No audio tracks.</div>';
  $("#audioCard").classList.remove("hide");

  if(a.subs.length){
    $("#subList").innerHTML=a.subs.map(x=>x.text?`
      <label class="track"><input type="checkbox" class="sub" value="${x.ti}" checked>
        <div><div class="t">${langName(x.lang)} <span class="badge">${x.codec}</span></div>
          ${x.title?`<div class="d">${x.title}</div>`:''}</div></label>`:`
      <label class="track" style="opacity:.55"><input type="checkbox" disabled>
        <div><div class="t">${langName(x.lang)} <span class="badge warn">${x.codec} · image-based</span></div>
          <div class="d">MP4 can't store image subtitles — this track can't be included.</div></div></label>`).join("");
    $("#subCard").classList.remove("hide");
  } else $("#subCard").classList.add("hide");

  $("#optsCard").classList.remove("hide");
  updatePreview();
}

$("#brSeg").addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;
  bitrate=b.dataset.br;$$("#brSeg button").forEach(x=>x.classList.toggle("on",x===b));updatePreview();});
["#downmix","#dialogue","#forceh264"].forEach(s=>$(s).addEventListener("change",updatePreview));
document.addEventListener("change",e=>{if(e.target.classList.contains("aud")||e.target.classList.contains("sub"))updatePreview();});

function selected(cls){return $$("."+cls+":checked").map(c=>parseInt(c.value));}
function updatePreview(){
  if(!analysis)return;
  const au=selected("aud"),su=selected("sub");
  let v="copy";const vc=analysis.video[0].codec;
  if($("#forceh264").checked||!["h264","hevc"].includes(vc))v="H.264 re-encode";
  else if(vc==="hevc")v="copy + hvc1";
  $("#cmdPreview").textContent=`Will produce: video ${v} · ${au.length} audio track(s) `+
    `${$("#downmix").checked?"→ AAC "+bitrate+" stereo":"→ AAC "+bitrate}`+
    `${$("#dialogue").checked?" (dialogue boost)":""} · ${su.length} subtitle(s) → mov_text · languages preserved.`;
  $("#go").disabled=au.length===0;
}

$("#go").onclick=async()=>{
  const opts={audio:selected("aud"),subs:selected("sub"),bitrate,
    downmix:$("#downmix").checked,dialogue:$("#dialogue").checked,force_h264:$("#forceh264").checked};
  $("#go").disabled=true;$("#progCard").classList.remove("hide");
  $("#status").textContent="Starting…";$("#status").className="muted";
  const r=await fetch("/convert",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({path:currentPath,opts})});
  const j=await r.json();
  if(!j.ok){$("#status").textContent=j.error||"Failed to start.";$("#status").className="err";$("#go").disabled=false;return;}
  jobId=j.job_id;poll=setInterval(checkJob,700);
};

async function checkJob(){
  const r=await fetch("/job?id="+jobId);const j=await r.json();
  $("#fill").style.width=(j.progress||0)+"%";
  $("#pct").textContent=Math.round(j.progress||0)+"%";
  $("#log").textContent=j.log||"";$("#log").scrollTop=$("#log").scrollHeight;
  if(j.status==="done"){clearInterval(poll);$("#go").disabled=false;
    $("#status").innerHTML='<span class="ok">✓ Done — saved next to your original. Open the .mp4 in QuickTime, then pick your HomePod pair in Control Center → Sound.</span>';}
  else if(j.status==="error"){clearInterval(poll);$("#go").disabled=false;
    $("#status").innerHTML='<span class="err">✗ Conversion failed — see log below.</span>';}
  else{$("#status").textContent="Converting…";$("#status").className="muted";}
}

// boot: open the browser at the default folder
browse("");
</script></body></html>"""


# ----------------------------------------------------------------------------
# HTTP server
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/" or parsed.path.startswith("/index"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif parsed.path == "/browse":
            qs = parse_qs(parsed.query)
            path = unquote(qs.get("path", [""])[0])
            self._send(200, list_dir(path))
        elif parsed.path == "/job":
            qs = parse_qs(parsed.query)
            qid = qs.get("id", [""])[0]
            with JOBS_LOCK:
                self._send(200, dict(JOBS.get(qid, {"status": "unknown",
                                                    "progress": 0, "log": ""})))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
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
            opts = body.get("opts", {}) or {}
            job_id = uuid.uuid4().hex[:12]
            _update(job_id, status="queued", progress=0, log="")
            threading.Thread(target=run_job, args=(job_id, path, opts),
                             daemon=True).start()
            self._send(200, {"ok": True, "job_id": job_id})
        else:
            self._send(404, {"error": "not found"})


def main():
    if not have_tools():
        print("ERROR: ffmpeg/ffprobe not found. Install with:  brew install ffmpeg")
        return
    url = f"http://{HOST}:{PORT}/"
    print("─" * 56)
    print("  HomePod Converter is running.")
    print(f"  Open:  {url}")
    print("  Press Control-C here to stop.")
    print("─" * 56)
    try:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()