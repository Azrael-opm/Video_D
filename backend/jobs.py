"""
NyxDownloader — background job engine.

Downloads run in worker threads guarded by a concurrency semaphore.
Progress is reported through yt-dlp hooks and polled by the frontend via
GET /api/jobs/{id}. Finished files are streamed to the client and garbage
collected after JOB_TTL_SECONDS.

For a multi-instance production deployment, swap the in-memory store below
for Redis (the Job dataclass is already JSON-serialisable).
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

import config
import db
from extractors import (base_opts, detect_platform, format_selector_for_audio,
                        format_selector_for_height, friendly_fetch_error,
                        yt_gated_error, YT_ALT_CLIENTS)

STARTED_AT = time.time()
# Changes on every server boot — the frontend uses it to detect a restart
# that killed an in-flight job (otherwise the UI would poll forever).
BOOT_ID = uuid.uuid4().hex[:12]


def _encode_threads() -> str:
    """1 thread on tiny hosts (Render free = 512 MB): decoder frame buffers
    scale with resolution × threads, so 4K HEVC would OOM a small box."""
    try:
        for line in open("/proc/meminfo"):
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return "1" if kb < 1_000_000 else "2"
    except OSError:
        pass
    return "2"


ENC_THREADS = _encode_threads()


class JobCancelled(Exception):
    """Raised inside hooks to abort a job the admin cancelled."""

# ---------------------------------------------------------------------------
# Lifetime statistics (served to the admin dashboard)
# ---------------------------------------------------------------------------
STATS_LOCK = threading.Lock()
STATS: dict[str, Any] = {
    "jobs_total": 0,
    "jobs_done": 0,
    "jobs_failed": 0,
    "bytes_delivered": 0,
    "by_mode": {},
    "by_platform": {},
}


def _stat_inc(key: str, sub: str | None = None, n: int = 1) -> None:
    with STATS_LOCK:
        if sub is None:
            STATS[key] = STATS.get(key, 0) + n
        else:
            STATS.setdefault(key, {})
            STATS[key][sub] = STATS[key].get(sub, 0) + n


@dataclass
class Job:
    id: str
    url: str
    mode: str                        # "video" | "audio" | "image" | "preview" | "upload_recode"
    height: Optional[int] = None
    audio_format: Optional[str] = None
    image_url: Optional[str] = None  # image mode / slideshow composite
    direct_url: Optional[str] = None  # direct .mp4 (X syndication fallback)
    title_hint: Optional[str] = None
    platform: str = "generic"
    preset: Optional[str] = None     # recode preset key
    source_path: Optional[str] = None  # recode input file
    target_mb: Optional[float] = None  # upload-recode target size
    recode_mode: Optional[str] = None  # compress | sync | repair
    status: str = "queued"           # queued|downloading|processing|done|error
    progress: float = 0.0            # 0–100
    speed: Optional[float] = None    # bytes/sec
    eta: Optional[float] = None      # seconds
    title: Optional[str] = None
    filename: Optional[str] = None   # user-facing name
    filesize: Optional[int] = None
    error: Optional[str] = None
    error_log: Optional[str] = None  # ffmpeg tail / diagnostics for failures
    client_ip: Optional[str] = None
    cancel_requested: bool = False
    created_at: float = field(default_factory=time.time)

    def snapshot(self) -> dict[str, Any]:
        d = asdict(self)
        d["status_url"] = f"/api/jobs/{self.id}"
        d["boot_id"] = BOOT_ID
        if self.status == "done":
            d["download_url"] = f"/api/jobs/{self.id}/file"
        return d


JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()
_SEM = threading.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)
_JUNK_EXTS = {".part", ".ytdl", ".tmp", ".log"}


# ---------------------------------------------------------------------------
def create_job(url: str, mode: str, height: int | None, audio_format: str | None,
               image_url: str | None = None, title_hint: str | None = None,
               preset: str | None = None, source_path: str | None = None,
               client_ip: str | None = None, direct_url: str | None = None) -> Job:
    job = Job(id=uuid.uuid4().hex, url=url, mode=mode, height=height,
              audio_format=audio_format, image_url=image_url, title_hint=title_hint,
              platform=detect_platform(url) if url else "local",
              preset=preset, source_path=source_path, client_ip=client_ip,
              direct_url=direct_url)
    if title_hint:
        job.title = title_hint
    with _LOCK:
        JOBS[job.id] = job
    _persist(job)
    _stat_inc("jobs_total")
    _stat_inc("by_mode", mode)
    _stat_inc("by_platform", job.platform)
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return job


def _persist(job: Job) -> None:
    """Mirror a job into SQLite (history survives restarts)."""
    try:
        db.job_upsert({
            "id": job.id, "url": job.url, "mode": job.mode, "platform": job.platform,
            "status": job.status, "progress": job.progress, "filesize": job.filesize,
            "filename": job.filename, "error": job.error, "preset": job.preset,
            "target_mb": job.target_mb, "height": job.height,
            "audio_format": job.audio_format, "image_url": job.image_url,
            "title": job.title, "client_ip": job.client_ip,
            "created_at": job.created_at,
            "finished_at": time.time() if job.status in ("done", "error", "cancelled") else None,
        })
    except Exception:  # noqa: BLE001 — persistence must never break downloads
        pass


def request_cancel(job_id: str) -> bool:
    job = get_job(job_id)
    if job is None or job.status in ("done", "error", "cancelled"):
        return False
    job.cancel_requested = True
    db.job_touch(job_id, cancel_requested=1)
    return True


def create_upload_recode_job(source_path: str, filename: str,
                             preset: str, target_mb: float,
                             recode_mode: str = "compress",
                             client_ip: str | None = None) -> Job:
    """Recode Studio: compress / fix sync / repair an uploaded file."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    title = re.sub(r"[^\w\-. ]+", "", stem).strip()[:80] or "video"
    job = Job(id=uuid.uuid4().hex, url="upload://" + filename, mode="upload_recode",
              platform="upload", preset=preset, source_path=source_path,
              target_mb=target_mb, title_hint=title, recode_mode=recode_mode,
              client_ip=client_ip)
    job.title = title
    job.status = "processing"
    with _LOCK:
        JOBS[job.id] = job
    _persist(job)
    _stat_inc("jobs_total")
    _stat_inc("by_mode", "upload_recode")
    _stat_inc("by_platform", "upload")
    threading.Thread(target=_run, args=(job,), daemon=True).start()
    return job


