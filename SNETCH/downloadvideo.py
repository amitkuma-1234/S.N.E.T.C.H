# ============================================================
# downloadvideo.py
# S.N.E.T.C.H · YouTube Video Downloader (backend)
#
# Exposes a Flask Blueprint with a small JSON API used by
# templates/downloadvideo.html + js/downloadvideo.js:
#
#   POST /api/downloadvideo/resolve      -> look up a video (URL or name), no download
#   POST /api/downloadvideo/start        -> start a background download, returns job_id
#   GET  /api/downloadvideo/progress/<id>-> poll live progress for a job_id
#   POST /api/downloadvideo/cancel/<id>  -> best-effort cancel of an in-flight download
#
# Wiring note: the page route itself (GET /downloadvideo) already lives in
# app.py and is untouched. To expose the API above on the same Flask app,
# app.py needs exactly two additional lines (see bottom of this file for
# the snippet) — nothing about any other feature is touched.
# ============================================================

import os
import re
import uuid
import shutil
import threading
import traceback

from flask import Blueprint, request, jsonify

try:
    import yt_dlp
except ImportError:  # pragma: no cover - surfaced as a clean error at request time
    yt_dlp = None

downloadvideo_bp = Blueprint("downloadvideo_api", __name__, url_prefix="/api/downloadvideo")


@downloadvideo_bp.errorhandler(Exception)
def handle_exception(e):
    """Ensure every error from this blueprint is JSON, never HTML."""
    code = getattr(e, "code", 500)
    return jsonify({"ok": False, "error": str(e) or "Internal server error"}), code

# ------------------------------------------------------------
# In-memory job store
# ------------------------------------------------------------
# jobs[job_id] = {
#   "status": "queued" | "downloading" | "processing" | "finished" | "error" | "cancelled",
#   "percent": float,
#   "speed": str,              # human readable, e.g. "3.2 MB/s"
#   "downloaded": str,         # human readable size
#   "total": str,              # human readable size
#   "eta": str,                # human readable time
#   "title": str,
#   "channel": str,
#   "duration": str,
#   "thumbnail": str,
#   "filepath": str,
#   "error": str,
#   "_cancel": bool,           # internal cancel flag
# }
jobs = {}
jobs_lock = threading.Lock()

YOUTUBE_URL_RE = re.compile(
    r"^(https?://)?(www\.)?(youtube\.com/(watch\?v=|shorts/|embed/)|youtu\.be/)[\w\-]+",
    re.IGNORECASE,
)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def is_youtube_url(text: str) -> bool:
    return bool(YOUTUBE_URL_RE.match(text.strip()))


def find_ffmpeg():
    return shutil.which("ffmpeg")


def downloads_folder() -> str:
    path = os.path.join(os.path.expanduser("~"), "Downloads")
    os.makedirs(path, exist_ok=True)
    return path


def human_size(num_bytes):
    if not num_bytes:
        return "—"
    num_bytes = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB"]:
        if num_bytes < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"


def human_speed(bytes_per_sec):
    if not bytes_per_sec:
        return "—"
    return f"{human_size(bytes_per_sec)}/s"


def human_eta(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def human_duration(seconds):
    if not seconds:
        return "—"
    seconds = int(seconds)
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def friendly_error(exc: Exception) -> str:
    """Map yt-dlp / network exceptions to clean, user-facing messages."""
    msg = str(exc).lower()

    if "private video" in msg:
        return "This video is private and can't be downloaded."
    if "sign in to confirm your age" in msg or ("age" in msg and "restrict" in msg):
        return "This video is age-restricted and can't be downloaded."
    if "video unavailable" in msg or "has been removed" in msg:
        return "This video has been removed or is unavailable."
    if "unable to download webpage" in msg or "network" in msg or "timed out" in msg or "connection" in msg:
        return "Network error. Please check your internet connection and try again."
    if "no video results" in msg or "not found" in msg:
        return "Couldn't find a matching video. Try a different name."
    if "unsupported url" in msg or "is not a valid url" in msg:
        return "That doesn't look like a valid YouTube link."
    if "ffmpeg" in msg:
        return "FFmpeg is required to merge video/audio but wasn't found on this system."
    return f"Download failed. Please try again. (Details: {str(exc)})"


def build_ydl_opts(download_path=None, progress_hook=None, browser=None):
    ffmpeg_path = find_ffmpeg()

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestvideo*+bestaudio/best" if ffmpeg_path else "best",
    }

    if browser:
        opts["cookiesfrombrowser"] = (browser, )

    if ffmpeg_path:
        opts["ffmpeg_location"] = ffmpeg_path
        opts["merge_output_format"] = "mp4"

    if download_path:
        opts["outtmpl"] = download_path

    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    return opts


def extract_metadata(query: str, browser: str = None):
    """Look up a video's metadata without downloading it.

    `query` is either a direct YouTube URL or a free-text video name.
    Returns a dict of metadata, or raises an exception on failure.
    """
    if yt_dlp is None:
        raise RuntimeError("yt-dlp is not installed on the server.")

    target = query if is_youtube_url(query) else f"ytsearch1:{query}"

    with yt_dlp.YoutubeDL(build_ydl_opts(browser=browser)) as ydl:
        info = ydl.extract_info(target, download=False)

    if info is None:
        raise ValueError("No video results found")

    if "entries" in info:
        entries = [e for e in info["entries"] if e]
        if not entries:
            raise ValueError("No video results found")
        info = entries[0]

    return {
        "video_url": info.get("webpage_url") or info.get("url"),
        "title": info.get("title") or "Untitled video",
        "channel": info.get("uploader") or info.get("channel") or "Unknown channel",
        "duration": human_duration(info.get("duration")),
        "duration_seconds": info.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or "",
    }


