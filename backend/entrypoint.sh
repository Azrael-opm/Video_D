#!/bin/sh
# Keep yt-dlp's extractors fresh on every boot — extractor breakage is the
# #1 cause of "this platform suddenly fails". Fails soft when offline.
# Skip with NO_YTDLP_UPDATE=1. Runs BEFORE the server so the updated package
# is what the process imports.
if [ -z "$NO_YTDLP_UPDATE" ]; then
  python -m pip install -q --no-cache-dir -U yt-dlp 2>/dev/null \
    || echo "[boot] yt-dlp self-update skipped (offline or pinned)"
fi
exec python -m uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
