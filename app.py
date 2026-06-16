from flask import Flask, request, jsonify, send_file, abort
import yt_dlp
import os
import uuid
import hashlib
import time
import threading
from datetime import datetime, timezone

app = Flask(__name__)
DOWNLOAD_DIR = "./downloads"
COOKIES_FILE = "cookies.txt"
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")  # apna VPS URL yahan set karo

# Token store: { token: { filepath, expires } }
token_store = {}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


# ─── Helper: Token Generate ───────────────────────
def generate_token(filepath, expires_in=3600):
    """Token generate karo aur file ko auto-delete schedule karo"""
    random_id = str(uuid.uuid4()).replace("-", "")
    token = hashlib.sha256(f"{filepath}{random_id}{time.time()}".encode()).hexdigest()
    expires_at = int(time.time()) + expires_in

    token_store[token] = {
        "filepath": filepath,
        "expires": expires_at,
    }

    # Auto-delete timer
    def delete_later():
        time.sleep(expires_in)
        entry = token_store.pop(token, None)
        if entry:
            try:
                if os.path.exists(entry["filepath"]):
                    os.remove(entry["filepath"])
                    print(f"[AUTO-DELETE] {entry['filepath']}")
            except Exception as e:
                print(f"[DELETE ERROR] {e}")

    t = threading.Thread(target=delete_later, daemon=True)
    t.start()

    return token, expires_at


# ─── 1. Video Info ───────────────────────────────
@app.route('/info', methods=['GET'])
def get_info():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'URL required'}), 400

    opts = {'cookiefile': COOKIES_FILE, 'quiet': True}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info)
            return jsonify({
                'title': info.get('title'),
                'duration': info.get('duration'),
                'thumbnail': info.get('thumbnail'),
                'uploader': info.get('uploader'),
                'formats_count': len(info.get('formats', [])),
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── 2. Available Formats ────────────────────────
@app.route('/formats', methods=['GET'])
def get_formats():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'URL required'}), 400

    opts = {'cookiefile': COOKIES_FILE, 'quiet': True}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = [
                {
                    'format_id': f.get('format_id'),
                    'ext': f.get('ext'),
                    'resolution': f.get('resolution'),
                    'filesize': f.get('filesize'),
                    'vcodec': f.get('vcodec'),
                    'acodec': f.get('acodec'),
                }
                for f in info.get('formats', [])
            ]
            return jsonify({'formats': formats})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── 3. Download + Link Generate ─────────────────
@app.route('/download', methods=['POST'])
def download():
    data = request.json or {}
    url = data.get('url')
    quality = data.get('quality', 'best')
    expires_in = int(data.get('expires_in', 3600))  # default 1 hour

    if not url:
        return jsonify({'error': 'URL required'}), 400

    opts = {
        'cookiefile': COOKIES_FILE,
        'format': quality,
        'outtmpl': f'{DOWNLOAD_DIR}/%(title)s_%(format_id)s.%(ext)s',
        'quiet': True,
    }

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)

            # FFmpeg merge ke baad .mp4 bhi check karo
            if not os.path.exists(filepath):
                mp4_path = os.path.splitext(filepath)[0] + '.mp4'
                if os.path.exists(mp4_path):
                    filepath = mp4_path

            if not os.path.exists(filepath):
                return jsonify({'error': 'File not found after download'}), 500

            # Token + link generate karo
            token, expires_at = generate_token(filepath, expires_in)
            filename = os.path.basename(filepath)
            ext = filename.rsplit('.', 1)[-1].upper() if '.' in filename else 'MP4'

            # Quality/resolution detect
            height = info.get('height')
            quality_label = f"{height}p" if height else quality

            download_url = f"{BASE_URL}/files/{filename.replace(' ', '%20')}?token={token}&expires={expires_at}"

            return jsonify({
                "status": True,
                "author": "@nexray - ElrayyXml",
                "result": {
                    "title": info.get('title', ''),
                    "author": info.get('uploader', ''),
                    "thumbnail": info.get('thumbnail', ''),
                    "duration": info.get('duration', 0),
                    "format": ext,
                    "quality": quality_label,
                    "url": download_url,
                },
                "timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.') +
                             f"{datetime.now().microsecond // 1000:03d}Z",
                "response_time": "0ms"  # optional: actual timing lagana ho to time.time() use karo
            })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── 4. File Serve (Token Check) ─────────────────