def get_job(job_id: str) -> Job | None:
    with _LOCK:
        return JOBS.get(job_id)


def purge_expired() -> int:
    """Housekeeping ONLY: enforce the disk cap.

    Job RECORDS are never auto-deleted — the admin decides what goes. The only
    automatic eviction is staged FILES when usage exceeds the configured disk
    cap (set it to 0 to disable even that; records always survive).
    """
    return _enforce_disk_cap()


def _enforce_disk_cap() -> int:
    """Disk watchdog: when usage passes the configured cap, delete the oldest
    finished FILES (never records) until we're back under it. Admins see the
    freed space in the audit log."""
    try:
        cap_bytes = float(db.get_setting("disk_cap_gb") or 0) * 1024 ** 3
        if cap_bytes <= 0:
            return 0
        used, _ = disk_usage()
        if used <= cap_bytes:
            return 0
        with _LOCK:
            finished = sorted((j for j in JOBS.values()
                               if j.status in ("done", "error", "cancelled")),
                              key=lambda j: j.created_at)
        freed = 0
        for job in finished:
            if used <= cap_bytes * 0.9:
                break
            _delete_files(job.id)   # files only — the DB record stays
            delta = job.filesize or 0
            used = max(0, used - delta)
            freed += delta
        return freed
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
def _capture_error_log(job: Job) -> None:
    """Attach the tail of this job's ffmpeg log (if any) so failures can be
    diagnosed from the admin dashboard / user error reports."""
    p = config.TMP_DIR / f"{job.id}.ffmpeg.log"
    if p.exists():
        try:
            job.error_log = p.read_bytes()[-2000:].decode(errors="replace")
        except OSError:
            pass


