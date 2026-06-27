#!/usr/bin/env python3
"""
build_cli.py
============
Downloads the video-downloader repo from GitHub and patches it into a
CLI-only version (no GUI / PyQt6 / gui.py / gui_styling.py).

Files are written next to this script, replacing any existing ones:
  cli.py
  utils.py        (PyQt6 + GUI-specific parts removed)
  workers.py      (PyQt6 / QThread workers removed, build_ydl_opts etc. kept)
  requirements.txt
"""

import os
import re
import shutil
import sys
import tempfile
import textwrap
import urllib.request
import zipfile

REPO_URL = "https://github.com/truelockmc/video-downloader/archive/refs/heads/main.zip"

# Output next to this script
OUT_DIR = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def download_repo(dest_zip: str):
    print(f"  -> Downloading from {REPO_URL} ...")
    urllib.request.urlretrieve(REPO_URL, dest_zip)
    print("  OK Download complete")


def extract_repo(zip_path: str, dest_dir: str) -> str:
    """Extracts the ZIP and returns the path to the inner repo folder."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(dest_dir)
    # GitHub ZIPs always contain one subfolder like 'video-downloader-main'
    entries = [e for e in os.listdir(dest_dir) if os.path.isdir(os.path.join(dest_dir, e))]
    if not entries:
        raise RuntimeError("ZIP contains no subfolder - unexpected format.")
    return os.path.join(dest_dir, entries[0])


# ---------------------------------------------------------------------------
# Patch utils.py: remove PyQt6 + GUI-specific functions
# ---------------------------------------------------------------------------

def patch_utils(src: str) -> str:
    lines = src.splitlines(keepends=True)
    result = []

    remove_line_patterns = [
        r"^from PyQt6\b",
        r"^import PyQt6\b",
    ]

    # Functions that need GUI -> replace with CLI stubs
    gui_functions = {"check_ffmpeg", "install_ffmpeg"}

    i = 0
    while i < len(lines):
        line = lines[i]

        # Drop PyQt6 import lines
        if any(re.match(p, line) for p in remove_line_patterns):
            i += 1
            continue

        # Detect function start
        func_match = re.match(r"^(def )(\w+)\(", line)
        if func_match:
            fname = func_match.group(2)
            if fname in gui_functions:
                # Write CLI stub
                if fname == "check_ffmpeg":
                    result.append(
                        "def check_ffmpeg():\n"
                        "    \"\"\"Check whether ffmpeg is available on PATH (CLI version).\"\"\"\n"
                        "    import shutil\n"
                        "    if not shutil.which(\"ffmpeg\"):\n"
                        "        print(\"[Warning] ffmpeg not found. Please install it: https://ffmpeg.org/download.html\", file=__import__('sys').stderr)\n"
                        "        return False\n"
                        "    return True\n\n"
                    )
                elif fname == "install_ffmpeg":
                    result.append(
                        "def install_ffmpeg():\n"
                        "    \"\"\"Print ffmpeg install hint (CLI version).\"\"\"\n"
                        "    print(\"Please install ffmpeg manually: https://ffmpeg.org/download.html\")\n\n"
                    )
                # Skip original block until next top-level def/class
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    if re.match(r"^(def |class |\S)", nxt) and not nxt.strip().startswith("#"):
                        break
                    i += 1
                continue

        result.append(line)
        i += 1

    return "".join(result)


# ---------------------------------------------------------------------------
# Patch workers.py: remove PyQt6 / QThread classes, keep the rest
# ---------------------------------------------------------------------------

def patch_workers(src: str) -> str:
    lines = src.splitlines(keepends=True)
    result = []

    remove_line_patterns = [
        r"^from PyQt6\b",
        r"^import PyQt6\b",
    ]

    # GUI-only worker classes to drop
    gui_classes = {"MetadataWorker", "DownloadWorker"}

    i = 0
    while i < len(lines):
        line = lines[i]

        # Drop PyQt6 import lines
        if any(re.match(p, line) for p in remove_line_patterns):
            i += 1
            continue

        # Detect class start
        cls_match = re.match(r"^class (\w+)", line)
        if cls_match:
            cname = cls_match.group(1)
            if cname in gui_classes:
                # Skip entire class block
                i += 1
                while i < len(lines):
                    nxt = lines[i]
                    if re.match(r"^(def |class |\S)", nxt) and not nxt.strip().startswith("#") and nxt.strip() != "":
                        break
                    i += 1
                continue

        result.append(line)
        i += 1

    patched = "".join(result)

    # Wrap curl_cffi import with try/except so the script still works without it
    patched = patched.replace(
        "from curl_cffi import requests\n",
        "try:\n    from curl_cffi import requests as _cffi_requests\nexcept ImportError:\n    _cffi_requests = None\n"
    )
    patched = re.sub(r'\brequests\.Session\b', '_cffi_requests.Session', patched)
    patched = re.sub(r'\brequests\.get\b',     '_cffi_requests.get',     patched)

    return patched


# ---------------------------------------------------------------------------
# requirements.txt
# ---------------------------------------------------------------------------

REQUIREMENTS = textwrap.dedent("""\
    yt-dlp>=2024.1.1
    curl_cffi>=0.6
""")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("=== video-downloader CLI Builder ===\n")
    print(f"Output directory: {OUT_DIR}\n")

    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "repo.zip")
        extract_path = os.path.join(tmp, "repo")
        os.makedirs(extract_path)

        # 1. Download
        print("[1/4] Downloading GitHub repo ...")
        download_repo(zip_path)

        # 2. Extract
        print("[2/4] Extracting repo ...")
        repo_dir = extract_repo(zip_path, extract_path)
        files = os.listdir(repo_dir)
        print(f"  OK Repo folder: {repo_dir}")
        print(f"  Found files: {', '.join(files)}")

        # 3. Sanity check
        for fname in ("cli.py", "utils.py", "workers.py"):
            if not os.path.exists(os.path.join(repo_dir, fname)):
                print(f"  ERROR {fname} not found in repo! Available: {files}")
                sys.exit(1)

        # 4. Patch & write next to this script
        print("[3/4] Patching and writing files ...")

        # cli.py: copy as-is
        shutil.copy(os.path.join(repo_dir, "cli.py"), os.path.join(OUT_DIR, "cli.py"))
        print("  OK cli.py  (unchanged)")

        # utils.py
        with open(os.path.join(repo_dir, "utils.py"), encoding="utf-8") as f:
            utils_src = f.read()
        with open(os.path.join(OUT_DIR, "utils.py"), "w", encoding="utf-8") as f:
            f.write(patch_utils(utils_src))
        print("  OK utils.py  (PyQt6 + GUI stubs removed)")

        # workers.py
        with open(os.path.join(repo_dir, "workers.py"), encoding="utf-8") as f:
            workers_src = f.read()
        with open(os.path.join(OUT_DIR, "workers.py"), "w", encoding="utf-8") as f:
            f.write(patch_workers(workers_src))
        print("  OK workers.py  (QThread workers removed)")

        # requirements.txt
        with open(os.path.join(OUT_DIR, "requirements.txt"), "w", encoding="utf-8") as f:
            f.write(REQUIREMENTS)
        print("  OK requirements.txt")

    print("\n[4/4] Done!\n")
    print("Next steps:")
    print("  pip install -r requirements.txt")
    print("  python cli.py <URL>")
    print("  python cli.py --help")
    print()


if __name__ == "__main__":
    main()