def run_download(job_id: str, video_url: str, browser: str = None):
    with jobs_lock:
        jobs[job_id]["status"] = "downloading"

    def hook(d):
        with jobs_lock:
            job = jobs.get(job_id)
            if not job:
                return
            if job.get("_cancel"):
                raise yt_dlp.utils.DownloadError("Download cancelled by user")

            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                percent = round((downloaded / total) * 100, 1) if total else job.get("percent", 0)

                job.update({
                    "status": "downloading",
                    "percent": percent,
                    "speed": human_speed(d.get("speed")),
                    "downloaded": human_size(downloaded),
                    "total": human_size(total),
                    "eta": human_eta(d.get("eta")),
                })

            elif d["status"] == "finished":
                job.update({
                    "status": "processing",
                    "percent": 99.0,
                    "eta": "—",
                })

    try:
        download_path = os.path.join(downloads_folder(), "%(title)s.%(ext)s")
        opts = build_ydl_opts(download_path=download_path, progress_hook=hook, browser=browser)

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(video_url, download=True)
            final_path = ydl.prepare_filename(info)
            # If ffmpeg merged into mp4, the actual extension may differ from the template
            if find_ffmpeg():
                root, _ext = os.path.splitext(final_path)
                mp4_path = root + ".mp4"
                if os.path.exists(mp4_path):
                    final_path = mp4_path

        with jobs_lock:
            jobs[job_id].update({
                "status": "finished",
                "percent": 100.0,
                "eta": "0s",
                "filepath": final_path,
                "downloads_folder": downloads_folder(),
            })

    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        with jobs_lock:
            job = jobs.get(job_id)
            if job:
                if job.get("_cancel"):
                    job["status"] = "cancelled"
                else:
                    job["status"] = "error"
                    job["error"] = friendly_error(exc)


# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------
@downloadvideo_bp.route("/resolve", methods=["POST"])
def api_resolve():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()

    if not query:
        return jsonify({"ok": False, "error": "Please enter a YouTube link or a video name."}), 400

    if yt_dlp is None:
        return jsonify({"ok": False, "error": "yt-dlp is not installed on the server. Run: pip install yt-dlp"}), 500

    try:
        browser_used = None
        try:
            meta = extract_metadata(query)
        except Exception as e:
            msg = str(e).lower()
            if "bot" in msg or "sign in" in msg or "age" in msg:
                try:
                    meta = extract_metadata(query, browser="chrome")
                    browser_used = "chrome"
                except Exception:
                    meta = extract_metadata(query, browser="edge")
                    browser_used = "edge"
            else:
                raise e
                
        return jsonify({"ok": True, "browser": browser_used, **meta})
    except Exception as exc:  # noqa: BLE001
        import traceback
        with open('yt_error.txt', 'w') as f:
            f.write(traceback.format_exc())
        traceback.print_exc()
        return jsonify({"ok": False, "error": friendly_error(exc)}), 400


@downloadvideo_bp.route("/start", methods=["POST"])
def api_start():
    data = request.get_json(silent=True) or {}
    video_url = (data.get("video_url") or "").strip()
    browser = data.get("browser")

    if not video_url:
        return jsonify({"ok": False, "error": "No video selected to download."}), 400

    if yt_dlp is None:
        return jsonify({"ok": False, "error": "yt-dlp is not installed on the server. Run: pip install yt-dlp"}), 500

    job_id = uuid.uuid4().hex
    with jobs_lock:
        jobs[job_id] = {
            "status": "queued",
            "percent": 0.0,
            "speed": "—",
            "downloaded": "—",
            "total": "—",
            "eta": "—",
            "title": data.get("title", ""),
            "channel": data.get("channel", ""),
            "duration": data.get("duration", ""),
            "thumbnail": data.get("thumbnail", ""),
            "filepath": "",
            "error": "",
            "_cancel": False,
        }

    thread = threading.Thread(target=run_download, args=(job_id, video_url, browser), daemon=True)
    thread.start()

    return jsonify({"ok": True, "job_id": job_id})


@downloadvideo_bp.route("/progress/<job_id>", methods=["GET"])
def api_progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Unknown download job."}), 404
        payload = {k: v for k, v in job.items() if not k.startswith("_")}

    return jsonify({"ok": True, **payload})


@downloadvideo_bp.route("/cancel/<job_id>", methods=["POST"])
def api_cancel(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Unknown download job."}), 404
        job["_cancel"] = True

    return jsonify({"ok": True})


# ============================================================
# ONE-TIME WIRING (required, ~2 lines in app.py)
# ============================================================
# This blueprint must be registered on the shared Flask `app`
# instance so the API routes above become reachable. Add, near
# the other feature imports at the top of app.py:
#
#     from downloadvideo import downloadvideo_bp
#
# and, anywhere after `app = Flask(__name__, ...)` is created:
#
#     app.register_blueprint(downloadvideo_bp)
#
# Nothing else in app.py needs to change — the existing
# `GET /downloadvideo` page route is untouched.
# ============================================================
