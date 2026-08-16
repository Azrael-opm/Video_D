"""
NyxDownloader — central configuration.

Everything is overridable via environment variables so the same code runs
in dev, behind a proxy, or in a container.
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# FFmpeg — required for merging bestvideo+bestaudio streams and for
# extracting/convertng audio (MP3 / M4A). Resolution order:
#   1. $FFMPEG_PATH (explicit path to the binary or its directory)
#   2. the static binary bundled with the `imageio-ffmpeg` pip package
#   3. `ffmpeg` on the system PATH (yt-dlp default when None)
# ---------------------------------------------------------------------------
def _resolve_ffmpeg() -> str | None:
    env = os.getenv("FFMPEG_PATH")
    if env:
        return env
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


FFMPEG_PATH: str | None = _resolve_ffmpeg()

# Optional cookies file (Netscape format) for platforms that gate content
# behind a login (some Instagram/X/TikTok content). e.g. export from your
# browser with "Get cookies.txt LOCALLY".
COOKIES_FILE: str | None = os.getenv("COOKIES_FILE") or None

# Where dashboard-uploaded cookies live (restored from SQLite on boot,
# so they survive redeploys on ephemeral hosts like Render/Railway).
COOKIES_PATH: Path = Path(os.getenv("COOKIES_PATH", str(Path(__file__).parent / "cookies.txt")))

# Where finished/intermediate media files are stored before being streamed
# to the client.
TMP_DIR: Path = Path(os.getenv("TMP_DIR", str(Path(__file__).parent / "downloads")))
TMP_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Rate limiting (slowapi syntax: "N/period" — second, minute, hour, day)
# ---------------------------------------------------------------------------
ANALYZE_RATE_LIMIT: str = os.getenv("ANALYZE_RATE_LIMIT", "15/minute")
DOWNLOAD_RATE_LIMIT: str = os.getenv("DOWNLOAD_RATE_LIMIT", "8/hour")
PREVIEW_RATE_LIMIT: str = os.getenv("PREVIEW_RATE_LIMIT", "30/hour")
RECODE_RATE_LIMIT: str = os.getenv("RECODE_RATE_LIMIT", "12/hour")
UPLOAD_RATE_LIMIT: str = os.getenv("UPLOAD_RATE_LIMIT", "10/hour")

# Recode Studio
MAX_UPLOAD_BYTES: int = int(os.getenv("MAX_UPLOAD_BYTES", str(800 * 1024 * 1024)))  # 800 MB

# Admin dashboard credentials. CHANGE THE PASSWORD for any real deployment.
ADMIN_PASSWORD: str = os.getenv("ADMIN_PASSWORD", "nyx-admin")

# Inline previews are transcoded down to this height (fast + lightweight).
PREVIEW_HEIGHT: int = int(os.getenv("PREVIEW_HEIGHT", "480"))

# ---------------------------------------------------------------------------
# Job engine
# ---------------------------------------------------------------------------
MAX_CONCURRENT_DOWNLOADS: int = int(os.getenv("MAX_CONCURRENT_DOWNLOADS", "3"))
JOB_TTL_SECONDS: int = int(os.getenv("JOB_TTL_SECONDS", str(30 * 60)))  # files kept 30 min
MAX_DOWNLOAD_BYTES: int = int(os.getenv("MAX_DOWNLOAD_BYTES", str(2 * 1024**3)))  # 2 GB cap

# Request timeouts passed to yt-dlp
SOCKET_TIMEOUT: int = 20
RETRIES: int = 3

# Video quality ladder presented to the user (highest first).
QUALITY_LADDER: list[int] = [2160, 1440, 1080, 720, 480, 360, 240, 144]
QUALITY_LABELS: dict[int, str] = {
    2160: "4K Ultra HD",
    1440: "2K QHD",
    1080: "Full HD",
    720: "HD",
    480: "SD",
    360: "Low",
    240: "Very low",
    144: "Minimum",
}
