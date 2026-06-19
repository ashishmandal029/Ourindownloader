from flask import Flask, request, jsonify, send_file, abort
import yt_dlp
import os
import re
import uuid
import hashlib
import time
import threading
from datetime import datetime, timezone
from urllib.parse import quote as url_quote
from html import escape as html_escape
import json
import requests

app = Flask(__name__)
DOWNLOAD_DIR = "./downloads"
COOKIES_FILE = "cookies.txt"
BASE_URL = os.environ.get("BASE_URL", "https://stkecho.eu.cc")  # apna VPS URL yahan set karo
CONCURRENT_FRAGMENTS = int(os.environ.get("CONCURRENT_FRAGMENTS", "16"))  # parallel fragment downloads — server bandwidth zyada hai to 16-32 try karo

# Token store: { token: { filepath, expires } }
token_store = {}

# Job store: { job_id: { status: "pending"|"done"|"error", result/error, created_at } }
jobs = {}
JOB_TTL = 6 * 3600  # purane jobs ko 6 ghante baad memory se clear kar do

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# ─── Comments store ────────────────────────────────
# { video_id: [ {id, name, text, ts}, ... ] }  — keyed by a stable video_id
# (hash of the source URL) so comments survive re-downloads/new tokens for
# the same episode/movie, not just the current ephemeral file.
COMMENTS_FILE = "comments.json"
comments_store = {}
_comments_lock = threading.Lock()


def _load_comments():
    global comments_store
    try:
        if os.path.exists(COMMENTS_FILE):
            with open(COMMENTS_FILE, "r", encoding="utf-8") as f:
                comments_store = json.load(f)
    except Exception as e:
        print(f"[COMMENTS LOAD ERROR] {e}")


def _save_comments():
    try:
        with open(COMMENTS_FILE, "w", encoding="utf-8") as f:
            json.dump(comments_store, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[COMMENTS SAVE ERROR] {e}")


def _video_id_for(source_url):
    """Stable id derived from the original source URL so the same video keeps
    its comment thread across different downloads/quality picks/tokens."""
    return hashlib.sha256((source_url or "").encode("utf-8")).hexdigest()[:16]


_load_comments()


# ─── Helper: Token Generate ───────────────────────
def generate_token(filepath, expires_in=3600, metadata=None):
    """Token generate karo aur file ko auto-delete schedule karo.
    metadata (optional): title/series/season/episode/duration/thumbnail/description
    etc, stored alongside so /watch can render episode details without
    re-running yt-dlp."""
    random_id = str(uuid.uuid4()).replace("-", "")
    token = hashlib.sha256(f"{filepath}{random_id}{time.time()}".encode()).hexdigest()
    expires_at = int(time.time()) + expires_in

    token_store[token] = {
        "filepath": filepath,
        "expires": expires_at,
        "metadata": metadata or {},
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


# ─── Helper: Job Cleanup ───────────────────────────
def _schedule_job_cleanup(job_id, ttl=JOB_TTL):
    """Purana job dict se hata do TTL ke baad — memory leak na ho."""
    def cleanup():
        time.sleep(ttl)
        jobs.pop(job_id, None)
    t = threading.Thread(target=cleanup, daemon=True)
    t.start()


# ─── Helper: Fallback title scraper ───────────────
# yt-dlp's hotstar extractor is weaker on /movies/ URLs than /shows/ URLs
# (the show extractor pulls full series/episode metadata via Hotstar's API;
# movies often only get a generic "hotstar video #<id>" placeholder title
# with everything else null). When we detect that placeholder, scrape the
# page's <title> / og:title / og:description tags directly as a fallback —
# these are present in the HTML even when yt-dlp's structured extraction misses them.
_PLACEHOLDER_TITLE_RE = re.compile(r'^hotstar video #\d+$', re.IGNORECASE)


def _is_placeholder_title(title):
    return bool(title) and bool(_PLACEHOLDER_TITLE_RE.match(title.strip()))


def _scrape_fallback_meta(url):
    """
    Best-effort scrape of <title>, og:title, og:description, og:image from
    the raw Hotstar page HTML. Returns a dict with whatever it could find;
    never raises (network/parse errors just result in an empty dict) since
    this is a non-critical enhancement layer over yt-dlp's real extraction.
    """
    result = {}
    try:
        resp = requests.get(
            url,
            headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'},
            timeout=10,
        )
        if not resp.ok:
            return result
        html = resp.text

        def _meta(prop_or_name):
            m = re.search(
                rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop_or_name)}["\'][^>]+content=["\']([^"\']+)["\']',
                html, re.IGNORECASE
            )
            return m.group(1).strip() if m else None

        og_title = _meta('og:title')
        og_desc = _meta('og:description')
        og_image = _meta('og:image')

        title_tag = None
        m = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
        if m:
            title_tag = m.group(1).strip()
            # Hotstar usually appends " - Hotstar" / " | Disney+ Hotstar" etc — strip that
            title_tag = re.split(r'\s*[\|\-–]\s*(?:Disney\+?\s*)?Hotstar\b.*$', title_tag, flags=re.IGNORECASE)[0].strip()

        best_title = og_title or title_tag
        if best_title:
            result['title'] = best_title
        if og_desc:
            result['description'] = og_desc
        if og_image:
            result['thumbnail'] = og_image

    except Exception as e:
        print(f"[FALLBACK SCRAPE ERROR] {e}")

    return result


def _fmt_upload_date(raw):
    """yt-dlp gives upload_date as YYYYMMDD string -> convert to YYYY-MM-DD"""
    if not raw or len(raw) != 8:
        return raw
    try:
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    except Exception:
        return raw


@app.route('/info', methods=['GET'])
def get_info():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'URL required'}), 400

    opts = {'cookiefile': COOKIES_FILE, 'quiet': False}

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            info = ydl.sanitize_info(info)

            series_name = (
                info.get('series')
                or info.get('show')
                or info.get('playlist_title')
                or info.get('album')
            )

            episode_name = (
                info.get('episode')
                or info.get('track')
                or info.get('title')
            )

            upload_date_raw = info.get('upload_date') or info.get('release_date')

            title = info.get('title')
            thumbnail = info.get('thumbnail')
            description = info.get('description')

            # ── Fallback: movies often only get a generic placeholder title from
            #    yt-dlp's hotstar extractor. Scrape the real title/og tags instead.
            if _is_placeholder_title(title):
                fallback = _scrape_fallback_meta(url)
                title = fallback.get('title') or title
                thumbnail = thumbnail or fallback.get('thumbnail')
                description = description or fallback.get('description')

            return jsonify({
                'title': title,
                'duration': info.get('duration'),
                'thumbnail': thumbnail,
                'uploader': info.get('uploader'),
                'formats_count': len(info.get('formats', [])),
                'series_name': series_name,
                'episode_name': episode_name if not _is_placeholder_title(episode_name) else title,
                'episode_number': info.get('episode_number'),
                'season_name': info.get('season'),
                'season_number': info.get('season_number'),
                'upload_date': _fmt_upload_date(upload_date_raw),
                'upload_date_raw': upload_date_raw,
                'release_timestamp': info.get('release_timestamp'),
                'description': description,
                'view_count': info.get('view_count'),
                'age_limit': info.get('age_limit'),
                'categories': info.get('categories'),
            })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500

# ─── 2. Available Formats ────────────────────────
@app.route('/formats', methods=['GET'])
def get_formats():
    url = request.args.get('url')
    if not url:
        return jsonify({'error': 'URL required'}), 400

    opts = {'cookiefile': COOKIES_FILE, 'quiet': False}

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
def _clean_name(s):
    """Filesystem-safe AND url-safe name: remove illegal/URL-special chars, collapse spaces, escape % for outtmpl."""
    if not s:
        return s
    s = str(s).strip()
    s = re.sub(r'[\\/:*?"<>|]', '', s)      # remove illegal filename chars
    s = re.sub(r'[#?&]', '', s)             # remove URL-special chars (# starts a fragment, ? / & break query parsing)
    s = re.sub(r'\s+', ' ', s).strip()      # collapse whitespace
    s = s.replace('%', '%%')                # escape % so yt-dlp outtmpl doesn't choke
    return s


def _build_base_name(info):
    """
    Builds filename base:
      - Series available  -> SeriesName S{season}E{episode} EpisodeName
      - No series metadata -> just Title  (old behaviour, unchanged)
    """
    series = info.get('series') or info.get('show')
    title = info.get('title') or 'video'
    episode_name = info.get('episode') or title

    if not series:
        return _clean_name(title)

    season_num = info.get('season_number')
    ep_num = info.get('episode_number')
    if season_num is None:
        season_num = 1  # most shows on Hotstar without explicit season -> treat as season 1

    se_tag = f"S{season_num}E{ep_num}" if ep_num is not None else f"S{season_num}"

    parts = [_clean_name(series), se_tag]
    cleaned_episode = _clean_name(episode_name)
    if cleaned_episode and cleaned_episode.lower() != _clean_name(series).lower():
        parts.append(cleaned_episode)

    return ' '.join(p for p in parts if p)