@app.route('/files/<path:filename>', methods=['GET'])
def serve_file(filename):
    token = request.args.get('token')

    if not token or token not in token_store:
        abort(403, "Invalid or expired token")

    entry = token_store[token]

    # Expiry check
    if time.time() > entry["expires"]:
        token_store.pop(token, None)
        try:
            if os.path.exists(entry["filepath"]):
                os.remove(entry["filepath"])
        except:
            pass
        abort(410, "Link expired and file deleted")

    filepath = entry["filepath"]
    if not os.path.exists(filepath):
        abort(404, "File not found")

    return send_file(filepath, as_attachment=True, download_name=os.path.basename(filepath))


# ─── 5. Progress Check ───────────────────────────
download_progress = {}

@app.route('/progress', methods=['GET'])
def progress():
    url = request.args.get('url')
    return jsonify(download_progress.get(url, {'status': 'not found'}))


# ─── 6. HTML UI ──────────────────────────────────
@app.route('/')
def ui():
    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OURIN — Hotstar Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=Space+Mono:wght@400;700&family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:     #030508;
  --bg2:    #060c12;
  --green:  #00ff88;
  --green2: #00cc6a;
  --cyan:   #00e5ff;
  --blue:   #0066ff;
  --purple: #7000ff;
  --red:    #ff2255;
  --text:   #e8f4f0;
  --muted:  #4a6860;
  --card:   rgba(0,255,136,0.04);
  --border: rgba(0,255,136,0.12);
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { scroll-behavior: smooth; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: 'Outfit', sans-serif;
  min-height: 100vh;
  overflow-x: hidden;
  cursor: none;
}
#cursor {
  width: 12px; height: 12px;
  background: var(--green);
  border-radius: 50%;
  position: fixed; top: 0; left: 0;
  pointer-events: none; z-index: 9999;
  transition: transform 0.1s;
  box-shadow: 0 0 20px var(--green), 0 0 40px var(--green);
}
#cursor-ring {
  width: 36px; height: 36px;
  border: 1px solid rgba(0,255,136,0.4);
  border-radius: 50%;
  position: fixed; top: 0; left: 0;
  pointer-events: none; z-index: 9998;
  transition: all 0.15s ease;
}
.grid-bg {
  position: fixed; inset: 0;
  background-image:
    linear-gradient(rgba(0,255,136,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(0,255,136,0.03) 1px, transparent 1px);
  background-size: 60px 60px;
  pointer-events: none;
}
body::before {
  content: '';
  position: fixed; inset: 0;
  background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='noise'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23noise)' opacity='0.03'/%3E%3C/svg%3E");
  pointer-events: none; z-index: 1000; opacity: 0.4;
}
.glow1 {
  position: fixed;
  width: 600px; height: 600px;
  background: radial-gradient(circle, rgba(0,255,136,0.06) 0%, transparent 70%);
  top: 50%; left: 50%; transform: translate(-50%,-50%);
  pointer-events: none; animation: glowPulse 5s ease-in-out infinite;
}
.glow2 {
  position: fixed;
  width: 350px; height: 350px;
  background: radial-gradient(circle, rgba(0,102,255,0.05) 0%, transparent 70%);
  top: 10%; right: 5%;
  pointer-events: none; animation: glowPulse 7s ease-in-out infinite reverse;
}
@keyframes glowPulse {
  0%,100% { opacity: 1; transform: translate(-50%,-50%) scale(1); }
  50%      { opacity: 0.6; transform: translate(-50%,-50%) scale(1.1); }
}
@keyframes fadeUp {
  from { opacity: 0; transform: translateY(20px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes pulse {
  0%,100% { opacity: 1; transform: scale(1); }
  50%      { opacity: 0.5; transform: scale(0.8); }
}
nav {
  position: fixed; top: 0; left: 0; right: 0;
  z-index: 100; padding: 18px 40px;
  display: flex; align-items: center; justify-content: space-between;
  background: linear-gradient(to bottom, rgba(3,5,8,0.95), transparent);
  backdrop-filter: blur(10px);
}
.nav-logo {
  font-family: 'Syne', sans-serif;
  font-weight: 800; font-size: 20px;
  color: var(--green); letter-spacing: -0.5px;
  display: flex; align-items: center; gap: 10px;
  text-decoration: none;
}
.nav-logo span {
  width: 8px; height: 8px;
  background: var(--green); border-radius: 50%;
  animation: pulse 2s infinite;
}
.nav-badge {
  font-family: 'Space Mono', monospace;
  font-size: 11px; color: var(--muted);
  letter-spacing: 1.5px; text-transform: uppercase;
}
.wrapper {
  position: relative; z-index: 2;
  min-height: 100vh;
  display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  padding: 100px 20px 60px;
}
.page-header {
  text-align: center; margin-bottom: 48px;
  animation: fadeUp 0.7s ease both;
}
.page-eyebrow {
  display: inline-flex; align-items: center; gap: 8px;
  background: rgba(0,255,136,0.07);
  border: 1px solid rgba(0,255,136,0.18);
  padding: 5px 14px;
  font-family: 'Space Mono', monospace;
  font-size: 10px; color: var(--green);
  letter-spacing: 2px; text-transform: uppercase;
  margin-bottom: 20px;
  clip-path: polygon(6px 0%, 100% 0%, calc(100% - 6px) 100%, 0% 100%);
}
.page-eyebrow::before {
  content: ''; width: 6px; height: 6px;
  background: var(--green); border-radius: 50%;
  animation: pulse 1.5s infinite;
}
.page-title {
  font-family: 'Syne', sans-serif;
  font-weight: 800;
  font-size: clamp(28px, 6vw, 56px);
  line-height: 1.05;
  letter-spacing: -1.5px;
}
.page-title .t1 { color: var(--text); }
.page-title .t2 {
  background: linear-gradient(90deg, var(--green), var(--cyan));
  -webkit-background-clip: text; -webkit-text-fill-color: transparent;
  background-clip: text;
}
.page-sub {
  margin-top: 12px;
  font-size: 15px; font-weight: 300;
  color: rgba(232,244,240,0.5);
  font-family: 'Space Mono', monospace;
  letter-spacing: 0.5px;
}
.dl-card {
  width: 100%; max-width: 680px;
  background: var(--card);
  border: 1px solid var(--border);
  padding: 32px;
  position: relative;
  animation: fadeUp 0.7s 0.1s ease both;
}
.dl-card::before {
  content: '';
  position: absolute; top: 0; left: 0; right: 0; height: 1px;
  background: linear-gradient(90deg, transparent, var(--green), transparent);
  opacity: 0.5;
}
.url-row {
  display: flex; gap: 10px; align-items: stretch;
  margin-bottom: 8px;
}
.url-input {
  flex: 1;
  background: rgba(0,255,136,0.04);
  border: 1px solid rgba(0,255,136,0.15);
  color: var(--text);
  font-family: 'Space Mono', monospace;
  font-size: 10px;
  padding: 10px 14px;
  outline: none;
  transition: border-color 0.3s, box-shadow 0.3s;
}
.url-input::placeholder { color: var(--muted); }
.url-input:focus {
  border-color: rgba(0,255,136,0.45);
  box-shadow: 0 0 0 3px rgba(0,255,136,0.06);
}
.btn {
  font-family: 'Space Mono', monospace;
  font-size: 12px; font-weight: 700;
  letter-spacing: 1px; text-transform: uppercase;
  border: none; padding: 12px 22px;
  cursor: none; transition: all 0.25s;
  display: inline-flex; align-items: center; gap: 6px;
  white-space: nowrap;
  clip-path: polygon(8px 0%, 100% 0%, calc(100% - 8px) 100%, 0% 100%);
}
.btn-fetch { background: var(--green); color: var(--bg); }
.btn-fetch:hover { background: var(--cyan); box-shadow: 0 0 20px rgba(0,229,255,0.35); }
.btn-fetch:disabled { background: var(--muted); color: rgba(255,255,255,0.3); cursor: default; }
.btn-download {
  width: 100%;
  background: linear-gradient(90deg, var(--green), var(--cyan));
  color: var(--bg);
  margin-top: 16px;
  justify-content: center;
  font-size: 13px; padding: 14px 28px;
  clip-path: polygon(10px 0%, 100% 0%, calc(100% - 10px) 100%, 0% 100%);
}
.btn-download:hover { box-shadow: 0 0 32px rgba(0,255,136,0.3); filter: brightness(1.1); }
.btn-download:disabled {
  background: rgba(255,255,255,0.06);
  color: var(--muted); cursor: default;
  clip-path: none; box-shadow: none; filter: none;
}
.status-line {
  font-family: 'Space Mono', monospace;
  font-size: 11px; color: var(--muted);
  min-height: 18px; margin-bottom: 12px;
  display: flex; align-items: center; gap: 8px;
  letter-spacing: 0.5px;
}
.status-line.ok  { color: var(--green); }
.status-line.err { color: var(--red); }
.status-line.spin { color: var(--cyan); }
.dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: currentColor; flex-shrink: 0;
  animation: pulse 1.2s infinite;
}
.video-info {
  display: none;
  align-items: center; gap: 14px;
  background: rgba(0,255,136,0.03);
  border: 1px solid rgba(0,255,136,0.1);
  padding: 12px 14px;
  margin-bottom: 16px;
}
.video-info.show { display: flex; }
.vi-thumb {
  width: 64px; height: 40px;
  object-fit: cover; flex-shrink: 0;
  border: 1px solid rgba(0,255,136,0.15);
}
.vi-meta { flex: 1; min-width: 0; }
.vi-title {
  font-size: 13px; font-weight: 600;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.vi-detail {
  font-family: 'Space Mono', monospace;
  font-size: 10px; color: var(--muted);
  margin-top: 4px; letter-spacing: 0.5px;
}
.divider {
  display: flex; align-items: center; gap: 12px;
  margin: 20px 0 14px;
}
.divider-line {
  flex: 1; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(0,255,136,0.15), transparent);
}
.divider-label {
  font-family: 'Space Mono', monospace;
  font-size: 10px; color: var(--muted);
  letter-spacing: 2px; text-transform: uppercase;
}
.format-box-wrap { display: none; }
.format-box-wrap.show { display: block; }
.format-tabs { display: flex; gap: 4px; margin-bottom: 10px; }
.ftab {
  font-family: 'Space Mono', monospace;
  font-size: 11px; letter-spacing: 1px; text-transform: uppercase;
  padding: 6px 14px; cursor: none;
  background: rgba(0,255,136,0.04);
  border: 1px solid rgba(0,255,136,0.1);
  color: var(--muted);
  transition: all 0.2s;
  clip-path: polygon(5px 0%, 100% 0%, calc(100% - 5px) 100%, 0% 100%);
}
.ftab.active {
  background: rgba(0,255,136,0.1);
  border-color: rgba(0,255,136,0.35);
  color: var(--green);
}
.ftab:hover:not(.active) { border-color: rgba(0,255,136,0.2); color: var(--text); }
.format-scroll {
  max-height: 260px;
  overflow-y: auto;
  border: 1px solid rgba(0,255,136,0.1);
  padding: 6px;
  background: rgba(0,0,0,0.2);
  scrollbar-width: thin;
  scrollbar-color: rgba(0,255,136,0.2) transparent;
}
.format-scroll::-webkit-scrollbar { width: 4px; }
.format-scroll::-webkit-scrollbar-track { background: transparent; }
.format-scroll::-webkit-scrollbar-thumb { background: rgba(0,255,136,0.2); border-radius: 2px; }
.format-item {
  display: grid;
  grid-template-columns: 1fr auto auto;
  align-items: center; gap: 10px;
  padding: 10px 12px;
  margin-bottom: 4px;
  border: 1px solid rgba(0,255,136,0.06);
  background: rgba(0,255,136,0.02);
  cursor: none; transition: all 0.2s;
  position: relative;
}
.format-item:last-child { margin-bottom: 0; }
.format-item:hover { border-color: rgba(0,255,136,0.2); background: rgba(0,255,136,0.05); }
.format-item.selected { border-color: var(--green); background: rgba(0,255,136,0.08); }
.format-item.selected::before {
  content: '';
  position: absolute; left: 0; top: 0; bottom: 0;
  width: 2px; background: var(--green);
}
.fi-label { font-size: 13px; font-weight: 500; color: var(--text); }
.fi-label .fi-res { font-family: 'Space Mono', monospace; font-size: 12px; color: var(--green); }
.fi-label .fi-codec { font-size: 11px; color: var(--muted); margin-top: 2px; }
.fi-ext {
  font-family: 'Space Mono', monospace;
  font-size: 10px; color: var(--cyan);
  background: rgba(0,229,255,0.08);
  border: 1px solid rgba(0,229,255,0.15);
  padding: 2px 8px;
  letter-spacing: 1px; text-transform: uppercase;
}
.fi-size { font-family: 'Space Mono', monospace; font-size: 11px; color: var(--muted); text-align: right; white-space: nowrap; }
.fi-check { display: none; position: absolute; right: 10px; font-size: 14px; }
.format-item.selected .fi-check { display: block; color: var(--green); }
.format-empty {
  text-align: center; padding: 30px;
  font-family: 'Space Mono', monospace;
  font-size: 11px; color: var(--muted); letter-spacing: 1px;
}
.selected-badge {
  display: none;
  align-items: center; gap: 8px;
  margin-top: 10px;
  font-family: 'Space Mono', monospace;
  font-size: 11px; color: var(--muted); letter-spacing: 0.5px;
}
.selected-badge.show { display: flex; }
.sb-val {
  color: var(--green);
  background: rgba(0,255,136,0.08);
  border: 1px solid rgba(0,255,136,0.2);
  padding: 2px 10px;
}
.progress-wrap { display: none; margin-top: 16px; }
.progress-wrap.show { display: block; }
.progress-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }
.progress-label {
  font-family: 'Space Mono', monospace;
  font-size: 11px; color: var(--muted); letter-spacing: 1px; text-transform: uppercase;
}
.progress-pct { font-family: 'Space Mono', monospace; font-size: 12px; font-weight: 700; color: var(--green); }
.progress-track { width: 100%; height: 4px; background: rgba(0,255,136,0.08); position: relative; overflow: hidden; }
.progress-bar {
  height: 100%;
  background: linear-gradient(90deg, var(--green), var(--cyan));
  width: 0%; transition: width 0.3s ease; position: relative;
}
.progress-bar::after {
  content: '';
  position: absolute; right: 0; top: 0; bottom: 0;
  width: 40px;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.4));
}
.progress-meta {
  display: flex; justify-content: space-between;
  margin-top: 8px;
  font-family: 'Space Mono', monospace;
  font-size: 10px; color: var(--muted); letter-spacing: 0.5px;
}
/* ── RESULT CARD ── */
.result-wrap { display: none; margin-top: 20px; }
.result-wrap.show { display: block; }
.result-card {
  background: rgba(0,255,136,0.03);
  border: 1px solid rgba(0,255,136,0.2);
  padding: 16px 18px;
}
.result-card::before {
  content: '// DOWNLOAD READY';
  display: block;
  font-family: 'Space Mono', monospace;
  font-size: 9px; color: var(--green);
  letter-spacing: 3px; margin-bottom: 12px;
  opacity: 0.7;
}
.result-row {
  display: flex; gap: 8px; align-items: baseline;
  margin-bottom: 6px;
  font-family: 'Space Mono', monospace; font-size: 11px;
}
.result-key { color: var(--muted); min-width: 80px; letter-spacing: 0.5px; }
.result-val { color: var(--text); word-break: break-all; }
.result-val.green { color: var(--green); }
.result-val.cyan  { color: var(--cyan); }
.btn-open-link {
  display: block; width: 100%; margin-top: 14px;
  background: transparent;
  border: 1px solid var(--green);
  color: var(--green);
  font-family: 'Space Mono', monospace;
  font-size: 11px; letter-spacing: 2px; text-transform: uppercase;
  padding: 10px; text-align: center;
  text-decoration: none;
  transition: all 0.2s;
  clip-path: polygon(6px 0%, 100% 0%, calc(100% - 6px) 100%, 0% 100%);
}
.btn-open-link:hover {
  background: rgba(0,255,136,0.1);
  box-shadow: 0 0 20px rgba(0,255,136,0.2);
}
.page-footer {
  text-align: center; margin-top: 40px;
  font-family: 'Space Mono', monospace;
  font-size: 10px; color: rgba(74,104,96,0.5);
  letter-spacing: 1.5px; text-transform: uppercase;
  animation: fadeUp 0.7s 0.3s ease both;
}
.page-footer a { color: var(--green); text-decoration: none; }
</style>
</head>
<body>

