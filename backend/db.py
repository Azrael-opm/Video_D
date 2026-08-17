"""
NyxDownloader — SQLite persistence layer.

Stores job history, deliveries, IP bans, audit trail, admin sessions/accounts
and live-editable settings. Local SQLite by default (zero-setup, single
instance). On ephemeral hosts (Render/Railway free) set TURSO_URL +
TURSO_TOKEN to persist everything in a remote Turso (libsql) database that
survives restarts and redeploys — every host shares the same history then.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

DB_PATH = Path(os.getenv("NYX_DB", str(Path(__file__).parent / "nyx.sqlite3")))

TURSO_URL = os.getenv("TURSO_URL") or None
TURSO_TOKEN = os.getenv("TURSO_TOKEN") or None

_LOCK = threading.Lock()
_conn: sqlite3.Connection | None = None
_remote: Any = None
_remote_failed = False

SETTINGS_CACHE_TTL = 5.0
_settings_cache: tuple[float, dict[str, str]] = (0.0, {})

# Defaults used when no override is stored. Kept in sync with config.py env.
SETTING_DEFAULTS: dict[str, str] = {
    "analyze_rate_limit": "15/minute",
    "download_rate_limit": "8/hour",
    "preview_rate_limit": "30/hour",
    "recode_rate_limit": "12/hour",
    "upload_rate_limit": "10/hour",
    "job_ttl_seconds": "1800",
    "max_download_mb": "2048",
    "max_upload_mb": "800",
    "preview_height": "480",
    "disk_cap_gb": "10",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, url TEXT, mode TEXT, platform TEXT,
  status TEXT, progress REAL, filesize INTEGER, filename TEXT,
  error TEXT, preset TEXT, target_mb REAL, height INTEGER, audio_format TEXT,
  image_url TEXT, title TEXT, client_ip TEXT,
  created_at REAL, finished_at REAL, cancel_requested INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT, bytes INTEGER, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_deliveries_ts ON deliveries(ts);

CREATE TABLE IF NOT EXISTS bans (
  ip TEXT PRIMARY KEY, reason TEXT, created_at REAL, expires_at REAL, created_by TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, detail TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY, username TEXT, created_at REAL, expires_at REAL
);

CREATE TABLE IF NOT EXISTS admins (
  username TEXT PRIMARY KEY, password_hash TEXT, totp_secret TEXT, created_at REAL
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS health_checks (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, platform TEXT, ok INTEGER, note TEXT
);
"""


def _c() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
    return _conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight schema migrations for existing databases."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "error_log" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN error_log TEXT")


def _remote_conn():
    """Remote libsql (Turso) connection; None when unavailable (→ local)."""
    global _remote, _remote_failed
    if _remote is not None or _remote_failed:
        return _remote
    try:
        try:
            import libsql_experimental as lib
        except ImportError:
            import libsql as lib  # type: ignore
        _remote = lib.connect(TURSO_URL, auth_token=TURSO_TOKEN)
        for stmt in (s for s in SCHEMA.split(";") if s.strip()):
            _remote.execute(stmt)
        try:
            cols = [r[0] for r in
                    _remote.execute("PRAGMA table_info(jobs)").fetchall()]
        except Exception:  # noqa: BLE001
            cols = []
        if cols and "error_log" not in cols:
            _remote.execute("ALTER TABLE jobs ADD COLUMN error_log TEXT")
    except Exception as exc:  # noqa: BLE001
        print(f"[db] remote DB unavailable ({exc}); using local SQLite")
        _remote_failed = True
        _remote = None
    return _remote


def _rows(cur) -> list[dict]:
    if getattr(cur, "description", None) is None:
        return []
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def q(sql: str, args: tuple = (), fetch: bool = True) -> Any:
    with _LOCK:
        if TURSO_URL:
            con = _remote_conn()
            if con is not None:
                cur = con.execute(sql, args)
                try:
                    con.commit()
                except Exception:  # noqa: BLE001 — some drivers autocommit
                    pass
                return _rows(cur) if fetch else getattr(cur, "rowcount", 0)
        cur = _c().execute(sql, args)
        _c().commit()
        if fetch:
            return [dict(r) for r in cur.fetchall()]
        return cur.rowcount


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------
_JOB_FIELDS = ("id", "url", "mode", "platform", "status", "progress", "filesize",
               "filename", "error", "error_log", "preset", "target_mb", "height",
               "audio_format", "image_url", "title", "client_ip", "created_at",
               "finished_at")


def job_upsert(j: dict) -> None:
    row = {k: j.get(k) for k in _JOB_FIELDS}
    cols = ", ".join(_JOB_FIELDS)
    marks = ", ".join("?" * len(_JOB_FIELDS))
    updates = ", ".join(f"{f}=excluded.{f}" for f in _JOB_FIELDS if f != "id")
    q(f"INSERT INTO jobs ({cols}) VALUES ({marks}) "
      f"ON CONFLICT(id) DO UPDATE SET {updates}",
      tuple(row.values()), fetch=False)


