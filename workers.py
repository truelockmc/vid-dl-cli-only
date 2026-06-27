#!/usr/bin/env python3
"""
MetadataWorker and DownloadWorker
- Adds a small ytdlp logger that prints postprocessor messages to terminal.
- Exports build_ydl_opts(...) so other modules (CLI) can reuse the same option-building logic.
- DownloadWorker supports an optional forced_outtmpl path (full path to output file).
"""

import os
import shutil
import sys
import time

import yt_dlp
try:
    from curl_cffi import requests as _cffi_requests
except ImportError:
    _cffi_requests = None

from utils import (
    format_filesize,
    friendly_error,
    get_videasy_headers,
    sanitize_filename,
)

# Impersonation target used for our own HTTP requests (thumbnails, direct downloads).
# curl_cffi handles the TLS fingerprinting so Cloudflare doesn't block us.
_CF_IMPERSONATE = "chrome136"


class YTDLPLogger:
    def __init__(self):
        self._last_was_progress = False

    def _is_progress(self, msg: str) -> bool:
        if not msg:
            return False
        s = msg.strip()
        return ("\r" in msg) or (
            s.startswith("[download]") and ("%" in s or "ETA" in s or "of" in s)
        )

    def _finish_progress_line(self):
        if self._last_was_progress:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self._last_was_progress = False

    def debug(self, msg):
        if not msg:
            return
        if self._is_progress(msg):
            parts = [p for p in msg.split("\r") if p != ""]
            current = parts[-1] if parts else msg
            # ensure we don't output a trailing newline, use '\r' to overwrite same line
            out = current.rstrip("\n")
            if not out.endswith("\r"):
                out = out + "\r"
            sys.stdout.write(out)
            sys.stdout.flush()
            self._last_was_progress = True
        else:
            self._finish_progress_line()
            if msg.startswith("["):
                print(msg)
            else:
                print(f"[yt-dlp DEBUG] {msg}")

    def info(self, msg):
        if not msg:
            return
        self._finish_progress_line()
        if msg.startswith("["):
            print(msg)
        else:
            print(f"[yt-dlp] {msg}")

    def warning(self, msg):
        if not msg:
            return
        self._finish_progress_line()
        if msg.startswith("["):
            print(msg)
        else:
            print(f"[yt-dlp WARNING] {msg}")

    def error(self, msg):
        if not msg:
            return
        self._finish_progress_line()
        if msg.startswith("["):
            print(msg)
        else:
            print(f"[yt-dlp ERROR] {msg}")


def build_ydl_opts(
    fmt,
    video_quality,
    audio_bitrate,
    net_config,
    download_playlist=False,
    deno_path: str = "",
):
    """
    Stateless builder for yt-dlp options that mirrors DownloadWorker._build_base_opts logic.
    Returned dict intentionally does not set 'progress_hooks' (caller attaches it) but does set
    format/merge/postprocessor options.

    deno_path: full path to a Deno executable. When provided, yt-dlp's js_runtimes option
               is set so Deno is used for signature/nsig solving, unlocking all YouTube formats.
               Without it yt-dlp still works but may miss some formats on YouTube.
    """
    ydl_opts = {
        "abort_on_error": False,
        "concurrent_fragment_downloads": int(
            net_config.get("concurrent_fragment_downloads", "3")
        ),
        "http_chunk_size": int(net_config.get("http_chunk_size", "1048576")),
        #  HLS stability
        "retries": 100,
        "fragment_retries": 100,
        "retry_sleep_functions": {
            "fragment": lambda n: 3,
            "http": lambda n: 3,
        },
        "socket_timeout": 10,
        "file_access_retries": 50,
        "downloader": "ffmpeg",
        "hls_use_mpegts": True,
        "continuedl": True,
        "noplaylist": not download_playlist,
        "cachedir": False,
        "logger": None,
        # Tell the generic extractor to impersonate a browser when it hits a
        # Cloudflare anti-bot 403. Equivalent to --extractor-args "generic:impersonate".
        # Only affects the generic extractor; YouTube and others are not impacted.
        "extractor_args": {
            "generic": {"impersonate": ["chrome"]},
        },
        # Send Videasy-compatible headers on all requests, including ffmpeg segment downloads.
        # http_headers is forwarded to ffmpeg via -headers, so these must be set explicitly.
        "http_headers": get_videasy_headers(),
        # Enable Deno as JS runtime (searches PATH by default).
        # yt-dlp needs this for YouTube signature/nsig solving to get all formats.
        # If a specific path is configured it will be set below.
        "js_runtimes": {"deno": {}},
        # Allow yt-dlp to fetch the EJS challenge-solver script from GitHub.
        # Without this Deno finds no solver script and signature solving fails.
        "remote_components": ["ejs:github"],
    }

    # If a specific Deno binary is configured, tell yt-dlp exactly where it is.
    if deno_path and os.path.isfile(deno_path):
        ydl_opts["js_runtimes"] = {"deno": {"path": deno_path}}

    if fmt in ["mp4 (with Audio)", "avi", "mkv"]:
        if video_quality == "best":
            ydl_opts["format"] = "bestvideo+bestaudio/best"
        else:
            if video_quality:
                ydl_opts["format"] = (
                    f"bestvideo[height<={video_quality}]+bestaudio/best[height<={video_quality}]"
                )
            else:
                ydl_opts["format"] = "bestvideo+bestaudio/best"
        if fmt == "mp4 (with Audio)":
            ydl_opts["merge_output_format"] = "mp4"
            ydl_opts["postprocessor_args"] = ["-c", "copy"]
        else:
            ydl_opts["merge_output_format"] = fmt.split()[0].lower()
    elif fmt == "mp4 (without Audio)":
        if video_quality == "best":
            ydl_opts["format"] = "bestvideo"
        else:
            if video_quality:
                ydl_opts["format"] = f"bestvideo[height<={video_quality}]"
            else:
                ydl_opts["format"] = "bestvideo"
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessor_args"] = ["-c", "copy"]
    elif fmt == "mp3":
        ydl_opts["format"] = "bestaudio"
        ydl_opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": audio_bitrate,
            }
        ]
    else:
        ydl_opts["format"] = "bestvideo+bestaudio/best"
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessor_args"] = ["-c", "copy"]
    return ydl_opts