<div class="grid-bg"></div>
<div class="glow1"></div>
<div class="glow2"></div>
<div id="cursor"></div>
<div id="cursor-ring"></div>

<nav>
  <a class="nav-logo" href="#">OURIN <span></span></a>
  <span class="nav-badge">// HOTSTAR DOWNLOADER</span>
</nav>

<div class="wrapper">

  <div class="page-header">
    <div class="page-eyebrow">STK Assistant &#8226; Media Tool</div>
    <h1 class="page-title">
      <span class="t1">HOTSTAR </span><span class="t2">DOWNLOADER</span>
    </h1>
    <p class="page-sub">// paste url &rarr; select format &rarr; download</p>
  </div>

  <div class="dl-card">

    <div class="url-row">
      <input class="url-input" id="urlInput" type="text" placeholder="https://www.hotstar.com/..." autocomplete="off" />
      <button class="btn btn-fetch" id="fetchBtn" onclick="getFormats()">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
        FETCH
      </button>
    </div>

    <div class="status-line" id="statusLine">
      <span id="statusText">Enter a Hotstar URL above to begin</span>
    </div>

    <div class="video-info" id="videoInfo">
      <img class="vi-thumb" id="viThumb" src="" alt="" />
      <div class="vi-meta">
        <div class="vi-title" id="viTitle">&#8212;</div>
        <div class="vi-detail" id="viDetail">&#8212;</div>
      </div>
    </div>

    <div class="format-box-wrap" id="formatBoxWrap">
      <div class="divider">
        <div class="divider-line"></div>
        <span class="divider-label">Select Format</span>
        <div class="divider-line"></div>
      </div>
      <div class="format-tabs">
        <button class="ftab active" id="tabVideo" onclick="switchTab('video')">&#9654; Video</button>
        <button class="ftab" id="tabAudio" onclick="switchTab('audio')">&#9835; Audio</button>
      </div>
      <div class="format-scroll" id="formatScroll">
        <div class="format-empty">No formats loaded yet</div>
      </div>
      <div class="selected-badge" id="selectedBadge">
        <span>Selected:</span>
        <span class="sb-val" id="sbVal">&#8212;</span>
        <span id="sbExtra"></span>
      </div>
    </div>

    <button class="btn btn-download" id="dlBtn" disabled onclick="downloadVideo()">
      &#11015; &nbsp;DOWNLOAD
    </button>

    <div class="progress-wrap" id="progressWrap">
      <div class="progress-header">
        <span class="progress-label" id="progressLabel">Preparing...</span>
        <span class="progress-pct" id="progressPct">0%</span>
      </div>
      <div class="progress-track">
        <div class="progress-bar" id="progressBar"></div>
      </div>
      <div class="progress-meta">
        <span id="progressSpeed">&#8212;</span>
        <span id="progressEta">ETA &#8212;</span>
      </div>
    </div>

    <!-- Result Card (shown after download link is ready) -->
    <div class="result-wrap" id="resultWrap">
      <div class="result-card">
        <div class="result-row"><span class="result-key">title</span><span class="result-val" id="resTitle">&#8212;</span></div>
        <div class="result-row"><span class="result-key">author</span><span class="result-val green" id="resAuthor">&#8212;</span></div>
        <div class="result-row"><span class="result-key">duration</span><span class="result-val cyan" id="resDuration">&#8212;</span></div>
        <div class="result-row"><span class="result-key">format</span><span class="result-val cyan" id="resFormat">&#8212;</span></div>
        <div class="result-row"><span class="result-key">quality</span><span class="result-val cyan" id="resQuality">&#8212;</span></div>
        <div class="result-row"><span class="result-key">expires</span><span class="result-val" id="resExpires">&#8212;</span></div>
        <a class="btn-open-link" id="resLink" href="#" target="_blank">&#11015; &nbsp;OPEN DOWNLOAD LINK</a>
      </div>
    </div>

  </div>

  <div class="page-footer">
    Powered by <a href="#">OURIN AI</a> &nbsp;&bull;&nbsp; dev: ashish &nbsp;&bull;&nbsp; ourinpro.vercel.app
  </div>