_KNOWN_VIDEO_CODECS = {'avc', 'hvc', 'hev', 'vp9', 'vp09', 'av1', 'h264', 'h265'}


def _codec_short(raw):
    """'avc1.42C00C' -> 'avc' | 'hvc1.1.6.L60.90' -> 'hvc' | 'mp4a.40.2' -> 'mp4a'"""
    if not raw or raw == 'none':
        return ''
    s = str(raw).split('.')[0]
    s = re.sub(r'[^A-Za-z0-9]', '', s)
    m = re.match(r'^([A-Za-z]+)(\d+)$', s)
    if m and m.group(1).lower() in _KNOWN_VIDEO_CODECS:
        return m.group(1)
    return s


def _fmt_size_short(num_bytes):
    """Human readable size for filenames: 45MB / 1.2GB"""
    if not num_bytes:
        return None
    if num_bytes > 1e9:
        return f"{num_bytes / 1e9:.1f}GB"
    if num_bytes > 1e6:
        return f"{num_bytes / 1e6:.0f}MB"
    return f"{max(num_bytes / 1e3, 1):.0f}KB"


def _build_quality_tag(quality, formats_list):
    """
    Builds a short, readable quality tag from the selected format_id(s), e.g:
      (320x180-hvc [mp4a])   -> video+audio (combined or merged)
      (320x180-avc)          -> video only, no audio track
      [mp4a]                 -> audio only
    """
    ids = [i for i in quality.split('+') if i]
    fmt_by_id = {f.get('format_id'): f for f in formats_list}
    chosen = [fmt_by_id[i] for i in ids if i in fmt_by_id]
    if not chosen:
        return ''

    resolution, vcodec_raw, acodec_raw = None, None, None
    for f in chosen:
        if f.get('vcodec') and f.get('vcodec') != 'none' and not vcodec_raw:
            vcodec_raw = f.get('vcodec')
            resolution = f.get('resolution')
        if f.get('acodec') and f.get('acodec') != 'none' and not acodec_raw:
            acodec_raw = f.get('acodec')

    vshort = _codec_short(vcodec_raw)
    ashort = _codec_short(acodec_raw)

    if resolution:
        core = resolution + (f"-{vshort}" if vshort else '')
        return f"({core}" + (f" [{ashort}])" if ashort else ")")
    if ashort:
        return f"[{ashort}]"
    return ''