def job_touch(job_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    q(f"UPDATE jobs SET {sets} WHERE id=?", (*fields.values(), job_id), fetch=False)


def job_get(job_id: str) -> Optional[dict]:
    rows = q("SELECT * FROM jobs WHERE id=?", (job_id,))
    return dict(rows[0]) if rows else None


def jobs_recent(limit: int = 500, status: str | None = None,
                platform: str | None = None) -> list[dict]:
    sql = "SELECT * FROM jobs"
    where, args = [], []
    if status:
        where.append("status=?"); args.append(status)
    if platform:
        where.append("platform=?"); args.append(platform)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in q(sql, tuple(args))]


def job_delete(job_id: str) -> None:
    q("DELETE FROM jobs WHERE id=?", (job_id,), fetch=False)
    q("DELETE FROM deliveries WHERE job_id=?", (job_id,), fetch=False)


def record_delivery(job_id: str, nbytes: int) -> None:
    q("INSERT INTO deliveries (job_id, bytes, ts) VALUES (?,?,?)",
      (job_id, nbytes, time.time()), fetch=False)


def interrupted_jobs() -> list[str]:
    """Jobs that were running when the server last went down."""
    rows = q("SELECT id FROM jobs WHERE status IN ('queued','downloading','processing')")
    ids = [r["id"] for r in rows]
    if ids:
        q("UPDATE jobs SET status='error', error='Server restarted mid-job', "
          "finished_at=? WHERE status IN ('queued','downloading','processing')",
          (time.time(),), fetch=False)
    return ids


# ---------------------------------------------------------------------------
# Time series & analytics
# ---------------------------------------------------------------------------
def series_jobs(hours: int = 24) -> list[dict]:
    since = time.time() - hours * 3600
    rows = q("""
        SELECT CAST(created_at/3600 AS INTEGER) AS hr,
               COUNT(*) AS total,
               SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
               SUM(CASE WHEN status IN ('error','cancelled') THEN 1 ELSE 0 END) AS failed
        FROM jobs WHERE created_at >= ? GROUP BY hr ORDER BY hr""", (since,))
    return [dict(r) for r in rows]


def series_bytes(hours: int = 24) -> list[dict]:
    since = time.time() - hours * 3600
    rows = q("""
        SELECT CAST(ts/3600 AS INTEGER) AS hr, SUM(bytes) AS bytes
        FROM deliveries WHERE ts >= ? GROUP BY hr ORDER BY hr""", (since,))
    return [dict(r) for r in rows]


def totals() -> dict:
    r = q("""SELECT COUNT(*) AS total,
               SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
               SUM(CASE WHEN status IN ('error','cancelled') THEN 1 ELSE 0 END) AS failed
             FROM jobs""")[0]
    b = q("SELECT COALESCE(SUM(bytes),0) AS bytes FROM deliveries")[0]
    return {"jobs_total": r["total"] or 0, "jobs_done": r["done"] or 0,
            "jobs_failed": r["failed"] or 0, "bytes_delivered": b["bytes"] or 0}


def platform_health(days: int = 7) -> list[dict]:
    since = time.time() - days * 86400
    rows = q("""
        SELECT platform, COUNT(*) AS total,
               SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
               SUM(CASE WHEN status IN ('error','cancelled') THEN 1 ELSE 0 END) AS failed
        FROM jobs WHERE created_at >= ? AND mode != 'upload_recode'
        GROUP BY platform ORDER BY total DESC""", (since,))
    out = []
    for r in rows:
        errs = q("""SELECT error, COUNT(*) AS n FROM jobs
                    WHERE platform=? AND status='error' AND created_at>=?
                    GROUP BY error ORDER BY n DESC LIMIT 3""",
                 (r["platform"], since))
        out.append({**dict(r), "top_errors": [{"error": e["error"], "n": e["n"]} for e in errs]})
    return out


def top_consumers(days: int = 7, limit: int = 15) -> list[dict]:
    since = time.time() - days * 86400
    rows = q("""
        SELECT j.client_ip AS ip, COUNT(*) AS jobs,
               COALESCE(SUM(d.bytes), 0) AS bytes
        FROM jobs j LEFT JOIN deliveries d ON d.job_id = j.id
        WHERE j.created_at >= ? AND j.client_ip IS NOT NULL
        GROUP BY j.client_ip ORDER BY jobs DESC LIMIT ?""", (since, limit))
    return [dict(r) for r in rows]


def failed_feed(limit: int = 25) -> list[dict]:
    rows = q("""SELECT created_at, platform, mode, error, client_ip, url
                FROM jobs WHERE status='error' ORDER BY created_at DESC LIMIT ?""",
             (limit,))
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Bans
# ---------------------------------------------------------------------------
def ban_ip(ip: str, reason: str, hours: Optional[float], by: str) -> None:
    expires = time.time() + hours * 3600 if hours else None
    q("INSERT INTO bans (ip, reason, created_at, expires_at, created_by) VALUES (?,?,?,?,?) "
      "ON CONFLICT(ip) DO UPDATE SET reason=excluded.reason, created_at=excluded.created_at, "
      "expires_at=excluded.expires_at, created_by=excluded.created_by",
      (ip, reason, time.time(), expires, by), fetch=False)