</div>

<script>
// ── CURSOR ──
const cursor = document.getElementById('cursor')
const ring   = document.getElementById('cursor-ring')
let mx = 0, my = 0, rx = 0, ry = 0

document.addEventListener('mousemove', e => { mx = e.clientX; my = e.clientY })

;(function loop() {
  rx += (mx - rx) * 0.15
  ry += (my - ry) * 0.15
  cursor.style.left = mx - 6 + 'px'
  cursor.style.top  = my - 6 + 'px'
  ring.style.left   = rx - 18 + 'px'
  ring.style.top    = ry - 18 + 'px'
  requestAnimationFrame(loop)
})()

document.querySelectorAll('button, a, input').forEach(el => {
  el.addEventListener('mouseenter', () => {
    cursor.style.transform = 'scale(2)'
    ring.style.transform   = 'scale(1.5)'
    ring.style.borderColor = 'rgba(0,255,136,0.7)'
  })
  el.addEventListener('mouseleave', () => {
    cursor.style.transform = 'scale(1)'
    ring.style.transform   = 'scale(1)'
    ring.style.borderColor = 'rgba(0,255,136,0.4)'
  })
})

// ── STATE ──
let currentTab     = 'video'
let videoFormats   = []
let audioFormats   = []
let selectedVideo  = null
let selectedAudio  = null
let currentUrl     = ''