def _run(job: Job) -> None:
    try:
        with _SEM:
            _download(job)
    except JobCancelled:
        job.status, job.error = "cancelled", "Cancelled by admin."
        _stat_inc("jobs_failed")
        _persist(job)
    except DownloadError as exc:
        job.status, job.error = "error", friendly_fetch_error(
            job.platform or "", job.url or "", str(exc))
        _capture_error_log(job)
        _stat_inc("jobs_failed")
        _persist(job)
    except Exception as exc:  # noqa: BLE001 — never kill the worker silently
        job.status, job.error = "error", f"Unexpected error: {exc}"
        _capture_error_log(job)
        _stat_inc("jobs_failed")
        _persist(job)


def _check_cancel(job: Job) -> None:
    if job.cancel_requested:
        raise JobCancelled()


def _download(job: Job) -> None:
    # ---- direct .mp4 URL (X/Twitter syndication fallback) ------------------
    if job.direct_url:
        path = _download_direct(job)
    # ---- Recode Studio: uploaded file → size-targeted two-pass encode -----
    elif job.mode == "upload_recode":
        path = _upload_recode(job)
    # ---- direct image download (no yt-dlp needed) -------------------------
    elif job.mode == "image":
        path = _download_image(job)
    else:
        path = _download_with_ytdlp(job)

    if job.mode == "preview":
        job.status = "processing"
        path = _make_preview_file(job, path)

    # ---- image + music → playable slideshow video -------------------------
    elif job.mode == "video" and job.image_url:
        job.status = "processing"
        path = _compose_slideshow(job, path)



    ext = path.suffix.lstrip(".")
    safe_title = re.sub(r"[^\w\-. ]+", "", (job.title or "media")).strip()[:80] or "media"
    if job.mode == "upload_recode":
        suffix = {"compress": "compressed", "sync": "sync fixed",
                  "repair": "repaired"}.get(job.recode_mode or "compress", "recode")
        safe_title += f" ({suffix})"
    job.filename = f"{safe_title}.{ext}"
    job.filesize = path.stat().st_size
    job.progress = 100.0
    job.status = "done"
    _stat_inc("jobs_done")
    _persist(job)


def _download_with_ytdlp(job: Job) -> Path:
    outtmpl = str(config.TMP_DIR / f"{job.id}.%(ext)s")
    opts = base_opts(skip_download=False)
    opts.update({
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "progress_hooks": [_progress_hook(job)],
        "postprocessor_hooks": [_pp_hook(job)],
        "max_filesize": int(float(db.get_setting("max_download_mb")) * 1024 * 1024),
        "concurrent_fragment_downloads": 4,   # faster DASH/HLS fetches
        "noprogress": True,
    })

    if job.mode == "audio":
        opts["format"] = format_selector_for_audio(job.audio_format or "mp3")
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": job.audio_format or "mp3",
            "preferredquality": "0",           # ~320 kbps VBR for MP3
        }]
    elif job.mode == "preview":
        # Best CLEAN rendition (watermarked ones carry format_note=watermarked),
        # transcoded down to PREVIEW_HEIGHT afterwards. Best-clean avoids any
        # watermarked fallback stream on routes that expose one.
        opts["format"] = ("b[format_note!=watermarked]"
                          "/bv*+ba[format_note!=watermarked]"
                          "/wv*+wa/w/b")
    elif job.mode == "video" and job.image_url:
        # Slideshow composite: we only need the post's AUDIO track here.
        opts["format"] = "bestaudio/best"
    else:
        opts["format"] = format_selector_for_height(job.height or 1080)

    job.status = "downloading"
    # YouTube bot-checks gate some player clients but not others — when a
    # gated error hits, transparently retry the download via alternates.
    clients = [None] + (YT_ALT_CLIENTS if job.platform == "youtube" else [])
    last_exc: Optional[DownloadError] = None
    for client in clients:
        o = opts
        if client:
            o = {**opts, "extractor_args": {**opts.get("extractor_args", {}),
                                            "youtube": {"player_client": [client]}}}
        try:
            with YoutubeDL(o) as ydl:
                ydl.download([job.url])
            last_exc = None
            break
        except DownloadError as exc:
            if job.platform != "youtube" or not yt_gated_error(str(exc)):
                raise
            last_exc = exc
    if last_exc is not None:
        raise last_exc

    path = _find_output(job.id)
    if path is None:
        raise RuntimeError("yt-dlp finished but no output file was produced "
                           "(the file may have exceeded the size cap).")
    return path


