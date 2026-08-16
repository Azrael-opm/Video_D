"""
NyxDownloader — Recode Studio: uploaded-file registry + encoding plans.

The studio compresses a user's own video to a DESIRED file size while keeping
the original resolution. Plans use two-pass x264 so the output lands on the
target size, and platform specs cap the bitrate so the result is
"upload-ready": TikTok/Instagram/etc. won't re-compress a file that already
meets their codec/bitrate/container requirements.
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import config

# ---------------------------------------------------------------------------
# Platform publishing specs — ceilings that keep platforms from re-encoding.
# (H.264 High + AAC-LC in MP4 at/below these bitrates passes untouched.)
# ---------------------------------------------------------------------------
PLATFORM_SPECS: dict[str, dict[str, Any]] = {
    "none":      {"label": "No platform — just compress", "max_video_kbps": None, "audio_kbps": 192},
    "tiktok":    {"label": "TikTok",    "max_video_kbps": 4000,  "audio_kbps": 160},
    "instagram": {"label": "Instagram", "max_video_kbps": 4500,  "audio_kbps": 160},
    "youtube":   {"label": "YouTube",   "max_video_kbps": 12000, "audio_kbps": 192},
    "facebook":  {"label": "Facebook",  "max_video_kbps": 4000,  "audio_kbps": 160},
    "snapchat":  {"label": "Snapchat",  "max_video_kbps": 3000,  "audio_kbps": 128},
}

# ---------------------------------------------------------------------------
@dataclass
class Upload:
    id: str
    path: str
    filename: str
    size: int
    created_at: float = field(default_factory=time.time)
    probe: dict = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        return {
            "upload_id": self.id,
            "filename": self.filename,
            "size": self.size,
            **self.probe,
        }


UPLOADS: dict[str, Upload] = {}
_ULOCK = threading.Lock()
MEDIA_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".mts", ".3gp"}


def register_upload(filename: str, path: Path, size: int) -> Upload:
    probe = probe_media(path)
    up = Upload(id=uuid.uuid4().hex, path=str(path), filename=filename,
                size=size, probe=probe)
    with _ULOCK:
        UPLOADS[up.id] = up
    return up


def get_upload(upload_id: str) -> Upload | None:
    with _ULOCK:
        return UPLOADS.get(re.sub(r"[^a-f0-9]", "", upload_id or ""))


def get_upload_from_path(path: str) -> Upload | None:
    with _ULOCK:
        for up in UPLOADS.values():
            if up.path == path:
                return up
    return None


def purge_expired_uploads() -> int:
    now = time.time()
    with _ULOCK:
        stale = [u for u in UPLOADS.values() if now - u.created_at > config.JOB_TTL_SECONDS]
        for u in stale:
            UPLOADS.pop(u.id, None)
    for u in stale:
        try:
            Path(u.path).unlink(missing_ok=True)
        except OSError:
            pass
    return len(stale)


# ---------------------------------------------------------------------------
# Media probing (ffmpeg -i parsing — works for any container)
# ---------------------------------------------------------------------------
def probe_media(path: Path) -> dict[str, Any]:
    info: dict[str, Any] = {}
    if not config.FFMPEG_PATH:
        return info
    try:
        out = subprocess.run([config.FFMPEG_PATH, "-i", str(path)],
                             capture_output=True, text=True, timeout=60)
        err = out.stderr or ""
    except Exception:  # noqa: BLE001
        return info

    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        info["duration"] = round(int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3)), 2)
    m = re.search(r"bitrate:\s*(\d+)\s*kb/s", err)
    if m:
        info["bitrate_kbps"] = int(m.group(1))
    m = re.search(r"Video:\s*([a-z0-9]+)", err, re.I)
    if m:
        info["vcodec"] = m.group(1).lower()
    m = re.search(r"(\d{2,5})x(\d{2,5})", err)
    if m:
        info["width"], info["height"] = int(m.group(1)), int(m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*fps", err)
    if m:
        info["fps"] = float(m.group(1))
    m = re.search(r"Audio:\s*([a-z0-9_]+)", err, re.I)
    if m:
        info["acodec"] = m.group(1).lower()
    info["has_audio"] = "acodec" in info
    return info


# ---------------------------------------------------------------------------
# Encoding plan
# ---------------------------------------------------------------------------
def _rating(bpp: float) -> tuple[str, str]:
    if bpp >= 0.10:
        return "excellent", "Visually indistinguishable from the original."
    if bpp >= 0.06:
        return "great", "Effectively transparent quality for social content."
    if bpp >= 0.035:
        return "good", "Very good — only fast motion may soften slightly."
    if bpp >= 0.02:
        return "fair", "Noticeable softness in busy scenes."
    return "low", "Visible quality loss expected at this size."


def compute_plan(up: Upload, target_mb: float | None, preset: str = "none",
                 mode: str = "compress") -> dict[str, Any]:
    """Encoding plan for the three studio functions:
    compress  — two-pass, land on a target size (platform upload-ready)
    sync      — fix drift / high-bitrate lag (CFR + regenerated timestamps)
    repair    — rebuild a broken container (remux first, re-encode as fallback)
    """
    probe = up.probe or {}
    duration = float(probe.get("duration") or 0)
    width = int(probe.get("width") or 0)
    height = int(probe.get("height") or 0)
    fps = float(probe.get("fps") or 30)

    # Repair/sync work even when duration metadata is missing — that broken
    # metadata is what they fix. Only compression needs real duration math.
    if duration <= 0 and mode == "compress":
        raise ValueError(
            "This file's duration can't be read, so a target size can't be computed. "
            "Run 'Repair container' first, then compress the repaired file.")

    base = {
        "upload_id": up.id, "mode": mode, "preset": preset,
        "original_mb": round(up.size / 1048576, 2),
        "resolution": f"{width}×{height}" if width else "?",
        "duration": duration, "warnings": [],
    }

    # ---- Fix A/V sync & drift ---------------------------------------------
    if mode == "sync":
        return {**base,
                "platform_label": "Sync & drift repair",
                "estimated_mb": round(up.size / 1048576 * 0.95, 2),
                "video_kbps": int((probe.get("bitrate_kbps") or 0) * 0.9),
                "audio_kbps": 192 if probe.get("has_audio", True) else 0,
                "rating": "great",
                "rating_note": ("Re-encodes with constant frame rate and rebuilt timestamps — "
                                "fixes videos that play slower than their audio. Quality kept "
                                "at CRF 18 (visually lossless); size usually similar or smaller."),
                "two_pass": False}

    # ---- Repair container ---------------------------------------------------
    if mode == "repair":
        return {**base,
                "platform_label": "Container repair",
                "estimated_mb": round(up.size / 1048576, 2),
                "video_kbps": probe.get("bitrate_kbps") or 0,
                "audio_kbps": 0,
                "rating": "excellent",
                "rating_note": ("Rebuilds the MP4 container: fixes missing duration/time info and "
                                "broken indexes WITHOUT re-encoding when possible (100% original "
                                "quality). Falls back to a gentle re-encode only if streams are damaged."),
                "two_pass": False}

    # ---- Compress to target size --------------------------------------------
    spec = PLATFORM_SPECS.get(preset, PLATFORM_SPECS["none"])
    warnings: list[str] = []
    if target_mb is None:
        raise ValueError("Pick a target size to compress to.")
    target_bytes = target_mb * 1024 * 1024
    if target_bytes >= up.size:
        raise ValueError(
            f"Target must be smaller than the original ({up.size / 1048576:.1f} MB).")

    total_kbps = target_bytes * 8 / duration / 1000
    audio_kbps = spec["audio_kbps"]
    if not probe.get("has_audio", True):
        audio_kbps = 0
    video_kbps = total_kbps - audio_kbps - total_kbps * 0.02  # container overhead

    if video_kbps < 150:
        raise ValueError(
            "That target is too small for this video's length — raise the target size.")

    capped = False
    if spec["max_video_kbps"] and video_kbps > spec["max_video_kbps"]:
        video_kbps = spec["max_video_kbps"]
        capped = True
        warnings.append(
            f"Capped at {spec['label']}'s recommended ceiling — the output will be "
            "even smaller than your target, and the platform won't re-compress it.")

    video_kbps = int(round(video_kbps))
    est_bytes = int((video_kbps + audio_kbps) * 1000 / 8 * duration * 1.02)

    bpp = (video_kbps * 1000) / (width * height * fps) if width and height else 0
    rating, rating_note = _rating(bpp)
    if rating in ("fair", "low"):
        warnings.append(
            "Heads-up: squeezing this much into this size forces real compression. "
            "For a cleaner result, pick a larger target size.")

    return {**base,
            "platform_label": spec["label"],
            "target_mb": target_mb,
            "estimated_mb": round(est_bytes / 1048576, 2),
            "video_kbps": video_kbps,
            "audio_kbps": audio_kbps,
            "capped_by_platform": capped,
            "bits_per_pixel": round(bpp, 4),
            "rating": rating,
            "rating_note": rating_note,
            "warnings": warnings,
            "two_pass": True}