def _run_download_job(job_id, url, quality, expires_in):
    """
    Actual heavy lifting (probe + yt-dlp download + ffmpeg merge + token gen)
    runs here in a background thread. No HTTP connection is attached to this,
    so it can take as long as it needs without hitting any gateway timeout
    (Cloudflare's 524 etc).
    """
    try:
        # Step 1: metadata-only fetch to compute the filename base
        probe_opts = {'cookiefile': COOKIES_FILE, 'quiet': True}
        with yt_dlp.YoutubeDL(probe_opts) as probe_ydl:
            probe_info = probe_ydl.extract_info(url, download=False)
            probe_info = probe_ydl.sanitize_info(probe_info)

        # ── Same placeholder-title fallback as /info — fixes movie filenames
        #    like "hotstar video #1271630221" which broke download URLs (the
        #    '#' was being parsed as a URL fragment by browsers).
        if _is_placeholder_title(probe_info.get('title')):
            fallback = _scrape_fallback_meta(url)
            if fallback.get('title'):
                probe_info['title'] = fallback['title']
                if _is_placeholder_title(probe_info.get('episode')):
                    probe_info['episode'] = fallback['title']

        base_name = _build_base_name(probe_info)
        quality_tag = _build_quality_tag(quality, probe_info.get('formats', []))
        name_prefix = f"{base_name} {quality_tag}".strip()

        # Step 2: actual download using the computed filename base
        opts = {
            'cookiefile': COOKIES_FILE,
            'format': quality,
            'outtmpl': f'{DOWNLOAD_DIR}/{name_prefix}.%(ext)s',
            'quiet': False,

            # ── Speed tuning ──
            # HLS/DASH streams (the "frag X/Y" downloads) are fetched ONE fragment at a
            # time by default — totally unrelated to server bandwidth. This downloads
            # multiple fragments in parallel, which is what actually saturates the link.
            'concurrent_fragment_downloads': CONCURRENT_FRAGMENTS,
            'http_chunk_size': 10 * 1024 * 1024,  # 10MB chunks for regular (non-fragmented) HTTP downloads
            'retries': 10,
            'fragment_retries': 10,
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = ydl.prepare_filename(info)

            # Reuse the scraped fallback title (if we already fetched one above)
            # so the result payload matches the filename, not the placeholder.
            if _is_placeholder_title(info.get('title')) and _is_placeholder_title(probe_info.get('title')) is False and probe_info.get('title'):
                info['title'] = probe_info['title']

            # FFmpeg merge ke baad .mp4 bhi check karo
            if not os.path.exists(filepath):
                mp4_path = os.path.splitext(filepath)[0] + '.mp4'
                if os.path.exists(mp4_path):
                    filepath = mp4_path

            if not os.path.exists(filepath):
                jobs[job_id] = {'status': 'error', 'error': 'File not found after download'}
                return

            # Actual file size ko filename mein add karo (download ke baad hi pata chalta hai)
            try:
                actual_size = os.path.getsize(filepath)
                size_str = _fmt_size_short(actual_size)
                if size_str:
                    stem, fext = os.path.splitext(filepath)
                    sized_filepath = f"{stem} {size_str}{fext}"
                    os.rename(filepath, sized_filepath)
                    filepath = sized_filepath
            except Exception:
                pass  # naming is cosmetic only — never fail the download over this

            # Token + link generate karo (metadata bhi saath store karo, /watch page ke liye)
            watch_metadata = {
                "title": info.get('title', ''),
                "series": info.get('series') or info.get('show'),
                "season_number": info.get('season_number'),
                "episode_number": info.get('episode_number'),
                "duration": info.get('duration', 0),
                "thumbnail": info.get('thumbnail') or probe_info.get('thumbnail') or '',
                "description": info.get('description') or probe_info.get('description') or '',
                "uploader": info.get('uploader', ''),
                "upload_date": info.get('upload_date') or probe_info.get('upload_date'),
                "source_url": url,
                "video_id": _video_id_for(url),
            }
            token, expires_at = generate_token(filepath, expires_in, metadata=watch_metadata)
            filename = os.path.basename(filepath)
            ext = filename.rsplit('.', 1)[-1].upper() if '.' in filename else 'MP4'

            # Quality/resolution detect
            height = info.get('height')
            quality_label = f"{height}p" if height else quality

            download_url = f"{BASE_URL}/files/{url_quote(filename)}?token={token}&expires={expires_at}"
            watch_url = f"{BASE_URL}/watch/{token}"

            jobs[job_id] = {
                'status': 'done',
                'result': {
                    "title": info.get('title', ''),
                    "author": info.get('uploader', ''),
                    "thumbnail": info.get('thumbnail', ''),
                    "duration": info.get('duration', 0),
                    "series": info.get('series') or info.get('show'),
                    "season_number": info.get('season_number'),
                    "episode_number": info.get('episode_number'),
                    "format": ext,
                    "quality": quality_label,
                    "url": download_url,
                    "watch_url": watch_url,
                },
            }

    except Exception as e:
        jobs[job_id] = {'status': 'error', 'error': str(e)}
    finally:
        _schedule_job_cleanup(job_id)


@app.route('/download', methods=['POST'])
def download():
    """
    Kicks off the download in a background thread and returns a job_id
    IMMEDIATELY (within milliseconds). This is the key fix for the 524
    Cloudflare timeout — the HTTP request/response cycle here is tiny,
    so there's nothing for the proxy to time out on.

    Poll /download/status?job_id=... to get the result once it's ready.
    """
    data = request.json or {}
    url = data.get('url')
    quality = data.get('quality', 'best')
    expires_in = int(data.get('expires_in', 3600))

    if not url:
        return jsonify({'error': 'URL required'}), 400

    job_id = uuid.uuid4().hex
    jobs[job_id] = {'status': 'pending', 'created_at': time.time()}

    t = threading.Thread(target=_run_download_job, args=(job_id, url, quality, expires_in), daemon=True)
    t.start()

    return jsonify({'status': True, 'job_id': job_id})


@app.route('/download/status', methods=['GET'])
def download_status():
    """Poll this with the job_id returned from /download."""
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({'error': 'job_id required'}), 400

    job = jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found (expired or invalid job_id)'}), 404

    return jsonify(job)


# ─── 4. File Serve (Token Check) ─────────────────
@app.route('/files/<path:filename>', methods=['GET'])
def serve_file(filename):
    token = request.args.get('token')
    # Default behaviour (download links, bot links) is unchanged: as_attachment=True.
    # The /watch player passes ?inline=1 so the <video> tag streams it instead
    # of the browser trying to download it.
    inline = request.args.get('inline') == '1'

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

    return send_file(
        filepath,
        as_attachment=not inline,
        download_name=os.path.basename(filepath),
        conditional=True,  # explicit Range-request support, needed for video seeking
    )


# ─── 4a. Comments (per-video, persisted to disk) ─────
@app.route('/comments', methods=['GET'])
def get_comments():
    video_id = request.args.get('video_id')
    if not video_id:
        return jsonify({'error': 'video_id required'}), 400
    items = comments_store.get(video_id, [])
    return jsonify({'comments': items, 'count': len(items)})


@app.route('/comments', methods=['POST'])
def post_comment():
    data = request.json or {}
    video_id = (data.get('video_id') or '').strip()
    text = (data.get('text') or '').strip()
    name = (data.get('name') or 'Anonymous').strip()[:40] or 'Anonymous'

    if not video_id or not text:
        return jsonify({'error': 'video_id and text required'}), 400
    text = text[:500]  # ek comment ki max length cap karo

    comment = {
        'id': str(uuid.uuid4()),
        'name': name,
        'text': text,
        'ts': int(time.time()),
    }

    with _comments_lock:
        comments_store.setdefault(video_id, [])
        comments_store[video_id].append(comment)
        comments_store[video_id] = comments_store[video_id][-200:]  # per-video cap
        _save_comments()

    return jsonify({'status': True, 'comment': comment, 'count': len(comments_store[video_id])})


# ─── 4b. Online Player (Watch Page) ──────────────
def _watch_expired_page():
    return '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Link Expired — OURIN</title>
<style>
body{background:#030508;color:#e8f4f0;font-family:'Outfit',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;text-align:center;margin:0;padding:20px}
.box{max-width:360px}
h1{color:#ff2255;font-size:22px;margin-bottom:10px}
p{color:#4a6860;font-size:14px;line-height:1.5}
</style></head>
<body><div class="box"><h1>&#9888; Link Expired</h1><p>This watch link has expired or the file was already removed from the server. Please fetch a fresh download link.</p></div></body></html>''', 410


def _fmt_duration_hms(seconds):
    if not seconds:
        return "0:00"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


@app.route('/watch/<token>', methods=['GET'])
def watch_page(token):
    entry = token_store.get(token)
    if not entry:
        return _watch_expired_page()

    if time.time() > entry["expires"]:
        token_store.pop(token, None)
        return _watch_expired_page()

    filepath = entry["filepath"]
    if not os.path.exists(filepath):
        return _watch_expired_page()

    meta = entry.get("metadata") or {}
    filename = os.path.basename(filepath)

    title = meta.get("title") or os.path.splitext(filename)[0]
    series = meta.get("series")
    season_number = meta.get("season_number")
    episode_number = meta.get("episode_number")
    duration = meta.get("duration") or 0
    description = meta.get("description") or ""
    uploader = meta.get("uploader") or ""
    thumbnail = meta.get("thumbnail") or ""

    # video_id: stable across re-downloads of the same source (so comments
    # persist). Older tokens issued before this field existed fall back to a
    # best-effort hash of title/series/episode.
    video_id = meta.get("video_id") or hashlib.sha256(
        f"{meta.get('source_url','')}|{title}|{series}|{season_number}|{episode_number}".encode("utf-8")
    ).hexdigest()[:16]

    video_src = f"/files/{url_quote(filename)}?token={token}&expires={entry['expires']}&inline=1"
    download_src = f"/files/{url_quote(filename)}?token={token}&expires={entry['expires']}"

    ext = filename.rsplit('.', 1)[-1].upper() if '.' in filename else 'MP4'

    # Episode/season tag line, only shown if metadata is present (movies won't have these)
    se_parts = []
    if season_number is not None:
        se_parts.append(f"S{season_number}")
    if episode_number is not None:
        se_parts.append(f"E{episode_number}")
    se_tag = "".join(se_parts)

    page_data = json.dumps({
        "videoSrc": video_src,
        "downloadSrc": download_src,
        "title": title,
        "qualityLabel": meta.get("quality_label") or "Auto",
        "ext": ext,
        "videoId": video_id,
    })

    # ── Pre-build optional HTML fragments as plain variables (NOT nested inside
    #    the big f-string's {} expressions). Python <3.12 disallows reusing the
    #    same quote character inside an f-string's expression portion, so doing
    #    this here keeps the page working on any Python 3 version on the server.
    se_tag_html = (" · " + html_escape(se_tag)) if se_tag else ""
    desc_html = f'<div class="d-desc">{html_escape(description)}</div>' if description else ""
    quality_label_html = html_escape(meta.get("quality_label") or "Auto")

    # ── Info box rows (only render rows that have data) ──
    def _irow(key, val, cls=""):
        if val is None or val == "" or val == "—":
            return ""
        cls_attr = f' class="info-val {cls}"' if cls else ' class="info-val"'
        return f'<div class="info-row"><span class="info-key">{key}</span><span{cls_attr}>{html_escape(str(val))}</span></div>'

    expires_fmt = ""
    try:
        exp_ts = entry["expires"]
        from datetime import datetime, timezone
        exp_dt = datetime.fromtimestamp(exp_ts)
        mins_left = max(0, int((exp_ts - time.time()) / 60))
        expires_fmt = f"{exp_dt.strftime('%I:%M:%S %p')} ({mins_left} min left)"
    except Exception:
        pass

    info_rows_html = "".join([
        _irow("title",   meta.get("title") or os.path.splitext(filename)[0], "green"),
        _irow("series",  meta.get("series"), "cyan"),
        _irow("season",  f"Season {season_number}" if season_number is not None else None, "cyan"),
        _irow("episode", f"Episode {episode_number}" if episode_number is not None else None, "cyan"),
        _irow("author",  uploader or "—", "dim"),
        _irow("duration", _fmt_duration_hms(duration) if duration else None, "cyan"),
        _irow("format",  f"{ext} · {quality_label_html}"),
        _irow("quality", quality_label_html),
        _irow("expires", expires_fmt, "dim"),
    ])

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>{html_escape(title)} — OURIN Player</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@700;800&family=Space+Mono:wght@400;700&family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root {{
  --bg: #030508; --green: #00ff88; --cyan: #00e5ff; --red: #ff2255;
  --text: #e8f4f0; --muted: #4a6860; --border: rgba(0,255,136,0.15);
}}
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
button {{
  background: none; border: none; padding: 0; margin: 0;
  cursor: pointer; -webkit-appearance: none; appearance: none;
  -webkit-tap-highlight-color: transparent; outline: none;
}}
html, body {{ background: var(--bg); color: var(--text); font-family: 'Outfit', sans-serif; overscroll-behavior: none; }}
a {{ color: inherit; text-decoration: none; }}

/* ── PLAYER (fixed at top) ── */
.player-shell {{
  position: sticky; top: 0; z-index: 50;
  width: 100%; aspect-ratio: 16/9; max-height: 56vh;
  background: #000; overflow: hidden;
  touch-action: none;
}}
video {{ width: 100%; height: 100%; object-fit: contain; background: #000; display: block; }}

.tap-zones {{ position: absolute; inset: 0; display: grid; grid-template-columns: 1fr 1fr 1fr; z-index: 5; }}
.tap-zone {{ }}

.controls-overlay {{
  position: absolute; inset: 0; z-index: 10;
  display: flex; flex-direction: column; justify-content: space-between;
  background: linear-gradient(to bottom, rgba(0,0,0,0.55) 0%, transparent 22%, transparent 65%, rgba(0,0,0,0.75) 100%);
  opacity: 1; transition: opacity 0.25s ease;
  pointer-events: none;
}}
.controls-overlay.hidden {{ opacity: 0; }}
.controls-overlay button,
.controls-overlay a,
.controls-overlay input,
.controls-overlay .seek-track,
.controls-overlay .dropdown-menu {{ pointer-events: auto; }}

.top-bar {{ display: flex; align-items: center; gap: 10px; padding: 10px 12px; }}
.back-btn {{ width: 34px; height: 34px; display: flex; align-items: center; justify-content: center; background: rgba(255,255,255,0.08); border-radius: 50%; flex-shrink: 0; }}
.top-title {{ font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }}

.center-controls {{ display: flex; align-items: center; justify-content: center; gap: 36px; }}
.center-btn {{ width: 46px; height: 46px; display: flex; align-items: center; justify-content: center; color: rgba(255,255,255,0.88); opacity: 0.95; filter: drop-shadow(0 1px 4px rgba(0,0,0,0.7)); }}
.center-btn svg {{ width: 28px; height: 28px; }}
.play-btn {{ width: 64px; height: 64px; background: rgba(0,255,136,0.15); border: 1.5px solid rgba(0,255,136,0.5); border-radius: 50%; }}
.play-btn svg {{ width: 30px; height: 30px; color: var(--green); }}

.bottom-bar {{ padding: 6px 12px 12px; }}
.seek-row {{ display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }}
.time-label {{ font-family: 'Space Mono', monospace; font-size: 10px; color: #fff; min-width: 38px; text-align: center; }}
.seek-track {{
  flex: 1; height: 4px; background: rgba(255,255,255,0.25); border-radius: 2px;
  position: relative; cursor: pointer;
}}
.seek-buffered {{ position: absolute; left: 0; top: 0; bottom: 0; background: rgba(255,255,255,0.35); border-radius: 2px; width: 0%; }}
.seek-progress {{ position: absolute; left: 0; top: 0; bottom: 0; background: var(--green); border-radius: 2px; width: 0%; }}
.seek-handle {{ position: absolute; top: 50%; width: 11px; height: 11px; background: var(--green); border-radius: 50%; transform: translate(-50%,-50%); left: 0%; box-shadow: 0 0 8px rgba(0,255,136,0.7); }}

.bottom-icons {{ display: flex; align-items: center; gap: 16px; }}
.icon-btn {{ width: 26px; height: 26px; display: flex; align-items: center; justify-content: center; color: rgba(255,255,255,0.85); filter: drop-shadow(0 1px 3px rgba(0,0,0,0.7)); }}
.icon-btn svg {{ width: 19px; height: 19px; }}
.spacer {{ flex: 1; }}
.vol-wrap {{ display: flex; align-items: center; gap: 6px; }}
.vol-slider {{ width: 56px; height: 3px; -webkit-appearance: none; background: rgba(255,255,255,0.3); border-radius: 2px; outline: none; }}
.vol-slider::-webkit-slider-thumb {{ -webkit-appearance: none; width: 10px; height: 10px; border-radius: 50%; background: var(--green); }}

.pill-btn {{
  font-family: 'Space Mono', monospace; font-size: 10px; color: #fff;
  background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2);
  border-radius: 20px; padding: 4px 10px; white-space: nowrap;
}}

.dropdown-menu {{
  position: absolute; bottom: 38px; right: 0;
  background: rgba(8,14,12,0.97); border: 1px solid var(--border);
  border-radius: 8px; padding: 6px; min-width: 110px; display: none; z-index: 20;
}}
.dropdown-menu.show {{ display: block; }}
.dropdown-item {{
  font-family: 'Space Mono', monospace; font-size: 11px; color: var(--text);
  padding: 7px 10px; border-radius: 5px; cursor: pointer;
}}
.dropdown-item.active {{ color: var(--green); background: rgba(0,255,136,0.08); }}
.menu-anchor {{ position: relative; }}

.skeleton-spinner {{
  position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
  width: 36px; height: 36px; border: 3px solid rgba(0,255,136,0.2); border-top-color: var(--green);
  border-radius: 50%; animation: spin 0.8s linear infinite; z-index: 15; display: none;
}}
.skeleton-spinner.show {{ display: block; }}
@keyframes spin {{ to {{ transform: translate(-50%,-50%) rotate(360deg); }} }}

/* ── DETAILS SECTION (below player) ── */
.details-wrap {{ padding: 16px 16px 48px; max-width: 760px; margin: 0 auto; }}
.d-title {{ font-family: 'Syne', sans-serif; font-weight: 800; font-size: 20px; line-height: 1.25; margin-bottom: 6px; }}
.d-desc {{ font-size: 13.5px; line-height: 1.65; color: rgba(232,244,240,0.72); margin-bottom: 16px; }}
.d-meta {{ font-family: 'Space Mono', monospace; font-size: 11px; color: var(--muted); margin-top: 14px; }}

/* ── Action row ── */
.action-row {{ display: flex; align-items: flex-start; gap: 22px; margin-bottom: 20px; }}
.action-item {{ display: flex; flex-direction: column; align-items: center; gap: 6px; }}
.action-btn {{
  width: 48px; height: 48px; border-radius: 50%;
  background: rgba(255,34,85,0.10); border: 1.5px solid rgba(255,34,85,0.45);
  display: flex; align-items: center; justify-content: center;
  color: var(--red); flex-shrink: 0; transition: transform 0.15s ease, background 0.15s ease;
}}
.action-btn:active {{ transform: scale(0.88); background: rgba(255,34,85,0.22); }}
.action-btn svg {{ width: 21px; height: 21px; }}
.action-label {{ font-family: 'Space Mono', monospace; font-size: 9px; letter-spacing: 0.5px; color: var(--muted); text-transform: uppercase; }}

/* ── Info box (like image 2) ── */
.info-box {{
  background: rgba(0,255,136,0.03);
  border: 1px solid var(--border);
  border-radius: 10px;
  overflow: hidden;
  margin-bottom: 20px;
}}
.info-row {{
  display: flex; align-items: baseline;
  padding: 9px 14px;
  border-bottom: 1px solid rgba(0,255,136,0.07);
  gap: 12px;
}}
.info-row:last-child {{ border-bottom: none; }}
.info-key {{
  font-family: 'Space Mono', monospace; font-size: 11px;
  color: var(--muted); min-width: 80px; flex-shrink: 0;
}}
.info-val {{
  font-family: 'Space Mono', monospace; font-size: 12px;
  color: var(--cyan); word-break: break-word; flex: 1;
}}
.info-val.green {{ color: var(--green); }}
.info-val.dim {{ color: rgba(232,244,240,0.55); }}

.section-divider {{ border: none; height: 1px; margin: 20px 0; background: linear-gradient(90deg, transparent, rgba(0,255,136,0.25), transparent); }}

/* ── Comment bottom-sheet ── */
.comment-overlay {{
  position: fixed; inset: 0; z-index: 99; background: rgba(0,0,0,0.6);
  opacity: 0; pointer-events: none; transition: opacity 0.25s ease;
}}
.comment-overlay.show {{ opacity: 1; pointer-events: auto; }}
.comment-sheet {{
  position: fixed; left: 0; right: 0; bottom: 0; z-index: 100;
  background: #0a0f0d; border-top: 1px solid var(--border);
  border-radius: 16px 16px 0 0; max-height: 72vh;
  display: flex; flex-direction: column;
  transform: translateY(100%); transition: transform 0.3s ease;
}}
.comment-sheet.show {{ transform: translateY(0); }}
.comment-sheet-header {{
  display: flex; align-items: center; justify-content: space-between;
  padding: 14px 16px; border-bottom: 1px solid var(--border); flex-shrink: 0;
}}
.comment-sheet-title {{ font-family: 'Syne', sans-serif; font-weight: 700; font-size: 14px; }}
.comment-close {{ width: 28px; height: 28px; display: flex; align-items: center; justify-content: center; color: var(--muted); border-radius: 50%; background: rgba(255,255,255,0.06); }}
.comment-list {{ flex: 1; overflow-y: auto; padding: 10px 16px; }}
.comment-item {{ padding: 10px 0; border-bottom: 1px solid rgba(255,255,255,0.06); }}
.comment-name {{ font-size: 12px; font-weight: 600; color: var(--green); }}
.comment-text {{ font-size: 13px; color: var(--text); margin-top: 3px; line-height: 1.5; word-break: break-word; }}
.comment-time {{ font-family: 'Space Mono', monospace; font-size: 9px; color: var(--muted); margin-top: 5px; }}
.comment-empty {{ text-align: center; color: var(--muted); font-size: 12px; padding: 34px 0; }}
.comment-input-row {{ display: flex; gap: 8px; padding: 12px 16px; border-top: 1px solid var(--border); flex-shrink: 0; }}
.comment-input {{
  flex: 1; background: rgba(255,255,255,0.06); border: 1px solid rgba(255,255,255,0.12);
  border-radius: 20px; padding: 10px 14px; color: var(--text); font-size: 13px; font-family: 'Outfit', sans-serif;
}}
.comment-input:focus {{ outline: none; border-color: rgba(255,34,85,0.4); }}
.comment-send {{
  width: 38px; height: 38px; border-radius: 50%; background: var(--red);
  display: flex; align-items: center; justify-content: center; flex-shrink: 0; color: #04140f;
}}
.comment-send svg {{ width: 17px; height: 17px; }}
</style>
</head>
<body>

<div class="player-shell" id="playerShell">
  <video id="video" src="{video_src}" playsinline webkit-playsinline preload="metadata"></video>
  <div class="skeleton-spinner" id="spinner"></div>

  <div class="tap-zones">
    <div class="tap-zone" id="tapLeft"></div>
    <div class="tap-zone" id="tapCenter"></div>
    <div class="tap-zone" id="tapRight"></div>
  </div>

  <div class="controls-overlay" id="overlay">
    <div class="top-bar">
      <a class="back-btn" href="javascript:history.back()">
        <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.2"><path d="M15 18l-6-6 6-6"/></svg>
      </a>
      <div class="top-title">{html_escape(title)}{se_tag_html}</div>
    </div>

    <div class="center-controls">
      <button class="center-btn" id="btnBack10">
        <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><path d="M3 12a9 9 0 1 0 2.6-6.4"/><path d="M3 4v5h5"/></svg>
      </button>
      <button class="center-btn play-btn" id="btnPlayCenter">
        <svg id="iconPlayCenter" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>
      </button>
      <button class="center-btn" id="btnFwd10">
        <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/></svg>
      </button>
    </div>

    <div class="bottom-bar">
      <div class="seek-row">
        <span class="time-label" id="curTime">0:00</span>
        <div class="seek-track" id="seekTrack">
          <div class="seek-buffered" id="seekBuffered"></div>
          <div class="seek-progress" id="seekProgress"></div>
          <div class="seek-handle" id="seekHandle"></div>
        </div>
        <span class="time-label" id="durTime">0:00</span>
      </div>
      <div class="bottom-icons">
        <button class="icon-btn" id="btnPlaySmall">
          <svg id="iconPlaySmall" viewBox="0 0 24 24" fill="#fff"><path d="M8 5v14l11-7z"/></svg>
        </button>
        <div class="vol-wrap">
          <button class="icon-btn" id="btnMute">
            <svg id="iconVol" viewBox="0 0 24 24" fill="#fff"><path d="M3 10v4h4l5 5V5L7 10H3z"/></svg>
          </button>
          <input type="range" class="vol-slider" id="volSlider" min="0" max="1" step="0.05" value="1">
        </div>
        <div class="spacer"></div>
        <div class="menu-anchor">
          <button class="pill-btn" id="btnSpeed">1x</button>
          <div class="dropdown-menu" id="speedMenu">
            <div class="dropdown-item" data-speed="0.5">0.5x</div>
            <div class="dropdown-item" data-speed="0.75">0.75x</div>
            <div class="dropdown-item active" data-speed="1">1x (Normal)</div>
            <div class="dropdown-item" data-speed="1.25">1.25x</div>
            <div class="dropdown-item" data-speed="1.5">1.5x</div>
            <div class="dropdown-item" data-speed="2">2x</div>
          </div>
        </div>
        <div class="menu-anchor">
          <button class="pill-btn" id="btnQuality">{quality_label_html}</button>
          <div class="dropdown-menu" id="qualityMenu">
            <div class="dropdown-item active">{quality_label_html} (Source)</div>
          </div>
        </div>
        <button class="icon-btn" id="btnFullscreen">
          <svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3M3 16v3a2 2 0 0 0 2 2h3m11 0h3a2 2 0 0 0 2-2v-3"/></svg>
        </button>
      </div>
    </div>
  </div>
</div>

<div class="details-wrap">
  <!-- Title -->
  <div class="d-title">{html_escape(title)}</div>

  <!-- Description -->
  {desc_html}

  <!-- Action buttons: Like / Download / Share / Comment -->
  <div class="action-row">
    <div class="action-item">
      <button class="action-btn" id="btnLike" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M14 9V5a3 3 0 0 0-3-3l-4 9v11h11.28a2 2 0 0 0 2-1.7l1.38-9a2 2 0 0 0-2-2.3H14z"/><path d="M7 22H4a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h3"/></svg>
      </button>
      <span class="action-label" id="likeLabel">Like</span>
    </div>
    <div class="action-item">
      <a class="action-btn" href="{download_src}" download>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"><path d="M12 3v12m0 0l-4-4m4 4l4-4M5 21h14"/></svg>
      </a>
      <span class="action-label">Download</span>
    </div>
    <div class="action-item">
      <button class="action-btn" id="btnShare" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="M8.6 13.5l6.8 3.9M15.4 6.6L8.6 10.5"/></svg>
      </button>
      <span class="action-label">Share</span>
    </div>
    <div class="action-item">
      <button class="action-btn" id="btnComment" type="button">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
      </button>
      <span class="action-label" id="commentLabel">Comment</span>
    </div>
  </div>

  <hr class="section-divider">

  <!-- Info box (Space Mono table style) -->
  <div class="info-box">
    {info_rows_html}
  </div>

  <hr class="section-divider">
</div>

<div class="comment-overlay" id="commentOverlay"></div>
<div class="comment-sheet" id="commentSheet">
  <div class="comment-sheet-header">
    <div class="comment-sheet-title">Comments</div>
    <button class="comment-close" id="btnCloseComments" type="button">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
    </button>
  </div>
  <div class="comment-list" id="commentList"></div>
  <div class="comment-input-row">
    <input class="comment-input" id="commentInput" type="text" placeholder="Add a comment..." maxlength="500">
    <button class="comment-send" id="btnSendComment" type="button">
      <svg viewBox="0 0 24 24" fill="currentColor"><path d="M2 21l21-9L2 3v7l15 2-15 2v7z"/></svg>
    </button>
  </div>
</div>

<script>
const PAGE_DATA = {page_data}
const video       = document.getElementById('video')
const overlay     = document.getElementById('overlay')
const spinner     = document.getElementById('spinner')
const seekTrack    = document.getElementById('seekTrack')
const seekProgress = document.getElementById('seekProgress')
const seekBuffered = document.getElementById('seekBuffered')
const seekHandle   = document.getElementById('seekHandle')
const curTimeEl   = document.getElementById('curTime')
const durTimeEl   = document.getElementById('durTime')
const volSlider   = document.getElementById('volSlider')

let isScrubbing = false
let overlayHideTimer = null
let lastTapTime = 0

// ── Play/Pause ──
function setPlayIcons(playing) {{
  document.getElementById('iconPlayCenter').innerHTML = playing
    ? '<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>'
    : '<path d="M8 5v14l11-7z"/>'
  document.getElementById('iconPlaySmall').innerHTML = playing
    ? '<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>'
    : '<path d="M8 5v14l11-7z"/>'
}}
function togglePlay() {{
  if (video.paused) {{ video.play(); }} else {{ video.pause(); }}
}}
video.addEventListener('play',  () => {{ setPlayIcons(true);  scheduleHideOverlay() }})
video.addEventListener('pause', () => {{ setPlayIcons(false); showOverlay() }})
document.getElementById('btnPlayCenter').addEventListener('click', togglePlay)
document.getElementById('btnPlaySmall').addEventListener('click', togglePlay)

// ── Skip ±10s ──
document.getElementById('btnBack10').addEventListener('click', () => {{ video.currentTime = Math.max(0, video.currentTime - 10) }})
document.getElementById('btnFwd10').addEventListener('click',  () => {{ video.currentTime = Math.min(video.duration || 1e9, video.currentTime + 10) }})

// ── Double-tap left/right to skip, single tap to show/hide controls ──
function handleTap(zone) {{
  const now = Date.now()
  if (zone !== 'center' && now - lastTapTime < 300) {{
    if (zone === 'left')  video.currentTime = Math.max(0, video.currentTime - 10)
    if (zone === 'right') video.currentTime = Math.min(video.duration || 1e9, video.currentTime + 10)
    lastTapTime = 0
    return
  }}
  lastTapTime = now

  // Controls hidden -> any tap anywhere brings them back.
  if (overlay.classList.contains('hidden')) {{
    showOverlay()
    return
  }}

  // Controls visible -> center keeps its play/pause-on-tap behaviour,
  // but tapping the blank/black space on either side hides the overlay right away.
  if (zone === 'center') {{
    togglePlay()
    scheduleHideOverlay()
  }} else {{
    hideOverlayNow()
  }}
}}
document.getElementById('tapLeft').addEventListener('click', () => handleTap('left'))
document.getElementById('tapRight').addEventListener('click', () => handleTap('right'))
document.getElementById('tapCenter').addEventListener('click', () => handleTap('center'))

function showOverlay() {{ overlay.classList.remove('hidden'); scheduleHideOverlay() }}
function hideOverlaySoon() {{ scheduleHideOverlay() }}
function hideOverlayNow() {{ clearTimeout(overlayHideTimer); overlay.classList.add('hidden') }}
function scheduleHideOverlay() {{
  clearTimeout(overlayHideTimer)
  if (video.paused) return
  overlayHideTimer = setTimeout(() => overlay.classList.add('hidden'), 3000)
}}

// ── Seek bar ──
function fmtT(s) {{
  if (!isFinite(s) || s < 0) s = 0
  s = Math.floor(s)
  const m = Math.floor(s / 60), sec = s % 60
  const h = Math.floor(m / 60)
  if (h > 0) return `${{h}}:${{String(m % 60).padStart(2,'0')}}:${{String(sec).padStart(2,'0')}}`
  return `${{m}}:${{String(sec).padStart(2,'0')}}`
}}
function updateSeekUI() {{
  if (!video.duration) return
  const pct = (video.currentTime / video.duration) * 100
  seekProgress.style.width = pct + '%'
  seekHandle.style.left = pct + '%'
  curTimeEl.textContent = fmtT(video.currentTime)
  durTimeEl.textContent = fmtT(video.duration)
  if (video.buffered.length) {{
    const bufEnd = video.buffered.end(video.buffered.length - 1)
    seekBuffered.style.width = (bufEnd / video.duration * 100) + '%'
  }}
}}
video.addEventListener('timeupdate', () => {{ if (!isScrubbing) updateSeekUI() }})
video.addEventListener('loadedmetadata', updateSeekUI)
video.addEventListener('progress', updateSeekUI)

function seekFromEvent(e) {{
  const rect = seekTrack.getBoundingClientRect()
  const clientX = e.touches ? e.touches[0].clientX : e.clientX
  let pct = (clientX - rect.left) / rect.width
  pct = Math.max(0, Math.min(1, pct))
  if (video.duration) video.currentTime = pct * video.duration
  seekProgress.style.width = (pct*100) + '%'
  seekHandle.style.left = (pct*100) + '%'
  curTimeEl.textContent = fmtT(pct * (video.duration || 0))
}}
seekTrack.addEventListener('mousedown', e => {{ isScrubbing = true; seekFromEvent(e) }})
seekTrack.addEventListener('touchstart', e => {{ isScrubbing = true; seekFromEvent(e) }}, {{passive:true}})
window.addEventListener('mousemove', e => {{ if (isScrubbing) seekFromEvent(e) }})
window.addEventListener('touchmove', e => {{ if (isScrubbing) seekFromEvent(e) }}, {{passive:true}})
window.addEventListener('mouseup', () => {{ isScrubbing = false }})
window.addEventListener('touchend', () => {{ isScrubbing = false }})

// ── Buffering spinner ──
video.addEventListener('waiting', () => spinner.classList.add('show'))
video.addEventListener('playing', () => spinner.classList.remove('show'))
video.addEventListener('canplay', () => spinner.classList.remove('show'))

// ── Volume ──
volSlider.addEventListener('input', () => {{
  video.volume = volSlider.value
  video.muted = parseFloat(volSlider.value) === 0
  updateVolIcon()
}})
document.getElementById('btnMute').addEventListener('click', () => {{
  video.muted = !video.muted
  if (!video.muted && video.volume === 0) {{ video.volume = 1; volSlider.value = 1 }}
  updateVolIcon()
}})
function updateVolIcon() {{
  const icon = document.getElementById('iconVol')
  if (video.muted || video.volume === 0) {{
    icon.innerHTML = '<path d="M16.5 12L19 9.5M19 9.5L21.5 7M19 9.5L16.5 7M19 9.5L21.5 12"/><path d="M3 10v4h4l5 5V5L7 10H3z" fill="#fff" stroke="none"/>'
  }} else {{
    icon.innerHTML = '<path d="M3 10v4h4l5 5V5L7 10H3z"/>'
  }}
}}

// ── Speed dropdown ──
const speedMenu = document.getElementById('speedMenu')
document.getElementById('btnSpeed').addEventListener('click', e => {{
  e.stopPropagation()
  speedMenu.classList.toggle('show')
  document.getElementById('qualityMenu').classList.remove('show')
}})
speedMenu.querySelectorAll('.dropdown-item').forEach(item => {{
  item.addEventListener('click', () => {{
    const speed = parseFloat(item.dataset.speed)
    video.playbackRate = speed
    document.getElementById('btnSpeed').textContent = speed + 'x'
    speedMenu.querySelectorAll('.dropdown-item').forEach(i => i.classList.remove('active'))
    item.classList.add('active')
    speedMenu.classList.remove('show')
  }})
}})

// ── Quality dropdown (single source for now — structured for future multi-quality) ──
const qualityMenu = document.getElementById('qualityMenu')
document.getElementById('btnQuality').addEventListener('click', e => {{
  e.stopPropagation()
  qualityMenu.classList.toggle('show')
  speedMenu.classList.remove('show')
}})
document.addEventListener('click', () => {{ speedMenu.classList.remove('show'); qualityMenu.classList.remove('show') }})

// ── Fullscreen ──
document.getElementById('btnFullscreen').addEventListener('click', () => {{
  const shell = document.getElementById('playerShell')
  if (!document.fullscreenElement) {{
    (shell.requestFullscreen || shell.webkitRequestFullscreen || function(){{}}).call(shell)
  }} else {{
    (document.exitFullscreen || document.webkitExitFullscreen || function(){{}}).call(document)
  }}
}})

// ── Keyboard shortcuts (desktop) ──
document.addEventListener('keydown', e => {{
  if (e.code === 'Space') {{ e.preventDefault(); togglePlay() }}
  if (e.code === 'ArrowLeft')  video.currentTime = Math.max(0, video.currentTime - 10)
  if (e.code === 'ArrowRight') video.currentTime = Math.min(video.duration || 1e9, video.currentTime + 10)
  if (e.code === 'ArrowUp')   {{ video.volume = Math.min(1, video.volume + 0.1); volSlider.value = video.volume }}
  if (e.code === 'ArrowDown') {{ video.volume = Math.max(0, video.volume - 0.1); volSlider.value = video.volume }}
}})

// ── Share ──
document.getElementById('btnShare').addEventListener('click', async () => {{
  const shareData = {{ title: PAGE_DATA.title, url: window.location.href }}
  if (navigator.share) {{
    try {{ await navigator.share(shareData) }} catch (e) {{}}
  }} else {{
    try {{
      await navigator.clipboard.writeText(window.location.href)
      const lbl = document.querySelector('#btnShare').nextElementSibling
      const old = lbl.textContent
      lbl.textContent = 'Copied!'
      setTimeout(() => {{ lbl.textContent = old }}, 1500)
    }} catch (e) {{}}
  }}
}})

// ── Like ──
;(function() {{
  const btn      = document.getElementById('btnLike')
  const lbl      = document.getElementById('likeLabel')
  const LIKE_KEY = 'ourin_like_' + PAGE_DATA.videoId
  let liked = false
  try {{ liked = localStorage.getItem(LIKE_KEY) === '1' }} catch(e) {{}}

  function _applyLike(state) {{
    liked = state
    if (liked) {{
      btn.style.background = 'rgba(0,255,136,0.15)'
      btn.style.borderColor = 'rgba(0,255,136,0.6)'
      btn.style.color = 'var(--green)'
      btn.querySelector('svg').setAttribute('fill', 'var(--green)')
      lbl.textContent = 'Liked'
    }} else {{
      btn.style.background = ''
      btn.style.borderColor = ''
      btn.style.color = ''
      btn.querySelector('svg').setAttribute('fill', 'none')
      lbl.textContent = 'Like'
    }}
    try {{ localStorage.setItem(LIKE_KEY, liked ? '1' : '0') }} catch(e) {{}}
  }}

  _applyLike(liked)  // restore state on page load
  btn.addEventListener('click', () => _applyLike(!liked))
}})()

// ── Comments ──
const commentOverlay = document.getElementById('commentOverlay')
const commentSheet   = document.getElementById('commentSheet')
const commentList    = document.getElementById('commentList')
const commentInput   = document.getElementById('commentInput')
const commentLabel   = document.getElementById('commentLabel')

function escapeHtml(str) {{
  const d = document.createElement('div')
  d.textContent = str == null ? '' : String(str)
  return d.innerHTML
}}

function fmtCommentTime(ts) {{
  try {{ return new Date(ts * 1000).toLocaleString() }} catch (e) {{ return '' }}
}}

function renderComments(list) {{
  if (!list || !list.length) {{
    commentList.innerHTML = '<div class="comment-empty">No comments yet — be the first!</div>'
    return
  }}
  commentList.innerHTML = list.map(c => `
    <div class="comment-item">
      <div class="comment-name">${{escapeHtml(c.name || 'Anonymous')}}</div>
      <div class="comment-text">${{escapeHtml(c.text)}}</div>
      <div class="comment-time">${{fmtCommentTime(c.ts)}}</div>
    </div>
  `).join('')
  commentList.scrollTop = commentList.scrollHeight
}}

async function loadComments() {{
  commentList.innerHTML = '<div class="comment-empty">Loading...</div>'
  try {{
    const res = await fetch('/comments?video_id=' + encodeURIComponent(PAGE_DATA.videoId))
    const data = await res.json()
    renderComments(data.comments)
    if (typeof data.count === 'number') commentLabel.textContent = data.count ? `Comment (${{data.count}})` : 'Comment'
  }} catch (e) {{
    commentList.innerHTML = '<div class="comment-empty">Failed to load comments</div>'
  }}
}}

function openComments() {{
  commentOverlay.classList.add('show')
  commentSheet.classList.add('show')
  loadComments()
}}
function closeComments() {{
  commentOverlay.classList.remove('show')
  commentSheet.classList.remove('show')
}}

document.getElementById('btnComment').addEventListener('click', openComments)
document.getElementById('btnCloseComments').addEventListener('click', closeComments)
commentOverlay.addEventListener('click', closeComments)

async function postComment() {{
  const text = commentInput.value.trim()
  if (!text) return
  commentInput.value = ''
  try {{
    await fetch('/comments', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ video_id: PAGE_DATA.videoId, text }})
    }})
    loadComments()
  }} catch (e) {{}}
}}
document.getElementById('btnSendComment').addEventListener('click', postComment)
commentInput.addEventListener('keydown', e => {{ if (e.key === 'Enter') postComment() }})

// Initial comment count badge (without opening the sheet)
fetch('/comments?video_id=' + encodeURIComponent(PAGE_DATA.videoId))
  .then(r => r.json())
  .then(d => {{ if (d.count) commentLabel.textContent = `Comment (${{d.count}})` }})
  .catch(() => {{}})

showOverlay()
</script>
</body>
</html>'''




# ─── 5. Progress Check (legacy — kept for compatibility) ─────────
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
  position: fixed;
  top: 0;
  left: 0;
  right: 0;
  z-index: 100;
  padding: 18px 40px;
  display: flex;
  align-items: center;
  justify-content: space-between;

  background: rgba(3,5,8,0.92);
  backdrop-filter: blur(10px);

  border-bottom: 1px solid rgba(0,255,136,0.25);
}
.nav-badge {
  font-family: 'Space Mono', monospace;
  font-size: 8px;
  letter-spacing: 1px;
  text-transform: uppercase;

  color: var(--green);
  padding: 8px 14px;

  background: rgba(0,255,136,0.05);
  border: 1px solid rgba(0,255,136,0.25);

  backdrop-filter: blur(8px);

  box-shadow:
    0 0 12px rgba(0,255,136,0.08),
    inset 0 0 10px rgba(0,255,136,0.03);

  clip-path: polygon(
    6px 0,
    100% 0,
    100% calc(100% - 6px),
    calc(100% - 6px) 100%,
    0 100%,
    0 6px
  );

  position: relative;
}

.nav-badge::before{
  content:'';
  position:absolute;
  top:-1px;
  left:0;
  width:100%;
  height:1px;
  background:linear-gradient(
    90deg,
    transparent,
    var(--green),
    transparent
  );
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
  font-size: 10px; letter-spacing: 0.5px; text-transform: uppercase;
  padding: 6px 8px; cursor: none;
  flex: 1; text-align: center;
  background: rgba(0,255,136,0.04);
  border: 1px solid rgba(0,255,136,0.1);
  color: var(--muted);
  transition: all 0.2s;
  white-space: nowrap;
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
  align-items: center; gap: 5px;
  margin-top: 6px;
  font-family: 'Space Mono', monospace;
  font-size: 8px; color: var(--muted); letter-spacing: 0.5px;
}
.selected-badge.show { display: flex; }
.sb-val {
  color: var(--green);
  background: rgba(0,255,136,0.08);
  border: 1px solid rgba(0,255,136,0.2);
  padding: 2px 8px;
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
.btn-watch-link {
  display: block; width: 100%; margin-top: 14px;
  background: linear-gradient(90deg, var(--cyan), var(--blue));
  color: #04141f;
  font-family: 'Space Mono', monospace;
  font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase;
  padding: 10px; text-align: center;
  text-decoration: none;
  transition: all 0.2s;
  clip-path: polygon(6px 0%, 100% 0%, calc(100% - 6px) 100%, 0% 100%);
}
.btn-watch-link:hover {
  filter: brightness(1.1);
  box-shadow: 0 0 20px rgba(0,229,255,0.3);
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
    <span class="nav-badge">DOWNLOADER</span>
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
        <button class="ftab active" id="tabCombined" onclick="switchTab('combined')">Video+Audio</button>
        <button class="ftab" id="tabVideo" onclick="switchTab('video')">&#9654; Video</button>
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
        <div class="result-row" id="resSeriesRow" style="display:none"><span class="result-key">series</span><span class="result-val green" id="resSeries">&#8212;</span></div>
        <div class="result-row" id="resSeasonRow" style="display:none"><span class="result-key">season</span><span class="result-val cyan" id="resSeason">&#8212;</span></div>
        <div class="result-row" id="resEpisodeRow" style="display:none"><span class="result-key">episode</span><span class="result-val cyan" id="resEpisode">&#8212;</span></div>
        <div class="result-row"><span class="result-key">author</span><span class="result-val green" id="resAuthor">&#8212;</span></div>
        <div class="result-row"><span class="result-key">duration</span><span class="result-val cyan" id="resDuration">&#8212;</span></div>
        <div class="result-row"><span class="result-key">format</span><span class="result-val cyan" id="resFormat">&#8212;</span></div>
        <div class="result-row"><span class="result-key">quality</span><span class="result-val cyan" id="resQuality">&#8212;</span></div>
        <div class="result-row"><span class="result-key">expires</span><span class="result-val" id="resExpires">&#8212;</span></div>
        <a class="btn-watch-link" id="resWatchLink" href="#" target="_blank" style="display:none">&#9654; &nbsp;PLAY ONLINE</a>
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
let currentTab       = 'combined'
let combinedFormats  = []
let videoFormats     = []
let audioFormats     = []
let selectedCombined = null
let selectedVideo    = null
let selectedAudio    = null
let currentUrl       = ''

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
  if (!bytes) return '—'
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
  selectedCombined = null; selectedVideo = null; selectedAudio = null
  document.getElementById('selectedBadge').classList.remove('show')

  setStatus('Fetching formats...', 'spin')

  try {
    const infoRes = await fetch('/info?url=' + encodeURIComponent(url))
    const info    = await infoRes.json()
    if (!infoRes.ok) throw new Error(info.error || 'Info fetch failed')

    const vi = document.getElementById('videoInfo')
    document.getElementById('viThumb').src = info.thumbnail || ''
    document.getElementById('viThumb').style.display = info.thumbnail ? 'block' : 'none'

    // Title: prefer "Series S#E# — Episode Name" if series metadata exists
    const epTag = [
      info.season_number ? 'S' + info.season_number : '',
      info.episode_number ? 'E' + info.episode_number : ''
    ].filter(Boolean).join('')
    const displayTitle = info.series_name
      ? `${info.series_name}${epTag ? ' ' + epTag : ''} — ${info.episode_name || info.title}`
      : (info.title || 'Unknown Title')
    document.getElementById('viTitle').textContent = displayTitle

    document.getElementById('viDetail').textContent =
      [info.uploader, fmtDuration(info.duration), info.upload_date, info.formats_count + ' formats'].filter(Boolean).join('  ·  ')
    vi.classList.add('show')

    const fmtRes  = await fetch('/formats?url=' + encodeURIComponent(url))
    const fmtData = await fmtRes.json()
    if (!fmtRes.ok) throw new Error(fmtData.error || 'Formats fetch failed')

    const hasVideo = f => f.vcodec && f.vcodec !== 'none'
    const hasAudio = f => f.acodec && f.acodec !== 'none'

    const realCombined = fmtData.formats.filter(f => hasVideo(f) && hasAudio(f))
    videoFormats        = fmtData.formats.filter(f => hasVideo(f) && !hasAudio(f))
    audioFormats        = fmtData.formats.filter(f => !hasVideo(f) && hasAudio(f))

    if (realCombined.length) {
      // Some sources (rare) provide actual single-file muxed streams — use them as-is
      combinedFormats = realCombined
    } else if (videoFormats.length && audioFormats.length) {
      // Hotstar-style DASH: video & audio always come as separate streams.
      // Pair every video resolution with the single best audio track so the
      // user can pick "320x180+audio" and get a ready-to-merge download.
      const bestAudio = [...audioFormats].sort((a, b) => (b.filesize || 0) - (a.filesize || 0))[0]
      combinedFormats = videoFormats.map(v => ({
        format_id: v.format_id + '+' + bestAudio.format_id,
        resolution: v.resolution,
        ext: v.ext,
        vcodec: v.vcodec,
        acodec: bestAudio.acodec,
        filesize: (v.filesize || 0) + (bestAudio.filesize || 0),
        synthetic: true,
      }))
    } else {
      combinedFormats = []
    }

    document.getElementById('formatBoxWrap').classList.add('show')
    // Prefer combined tab (no manual video+audio merging needed); fall back if none available
    switchTab(combinedFormats.length ? 'combined' : 'video')

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
    scroll.innerHTML = `<div class="format-empty">No ${type === 'combined' ? 'video+audio' : type} formats available</div>`
    return
  }
  scroll.innerHTML = list.map((f, i) => {
    const isSelected = type === 'combined' ? selectedCombined === f.format_id
                      : type === 'video'    ? selectedVideo === f.format_id
                      :                        selectedAudio === f.format_id
    return `
    <div class="format-item${isSelected ? ' selected' : ''}"
         onclick="selectFormat('${f.format_id}', '${type}', ${i})"
         data-idx="${i}" data-type="${type}">
      <div class="fi-label">
        <div class="fi-res">${f.synthetic ? (f.resolution || 'audio') + '+audio' : (f.resolution || f.format_id)}</div>
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
  document.getElementById('tabCombined').classList.toggle('active', tab === 'combined')
  document.getElementById('tabVideo').classList.toggle('active', tab === 'video')
  document.getElementById('tabAudio').classList.toggle('active', tab === 'audio')
  if      (tab === 'combined') renderFormats(combinedFormats, 'combined')
  else if (tab === 'video')    renderFormats(videoFormats, 'video')
  else                         renderFormats(audioFormats, 'audio')
}

// ── SELECT FORMAT ──
function selectFormat(id, type, idx) {
  if (type === 'combined') {
    // Picking a combined format means no separate video/audio merge is needed
    selectedCombined = selectedCombined === id ? null : id
    selectedVideo = null
    selectedAudio = null
  } else if (type === 'video') {
    selectedVideo = selectedVideo === id ? null : id
    selectedCombined = null  // switching to manual mode clears the combined pick
  } else {
    selectedAudio = selectedAudio === id ? null : id
    selectedCombined = null
  }

  const list = type === 'combined' ? combinedFormats : type === 'video' ? videoFormats : audioFormats
  renderFormats(list, type)

  const badge = document.getElementById('selectedBadge')
  const sbVal  = document.getElementById('sbVal')
  if (selectedCombined || selectedVideo || selectedAudio) {
    badge.classList.add('show')
    const parts = []
    if (selectedCombined) {
      const cf = combinedFormats.find(f => f.format_id === selectedCombined)
      parts.push(`${cf?.resolution || selectedCombined}+audio`)
    }
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
  document.getElementById('dlBtn').disabled = !(selectedCombined || selectedVideo || selectedAudio)
}

// ── DOWNLOAD (now polls a job instead of waiting on one long request) ──
let pollTimer = null

async function downloadVideo() {
  const url = document.getElementById('urlInput').value.trim()
  if (!url) { setStatus('URL missing!', 'err'); return }
  if (!selectedCombined && !selectedVideo && !selectedAudio) { setStatus('Pehle format select karo', 'err'); return }

  const quality = selectedCombined
    ? selectedCombined
    : (selectedVideo && selectedAudio
        ? selectedVideo + '+' + selectedAudio
        : (selectedVideo || selectedAudio))

  // Show progress
  const pw = document.getElementById('progressWrap')
  pw.classList.add('show')
  setProgress(0, 'Starting...')
  document.getElementById('progressSpeed').textContent = '—'
  document.getElementById('progressEta').textContent   = 'ETA —'
  document.getElementById('resultWrap').classList.remove('show')
  document.getElementById('dlBtn').disabled = true

  setStatus('Starting download job...', 'spin')

  try {
    // Step 1: kick off the job — responds almost instantly now
    const res = await fetch('/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url, quality, expires_in: 3600 })
    })
    const data = await res.json()

    if (!data.status || !data.job_id) {
      setStatus('Error: ' + (data.error || 'Unknown error'), 'err')
      setProgress(0, 'Failed')
      document.getElementById('dlBtn').disabled = false
      return
    }

    setStatus('Downloading & processing... (this can take a while for long videos)', 'spin')
    pollJobStatus(data.job_id)

  } catch (e) {
    setStatus('Request failed: ' + e.message, 'err')
    document.getElementById('dlBtn').disabled = false
  }
}

function pollJobStatus(jobId) {
  let elapsed = 0
  const stepMs = 4000

  if (pollTimer) clearInterval(pollTimer)

  pollTimer = setInterval(async () => {
    elapsed += stepMs
    try {
      const res = await fetch('/download/status?job_id=' + encodeURIComponent(jobId))
      const job = await res.json()

      if (!res.ok) {
        clearInterval(pollTimer)
        setStatus('Error: ' + (job.error || 'Job lookup failed'), 'err')
        setProgress(0, 'Failed')
        document.getElementById('dlBtn').disabled = false
        return
      }

      if (job.status === 'pending') {
        // Fake-ish progress just so the bar doesn't look frozen — real % isn't tracked server-side
        const pct = Math.min(92, Math.round(5 + elapsed / 1000))
        setProgress(pct, 'Downloading...')
        document.getElementById('progressEta').textContent = `~${Math.round(elapsed/1000)}s elapsed`
        return
      }

      if (job.status === 'done') {
        clearInterval(pollTimer)
        setProgress(100, 'Done!')
        setStatus('Download link ready!', 'ok')
        fillResult(job.result)
        document.getElementById('dlBtn').disabled = false
        return
      }

      if (job.status === 'error') {
        clearInterval(pollTimer)
        setStatus('Error: ' + (job.error || 'Download failed'), 'err')
        setProgress(0, 'Failed')
        document.getElementById('dlBtn').disabled = false
        return
      }
    } catch (e) {
      clearInterval(pollTimer)
      setStatus('Polling failed: ' + e.message, 'err')
      document.getElementById('dlBtn').disabled = false
    }
  }, stepMs)
}

function fillResult(result) {
  document.getElementById('resTitle').textContent = result.title || '—'

  const seriesRow  = document.getElementById('resSeriesRow')
  const seasonRow  = document.getElementById('resSeasonRow')
  const episodeRow = document.getElementById('resEpisodeRow')
  if (result.series) {
    document.getElementById('resSeries').textContent = result.series
    seriesRow.style.display = 'flex'
  } else {
    seriesRow.style.display = 'none'
  }
  if (result.season_number != null) {
    document.getElementById('resSeason').textContent = 'Season ' + result.season_number
    seasonRow.style.display = 'flex'
  } else {
    seasonRow.style.display = 'none'
  }
  if (result.episode_number != null) {
    document.getElementById('resEpisode').textContent = 'Episode ' + result.episode_number
    episodeRow.style.display = 'flex'
  } else {
    episodeRow.style.display = 'none'
  }

  document.getElementById('resAuthor').textContent   = result.author || '—'
  document.getElementById('resDuration').textContent = fmtDuration(result.duration)
  document.getElementById('resFormat').textContent   = result.format + ' · ' + result.quality
  document.getElementById('resQuality').textContent  = result.quality
  document.getElementById('resExpires').textContent  = fmtExpiry(parseInt(new URL(result.url).searchParams.get('expires')))
  document.getElementById('resLink').href            = result.url

  const watchBtn = document.getElementById('resWatchLink')
  if (result.watch_url) {
    watchBtn.href = result.watch_url
    watchBtn.style.display = 'block'
  } else {
    watchBtn.style.display = 'none'
  }

  document.getElementById('resultWrap').classList.add('show')
}

function setProgress(pct, label) {
  document.getElementById('progressBar').style.width   = pct + '%'
  document.getElementById('progressPct').textContent   = pct + '%'
  document.getElementById('progressLabel').textContent = label || 'Downloading...'
}

// ── ENTER KEY ──
document.getElementById('urlInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') getFormats()
})
</script>
</body>
</html>'''


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