def _download_image(job: Job) -> Path:
    """Stream a single image (highest-quality rendition chosen at analyze time)."""
    import httpx
    from extractors import HTTP_HEADERS, referer_for

    url = job.image_url or ""
    if not url.startswith("http"):
        raise RuntimeError("No image URL available for this post.")

    headers = dict(HTTP_HEADERS)
    ref = referer_for(url)
    if ref:
        headers["Referer"] = ref

    ext = "jpg"
    m = re.search(r"\.(jpe?g|png|webp)(?:[?#]|$)", url, re.I)
    if m:
        ext = "jpg" if m.group(1).lower() in ("jpg", "jpeg") else m.group(1).lower()

    job.status = "downloading"
    dest = config.TMP_DIR / f"{job.id}.{ext}"
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            if "png" in ctype:
                ext = "png"
            elif "webp" in ctype:
                ext = "webp"
            elif "jpeg" in ctype or "jpg" in ctype:
                ext = "jpg"
            dest = dest.with_suffix("." + ext)
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(65536):
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        job.progress = min(99.0, done / total * 100)
    job.progress = 99.0
    return dest


def _download_direct(job: Job) -> Path:
    """Download a direct .mp4 URL (X/Twitter syndication CDN). In audio mode
    the mp4 is downloaded then the audio track is extracted via FFmpeg."""
    import httpx
    from extractors import HTTP_HEADERS

    job.status = "downloading"
    dest = config.TMP_DIR / f"{job.id}.mp4"
    with httpx.Client(headers=dict(HTTP_HEADERS), timeout=60,
                      follow_redirects=True) as client:
        with client.stream("GET", job.direct_url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length") or 0)
            done = 0
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes(65536):
                    fh.write(chunk)
                    done += len(chunk)
                    if total:
                        job.progress = min(99.0, done / total * 100)

    if job.mode == "audio":
        job.status = "processing"
        ext = "mp3" if (job.audio_format or "mp3") == "mp3" else "m4a"
        out = config.TMP_DIR / f"{job.id}.{ext}"
        cmd = [config.FFMPEG_PATH, "-y", "-i", str(dest), "-vn"]
        cmd += (["-c:a", "libmp3lame", "-q:a", "0"] if ext == "mp3"
                else ["-c:a", "aac", "-b:a", "192k"])
        cmd.append(str(out))
        subprocess.run(cmd, capture_output=True, check=True, timeout=300)
        dest.unlink(missing_ok=True)
        return out
    return dest


def _compose_slideshow(job: Job, audio_path: Path) -> Path:
    """Turn (image + downloaded music track) into a playable MP4 slideshow."""
    if not config.FFMPEG_PATH:
        raise RuntimeError("FFmpeg is required to combine an image with its music.")
    import httpx
    from extractors import HTTP_HEADERS, referer_for

    # 1) fetch the image next to the audio
    img_path = config.TMP_DIR / f"{job.id}.img"
    headers = dict(HTTP_HEADERS)
    ref = referer_for(job.image_url)
    if ref:
        headers["Referer"] = ref
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        resp = client.get(job.image_url)
        resp.raise_for_status()
        ctype = resp.headers.get("content-type", "")
        img_ext = ".png" if "png" in ctype else (".webp" if "webp" in ctype else ".jpg")
        img_path = img_path.with_suffix(img_ext)
        img_path.write_bytes(resp.content)

    # 2) mux: still image looped for the length of the audio.
    # Output goes to a temp name first — the audio file itself may already
    # be {job.id}.mp4 (TikTok serves combined MP4s), and ffmpeg refuses to
    # read and write the same path.
    tmp_out = config.TMP_DIR / f"{job.id}.slide.mp4"
    cmd = [
        config.FFMPEG_PATH, "-y",
        "-loop", "1", "-i", str(img_path),
        "-i", str(audio_path),
        "-vf", "scale='min(iw,1920)':-2,format=yuv420p",
        "-c:v", "libx264", "-tune", "stillimage", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest", "-movflags", "+faststart",
        str(tmp_out),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=300)
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode(errors="replace")[-280:]
        raise RuntimeError(f"Slideshow composition failed: {tail.strip()}") from exc
    finally:
        img_path.unlink(missing_ok=True)

    out = config.TMP_DIR / f"{job.id}.mp4"
    audio_path.unlink(missing_ok=True)
    tmp_out.replace(out)
    return out


def _ffmpeg_with_progress(cmd: list, job: Job, duration: float,
                          lo: float, hi: float) -> None:
    """Run an ffmpeg encode, streaming `-progress pipe:1` into job.progress.

    Cross-platform design: a reader thread pushes stdout lines into a queue;
    the main loop consumes with a timeout. (select() only works on sockets
    under Windows — WinError 10038 — so it is deliberately not used.)
    stderr goes to a FILE, never a pipe: a chatty/corrupted input can flood
    ffmpeg's log, and an undrained pipe deadlocks the encoder. A stall
    watchdog kills ffmpeg if it goes silent for 2 minutes.
    """
    import queue

    err_path = config.TMP_DIR / f"{job.id}.ffmpeg.log"
    err_fh = open(err_path, "wb")
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_fh,
                                cwd=str(config.TMP_DIR))
        lines: "queue.Queue" = queue.Queue()

        def _reader() -> None:
            try:
                while True:
                    raw = proc.stdout.readline()
                    if not raw:
                        break
                    lines.put(raw)
            except ValueError:
                pass                       # pipe closed during shutdown
            finally:
                lines.put(None)            # EOF sentinel

        threading.Thread(target=_reader, daemon=True).start()

        silent_seconds = 0
        while True:
            if job.cancel_requested:
                proc.terminate()
                raise JobCancelled()
            try:
                raw = lines.get(timeout=15)
            except queue.Empty:
                if proc.poll() is not None:
                    break                  # finished between checks
                silent_seconds += 15
                if silent_seconds >= 120:
                    proc.kill()
                    raise RuntimeError(
                        "FFmpeg stalled for 2 minutes with no output — "
                        "this file may be too damaged to process.")
                continue
            if raw is None:
                break
            silent_seconds = 0
            line = raw.decode(errors="replace").strip()
            if line.startswith("out_time_us="):
                try:
                    secs = int(line.split("=", 1)[1]) / 1_000_000
                    if duration:
                        job.progress = lo + min(hi - lo, secs / duration * (hi - lo))
                except ValueError:
                    pass
        proc.wait()
    finally:
        err_fh.close()
    if proc.returncode != 0:
        try:
            tail = err_path.read_bytes()[-600:].decode(errors="replace")
        except OSError:
            tail = ""
        rc = proc.returncode
        note = ""
        if rc in (137, -9, 247):
            note = (" | NOTE: exit code 137 = the process was KILLED, almost always out-of-memory "
                    "on a small host. Try again, use a shorter clip, or raise the service's memory "
                    "limit (Railway: Settings → memory).")
        raise RuntimeError(f"FFmpeg failed (exit {rc}){note}: {tail.strip()[-300:]}")