// ── HELPERS ──
function setStatus(msg, type = '') {
  const sl = document.getElementById('statusLine')
  sl.className = 'status-line ' + type
  if (type === 'spin') {
    sl.innerHTML = `<span class="dot"></span><span id="statusText">${msg}</span>`
  } else if (type === 'ok') {
    sl.innerHTML = `<span style="color:var(--green)">&#10003;</span> <span id="statusText">${msg}</span>`
  } else if (type === 'err') {
    sl.innerHTML = `<span style="color:var(--red)">&#10007;</span> <span id="statusText">${msg}</span>`
  } else {
    sl.innerHTML = `<span id="statusText">${msg}</span>`
  }
}

function fmtSize(bytes) {
  if (!bytes) return '&#8212;'
  if (bytes > 1e9) return (bytes / 1e9).toFixed(2) + ' GB'
  if (bytes > 1e6) return (bytes / 1e6).toFixed(1) + ' MB'
  return (bytes / 1e3).toFixed(0) + ' KB'
}

function fmtDuration(s) {
  if (!s) return ''
  const m = Math.floor(s / 60), sec = s % 60
  return m + ':' + String(sec).padStart(2, '0')
}

function fmtExpiry(ts) {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString() + ' (' + Math.round((ts - Date.now()/1000)/60) + ' min left)'
}

