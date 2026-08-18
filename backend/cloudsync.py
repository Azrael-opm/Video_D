"""
Optional zero-signup cloud persistence.

Mirrors the SQLite file to a dedicated orphan branch of a GitHub repo so
ephemeral hosts (Render / Railway free) keep jobs, audit, bans, settings and
sessions across restarts and redeploys. No new accounts — reuses a GitHub PAT.

Env:
  SYNC_TOKEN   GitHub PAT with repo scope (required to enable)
  SYNC_REPO    owner/repo   (default Azrael-opm/Video_D)
  SYNC_BRANCH  branch name  (default nyx-data)

Boot: if the local DB is empty, the snapshot is pulled down before the app
serves. Writes flip a dirty flag; a background thread re-uploads ~2 minutes
after the last write. Every upload is an ORPHAN commit force-pointed at the
branch, so the branch never grows history (one commit forever).
"""
from __future__ import annotations

import base64
import os
import sqlite3
import threading
import time
from pathlib import Path

import httpx

import config
import db

REPO = os.getenv("SYNC_REPO", "Azrael-opm/Video_D")
BRANCH = os.getenv("SYNC_BRANCH", "nyx-data")
TOKEN = os.getenv("SYNC_TOKEN") or None
API = "https://api.github.com"

_lock = threading.Lock()
_last_sync = 0.0


def _enabled() -> bool:
    # Turso (live remote DB) supersedes the GitHub mirror when configured.
    return bool(TOKEN) and not db.TURSO_URL


def _api(path: str, method: str = "GET", payload: dict | None = None) -> httpx.Response:
    return httpx.request(
        method, f"{API}{path}", json=payload,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "NyxDownloader-cloudsync"},
        timeout=30, follow_redirects=True)


def _local_db_empty() -> bool:
    if not db.DB_PATH.exists():
        return True
    try:
        con = sqlite3.connect(str(db.DB_PATH))
        n = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        a = con.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        con.close()
        return (n + a) == 0
    except Exception:  # noqa: BLE001
        return True


def restore() -> None:
    """Pull the snapshot down at boot when the local DB has no history."""
    if not _enabled() or not _local_db_empty():
        return
    try:
        r = _api(f"/repos/{REPO}/contents/nyx.sqlite3?ref={BRANCH}")
        if r.status_code != 200:
            return
        data = base64.b64decode(r.json().get("content", ""))
        if len(data) < 4096:
            return
        for suffix in ("", "-wal", "-shm"):
            (Path(str(db.DB_PATH) + suffix)).unlink(missing_ok=True)
        db.DB_PATH.write_bytes(data)
        print(f"[cloudsync] restored {len(data)} bytes of history from {REPO}@{BRANCH}")
    except Exception as exc:  # noqa: BLE001 — never block boot
        print(f"[cloudsync] restore skipped ({exc})")


def snapshot() -> bool:
    """Checkpoint the WAL and push the DB file as a single orphan commit."""
    if not _enabled():
        return False
    with _lock:
        try:
            con = sqlite3.connect(str(db.DB_PATH))
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
            blob = db.DB_PATH.read_bytes()
            b = _api(f"/repos/{REPO}/git/blobs", "POST",
                     {"content": base64.b64encode(blob).decode(),
                      "encoding": "base64"}).json()
            t = _api(f"/repos/{REPO}/git/trees", "POST",
                     {"tree": [{"path": "nyx.sqlite3", "mode": "100644",
                                "type": "blob", "sha": b["sha"]}]}).json()
            c = _api(f"/repos/{REPO}/git/commits", "POST",
                     {"message": "nyx data snapshot", "tree": t["sha"],
                      "parents": []}).json()
            ref = _api(f"/repos/{REPO}/git/refs/heads/{BRANCH}")
            if ref.status_code == 404:
                _api(f"/repos/{REPO}/git/refs", "POST",
                     {"ref": f"refs/heads/{BRANCH}", "sha": c["sha"]})
            else:
                _api(f"/repos/{REPO}/git/refs/heads/{BRANCH}", "PATCH",
                     {"sha": c["sha"], "force": True})
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[cloudsync] snapshot failed ({exc})")
            return False


def _loop() -> None:
    global _last_sync
    while True:
        time.sleep(30)
        if not _enabled() or not db.DIRTY:
            continue
        if time.time() - _last_sync < 120:
            continue
        if snapshot():
            db.DIRTY = False
            _last_sync = time.time()


def start() -> None:
    if not _enabled():
        return
    threading.Thread(target=_loop, daemon=True, name="cloudsync").start()
    print(f"[cloudsync] enabled — mirroring history to {REPO}@{BRANCH}")