def _ffmpeg_available():
    return shutil.which("ffmpeg") is not None


def _is_direct_download_url(url):
    """Return True for URLs that serve a file directly (e.g. SharePoint download links)
    where yt-dlp cannot determine a sensible file extension from the URL path."""
    import urllib.parse

    parsed = urllib.parse.urlparse(url)
    path = parsed.path.lower()
    direct_patterns = ["download.aspx", "/_layouts/15/download"]
    return any(p in path for p in direct_patterns)


def _parse_ffmpeg_duration(stderr_line):
    """Extract total duration in seconds from an ffmpeg Duration line.
    Returns float seconds or None."""
    import re

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.?\d*)", stderr_line)
    if m:
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        return h * 3600 + mi * 60 + s
    return None


def _parse_ffmpeg_progress(stderr_line):
    """Extract (elapsed_seconds, size_kb) from an ffmpeg progress line.
    Returns (float, int) or (None, None)."""
    import re

    time_m = re.search(r"time=(\d+):(\d+):(\d+\.?\d*)", stderr_line)
    size_m = re.search(r"size=\s*(\d+)kB", stderr_line)
    if time_m:
        h, mi, s = int(time_m.group(1)), int(time_m.group(2)), float(time_m.group(3))
        elapsed = h * 3600 + mi * 60 + s
        size_kb = int(size_m.group(1)) if size_m else 0
        return elapsed, size_kb
    return None, None


def _download_direct(url, folder, progress_cb=None, size_cb=None, cancelled_cb=None):
    """Download a direct-serving URL (e.g. SharePoint) by streaming via ffmpeg.

    yt-dlp cannot handle these because the URL path ends with .aspx rather than a video
    extension, causing its internal safety check to abort.  We bypass yt-dlp entirely:
      1. HEAD the URL to get the Content-Disposition filename (if available).
      2. Run ffmpeg -i <url> -c copy output.mp4, parsing stderr for progress updates.
    """
    import re
    import subprocess
    import urllib.parse

    # --- resolve final URL (follow redirects with HEAD) ---
    head = requests.head(
        url, allow_redirects=True, timeout=15, impersonate=_CF_IMPERSONATE
    )
    final_url = head.url

    # --- determine output filename ---
    cd = head.headers.get("Content-Disposition", "")
    match = re.search(
        r'filename[^;=\n]*=(["\'])?([^;\n]*?)\1(?:;|$)', cd, re.IGNORECASE
    )
    if match:
        raw_name = match.group(2).strip()
        base = os.path.splitext(raw_name)[0]
    else:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(final_url).query)
        token = qs.get("share", ["video"])[0]
        base = sanitize_filename(token) or "video"

    out_path = os.path.join(folder, f"{base}.mp4")
    counter = 1
    while os.path.exists(out_path):
        out_path = os.path.join(folder, f"{base}_{counter}.mp4")
        counter += 1

    print(f"[direct-dl] Saving to: {out_path}")

    # --- run ffmpeg, read stderr for progress ---
    cmd = [
        shutil.which("ffmpeg") or "ffmpeg",
        "-y",
        "-i",
        final_url,
        "-c",
        "copy",
        out_path,
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        bufsize=1,
    )

    total_secs = None  # filled once we see the Duration line

    for raw_line in proc.stderr:
        line = raw_line.rstrip()
        if not line:
            continue
        print(f"[ffmpeg] {line}")

        if cancelled_cb and cancelled_cb():
            proc.terminate()
            raise Exception("Cancelled")

        # Try to get total duration from the input info block
        if total_secs is None:
            total_secs = _parse_ffmpeg_duration(line)

        # Parse progress lines (contain "time=" and "size=")
        elapsed, size_kb = _parse_ffmpeg_progress(line)
        if elapsed is not None and progress_cb:
            if total_secs and total_secs > 0:
                percent = min(elapsed / total_secs * 100.0, 99.9)
            else:
                percent = -1  # unknown total; UI should show indeterminate

            # Build a human-readable size string
            size_mb = size_kb / 1024.0
            if size_mb >= 1024:
                size_str = f"{size_mb / 1024:.2f} GB"
            else:
                size_str = f"{size_mb:.1f} MB"

            status = f"Downloading ({size_str})"
            progress_cb(max(percent, 0), status)
            if size_cb:
                size_cb(size_str)

    proc.wait()
    if proc.returncode != 0:
        raise Exception(f"ffmpeg exited with code {proc.returncode}")
    if progress_cb:
        progress_cb(100.0, "Finished")
    return out_path