// ── FETCH FORMATS ──
async function getFormats() {
  const url = document.getElementById('urlInput').value.trim()
  if (!url) { setStatus('URL daalo pehle!', 'err'); return }
  currentUrl = url

  const btn = document.getElementById('fetchBtn')
  btn.disabled = true
  btn.textContent = '...'

  document.getElementById('formatBoxWrap').classList.remove('show')
  document.getElementById('videoInfo').classList.remove('show')
  document.getElementById('dlBtn').disabled = true
  document.getElementById('progressWrap').classList.remove('show')
  document.getElementById('resultWrap').classList.remove('show')
  selectedVideo = null; selectedAudio = null
  document.getElementById('selectedBadge').classList.remove('show')

  setStatus('Fetching formats...', 'spin')

  try {
    const infoRes = await fetch('/info?url=' + encodeURIComponent(url))
    const info    = await infoRes.json()
    if (!infoRes.ok) throw new Error(info.error || 'Info fetch failed')

    const vi = document.getElementById('videoInfo')
    document.getElementById('viThumb').src = info.thumbnail || ''
    document.getElementById('viThumb').style.display = info.thumbnail ? 'block' : 'none'
    document.getElementById('viTitle').textContent = info.title || 'Unknown Title'
    document.getElementById('viDetail').textContent =
      [info.uploader, fmtDuration(info.duration), info.formats_count + ' formats'].filter(Boolean).join('  ·  ')
    vi.classList.add('show')

    const fmtRes  = await fetch('/formats?url=' + encodeURIComponent(url))
    const fmtData = await fmtRes.json()
    if (!fmtRes.ok) throw new Error(fmtData.error || 'Formats fetch failed')

    videoFormats = fmtData.formats.filter(f => f.vcodec && f.vcodec !== 'none')
    audioFormats = fmtData.formats.filter(f => !f.vcodec || f.vcodec === 'none')

    document.getElementById('formatBoxWrap').classList.add('show')
    switchTab('video')

    setStatus(`${fmtData.formats.length} formats found — select one below`, 'ok')
  } catch (e) {
    setStatus(e.message || 'Something went wrong', 'err')
  } finally {
    btn.disabled = false
    btn.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg> FETCH`
  }
}

// ── RENDER FORMATS ──
function renderFormats(list, type) {
  const scroll = document.getElementById('formatScroll')
  if (!list.length) {
    scroll.innerHTML = `<div class="format-empty">No ${type} formats available</div>`
    return
  }
  scroll.innerHTML = list.map((f, i) => {
    const isSelected = type === 'video' ? selectedVideo === f.format_id : selectedAudio === f.format_id
    return `
    <div class="format-item${isSelected ? ' selected' : ''}"
         onclick="selectFormat('${f.format_id}', '${type}', ${i})"
         data-idx="${i}" data-type="${type}">
      <div class="fi-label">
        <div class="fi-res">${f.resolution || f.format_id}</div>
        <div class="fi-codec">${f.vcodec !== 'none' ? (f.vcodec || '') : ''} ${f.acodec !== 'none' ? (f.acodec || '') : ''}</div>
      </div>
      <span class="fi-ext">${f.ext || '?'}</span>
      <div class="fi-size">${fmtSize(f.filesize)}</div>
      <span class="fi-check">&#10003;</span>
    </div>`
  }).join('')
}

// ── SWITCH TAB ──
function switchTab(tab) {
  currentTab = tab
  document.getElementById('tabVideo').classList.toggle('active', tab === 'video')
  document.getElementById('tabAudio').classList.toggle('active', tab === 'audio')
  if (tab === 'video') renderFormats(videoFormats, 'video')
  else                 renderFormats(audioFormats, 'audio')
}

// ── SELECT FORMAT ──
function selectFormat(id, type, idx) {
  if (type === 'video') {
    selectedVideo = selectedVideo === id ? null : id
  } else {
    selectedAudio = selectedAudio === id ? null : id
  }
  const list = type === 'video' ? videoFormats : audioFormats
  renderFormats(list, type)

  const badge = document.getElementById('selectedBadge')
  const sbVal  = document.getElementById('sbVal')
  if (selectedVideo || selectedAudio) {
    badge.classList.add('show')
    const parts = []
    if (selectedVideo) {
      const vf = videoFormats.find(f => f.format_id === selectedVideo)
      parts.push(`Video: ${vf?.resolution || selectedVideo}`)
    }
    if (selectedAudio) {
      const af = audioFormats.find(f => f.format_id === selectedAudio)
      parts.push(`Audio: ${af?.format_id || selectedAudio}`)
    }
    sbVal.textContent = parts.join(' + ')
  } else {
    badge.classList.remove('show')
  }
  document.getElementById('dlBtn').disabled = !(selectedVideo || selectedAudio)
}

// ── DOWNLOAD ──
async function downloadVideo() {
  const url = document.getElementById('urlInput').value.trim()
  if (!url) { setStatus('URL missing!', 'err'); return }
  if (!selectedVideo && !selectedAudio) { setStatus('Pehle format select karo', 'err'); return }

  const quality = selectedAudio && selectedVideo
    ? selectedVideo + '+' + selectedAudio
    : (selectedVideo || selectedAudio)

  // Show progress
  const pw = document.getElementById('progressWrap')
  pw.classList.add('show')
  setProgress(0, 'Starting...')
  document.getElementById('progressSpeed').textContent = '&#8212;'
  document.getElementById('progressEta').textContent   = 'ETA &#8212;'
  document.getElementById('resultWrap').classList.remove('show')
  document.getElementById('dlBtn').disabled = true

  setStatus('Downloading & generating link...', 'spin')
  simulateProgress()

  try {
    const res = await fetch('/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, quality, expires_in: 3600 })
    })
    const data = await res.json()

    if (data.status) {
      setProgress(100, 'Done!')
      setStatus('Download link ready!', 'ok')

      // Fill result card
      document.getElementById('resTitle').textContent    = data.result.title || '&#8212;'
      document.getElementById('resAuthor').textContent   = data.result.author || '&#8212;'
      document.getElementById('resDuration').textContent = fmtDuration(data.result.duration)
      document.getElementById('resFormat').textContent   = data.result.format + ' · ' + data.result.quality
      document.getElementById('resQuality').textContent  = data.result.quality
      document.getElementById('resExpires').textContent  = fmtExpiry(parseInt(new URL(data.result.url).searchParams.get('expires')))
      document.getElementById('resLink').href            = data.result.url
      document.getElementById('resultWrap').classList.add('show')
    } else {
      setStatus('Error: ' + (data.error || 'Unknown error'), 'err')
      setProgress(0, 'Failed')
    }
  } catch (e) {
    setStatus('Request failed: ' + e.message, 'err')
  } finally {
    document.getElementById('dlBtn').disabled = false
  }
}

function setProgress(pct, label) {
  document.getElementById('progressBar').style.width   = pct + '%'
  document.getElementById('progressPct').textContent   = pct + '%'
  document.getElementById('progressLabel').textContent = label || 'Downloading...'
}

function simulateProgress() {
  let p = 0
  const stages = [
    { target: 15, label: 'Connecting...', speed: '&#8212;' },
    { target: 55, label: 'Downloading...', speed: '2.4 MB/s' },
    { target: 80, label: 'Processing...', speed: '4.1 MB/s' },
    { target: 92, label: 'Finalizing...', speed: '1.2 MB/s' },
  ]
  let si = 0
  const timer = setInterval(() => {
    if (si >= stages.length) { clearInterval(timer); return }
    const stage = stages[si]
    p = Math.min(p + Math.random() * 3, stage.target)
    setProgress(Math.round(p), stage.label)
    document.getElementById('progressSpeed').textContent = stage.speed
    const eta = Math.max(0, Math.round((100 - p) / 3))
    document.getElementById('progressEta').textContent = eta > 0 ? `ETA ~${eta}s` : 'Almost done'
    if (p >= stage.target) si++
  }, 200)
}

// ── ENTER KEY ──
document.getElementById('urlInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') getFormats()
})
</script>
</body>
</html>'''


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