def unban_ip(ip: str) -> None:
    q("DELETE FROM bans WHERE ip=?", (ip,), fetch=False)


def active_bans() -> list[dict]:
    now = time.time()
    q("DELETE FROM bans WHERE expires_at IS NOT NULL AND expires_at < ?", (now,), fetch=False)
    return [dict(r) for r in q("SELECT * FROM bans ORDER BY created_at DESC")]


def is_banned(ip: str) -> bool:
    rows = q("SELECT expires_at FROM bans WHERE ip=?", (ip,))
    if not rows:
        return False
    exp = rows[0]["expires_at"]
    return exp is None or exp > time.time()


# ---------------------------------------------------------------------------
# Audit / sessions / admins / settings
# ---------------------------------------------------------------------------
def audit(actor: str, action: str, detail: str = "") -> None:
    q("INSERT INTO audit (ts, actor, action, detail) VALUES (?,?,?,?)",
      (time.time(), actor, action, detail[:500]), fetch=False)


def audit_recent(limit: int = 100) -> list[dict]:
    return [dict(r) for r in
            q("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))]


def session_create(token: str, username: str, ttl_hours: float = 12) -> None:
    q("INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?,?,?,?)",
      (token, username, time.time(), time.time() + ttl_hours * 3600), fetch=False)


def session_user(token: str) -> Optional[str]:
    rows = q("SELECT username, expires_at FROM sessions WHERE token=?", (token,))
    if not rows:
        return None
    if rows[0]["expires_at"] < time.time():
        q("DELETE FROM sessions WHERE token=?", (token,), fetch=False)
        return None
    return rows[0]["username"]


def session_revoke(token: str) -> None:
    q("DELETE FROM sessions WHERE token=?", (token,), fetch=False)


def sessions_active() -> list[dict]:
    q("DELETE FROM sessions WHERE expires_at < ?", (time.time(),), fetch=False)
    return [dict(r) for r in q("SELECT * FROM sessions ORDER BY created_at DESC")]


def admin_get(username: str) -> Optional[dict]:
    rows = q("SELECT * FROM admins WHERE username=?", (username,))
    return dict(rows[0]) if rows else None


def admin_upsert(username: str, password_hash: str, totp_secret: Optional[str]) -> None:
    q("INSERT INTO admins (username, password_hash, totp_secret, created_at) VALUES (?,?,?,?) "
      "ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash, "
      "totp_secret=excluded.totp_secret",
      (username, password_hash, totp_secret, time.time()), fetch=False)


def admin_delete(username: str) -> None:
    q("DELETE FROM admins WHERE username=?", (username,), fetch=False)
    q("DELETE FROM sessions WHERE username=?", (username,), fetch=False)


def admins_list() -> list[dict]:
    return [dict(r) for r in q("SELECT * FROM admins ORDER BY created_at")]


def set_setting(key: str, value: str) -> None:
    global _settings_cache
    q("INSERT INTO settings (key, value) VALUES (?,?) "
      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value), fetch=False)
    _settings_cache = (0.0, {})   # invalidate so reads see the write immediately


def all_settings() -> dict[str, str]:
    global _settings_cache
    now = time.time()
    ts, cache = _settings_cache
    if now - ts < SETTINGS_CACHE_TTL:
        return cache
    merged = dict(SETTING_DEFAULTS)
    for r in q("SELECT key, value FROM settings"):
        merged[r["key"]] = r["value"]
    _settings_cache = (now, merged)
    return merged


def get_setting(key: str) -> str:
    return all_settings().get(key, SETTING_DEFAULTS.get(key, ""))


def health_save(platform: str, ok: bool, note: str) -> None:
    q("INSERT INTO health_checks (ts, platform, ok, note) VALUES (?,?,?,?)",
      (time.time(), platform, 1 if ok else 0, note[:300]), fetch=False)


def health_latest() -> list[dict]:
    rows = q("""SELECT h.* FROM health_checks h
                JOIN (SELECT platform, MAX(ts) AS m FROM health_checks GROUP BY platform) t
                ON t.platform = h.platform AND t.m = h.ts ORDER BY h.platform""")
    return [dict(r) for r in rows]


def reset_data() -> dict[str, int]:
    """Wipe all operational data (jobs, deliveries, audit, bans, health checks).
    Admins, sessions and settings are preserved. Returns row counts removed."""
    removed = {}
    for table in ("jobs", "deliveries", "audit", "bans", "health_checks"):
        removed[table] = q(f"DELETE FROM {table}", (), fetch=False) or 0
    return removed