def _upload_recode(job: Job) -> Path:
    """Recode Studio worker — three functions:

    compress : two-pass x264 targeting an exact file size (platform-ready)
    sync     : rebuilds timestamps (-fflags +genpts) and forces a constant
               frame rate (-fps_mode cfr) — fixes videos that play slower than
               their audio because of broken VFR timing / runaway bitrate
    repair   : rewrites the MP4 container without re-encoding when possible
               (fixes missing duration / corrupted indexes at 100% original
               quality); falls back to a gentle re-encode for damaged streams
    """
    import os
    import uploads as uploads_mod

    if not config.FFMPEG_PATH:
        raise RuntimeError("FFmpeg is required for recoding.")
    src = Path(job.source_path) if job.source_path else None
    if src is None or not src.exists():
        raise RuntimeError("Uploaded file expired — upload it again.")

    up = uploads_mod.get_upload_from_path(str(src))
    if up is None:
        raise RuntimeError("Upload record not found.")
    mode = job.recode_mode or "compress"
    plan = uploads_mod.compute_plan(up, job.target_mb, job.preset or "none", mode)
    duration = plan["duration"]
    out = config.TMP_DIR / f"{job.id}.mp4"
    common_in = ["-fflags", "+genpts", "-err_detect", "ignore_err", "-i", str(src)]
    job.progress = 5.0
    job.status = "processing"
    _check_cancel(job)

    # ---- repair: stream-copy remux first, re-encode only if forced --------
    if mode == "repair":
        job.progress = 20.0
        cmd = [config.FFMPEG_PATH, "-y", *common_in,
               "-map", "0", "-c", "copy", "-movflags", "+faststart",
               str(out)]
        res = subprocess.run(cmd, capture_output=True, cwd=str(config.TMP_DIR), timeout=3600)
        if res.returncode == 0 and out.exists() and out.stat().st_size > 1024:
            job.progress = 99.0
            return out
        out.unlink(missing_ok=True)
        # damaged streams → gentle re-encode as fallback
        job.progress = 30.0
        cmd = [config.FFMPEG_PATH, "-y", *common_in,
               "-map_metadata", "-1",
               "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
               "-preset", "veryfast", "-threads", ENC_THREADS, "-crf", "16", "-fps_mode", "cfr",
               "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
               "-movflags", "+faststart", "-progress", "pipe:1", "-nostats",
               str(out)]
        _ffmpeg_with_progress(cmd, job, duration, 30.0, 98.0)
        if not out.exists():
            raise RuntimeError("Repair failed on this file.")
        job.progress = 99.0
        return out

    # ---- sync: quality-preserving CFR rebuild ------------------------------
    if mode == "sync":
        abr = 192 if (up.probe or {}).get("has_audio", True) else 0
        cmd = [config.FFMPEG_PATH, "-y", *common_in,
               "-map_metadata", "-1",
               "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
               "-preset", "veryfast", "-threads", ENC_THREADS, "-crf", "18", "-fps_mode", "cfr",
               "-movflags", "+faststart", "-progress", "pipe:1", "-nostats"]
        cmd += ["-c:a", "aac", "-b:a", f"{abr}k", "-ar", "48000"] if abr else ["-an"]
        cmd.append(str(out))
        _ffmpeg_with_progress(cmd, job, duration, 5.0, 98.0)
        if not out.exists():
            raise RuntimeError("Sync repair failed on this file.")
        job.progress = 99.0
        return out

    # ---- compress: two-pass size-targeted encode ---------------------------
    vbr, abr = plan["video_kbps"], plan["audio_kbps"]
    passlog = str(config.TMP_DIR / f"{job.id}.ffpass")

    # Pass 1 also streams progress — no silent 5% wait on long files.
    # VBV settings must match pass 2 or x264's two-pass ratecontrol breaks.
    cmd1 = [config.FFMPEG_PATH, "-y", *common_in,
            "-c:v", "libx264", "-b:v", f"{vbr}k",
            "-maxrate", f"{int(vbr * 1.3)}k", "-bufsize", f"{int(vbr * 2.6)}k",
            "-preset", "veryfast", "-threads", ENC_THREADS,
            "-fps_mode", "cfr", "-pass", "1", "-passlogfile", passlog,
            "-an", "-progress", "pipe:1", "-nostats", "-f", "null", os.devnull]
    _ffmpeg_with_progress(cmd1, job, duration, 5.0, 40.0)
    job.progress = 40.0
    _check_cancel(job)

    cmd2 = [config.FFMPEG_PATH, "-y", *common_in,
            "-map_metadata", "-1",
            "-c:v", "libx264", "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-b:v", f"{vbr}k", "-maxrate", f"{int(vbr * 1.3)}k",
            "-bufsize", f"{int(vbr * 2.6)}k",
            "-preset", "veryfast", "-threads", ENC_THREADS, "-pass", "2", "-passlogfile", passlog,
            "-fps_mode", "cfr",
            "-movflags", "+faststart", "-progress", "pipe:1", "-nostats"]
    if abr:
        cmd2 += ["-c:a", "aac", "-b:a", f"{abr}k", "-ar", "48000"]
    else:
        cmd2 += ["-an"]
    cmd2.append(str(out))

    try:
        _ffmpeg_with_progress(cmd2, job, duration, 40.0, 98.0)
    finally:
        for suffix in ("-0.log", "-0.log.mbtree"):
            (config.TMP_DIR / f"{job.id}.ffpass{suffix}").unlink(missing_ok=True)
    if not out.exists():
        raise RuntimeError("Compression produced no output file.")
    job.progress = 99.0
    return out


