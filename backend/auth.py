"""
NyxDownloader — auth primitives: PBKDF2 password hashing, RFC-6238 TOTP,
and a settings-driven dynamic rate guard (overrides slowapi's static limits
when you edit limits in the dashboard).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import struct
import threading
import time
from collections import deque

import db

# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return f"pbkdf2$200000${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iters, salt, hexdigest = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), hexdigest)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# TOTP (RFC 6238) — no external dependency
# ---------------------------------------------------------------------------
def totp_new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _hotp(key: bytes, counter: int) -> str:
    msg = struct.pack(">Q", counter)
    h = hmac.new(key, msg, hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{code:06d}"


def totp_code(secret_b32: str, offset: int = 0) -> str:
    key = base64.b32decode(secret_b32)
    return _hotp(key, int(time.time() // 30) + offset)


def totp_verify(secret_b32: str, code: str) -> bool:
    code = re.sub(r"\s+", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return False
    return any(hmac.compare_digest(code, totp_code(secret_b32, off))
               for off in (-1, 0, 1))


def otpauth_uri(secret_b32: str, username: str) -> str:
    return f"otpauth://totp/NyxDownloader:{username}?secret={secret_b32}&issuer=NyxDownloader"


# ---------------------------------------------------------------------------
# Dynamic rate guard — sliding windows per (ip, bucket). Limits come from the
# live settings table so dashboard edits apply immediately.
# ---------------------------------------------------------------------------
_WINDOWS: dict[tuple[str, str], deque[float]] = {}
_WINDOWS_LOCK = threading.Lock()
_LAST_SWEEP = 0.0


def parse_limit(expr: str) -> tuple[int, float] | None:
    """'15/minute' -> (15, 60.0)"""
    m = re.fullmatch(r"(\d+)\s*/\s*(second|minute|hour|day)", expr or "")
    if not m:
        return None
    n = int(m.group(1))
    secs = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[m.group(2)]
    return n, float(secs)


DEFAULT_RATE_LIMITS = {
    "analyze_rate_limit": "15/minute",
    "download_rate_limit": "8/hour",
    "preview_rate_limit": "30/hour",
    "recode_rate_limit": "12/hour",
    "upload_rate_limit": "10/hour",
}


def rate_allowed(ip: str, bucket: str, setting_key: str) -> bool:
    global _LAST_SWEEP
    limit = parse_limit(db.get_setting(setting_key))
    if not limit or not 1 <= limit[0] <= 1_000_000:
        limit = parse_limit(DEFAULT_RATE_LIMITS.get(setting_key, "15/minute"))
    if not limit:
        return True
    n, window = limit
    now = time.monotonic()
    key = (ip, bucket)
    with _WINDOWS_LOCK:
        dq = _WINDOWS.get(key, deque())
        while dq and now - dq[0] >= window:
            dq.popleft()
        allowed = len(dq) < n
        if allowed:
            dq.append(now)
            _WINDOWS[key] = dq
        elif dq:
            _WINDOWS[key] = dq
        else:
            _WINDOWS.pop(key, None)

        # Drop idle client keys periodically so long-lived public services do
        # not retain one deque for every IP that has ever visited.
        if now - _LAST_SWEEP > 300:
            cutoff = now - 86400
            for old_key, events in tuple(_WINDOWS.items()):
                if not events or events[-1] < cutoff:
                    _WINDOWS.pop(old_key, None)
            _LAST_SWEEP = now
        return allowed


def client_ip(request) -> str:
    # Uvicorn's ProxyHeadersMiddleware rewrites request.client only for peers
    # in FORWARDED_ALLOW_IPS. Never trust X-Forwarded-For directly: a public
    # caller can forge it to evade bans and per-IP rate limits.
    client = getattr(request, "client", None)
    host = getattr(client, "host", None)
    return str(host) if host else "unknown"
