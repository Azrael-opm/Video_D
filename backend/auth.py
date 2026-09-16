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
import time
from collections import defaultdict, deque

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
_WINDOWS: dict[str, deque] = defaultdict(deque)


def parse_limit(expr: str) -> tuple[int, float] | None:
    """'15/minute' -> (15, 60.0)"""
    m = re.fullmatch(r"(\d+)\s*/\s*(second|minute|hour|day)", expr or "")
    if not m:
        return None
    n = int(m.group(1))
    secs = {"second": 1, "minute": 60, "hour": 3600, "day": 86400}[m.group(2)]
    return n, float(secs)


def rate_allowed(ip: str, bucket: str, setting_key: str) -> bool:
    limit = parse_limit(db.get_setting(setting_key))
    if not limit:
        return True
    n, window = limit
    now = time.time()
    dq = _WINDOWS[f"{ip}|{bucket}"]
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= n:
        return False
    dq.append(now)
    return True


def client_ip(request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
