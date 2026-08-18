"""
NyxDownloader — FastAPI application (production build).

Public API
----------
GET  /health                 liveness probe
POST /api/analyze            inspect a URL -> metadata + quality options
POST /api/download           enqueue a download job
GET  /api/jobs/{id}          poll job status / progress
GET  /api/jobs/{id}/file     stream the finished file to the client
POST /api/recode/upload      Recode Studio: upload a video
GET  /api/recode/plan        Recode Studio: encoding plan
POST /api/recode/start       Recode Studio: start two-pass encode

Admin API (X-Admin-Token bearer auth)
-------------------------------------
POST /api/admin/login|logout · GET /api/admin/me
GET  /api/admin/stats|series|health|abuse|jobs|audit|sessions|admins|settings
POST /api/admin/jobs/{id}/cancel|retry · DELETE /api/admin/jobs/{id}
POST /api/admin/purge · POST /api/admin/selftest
POST /api/admin/bans · DELETE /api/admin/bans/{ip}
PUT  /api/admin/settings · POST /api/admin/admins · DELETE /api/admin/admins/{u}
POST /api/admin/totp/setup|confirm · DELETE /api/admin/totp
GET  /api/admin/export/jobs.csv|deliveries.csv
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

import yt_dlp.version
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

import auth
import cloudsync
import config
import db
import extractors
import jobs as jobstore
import uploads as uploads_mod

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

# ---------------------------------------------------------------------------
# Rate limiting: slowapi carries a deliberately huge baseline; the REAL
# limits are the live, dashboard-editable ones enforced by auth.rate_allowed.
# ---------------------------------------------------------------------------
limiter = Limiter(key_func=auth.client_ip, storage_uri="memory://")
BASELINE = "100000/minute"


def _limit(request: Request, setting_key: str) -> None:
    ip = auth.client_ip(request)
    if db.is_banned(ip):
        raise HTTPException(status_code=403, detail="Banned.")
    if not auth.rate_allowed(ip, request.url.path, setting_key):
        raise HTTPException(status_code=429,
                            detail="Rate limit exceeded — please slow down.")


@asynccontextmanager
async def lifespan(_: FastAPI):
    cloudsync.restore()          # pull history back BEFORE the db initializes
    if not db.admins_list():
        db.admin_upsert("admin", auth.hash_password(config.ADMIN_PASSWORD), None)
    _restore_cookies()
    jobstore.start_cleanup_loop()
    cloudsync.start()
    yield


app = FastAPI(title="NyxDownloader API", version="2.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ban_and_cache(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and db.is_banned(auth.client_ip(request)):
        return Response("Banned", status_code=403)
    response = await call_next(request)
    if path.startswith("/api/"):
        # Job polling must NEVER be cached by a browser or reverse proxy —
        # a cached progress response freezes the UI mid-encode.
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    elif path in ("/", "/recode", "/admin") or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class AnalyzeRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2000)


class DownloadRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2000)
    mode: Literal["video", "audio", "image"]
    height: Optional[int] = Field(None, ge=144, le=2160)
    audio_format: Optional[Literal["mp3", "m4a"]] = None
    image_url: Optional[str] = Field(None, max_length=2000)
    direct_url: Optional[str] = Field(None, max_length=4000)
    title: Optional[str] = Field(None, max_length=300)


class PreviewRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=2000)


class RecodeStartRequest(BaseModel):
    upload_id: str = Field(..., min_length=8, max_length=64)
    target_mb: Optional[float] = Field(None, gt=0, le=10240)
    preset: Literal["none", "tiktok", "instagram", "youtube", "facebook", "snapchat"] = "none"
    mode: Literal["compress", "sync", "repair"] = "compress"
    fast: bool = True   # 1-pass ≈2× faster; 2-pass for exact-size precision


class AdminLogin(BaseModel):
    username: str = Field("admin", max_length=64)
    password: str = Field(..., min_length=1, max_length=200)
    code: Optional[str] = Field(None, max_length=8)


class BanRequest(BaseModel):
    ip: str = Field(..., min_length=7, max_length=45)
    reason: str = Field("", max_length=200)
    hours: Optional[float] = Field(None, gt=0, le=24 * 365)  # None = permanent


class AdminCreate(BaseModel):
    username: str = Field(..., min_length=2, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(..., min_length=8, max_length=200)


class SettingsUpdate(BaseModel):
    settings: dict[str, str]


class TotpConfirm(BaseModel):
    code: str = Field(..., min_length=6, max_length=8)


# ---------------------------------------------------------------------------
# Public endpoints
# ---------------------------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "ok", "ffmpeg": bool(config.FFMPEG_PATH),
            "version": "2.0.0", "build": config.BUILD_TAG,
            "ytdlp": yt_dlp.version.__version__}


@app.post("/api/analyze")
@limiter.limit(BASELINE)
def analyze(request: Request, body: AnalyzeRequest):
    _limit(request, "analyze_rate_limit")
    try:
        data = extractors.analyze(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return data


@app.post("/api/download")
@limiter.limit(BASELINE)
def download(request: Request, body: DownloadRequest):
    _limit(request, "download_rate_limit")
    try:
        url = extractors.normalize_media_url(extractors.validate_url(body.url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    if body.mode == "video" and not config.FFMPEG_PATH:
        raise HTTPException(status_code=503, detail="FFmpeg is not installed on the server.")
    if body.mode == "audio" and body.audio_format not in ("mp3", "m4a"):
        raise HTTPException(status_code=422, detail="audio_format must be 'mp3' or 'm4a'.")
    if body.mode == "image" and not (body.image_url or "").startswith("http"):
        raise HTTPException(status_code=422, detail="image mode requires image_url.")

    job = jobstore.create_job(url, body.mode, body.height, body.audio_format,
                              image_url=body.image_url, title_hint=body.title,
                              client_ip=auth.client_ip(request),
                              direct_url=body.direct_url)
    return job.snapshot()


@app.post("/api/preview")
@limiter.limit(BASELINE)
def preview(request: Request, body: PreviewRequest):
    _limit(request, "preview_rate_limit")
    try:
        url = extractors.normalize_media_url(extractors.validate_url(body.url))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    job = jobstore.create_job(url, "preview", None, None, client_ip=auth.client_ip(request))
    return job.snapshot()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    jid = re.sub(r"[^a-f0-9]", "", job_id)
    job = jobstore.get_job(jid)
    if job is not None:
        return job.snapshot()
    row = db.job_get(jid)          # finished long ago — answer from history
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown or expired job id.")
    row["status_url"] = f"/api/jobs/{jid}"
    return row


@app.get("/api/jobs/{job_id}/file")
def job_file(job_id: str):
    job = jobstore.get_job(re.sub(r"[^a-f0-9]", "", job_id))
    if job is None or job.status != "done":
        raise HTTPException(status_code=404, detail="File not ready or job expired.")
    path = jobstore._find_output(job.id)
    if path is None or not path.exists():
        raise HTTPException(status_code=410, detail="File expired and was cleaned up.")
    jobstore.note_delivered(job.id)
    db.record_delivery(job.id, job.filesize or path.stat().st_size)
    return FileResponse(path, filename=job.filename or path.name,
                        media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# Recode Studio — upload, plan, two-pass encode
# ---------------------------------------------------------------------------
@app.post("/api/recode/upload")
@limiter.limit(BASELINE)
async def recode_upload(request: Request, file: UploadFile = File(...)):
    _limit(request, "upload_rate_limit")
    if not config.FFMPEG_PATH:
        raise HTTPException(status_code=503, detail="FFmpeg is not installed on the server.")
    import uuid as _uuid

    max_mb = float(db.get_setting("max_upload_mb") or 800)
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in uploads_mod.MEDIA_EXTS:
        suffix = ".mp4"
    up_id = _uuid.uuid4().hex
    dest = config.TMP_DIR / f"{up_id}.src{suffix}"

    size = 0
    try:
        with open(dest, "wb") as fh:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > max_mb * 1024 * 1024:
                    raise HTTPException(status_code=413,
                                        detail=f"File exceeds the {int(max_mb)} MB upload limit.")
                fh.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    if size < 1024:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail="That file looks empty.")

    up = uploads_mod.register_upload(file.filename or f"video{suffix}", dest, size)
    # Note: files with unreadable duration are ACCEPTED — that's precisely the
    # "missing time info" case the Repair function exists to fix.
    return up.snapshot()


@app.get("/api/recode/plan")
def recode_plan(request: Request, upload_id: str, target_mb: Optional[float] = None,
                preset: str = "none", mode: str = "compress"):
    up = uploads_mod.get_upload(upload_id)
    if up is None:
        raise HTTPException(status_code=404, detail="Upload not found or expired.")
    if mode not in ("compress", "sync", "repair"):
        raise HTTPException(status_code=422, detail="mode must be compress, sync or repair.")
    try:
        return uploads_mod.compute_plan(up, target_mb, preset, mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@app.post("/api/recode/start")
@limiter.limit(BASELINE)
def recode_start(request: Request, body: RecodeStartRequest):
    _limit(request, "recode_rate_limit")
    if not config.FFMPEG_PATH:
        raise HTTPException(status_code=503, detail="FFmpeg is not installed on the server.")
    up = uploads_mod.get_upload(body.upload_id)
    if up is None:
        raise HTTPException(status_code=404, detail="Upload not found or expired — upload again.")
    if body.mode == "compress" and body.target_mb is None:
        raise HTTPException(status_code=422, detail="Compress mode needs a target_mb.")
    try:
        uploads_mod.compute_plan(up, body.target_mb, body.preset, body.mode)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    job = jobstore.create_upload_recode_job(up.path, up.filename, body.preset,
                                            body.target_mb, body.mode,
                                            client_ip=auth.client_ip(request),
                                            fast=body.fast)
    return job.snapshot()


@app.get("/api/recode/platforms")
def recode_platforms():
    return [{"id": k, "label": v["label"], "max_video_kbps": v["max_video_kbps"],
             "audio_kbps": v["audio_kbps"]}
            for k, v in uploads_mod.PLATFORM_SPECS.items()]


@app.get("/recode")
def recode_page():
    return FileResponse(FRONTEND_DIR / "recode.html")


# ===========================================================================
# Platform cookies (Instagram stories etc.) — managed from the admin console
# ===========================================================================
import base64 as _b64


def _cookies_map() -> dict[str, str]:
    """Per-platform raw cookie texts, stored as one base64 JSON blob."""
    raw = db.get_setting("cookies_map_b64")
    if not raw:
        # migrate the old single-domain row if present
        old = db.get_setting("cookies_b64")
        if old:
            try:
                text = _b64.b64decode(old).decode()
                dom = db.get_setting("cookies_domain") or "instagram.com"
                return {dom: text}
            except Exception:  # noqa: BLE001
                return {}
        return {}
    try:
        return json.loads(_b64.b64decode(raw).decode())
    except Exception:  # noqa: BLE001
        return {}


def _save_cookies_map(cmap: dict[str, str]) -> None:
    db.set_setting("cookies_map_b64",
                   _b64.b64encode(json.dumps(cmap).encode()).decode())
    # rebuild the combined cookies.txt from every stored platform
    lines = ["# Netscape HTTP Cookie File"]
    for dom, text in cmap.items():
        converted = _to_netscape(text, dom)
        lines.extend(l for l in converted.splitlines()
                     if l and not l.startswith("#"))
    config.COOKIES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config.COOKIES_FILE = str(config.COOKIES_PATH)
    db._settings_cache = (0.0, {})


def _restore_cookies() -> None:
    """On ephemeral hosts the cookies file dies with the container; restore it
    from the stored per-platform map so login-gated platforms keep working.
    On hosts whose *disk* is ephemeral too (Render free), fall back to
    COOKIES_B64 / COOKIES_TEXT env vars — env survives restarts."""
    if config.COOKIES_FILE and Path(config.COOKIES_FILE).exists():
        return
    cmap = _cookies_map()
    if cmap:
        _save_cookies_map(cmap)
        return
    env_text = _env_cookies_text()
    if env_text:
        dom = os.getenv("COOKIES_DOMAIN", "youtube.com")
        config.COOKIES_PATH.write_text(_to_netscape(env_text, dom), encoding="utf-8")
        config.COOKIES_FILE = str(config.COOKIES_PATH)
        return
    # Bundled default cookies (shipped with the repo) — re-applied on every
    # boot so ephemeral hosts keep them across redeploys. Admin edits win
    # while the DB lives; the seed only fills an empty cookie store.
    seed = Path(__file__).parent / "seed_cookies.txt"
    if seed.exists() and not _cookies_map():
        _save_cookies_map({"instagram.com": seed.read_text(encoding="utf-8")})


def _env_cookies_text() -> str | None:
    """Cookies supplied via env: base64 of a cookies file (safe for any shell)
    or a raw multi-line paste (Render's env editor keeps newlines)."""
    b64 = os.getenv("COOKIES_B64")
    if b64:
        try:
            return _b64.b64decode(b64).decode()
        except Exception:  # noqa: BLE001
            return None
    return os.getenv("COOKIES_TEXT") or None


COOKIE_DOMAINS = {
    "instagram.com": ".instagram.com",
    "x.com": ".x.com",
    "tiktok.com": ".tiktok.com",
    "facebook.com": ".facebook.com",
    "reddit.com": ".reddit.com",
    "snapchat.com": ".snapchat.com",
    "youtube.com": ".youtube.com",
}


def _to_netscape(text: str, domain: str = "instagram.com") -> str:
    """Accept Netscape cookies.txt as-is (multi-domain); convert a raw
    `a=1; b=2` paste into Netscape lines for the chosen platform."""
    text = text.strip()
    if not text:
        return text
    if text.startswith("# Netscape") or "\t" in text:
        return text
    dom = COOKIE_DOMAINS.get(domain, ".instagram.com")
    pairs = re.split(r"[;\n]+", text)
    lines = ["# Netscape HTTP Cookie File"]
    for p in pairs:
        p = p.strip()
        if not p or "=" not in p:
            continue
        name, _, value = p.partition("=")
        lines.append(f"{dom}\tTRUE\t/\tTRUE\t0\t{name.strip()}\t{value.strip()}")
    return "\n".join(lines) + "\n"


class CookiesPayload(BaseModel):
    content: str = Field(..., min_length=10, max_length=200_000)
    domain: str = "instagram.com"


@app.get("/api/admin/cookies")
def admin_cookies_status(request: Request):
    _actor(request)
    cmap = _cookies_map()
    return {"configured": bool(config.COOKIES_FILE),
            "domains": cmap,
            "count": len(cmap),
            "source": "env" if (os.getenv("COOKIES_FILE") or _env_cookies_text())
                      else ("dashboard" if config.COOKIES_FILE else "none")}


@app.post("/api/admin/cookies")
def admin_cookies_set(request: Request, body: CookiesPayload):
    actor = _actor(request)
    cmap = _cookies_map()
    cmap[body.domain] = body.content
    _save_cookies_map(cmap)
    db.audit(actor, "cookies_set", f"{body.domain} cookies stored (survive redeploys)")
    return {"ok": True, "count": len(cmap)}


@app.delete("/api/admin/cookies")
def admin_cookies_delete(request: Request, domain: str | None = None):
    """Remove one platform's cookies (or all when no domain is given)."""
    actor = _actor(request)
    cmap = _cookies_map()
    if domain:
        cmap.pop(domain, None)
        if cmap:
            _save_cookies_map(cmap)
        else:
            _clear_all_cookies()
    else:
        _clear_all_cookies()
    db.audit(actor, "cookies_removed", domain or "all platforms")
    return {"ok": True, "count": len(_cookies_map())}


def _clear_all_cookies() -> None:
    if not os.getenv("COOKIES_FILE"):
        config.COOKIES_FILE = None
    try:
        config.COOKIES_PATH.unlink(missing_ok=True)
    except OSError:
        pass
    db.q("DELETE FROM settings WHERE key IN ('cookies_map_b64','cookies_b64','cookies_domain')",
         (), fetch=False)
    db._settings_cache = (0.0, {})


# ===========================================================================
# ADMIN
# ===========================================================================
SESSION_TTL_HOURS = 12.0


def _actor(request: Request) -> str:
    token = request.headers.get("x-admin-token", "")
    user = db.session_user(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired admin session.")
    return user


@app.post("/api/admin/login")
@limiter.limit("5/minute")
def admin_login(request: Request, body: AdminLogin):
    rec = db.admin_get(body.username)
    if rec is None or not auth.verify_password(body.password, rec["password_hash"]):
        db.audit(body.username, "login_failed", f"from {auth.client_ip(request)}")
        raise HTTPException(status_code=401, detail="Wrong username or password.")
    if rec["totp_secret"]:
        if not body.code:
            return {"token": None, "totp_required": True}
        if not auth.totp_verify(rec["totp_secret"], body.code):
            db.audit(body.username, "login_failed", "bad 2FA code")
            raise HTTPException(status_code=401, detail="Invalid 2FA code.")
    token = secrets.token_urlsafe(24)
    db.session_create(token, body.username, SESSION_TTL_HOURS)
    db.audit(body.username, "login", f"from {auth.client_ip(request)}")
    return {"token": token, "totp_required": False}


@app.post("/api/admin/logout")
def admin_logout(request: Request):
    token = request.headers.get("x-admin-token", "")
    user = db.session_user(token)
    db.session_revoke(token)
    if user:
        db.audit(user, "logout")
    return {"ok": True}


@app.get("/api/admin/me")
def admin_me(request: Request):
    user = _actor(request)
    rec = db.admin_get(user)
    return {"username": user, "totp_enabled": bool(rec and rec["totp_secret"]),
            "session_ttl_hours": SESSION_TTL_HOURS}


# ---- overview -------------------------------------------------------------
@app.get("/api/admin/stats")
def admin_stats(request: Request):
    _actor(request)
    snap = jobstore.stats_snapshot()
    settings = db.all_settings()
    snap["config"] = {
        **{k: settings[k] for k in db.SETTING_DEFAULTS},
        "max_concurrent_downloads": config.MAX_CONCURRENT_DOWNLOADS,
        "ffmpeg": bool(config.FFMPEG_PATH),
        "build": config.BUILD_TAG,
        "cookies_configured": bool(config.COOKIES_FILE),
        "ytdlp_version": yt_dlp.version.__version__,
        "ytdlp_age_days": _ytdlp_age_days(),
    }
    snap["disk_cap_gb"] = float(settings.get("disk_cap_gb") or 0)
    return snap


def _ytdlp_age_days() -> Optional[int]:
    try:
        parts = yt_dlp.version.__version__.split(".")
        import datetime as _dt
        rel = _dt.date(int(parts[0]), int(parts[1]), int(parts[2]))
        return (_dt.date.today() - rel).days
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/admin/series")
def admin_series(request: Request, hours: int = 24):
    _actor(request)
    hours = max(1, min(hours, 24 * 7))
    return {"jobs": db.series_jobs(hours), "bytes": db.series_bytes(hours)}


@app.get("/api/admin/health")
def admin_health(request: Request):
    _actor(request)
    return {"platforms": db.platform_health(days=7),
            "selftest": db.health_latest(),
            "ytdlp_version": yt_dlp.version.__version__,
            "ytdlp_age_days": _ytdlp_age_days()}


# ---- abuse ----------------------------------------------------------------
@app.get("/api/admin/abuse")
def admin_abuse(request: Request):
    _actor(request)
    return {"top_consumers": db.top_consumers(),
            "bans": db.active_bans(),
            "failed": db.failed_feed()}


@app.post("/api/admin/bans")
def admin_ban(request: Request, body: BanRequest):
    actor = _actor(request)
    db.ban_ip(body.ip, body.reason, body.hours, actor)
    db.audit(actor, "ban", f"{body.ip} ({body.hours or '∞'}h) {body.reason}")
    return {"ok": True}


@app.delete("/api/admin/bans/{ip}")
def admin_unban(request: Request, ip: str):
    actor = _actor(request)
    db.unban_ip(ip)
    db.audit(actor, "unban", ip)
    return {"ok": True}


# ---- jobs -----------------------------------------------------------------
@app.get("/api/admin/jobs")
def admin_jobs(request: Request, limit: int = 500, status: Optional[str] = None,
               platform: Optional[str] = None):
    _actor(request)
    # Live jobs first (fresh progress), history fills the rest from SQLite.
    live = sorted(jobstore.JOBS.values(), key=lambda j: j.created_at, reverse=True)
    live_out = [j.snapshot() for j in live[:limit]]
    live_ids = {j.id for j in live}
    if len(live_out) < limit:
        for row in db.jobs_recent(limit=limit, status=status, platform=platform):
            if row["id"] not in live_ids:
                live_out.append(row)
    if status:
        live_out = [j for j in live_out if j.get("status") == status]
    if platform:
        live_out = [j for j in live_out if j.get("platform") == platform]
    return live_out[:limit]


@app.post("/api/admin/jobs/{job_id}/cancel")
def admin_cancel_job(request: Request, job_id: str):
    actor = _actor(request)
    jid = re.sub(r"[^a-f0-9]", "", job_id)
    if not jobstore.request_cancel(jid):
        raise HTTPException(status_code=404, detail="Job not running or unknown.")
    db.audit(actor, "cancel", jid)
    return {"ok": True}


@app.post("/api/admin/jobs/{job_id}/retry")
def admin_retry_job(request: Request, job_id: str):
    actor = _actor(request)
    jid = re.sub(r"[^a-f0-9]", "", job_id)
    live = jobstore.get_job(jid)
    row = live.snapshot() if live else db.job_get(jid)
    if row is None:
        raise HTTPException(status_code=404, detail="Unknown job id.")
    if row.get("mode") not in ("video", "audio", "image", "preview"):
        raise HTTPException(status_code=422, detail="Only download jobs can be retried.")
    job = jobstore.create_job(row.get("url") or "", row["mode"], row.get("height"),
                              row.get("audio_format"), image_url=row.get("image_url"),
                              title_hint=row.get("title"), client_ip=row.get("client_ip"))
    db.audit(actor, "retry", jid)
    return job.snapshot()


@app.delete("/api/admin/jobs/{job_id}")
def admin_delete_job(request: Request, job_id: str):
    actor = _actor(request)
    jid = re.sub(r"[^a-f0-9]", "", job_id)
    jobstore.delete_job(jid)
    db.job_delete(jid)
    db.audit(actor, "delete_job", jid)
    return {"ok": True}


@app.post("/api/admin/purge")
def admin_purge(request: Request):
    actor = _actor(request)
    removed = jobstore.purge_all()
    db.audit(actor, "purge_all", f"{removed} jobs")
    return {"ok": True, "removed": removed}


@app.post("/api/admin/jobs/clear")
def admin_jobs_clear(request: Request):
    """Mass-clear the jobs list: memory + DB records + their files."""
    actor = _actor(request)
    removed = jobstore.purge_all()
    db_rows = db.q("DELETE FROM jobs", (), fetch=False) or 0
    db.audit(actor, "jobs_cleared", f"{max(removed, db_rows)} jobs")
    return {"ok": True, "removed": max(removed, db_rows)}


@app.post("/api/admin/disk/clear")
def admin_disk_clear(request: Request):
    """Delete every staged file (uploads, outputs, logs). Job records stay."""
    actor = _actor(request)
    freed = jobstore.clear_disk()
    db.audit(actor, "disk_cleared", f"{freed} bytes freed")
    return {"ok": True, "freed_bytes": freed}


@app.post("/api/admin/data/reset")
def admin_data_reset(request: Request):
    """Full data reset: jobs, deliveries, audit, bans, health checks,
    lifetime counters AND staged files. Admins/sessions/settings survive."""
    actor = _actor(request)
    removed_tables = db.reset_data()
    freed = jobstore.clear_disk()
    jobstore.reset_memory()
    db.audit(actor, "data_reset", f"tables={removed_tables}, {freed} bytes freed")
    return {"ok": True, "tables": removed_tables, "freed_bytes": freed}


@app.post("/api/admin/settings/reset")
def admin_settings_reset(request: Request):
    """True reset-to-defaults: delete every stored override so the built-in
    defaults (and env vars) apply again."""
    actor = _actor(request)
    db.q("DELETE FROM settings", (), fetch=False)
    import db as _db
    _db._settings_cache = (0.0, {})
    db.audit(actor, "settings_reset")
    return {"ok": True, "settings": db.all_settings(), "defaults": db.SETTING_DEFAULTS}


# ---- self-test ------------------------------------------------------------
_TEST_URLS = {
    "tiktok": "https://www.tiktok.com/@scout2015/video/6718335390845095173",
    "tiktok_photo": "https://www.tiktok.com/@sigmaking6901/photo/7652474109607054624",
    "pinterest": "https://www.pinterest.com/pin/664281013778109217/",
    "pinterest_image": "https://www.pinterest.com/pin/945263409311092421/",
    "youtube": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
    "twitter": "https://x.com/Gist_of_wisdom/status/2064670093829644659",
    "snapchat": "https://www.snapchat.com/@snapchat/spotlight/W7_EDlXWTBiXAEEniNoMPwAAYZGttemVpd3drAZ5wAjlDAZ5wAgliAAAAAw",
    "reddit": "https://www.reddit.com/r/BuyItForLife/s/pLI2gLlgCT",
    "instagram": "https://www.instagram.com/reel/DJxQKbRNgNq/",
    "facebook": "https://www.facebook.com/FacebookDevelopers/videos/10152454700553553/",
}
_selftest_state = {"running": False}


def _run_selftest() -> None:
    try:
        for platform, url in _TEST_URLS.items():
            try:
                with ThreadPoolExecutor(max_workers=1) as ex:
                    fut = ex.submit(extractors.analyze, url)
                    info = fut.result(timeout=30)
                kinds = []
                if info.get("video_options"):
                    kinds.append("video")
                if info.get("image_options"):
                    kinds.append("images")
                if info.get("audio_options"):
                    kinds.append("audio")
                db.health_save(platform, True, "+".join(kinds) or "ok")
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)[:200]
                blocked = any(k in msg.lower() for k in
                              ("bot", "login", "cookies", "unauthorized", "datacenter", "blocked"))
                note = ("IP blocked / login-gated from this server — expected on datacenter IPs"
                        if blocked else msg)
                db.health_save(platform, not blocked, note)
    finally:
        _selftest_state["running"] = False


@app.post("/api/admin/selftest")
def admin_selftest(request: Request):
    actor = _actor(request)
    if _selftest_state["running"]:
        return {"started": False, "reason": "already running"}
    _selftest_state["running"] = True
    threading.Thread(target=_run_selftest, daemon=True).start()
    db.audit(actor, "selftest")
    return {"started": True}


# ---- settings -------------------------------------------------------------
@app.get("/api/admin/settings")
def admin_settings(request: Request):
    _actor(request)
    return {"settings": db.all_settings(), "defaults": db.SETTING_DEFAULTS}


@app.put("/api/admin/settings")
def admin_settings_update(request: Request, body: SettingsUpdate):
    actor = _actor(request)
    changed = []
    for key, value in body.settings.items():
        if key not in db.SETTING_DEFAULTS:
            continue
        db.set_setting(key, str(value).strip())
        changed.append(key)
    if changed:
        db.audit(actor, "settings", ", ".join(changed))
    return {"ok": True, "changed": changed}


# ---- security: audit / sessions / admins / 2FA -----------------------------
@app.get("/api/admin/audit")
def admin_audit(request: Request):
    _actor(request)
    return db.audit_recent(200)


@app.get("/api/admin/sessions")
def admin_sessions(request: Request):
    _actor(request)
    return db.sessions_active()


@app.delete("/api/admin/sessions/{token}")
def admin_session_revoke(request: Request, token: str):
    actor = _actor(request)
    db.session_revoke(token)
    db.audit(actor, "revoke_session", token[:8] + "…")
    return {"ok": True}


@app.get("/api/admin/admins")
def admin_admins(request: Request):
    _actor(request)
    return [{"username": a["username"], "totp_enabled": bool(a["totp_secret"]),
             "created_at": a["created_at"]} for a in db.admins_list()]


@app.post("/api/admin/admins")
def admin_create_admin(request: Request, body: AdminCreate):
    actor = _actor(request)
    if db.admin_get(body.username):
        raise HTTPException(status_code=409, detail="Username taken.")
    db.admin_upsert(body.username, auth.hash_password(body.password), None)
    db.audit(actor, "admin_created", body.username)
    return {"ok": True}


@app.delete("/api/admin/admins/{username}")
def admin_delete_admin(request: Request, username: str):
    actor = _actor(request)
    if username == actor:
        raise HTTPException(status_code=422, detail="You can't delete yourself.")
    if len(db.admins_list()) <= 1:
        raise HTTPException(status_code=422, detail="Can't delete the last admin.")
    db.admin_delete(username)
    db.audit(actor, "admin_deleted", username)
    return {"ok": True}


@app.post("/api/admin/totp/setup")
def admin_totp_setup(request: Request):
    actor = _actor(request)
    secret = auth.totp_new_secret()
    _pending_totp[actor] = secret   # held until a valid code confirms it
    return {"secret": secret, "otpauth": auth.otpauth_uri(secret, actor)}


_pending_totp: dict[str, str] = {}


@app.post("/api/admin/totp/confirm")
def admin_totp_confirm(request: Request, body: TotpConfirm):
    actor = _actor(request)
    secret = _pending_totp.get(actor)
    if not secret:
        raise HTTPException(status_code=400, detail="No 2FA setup in progress.")
    if not auth.totp_verify(secret, body.code):
        raise HTTPException(status_code=422, detail="Code didn't match — try again.")
    rec = db.admin_get(actor)
    db.admin_upsert(actor, rec["password_hash"], secret)
    _pending_totp.pop(actor, None)
    db.audit(actor, "2fa_enabled")
    return {"ok": True}


@app.delete("/api/admin/totp")
def admin_totp_disable(request: Request):
    actor = _actor(request)
    rec = db.admin_get(actor)
    if not rec:
        raise HTTPException(status_code=404, detail="No such admin.")
    db.admin_upsert(actor, rec["password_hash"], None)
    db.audit(actor, "2fa_disabled")
    return {"ok": True}


# ---- CSV export -------------------------------------------------------------
@app.get("/api/admin/export/jobs.csv")
def export_jobs(request: Request):
    _actor(request)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "platform", "mode", "status", "filesize",
                "client_ip", "url", "error"])
    for row in db.jobs_recent(limit=10000):
        w.writerow([row["id"], time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(row["created_at"])),
                    row["platform"], row["mode"], row["status"], row["filesize"] or "",
                    row["client_ip"] or "", row["url"], row["error"] or ""])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=nyx_jobs.csv"})


@app.get("/api/admin/export/deliveries.csv")
def export_deliveries(request: Request):
    _actor(request)
    rows = db.q("SELECT job_id, bytes, ts FROM deliveries ORDER BY ts DESC LIMIT 10000")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["job_id", "bytes", "ts_utc"])
    for r in rows:
        w.writerow([r["job_id"], r["bytes"],
                    time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["ts"]))])
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=nyx_deliveries.csv"})


# ---------------------------------------------------------------------------
# Pages + static frontend — mount LAST (catch-all)
# ---------------------------------------------------------------------------
@app.get("/admin")
def admin_page():
    return FileResponse(FRONTEND_DIR / "admin.html")


app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
