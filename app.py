import os
import sys
import uuid
import glob
import json
import subprocess
import threading
from urllib.parse import urlparse
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
DOWNLOAD_DIR = os.path.join(os.path.dirname(__file__), "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

jobs = {}

def get_proxy():
    proxy = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or os.environ.get("ALL_PROXY")
        or os.environ.get("all_proxy")
    )
    if not proxy:
        return None
    proxy = proxy.strip()
    if not proxy:
        return None
    if "://" not in proxy:
        proxy = f"http://{proxy}"
    return proxy


def get_cookies_browser():
    """Return browser string for --cookies-from-browser, or None to disable.

    Format: "browser" or "browser:profile_path"
    Examples:
      - "chrome"
      - "firefox:C:\\Users\\me\\AppData\\Roaming\\zen\\Profiles\\xxx.Default (release)"

    If COOKIES_BROWSER is not set, auto-detects a Zen profile (Firefox-based).
    """
    value = (os.environ.get("COOKIES_BROWSER") or "").strip()
    if not value or value.lower() in ("0", "false", "none", "off"):
        # Auto-detect Zen browser profile (Firefox-based, shares cookies.sqlite format)
        try:
            appdata = os.environ.get("APPDATA", "")
            zen_profiles_dir = os.path.join(appdata, "zen", "Profiles")
            if os.path.isdir(zen_profiles_dir):
                for entry in sorted(os.listdir(zen_profiles_dir), reverse=True):
                    if "Default" in entry:
                        cookies = os.path.join(zen_profiles_dir, entry, "cookies.sqlite")
                        if os.path.exists(cookies):
                            return f"firefox:{os.path.join(zen_profiles_dir, entry)}"
        except OSError:
            pass
        return None
    # Only lowercase the browser name (before the colon), preserve path case
    if ":" in value:
        browser, sep, rest = value.partition(":")
        return f"{browser.lower()}{sep}{rest}"
    return value.lower()


def debug_env():
    """Print_env hints for debugging."""
    cookies = get_cookies_browser()
    print(f"[reclip] COOKIES_BROWSER env = {os.environ.get('COOKIES_BROWSER')!r}", flush=True)
    print(f"[reclip] Resolved cookies arg = {cookies!r}", flush=True)
    print(f"[reclip] Deno in PATH: {bool(os.environ.get('PATH', '') and 'deno' in os.environ.get('PATH', '').lower())}", flush=True)


# Print env debug info on startup
debug_env()


def build_youtube_args():
    """yt-dlp args that work around YouTube's 2025-2026 anti-bot countermeasures.

    - player_client=default,-android_sdkless,-ios : avoid clients that YouTube
      forces SABR streaming on (SABR responses have no direct URLs).
    - --remote-components ejs:github : download the JS challenge solver script
      from yt-dlp/ejs releases (needed for signature + n-parameter solving).
    - Spoofed User-Agent : YouTube fingerprints yt-dlp's default UA.
    - ffmpeg reconnect args : survive brief network blips during long downloads.

    Requires:
    - Deno installed (yt-dlp uses it to run the JS challenge solver)
    - bgutil-ytdlp-pot-provider Python plugin (provides PO Tokens via Deno script mode)
    - bgutil-ytdlp-pot-provider Deno/Node server running on port 4416 (for HTTP mode)
    """
    return [
        "--extractor-args", "youtube:player_client=default,-android_sdkless,-ios",
        "--remote-components", "ejs:github",
        "--user-agent",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "--downloader-args",
        "ffmpeg_i:-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    ]


def add_cookies_arg(cmd):
    """Attach --cookies-from-browser if COOKIES_BROWSER is set."""
    browser = get_cookies_browser()
    if browser:
        cmd += ["--cookies-from-browser", browser]


def normalize_url(raw_url):
    if not raw_url:
        return ""
    url = str(raw_url).strip()
    if (url.startswith("`") and url.endswith("`")) or (url.startswith('"') and url.endswith('"')) or (url.startswith("'") and url.endswith("'")):
        url = url[1:-1].strip()
    url = url.strip("`").strip()
    return url


def validate_url(url):
    if any(ch.isspace() for ch in url):
        return False
    lower = url.lower()
    if lower.count("http://") + lower.count("https://") != 1:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    if not parsed.netloc:
        return False
    return True


def yt_dlp_error_text(result):
    stderr = (result.stderr or "").strip()
    stdout = (result.stdout or "").strip()
    text = stderr if stderr else stdout
    if not text:
        return "yt-dlp failed"
    return text.splitlines()[-1].strip() or "yt-dlp failed"


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    out_template = os.path.join(DOWNLOAD_DIR, f"{job_id}.%(ext)s")

    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "-o", out_template]

    proxy = get_proxy()
    if proxy:
        cmd += ["--proxy", proxy]

    add_cookies_arg(cmd)

    # YouTube-specific countermeasure args (applied for all sites; harmless for non-YouTube)
    cmd += build_youtube_args()

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            job["status"] = "error"
            job["error"] = yt_dlp_error_text(result)
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}.*"))
        if not files:
            job["status"] = "error"
            job["error"] = "Download completed but no file was found"
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        job["status"] = "done"
        job["file"] = chosen
        ext = os.path.splitext(chosen)[1]
        title = job.get("title", "").strip()
        # Sanitize title for filename
        if title:
            safe_title = "".join(c for c in title if c not in r'\/:*?"<>|').strip()[:20].strip()
            job["filename"] = f"{safe_title}{ext}" if safe_title else os.path.basename(chosen)
        else:
            job["filename"] = os.path.basename(chosen)
    except subprocess.TimeoutExpired:
        job["status"] = "error"
        job["error"] = "Download timed out (5 min limit)"
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = normalize_url((data or {}).get("url", ""))
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not validate_url(url):
        return jsonify({"error": "Invalid URL. Paste a single full URL starting with http:// or https://"}), 400

    cmd = [sys.executable, "-m", "yt_dlp", "--no-playlist", "-j", url]

    proxy = get_proxy()
    if proxy:
        cmd += ["--proxy", proxy]

    add_cookies_arg(cmd)
    cmd += build_youtube_args()

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": yt_dlp_error_text(result)}), 400

        info = json.loads(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = normalize_url((data or {}).get("url", ""))
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not validate_url(url):
        return jsonify({"error": "Invalid URL. Paste a single full URL starting with http:// or https://"}), 400

    job_id = uuid.uuid4().hex[:10]
    jobs[job_id] = {"status": "downloading", "url": url, "title": title}

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