def _make_preview_file(job: Job, src: Path) -> Path:
    """Transcode a downloaded file into a small, universally playable
    PREVIEW_HEIGHT MP4 for the inline player. Falls back gracefully for
    audio-only media (or when FFmpeg is unavailable)."""
    if not config.FFMPEG_PATH:
        return src

    preview_h = int(db.get_setting("preview_height") or config.PREVIEW_HEIGHT)
    dst = config.TMP_DIR / f"{job.id}.preview.mp4"
    cmd = [
        config.FFMPEG_PATH, "-y", "-i", str(src),
        "-vf", f"scale=-2:{preview_h}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(dst),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                       stderr=subprocess.PIPE, timeout=180)
        src.unlink(missing_ok=True)
        final = config.TMP_DIR / f"{job.id}.mp4"
        dst.replace(final)
        return final
    except subprocess.CalledProcessError:
        # Most likely audio-only media (no video stream to scale).
        dst.unlink(missing_ok=True)
        if src.suffix.lower() in (".mp3", ".m4a", ".aac", ".mp4"):
            return src
        mp3 = config.TMP_DIR / f"{job.id}.mp3"
        try:
            subprocess.run(
                [config.FFMPEG_PATH, "-y", "-i", str(src), "-vn",
                 "-c:a", "libmp3lame", "-q:a", "4", str(mp3)],
                check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=120)
            src.unlink(missing_ok=True)
            return mp3
        except Exception:  # noqa: BLE001 — keep whatever we have
            mp3.unlink(missing_ok=True)
            return src
    except Exception:  # noqa: BLE001
        dst.unlink(missing_ok=True)
        return src


def _progress_hook(job: Job):
    def hook(d: dict) -> None:
        _check_cancel(job)
        info = d.get("info_dict") or {}
        if info.get("title") and not job.title:
            job.title = info["title"]
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                job.progress = min(99.0, done / total * 100)
            job.speed = d.get("speed")
            job.eta = d.get("eta")
            job.status = "downloading"
        elif status == "finished":
            job.progress = 99.0
    return hook


def _pp_hook(job: Job):
    def hook(d: dict) -> None:
        if d.get("postprocessor") in ("ExtractAudio", "Merger", "MoveFiles", "VideoConvert"):
            job.status = "processing"
    return hook


# ---------------------------------------------------------------------------
def _find_output(job_id: str) -> Path | None:
    candidates = [p for p in config.TMP_DIR.glob(f"{job_id}.*")
                  if p.suffix.lower() not in _JUNK_EXTS and p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _delete_files(job_id: str) -> None:
    for p in config.TMP_DIR.glob(f"{job_id}.*"):
        try:
            p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Admin helpers
# ---------------------------------------------------------------------------
def note_delivered(job_id: str) -> None:
    job = get_job(job_id)
    if job and job.filesize:
        _stat_inc("bytes_delivered", n=job.filesize)


def delete_job(job_id: str) -> bool:
    with _LOCK:
        job = JOBS.pop(job_id, None)
    if job is None:
        return False
    _delete_files(job_id)
    return True


def purge_all() -> int:
    with _LOCK:
        ids = list(JOBS.keys())
        JOBS.clear()
    for jid in ids:
        _delete_files(jid)
    return len(ids)


def clear_disk() -> int:
    """Delete every staged file in the downloads dir (uploads, outputs, logs).
    Returns bytes freed. Job records in the DB are left intact."""
    freed = 0
    try:
        for p in config.TMP_DIR.iterdir():
            if p.is_file():
                try:
                    freed += p.stat().st_size
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass
    return freed


def reset_memory() -> None:
    """Wipe in-memory jobs and lifetime counters (used by data reset)."""
    global STATS
    with _LOCK:
        JOBS.clear()
    with STATS_LOCK:
        for k in ("jobs_total", "jobs_done", "jobs_failed", "bytes_delivered"):
            STATS[k] = 0
        STATS["by_mode"] = {}
        STATS["by_platform"] = {}


def disk_usage() -> tuple[int, int]:
    total, count = 0, 0
    try:
        for p in config.TMP_DIR.iterdir():
            if p.is_file():
                total += p.stat().st_size
                count += 1
    except OSError:
        pass
    return total, count


def stats_snapshot() -> dict[str, Any]:
    with _LOCK:
        active = sum(1 for j in JOBS.values()
                     if j.status in ("queued", "downloading", "processing"))
        stored = len(JOBS)
        snap = {k: (dict(v) if isinstance(v, dict) else v) for k, v in STATS.items()}
    # Prefer persistent totals when the DB has history (survives restarts).
    try:
        t = db.totals()
        if t["jobs_total"] > snap.get("jobs_total", 0):
            snap.update({k: t[k] for k in
                         ("jobs_total", "jobs_done", "jobs_failed", "bytes_delivered")})
    except Exception:  # noqa: BLE001
        pass
    used, files = disk_usage()
    snap.update({
        "uptime_seconds": int(time.time() - STARTED_AT),
        "jobs_active": active,
        "jobs_stored": stored,
        "workers_in_use": config.MAX_CONCURRENT_DOWNLOADS - _SEM._value,
        "queue_depth": max(0, active - (config.MAX_CONCURRENT_DOWNLOADS - _SEM._value)),
        "disk_bytes": used,
        "disk_files": files,
    })
    return snap


# ---------------------------------------------------------------------------
def start_cleanup_loop() -> None:
    """Daemon thread that purges expired jobs and uploads every 5 minutes."""
    try:
        db.interrupted_jobs()   # mark jobs that died with a previous server
    except Exception:  # noqa: BLE001
        pass

    def loop() -> None:
        while True:
            time.sleep(300)
            try:
                purge_expired()
                from uploads import purge_expired_uploads
                purge_expired_uploads()
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=loop, daemon=True).start()
