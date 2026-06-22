#!/usr/bin/env python3
"""
432 Hz Converter — Local Server
Converts YouTube (and direct audio) links to 432 Hz MP3s using yt-dlp + ffmpeg.

Requirements:
  pip install yt-dlp
  brew install ffmpeg  (Mac)
  sudo apt install ffmpeg  (Linux)
  winget install ffmpeg  (Windows)

Run:
  python server.py

Then open http://localhost:8765 in your browser.
"""

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import uuid
import shutil
from pathlib import Path

PORT = 8765
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "432hz_converter"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# 432/440 ratio expressed as a semitones value for ffmpeg's rubberband/asetrate
# We use asetrate + atempo for broad ffmpeg compatibility (no rubberband needed)
# asetrate: changes pitch by resampling (440->432 means multiply rate by 432/440)
# atempo: corrects playback speed back to normal (1/ratio to undo speed change)
RATIO = 432 / 440  # 0.98181...
ATEMPO = 1 / RATIO  # 1.01852... — corrects tempo after pitch shift


def check_dependencies():
    missing = []
    for tool in ["yt-dlp", "ffmpeg"]:
        if shutil.which(tool) is None:
            missing.append(tool)
    return missing


def convert_to_432hz(source_url: str, output_name: str) -> tuple[bool, str, str]:
    """
    Download audio from URL (YouTube or direct), pitch-shift to 432 Hz, return path to MP3.
    Returns (success, file_path_or_error, file_size_str)
    """
    job_id = uuid.uuid4().hex[:8]
    work_dir = DOWNLOAD_DIR / job_id
    work_dir.mkdir(exist_ok=True)

    raw_path = work_dir / "raw"
    out_path = work_dir / f"{output_name}.mp3"

    try:
        # Step 1: Download audio with yt-dlp
        yt_cmd = [
            "yt-dlp",
            "--no-playlist",
            "--extract-audio",
            "--audio-format", "wav",       # download as wav for clean ffmpeg input
            "--audio-quality", "0",
            "--output", str(raw_path) + ".%(ext)s",
            "--no-warnings",
            source_url
        ]
        result = subprocess.run(yt_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return False, f"yt-dlp error: {result.stderr.strip() or result.stdout.strip()}", ""

        # Find the downloaded file
        candidates = list(work_dir.glob("raw.*"))
        if not candidates:
            return False, "yt-dlp ran but no file was created.", ""
        input_file = candidates[0]

        # Step 2: Pitch-shift to 432 Hz with ffmpeg
        # Method: asetrate lowers the sample rate (slowing & lowering pitch),
        # then aresample brings it back to 44100, then atempo corrects the speed.
        # Result: pitch is 432/440 of original, duration is unchanged.
        ffmpeg_cmd = [
            "ffmpeg", "-y",
            "-i", str(input_file),
            "-af", f"asetrate=44100*{RATIO},aresample=44100,atempo={ATEMPO}",
            "-ar", "44100",
            "-ac", "2",
            "-q:a", "0",          # highest quality VBR
            str(out_path)
        ]
        result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            return False, f"ffmpeg error: {result.stderr[-500:]}", ""

        if not out_path.exists():
            return False, "ffmpeg ran but output file missing.", ""

        size_mb = out_path.stat().st_size / 1024 / 1024
        return True, str(out_path), f"{size_mb:.1f} MB"

    except subprocess.TimeoutExpired:
        return False, "Conversion timed out (>2 min). Try a shorter video.", ""
    except Exception as e:
        return False, str(e), ""


class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        print(f"  {self.address_string()} → {format % args}")

    def send_json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        # Serve the frontend
        if parsed.path in ("/", "/index.html"):
            html_path = Path(__file__).parent / "index.html"
            if html_path.exists():
                content = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(content))
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_json(404, {"error": "index.html not found"})
            return

        # Serve a converted file for download
        if parsed.path.startswith("/download/"):
            parts = parsed.path.split("/download/", 1)
            if len(parts) == 2:
                file_path = Path(urllib.parse.unquote(parts[1]))
                if file_path.exists() and file_path.suffix == ".mp3":
                    content = file_path.read_bytes()
                    filename = file_path.name
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/mpeg")
                    self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                    self.send_header("Content-Length", len(content))
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(content)
                    return
            self.send_json(404, {"error": "File not found"})
            return

        # Health / dependency check
        if parsed.path == "/check":
            missing = check_dependencies()
            self.send_json(200, {"ok": len(missing) == 0, "missing": missing})
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        if self.path == "/convert":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
            except Exception:
                self.send_json(400, {"error": "Invalid JSON"})
                return

            source_url = (data.get("url") or "").strip()
            output_name = (data.get("name") or "audio-432hz").strip()
            # Sanitize filename: keep alphanumerics, dashes, underscores, spaces, and unicode word chars
            output_name = "".join(c for c in output_name if c.isalnum() or c in "-_ ").strip() or "audio-432hz"
            # Cap length to avoid filesystem "name too long" errors (255 byte limit on most filesystems)
            output_name = output_name.encode("utf-8")[:120].decode("utf-8", errors="ignore").strip() or "audio-432hz"

            if not source_url:
                self.send_json(400, {"error": "No URL provided"})
                return

            print(f"\n🎵 Converting: {source_url}")
            print(f"   Output name: {output_name}.mp3")

            missing = check_dependencies()
            if missing:
                self.send_json(500, {
                    "error": f"Missing tools: {', '.join(missing)}. See setup instructions."
                })
                return

            success, result, size = convert_to_432hz(source_url, output_name)
            if success:
                download_url = f"/download/{urllib.parse.quote(result, safe='')}"
                self.send_json(200, {
                    "ok": True,
                    "download_url": download_url,
                    "filename": f"{output_name}.mp3",
                    "size": size
                })
            else:
                self.send_json(500, {"error": result})
        else:
            self.send_json(404, {"error": "Not found"})


def main():
    print("=" * 52)
    print("  432 Hz Converter — Local Server")
    print("=" * 52)

    missing = check_dependencies()
    if missing:
        print(f"\n⚠️  Missing dependencies: {', '.join(missing)}")
        print("\nInstall them:")
        if "yt-dlp" in missing:
            print("  pip install yt-dlp")
        if "ffmpeg" in missing:
            print("  Mac:     brew install ffmpeg")
            print("  Linux:   sudo apt install ffmpeg")
            print("  Windows: winget install ffmpeg")
        print("\nThen re-run: python server.py\n")
        sys.exit(1)
    else:
        print("\n✅ yt-dlp and ffmpeg found")

    print(f"\n🚀 Server running at http://localhost:{PORT}")
    print("   Open that URL in your browser")
    print("   Press Ctrl+C to stop\n")

    server = http.server.ThreadingHTTPServer(("", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
