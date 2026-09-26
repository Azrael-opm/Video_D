"""
NyxDownloader — platform detection & media analysis.

All heavy lifting is delegated to yt-dlp, which natively supports every
target platform (TikTok, Instagram, Snapchat, Facebook, X/Twitter, Reddit,
YouTube) plus thousands more.

Watermark notes
---------------
* TikTok   — yt-dlp's TikTok extractor returns the clean (watermark-free)
             rendition by default; it is tried with stored cookies FIRST (a
             logged-in session passes the bot gate that stops anonymous
             datacenter traffic). If yt-dlp is still gated: tikdownloader.io
             serves direct CDN urls from its own fetch infrastructure, and
             tikwm (rate-limited per egress IP) is the last resort.
* Others   — Reels, Shorts, tweets, Reddit/FB/Snap videos are served as
             the original uploaded file, so there is no watermark to strip.
"""
from __future__ import annotations

import html as html_mod
import json
import random
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlsplit, urlunsplit

import httpx
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError, ExtractorError

import config

HTTP_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}

# Referer hosts that image CDNs expect, keyed by URL fragment.
REFERER_HOSTS = {
    "pinimg.com": "https://www.pinterest.com/",
    "twimg.com": "https://x.com/",
    "cdninstagram.com": "https://www.instagram.com/",
    "fbcdn.net": "https://www.facebook.com/",
    "fbsbx.com": "https://www.facebook.com/",
    "redd.it": "https://www.reddit.com/",
    "redditmedia.com": "https://www.reddit.com/",
    "ytimg.com": "https://www.youtube.com/",
    "tiktokcdn": "https://www.tiktok.com/",
    "tiktokv.com": "https://www.tiktok.com/",
    "ibytedtos.com": "https://www.tiktok.com/",
    "sncdn.com": "https://www.snapchat.com/",
    "sc-cdn.net": "https://www.snapchat.com/",
}


def referer_for(url: str) -> str | None:
    for frag, ref in REFERER_HOSTS.items():
        if frag in url:
            return ref
    return None

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
PLATFORM_PATTERNS: dict[str, re.Pattern] = {
    "tiktok":    re.compile(r"(tiktok\.com|vm\.tiktok|vt\.tiktok)", re.I),
    "instagram": re.compile(r"instagram\.com|instagr\.am", re.I),
    "snapchat":  re.compile(r"snapchat\.com|t\.snapchat\.com", re.I),
    "facebook":  re.compile(r"facebook\.com|fb\.watch|fb\.com|fb\.video", re.I),
    "twitter":   re.compile(r"(^|\.)((twitter|x)\.com|t\.co)$", re.I),
    "reddit":    re.compile(r"reddit\.com|redd\.it", re.I),
    "pinterest": re.compile(r"pinterest\.[a-z.]+|pin\.it", re.I),
    "youtube":   re.compile(r"youtube\.com|youtu\.be", re.I),
}

PLATFORM_META = {
    "tiktok":    {"name": "TikTok",    "color": "#000000"},
    "instagram": {"name": "Instagram", "color": "#E1306C"},
    "snapchat":  {"name": "Snapchat",  "color": "#FFFC00"},
    "facebook":  {"name": "Facebook",  "color": "#1877F2"},
    "twitter":   {"name": "X (Twitter)", "color": "#000000"},
    "reddit":    {"name": "Reddit",    "color": "#FF4500"},
    "pinterest": {"name": "Pinterest", "color": "#E60023"},
    "youtube":   {"name": "YouTube",   "color": "#FF0000"},
    "generic":   {"name": "Web",       "color": "#6e6e73"},
}


def detect_platform(url: str) -> str:
    host = urlparse(url if "://" in url else f"https://{url}").netloc
    for platform, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(host):
            return platform
    return "generic"


def validate_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("URL must start with http:// or https://")
    parsed = urlparse(url)
    if not parsed.netloc or "." not in parsed.netloc:
        raise ValueError("That doesn't look like a valid URL.")
    return url


def _strip_ansi(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# Platforms that have a cookie box in Admin → Security (cookies genuinely help here).
COOKIE_CAPABLE_PLATFORMS = {"tiktok", "instagram", "facebook", "reddit",
                            "snapchat", "twitter", "youtube"}


def friendly_fetch_error(platform: str, url: str, raw: str) -> str:
    """Turn a raw extractor failure into an accurate, platform-aware message.

    Cookies are only suggested when the failure is actually auth-related *and*
    the platform has a cookie box in Admin → Security — public content on
    cookie-free platforms must never blame cookies."""
    raw = _strip_ansi(raw)
    low = raw.lower()
    if platform == "instagram" and "/stories/" in url:
        return ("Instagram stories are only viewable while logged in — that's Instagram's "
                "rule for everyone, not a bug. (Operators: Instagram cookies lift this — "
                "Admin → Security or COOKIES_FILE.)")
    if platform == "youtube" and ("bot" in low or "sign in" in low):
        return ("YouTube is bot-checking this server's IP — a temporary host-side gate, "
                "the video itself is public and fine. Try again in a little while. "
                "(Operators: YouTube cookies lift this — Admin → Security or COOKIES_TEXT.)")
    if platform == "youtube" and "age" in low and ("restrict" in low or "confirm" in low):
        return ("This YouTube video is age-restricted — YouTube only serves it to "
                "signed-in accounts. (Operators: YouTube cookies unlock it — "
                "Admin → Security.)")
    if platform == "instagram":
        return ("Instagram didn't serve this post anonymously (some posts and most server "
                "networks get login-gated). (Operators: Instagram cookies lift this — "
                "Admin → Security.)")
    if platform == "reddit" and ("403" in raw or "blocked" in low):
        return ("Reddit is blocking this server's network outright (datacenter IPs). "
                "(Operators: Reddit cookies or residential hosting lift this.)")
    if platform == "tiktok":
        return ("TikTok is bot-checking this server right now — usually temporary, "
                "try again in a minute. (Operators: TikTok cookies in Admin → "
                "Security lift the gate.)")
    loginish = any(k in low for k in ("login", "sign in", "cookies", "private"))
    if loginish:
        if platform in COOKIE_CAPABLE_PLATFORMS:
            return ("This media is private or requires a login to access. Paste your "
                    "cookies in Admin → Security (or set COOKIES_FILE) to unlock it.")
        return "This media is private or requires a login to access."
    if "unsupported url" in low:
        return "This URL isn't supported. Check that it's a direct link to the post/video."
    return f"Couldn't fetch this media: {raw.splitlines()[0][:300]}"


# Alternate YouTube player clients, tried in order when the default client hits
# YouTube's bot-check. The check hits some clients (web/mweb hardest) while
# others (tv_embedded, android) usually still answer from datacenter IPs.
# Merged sets matter: a single gated client silently degrades quality (android
# alone = 360p), while a merged set returns every rendition the IP can reach.
# Gating flips minute-by-minute, so the primary set carries every client that
# has ever answered anonymously (tv_embedded is the current 4K workhorse).
YT_PRIMARY_CLIENTS = "tv,web,android,mweb,tv_embedded,ios"
YT_ALT_CLIENTS = ["tv_embedded,android", "tv,mweb,android", "android"]


def yt_gated_error(raw: str) -> bool:
    """True when a yt-dlp YouTube failure looks like IP/client gating (bot
    check, po-token wall) rather than a genuinely private/deleted video."""
    low = raw.lower()
    return any(k in low for k in ("bot", "sign in", "no video formats",
                                  "unavailable", "403", "confirm", "drm"))


def youtube_client_ladder(url: str, with_cookies: bool = False) -> Optional[dict]:
    """Retry metadata extraction with alternate player clients; first success
    wins. Returns None when every client is gated."""
    for client in YT_ALT_CLIENTS:
        try:
            opts = base_opts(with_cookies=with_cookies)
            opts["extractor_args"] = {**opts.get("extractor_args", {}),
                                      "youtube": {"player_client": [client]}}
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(normalize_media_url(url), download=False)
            if info and (info.get("formats") or info.get("entries")):
                return info
        except Exception:
            continue
    return None


def _youtube_best_quality(info: dict, url: str) -> dict:
    """YouTube silently degrades to android's 360p when richer clients are
    gated — no error raised, just a poor format list. Escalate through the
    alternate merged sets and keep the best result."""
    def grade(d: dict) -> tuple[int, int]:
        hs = [f.get("height") or 0 for f in d.get("formats") or []]
        return (max(hs, default=0), len(hs))
    best, best_g = info, grade(info)
    if best_g[0] > 360:
        return best
    for clients in YT_ALT_CLIENTS:
        try:
            opts = base_opts()
            opts["extractor_args"] = {**opts.get("extractor_args", {}),
                                      "youtube": {"player_client": [clients]}}
            with YoutubeDL(opts) as ydl:
                alt = ydl.extract_info(normalize_media_url(url), download=False)
        except Exception:
            continue
        if alt:
            g = grade(alt)
            if g > best_g:
                best, best_g = alt, g
        if best_g[0] > 360:
            break
    return best


def _walk_video_lists(node: Any):
    """Yield every `video_list` dict found anywhere in a nested structure
    (Pinterest story pins nest media assets deep inside pages/blocks)."""
    if isinstance(node, dict):
        if isinstance(node.get("video_list"), dict):
            yield node["video_list"]
        for v in node.values():
            yield from _walk_video_lists(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_video_lists(v)


def _pinterest_fallback(url: str) -> Optional[dict]:
    """Pinterest image pins make PinterestIE bail with 'No video formats found'.
    Call the very same resource API yt-dlp uses and rebuild the info dict from
    the pin's image ladder (incl. the untouched `orig` rendition) plus any
    audio/video assets nested in story_pin_data (image+music pins)."""
    m = re.search(r"/pin/(?:[\w-]+--)?(\d+)", url)
    if not m:
        return None
    pin_id = m.group(1)
    try:
        resp = httpx.get(
            "https://www.pinterest.com/resource/PinResource/get/",
            params={"data": json.dumps({"options": {
                "id": pin_id, "field_set_key": "unauth_react_main_pin"}})},
            headers={**HTTP_HEADERS, "X-Pinterest-PWS-Handler": "www/[username].js"},
            timeout=15, follow_redirects=True)
        resp.raise_for_status()
        data = resp.json()["resource_response"]["data"] or {}
    except Exception:  # noqa: BLE001
        return None

    thumbnails: list[dict] = []
    images = data.get("images")
    if isinstance(images, dict):
        for t in images.values():
            if isinstance(t, dict) and t.get("url"):
                thumbnails.append({"url": t["url"],
                                   "width": t.get("width"),
                                   "height": t.get("height")})

    formats: list[dict] = []
    seen_urls: set[str] = set()
    for video_list in _walk_video_lists(data.get("story_pin_data") or {}):
        for f in video_list.values():
            if not isinstance(f, dict):
                continue
            u = f.get("url")
            if not u or u in seen_urls:
                continue
            seen_urls.add(u)
            ext = u.split("?")[0].rsplit(".", 1)[-1].lower() if "." in u.split("?")[0] else ""
            if ext in ("mp3", "m4a", "aac"):
                formats.append({"url": u, "ext": ext, "vcodec": "none",
                                "acodec": "mp3" if ext == "mp3" else "aac",
                                "abr": f.get("bitrate")})
            elif ext in ("mp4", "mov", "m3u8"):
                formats.append({"url": u, "ext": "mp4",
                                "width": f.get("width"), "height": f.get("height")})

    if not thumbnails and not formats:
        return None

    attribution = data.get("closeup_attribution") or {}
    best_thumb = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
                     default=None)
    return {
        "id": pin_id,
        "title": data.get("title") or data.get("grid_title")
                 or attribution.get("full_name") or "Pinterest pin",
        "uploader": attribution.get("full_name"),
        "duration": None,
        "thumbnail": best_thumb["url"] if best_thumb else None,
        "thumbnails": thumbnails,
        "formats": formats,
        "webpage_url": f"https://www.pinterest.com/pin/{pin_id}/",
    }


def _tiktok_photo_extract(url: str, with_cookies: bool = False) -> Optional[dict]:
    """TikTok photo posts (`/@user/photo/ID`) are 'Unsupported URL' for yt-dlp,
    and even via the /video/ alias it keeps only the music track. The web
    payload still carries every full-res slide under imagePost.images — run
    the extraction with capture hooks and put the slides back."""
    m = re.search(r"/(?:photo|video)/(\d+)", url)
    if not m:
        return None
    item_id = m.group(1)
    author = re.search(r"/@([^/?]+)/", url)
    aliased = f"https://www.tiktok.com/@{author.group(1) if author else 'user'}/video/{item_id}"

    captured: dict[str, Any] = {}
    try:
        from yt_dlp.extractor.tiktok import TikTokIE
        ydl = YoutubeDL(base_opts(with_cookies=with_cookies))
        with ydl:
            ie = TikTokIE(ydl)
            orig_web = ie._parse_aweme_video_web
            orig_app = ie._parse_aweme_video_app

            def web_wrap(detail, *a, **k):
                captured["detail"] = detail
                return orig_web(detail, *a, **k)

            def app_wrap(detail, *a, **k):
                captured["detail"] = detail
                return orig_app(detail, *a, **k)

            ie._parse_aweme_video_web = web_wrap
            ie._parse_aweme_video_app = app_wrap
            info = ie.extract(aliased)
    except Exception:  # noqa: BLE001
        return None

    if not isinstance(info, dict):
        return None

    slides: list[dict] = []
    detail = captured.get("detail") or {}
    for s in ((detail.get("imagePost") or {}).get("images") or []):
        iu = s.get("imageURL") or {}
        urls = iu.get("urlList") or iu.get("url_list") or []
        if urls:
            slides.append({"url": urls[0],
                           "width": s.get("imageWidth") or iu.get("width"),
                           "height": s.get("imageHeight") or iu.get("height")})

    if not slides:
        # Capture hooks caught nothing (payload gated/changed) — hand back
        # None so analyze's fallback chain (tikwm etc.) gets a chance.
        return None
    existing = {t.get("url") for t in info.get("thumbnails") or []}
    info["thumbnails"] = [s for s in slides if s["url"] not in existing] + (info.get("thumbnails") or [])
    info.setdefault("webpage_url", url)
    return info


def normalize_media_url(url: str) -> str:
    """Platform URL fixups needed before extraction/download.
    TikTok only serves /photo/ items through the /video/ alias."""
    if detect_platform(url) == "tiktok":
        return re.sub(r"(/@[^/?]+/)photo/", r"\1video/", url)
    return url


def _instagram_fallback(url: str) -> Optional[dict]:
    """Instagram image/carousel posts: yt-dlp fetches the post data fine but
    raises 'There is no video in this post'. Re-run its webpage fetch (with
    TLS impersonation) and pull the image candidates out of the embedded
    RelayPrefetchedStreamCache JSON ourselves."""
    m = re.search(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url)
    if not m:
        return None
    code = m.group(1)
    try:
        from yt_dlp.extractor.instagram import InstagramIE
        ydl = YoutubeDL(base_opts())
        with ydl:
            ie = InstagramIE(ydl)
            webpage, _ = ie._download_webpage_handle(
                f"https://www.instagram.com/p/{code}/", code,
                impersonate=ie._can_impersonate)
    except Exception:  # noqa: BLE001
        return None

    images: list[dict] = []
    username: str | None = None
    caption: str | None = None

    def walk(node: Any) -> None:
        nonlocal username, caption
        if isinstance(node, dict):
            iv = node.get("image_versions2")
            if isinstance(iv, dict):
                # polaris items keep real dimensions on the parent node
                ow = node.get("original_width") or node.get("width")
                oh = node.get("original_height") or node.get("height")
                for c in iv.get("candidates") or []:
                    if isinstance(c, dict) and c.get("url"):
                        images.append({"url": c["url"],
                                       "width": c.get("width") or ow,
                                       "height": c.get("height") or oh})
            u = node.get("user")
            if isinstance(u, dict) and u.get("username") and not username:
                username = u["username"]
            cap = node.get("caption")
            if isinstance(cap, dict) and cap.get("text") and not caption:
                caption = cap["text"]
            elif isinstance(cap, str) and cap and not caption:
                caption = cap
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    sjs_re = None
    try:
        from yt_dlp.extractor.instagram import InstagramIE
        sjs_re = getattr(InstagramIE, "_SJS_RE", None)
    except Exception:  # noqa: BLE001
        pass
    if sjs_re is None:
        sjs_re = re.compile(
            r'<script type="application/json" data-sjs>(.+?)</script>', re.S)

    for blob in sjs_re.findall(webpage):
        try:
            walk(json.loads(blob))
        except Exception:  # noqa: BLE001
            continue

    seen: set[str] = set()
    unique: list[dict] = []
    for img in images:
        if img["url"] in seen:
            continue
        seen.add(img["url"])
        unique.append(img)
    if not unique:
        return None

    unique.sort(key=lambda c: (c.get("width") or 0) * (c.get("height") or 0), reverse=True)
    return {
        "id": code,
        "title": (caption or "").strip()[:140] or f"Instagram post by {username or 'unknown'}",
        "uploader": username,
        "duration": None,
        "thumbnail": unique[0]["url"],
        "thumbnails": unique,
        "formats": [],
        "webpage_url": f"https://www.instagram.com/p/{code}/",
    }


# --- universal OpenGraph fallback (any platform) ---------------------------
def _meta_props(page: str) -> dict[str, list[str]]:
    props: dict[str, list[str]] = {}
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        name = re.search(r'(?:property|name|itemprop)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        content = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if name and content:
            props.setdefault(name.group(1).lower(), []).append(
                html_mod.unescape(content.group(1)))
    return props


def _twimg_full_res(u: str) -> str:
    """X/Twitter media URLs expose a `name` size param — `orig` is full-res."""
    if "pbs.twimg.com/media/" not in u:
        return u
    parts = urlsplit(u)
    q = parse_qs(parts.query)
    q["name"] = ["orig"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(q, doseq=True), ""))


def _og_fallback(url: str, platform: str) -> Optional[dict]:
    """Last-resort image extraction from the page's OpenGraph metadata.
    Works for image posts on X/Twitter, Reddit, Facebook, TikTok covers,
    Snapchat, and anything else that publishes og:image."""
    try:
        resp = httpx.get(url, headers=HTTP_HEADERS, timeout=15, follow_redirects=True)
        if resp.status_code != 200:
            return None
        page = resp.text
    except Exception:  # noqa: BLE001
        return None

    props = _meta_props(page)
    raw_urls: list[str] = []
    for key in ("og:image", "og:image:secure_url", "og:image:url", "twitter:image",
                "twitter:image:src"):
        raw_urls.extend(props.get(key, []))

    def _as_int(vals: list[str]) -> Optional[int]:
        for v in vals or []:
            if v.isdigit():
                return int(v)
        return None

    width = _as_int(props.get("og:image:width", []))
    height = _as_int(props.get("og:image:height", []))

    images: list[dict] = []
    seen: set[str] = set()
    for u in raw_urls:
        u = u.strip()
        if not u.startswith("http"):
            u = urljoin(url, u)
        if platform == "twitter":
            u = _twimg_full_res(u)
        if u in seen:
            continue
        seen.add(u)
        images.append({"url": u, "width": width, "height": height})

    # Public pages (Facebook especially) expose direct mp4s via og:video —
    # a downloadable lifeline whenever yt-dlp itself is gated or confused.
    og_videos: list[dict] = []
    vseen: set[str] = set()
    for key in ("og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"):
        for u in props.get(key, []):
            u = (u or "").strip()
            if not u.startswith("http") or u in vseen:
                continue
            vseen.add(u)
            h = _as_int(props.get("og:video:height", []))
            og_videos.append({"kind": "video", "height": h,
                              "label": f"{h}p" if h else "Video · original quality",
                              "filesize": None, "is_estimate": False, "direct_url": u})

    if not images and not og_videos:
        return None

    title = (props.get("og:title") or props.get("twitter:title") or [""])[0]
    uploader = (props.get("og:site_name") or [""])[0]
    return {
        "id": urlparse(url).path.strip("/") or "post",
        "title": title or "Media post",
        "uploader": uploader or None,
        "duration": None,
        "thumbnail": images[0]["url"] if images else None,
        "thumbnails": images,
        "formats": [],
        "og_videos": og_videos,
        "webpage_url": url,
    }


# ---------------------------------------------------------------------------
# yt-dlp option builders
# ---------------------------------------------------------------------------
def base_opts(*, skip_download: bool = True, with_cookies: bool = False) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,          # a single post, never a whole playlist
        "skip_download": skip_download,
        "socket_timeout": config.SOCKET_TIMEOUT,
        "retries": config.RETRIES,
        "extractor_retries": 2,
    }
    if config.FFMPEG_PATH:
        opts["ffmpeg_location"] = config.FFMPEG_PATH
    # Cookies are a rescue tool, not the default: presenting a logged-in
    # session from a datacenter IP is what trips "suspicious activity"
    # warnings, so they're attached only when the caller escalates to them.
    if with_cookies and config.COOKIES_FILE:
        opts["cookiefile"] = config.COOKIES_FILE
    return opts


def cookies_configured() -> bool:
    return bool(config.COOKIES_FILE)


def gateish(raw: str) -> bool:
    """Does this failure look like the platform gating anonymous traffic?"""
    low = raw.lower()
    return any(k in low for k in ("login", "sign in", "cookie", "private", "bot",
                                  "unexpected", "empty", "unavailable", "403", "blocked"))


def opts_for_platform(platform: str, *, skip_download: bool = True,
                      with_cookies: bool = False) -> dict[str, Any]:
    """base_opts plus platform tweaks. For YouTube the merged player-client
    set is essential: when the web client is bot-gated yt-dlp otherwise
    *silently* degrades to the android client's 360p-only streams."""
    opts = base_opts(skip_download=skip_download, with_cookies=with_cookies)
    if platform == "youtube":
        opts["extractor_args"] = {**opts.get("extractor_args", {}),
                                  "youtube": {"player_client": [YT_PRIMARY_CLIENTS]}}
    return opts


def format_selector_for_height(height: int) -> str:
    """yt-dlp format selector for a target quality.

    Prefers the exact height, then falls back to the best available quality
    at or below it, merging bestvideo+bestaudio.
    """
    return (
        f"bestvideo[height={height}]+bestaudio/best[height={height}]"
        f"/bestvideo[height<={height}]+bestaudio/best[height<={height}]"
        f"/bestvideo+bestaudio/best"
    )


def format_selector_for_audio(audio_format: str) -> str:
    if audio_format == "m4a":
        return "bestaudio[ext=m4a]/bestaudio/best"
    return "bestaudio/best"  # converted to MP3 by postprocessor


# ---------------------------------------------------------------------------
# Size estimation helpers
# ---------------------------------------------------------------------------
def _fmt_size(fmt: dict, duration: float | None) -> int | None:
    """Best-effort byte size for a single format entry."""
    for key in ("filesize", "filesize_approx"):
        val = fmt.get(key)
        if val:
            return int(val)
    tbr = fmt.get("tbr") or fmt.get("vbr") or fmt.get("abr")
    if tbr and duration:
        return int(tbr * 1000 / 8 * duration)
    return None


def _pick_best_per_height(info: dict) -> dict[int, dict]:
    """Pick the best video format for each distinct height."""
    best: dict[int, dict] = {}
    for f in info.get("formats") or []:
        height = f.get("height")
        vcodec = f.get("vcodec") or "none"
        if not height or vcodec == "none":
            continue
        if height > 2160:
            continue
        score = (f.get("fps") or 0, f.get("tbr") or 0, f.get("preference") or 0)
        current = best.get(height)
        if current is None or score > (current.get("fps") or 0, current.get("tbr") or 0,
                                       current.get("preference") or 0):
            best[height] = f
    return best


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def analyze(url: str) -> dict[str, Any]:
    """Fetch full metadata + every quality option for a URL (no download)."""
    url = validate_url(url)
    platform = detect_platform(url)

    info: Optional[dict] = None
    # TikTok photo posts need the capture-hook extractor (yt-dlp drops slides).
    # Cookies first (TikTok policy — see the attempt order below), anonymous
    # as a safety net if the session is stale or the attempt got gated.
    if platform == "tiktok" and "/photo/" in url:
        ck = cookies_configured()
        for with_ck in ([True, False] if ck else [False]):
            try:
                info = _tiktok_photo_extract(url, with_cookies=with_ck)
                break
            except (DownloadError, ExtractorError) as exc:
                info = None
                if not gateish(str(exc)):
                    break
                time.sleep(random.uniform(0.5, 1.5))

    if info is None:
        # Attempt order — TikTok: cookies FIRST (a logged-in session passes
        # the bot gate that stops anonymous datacenter traffic), anonymous as
        # a safety net in case the session is stale. Every other platform
        # stays conservative: anonymous first, cookies only as a gated
        # rescue, so the account stays out of routine traffic (fewer
        # "suspicious activity" warnings).
        ck = cookies_configured()
        order = ([True, False] if (platform == "tiktok" and ck)
                 else [False] + ([True] if ck else []))
        exc: Optional[DownloadError] = None
        for i, with_ck in enumerate(order):
            try:
                with YoutubeDL(opts_for_platform(platform, with_cookies=with_ck)) as ydl:
                    info = ydl.extract_info(normalize_media_url(url), download=False)
                break
            except (DownloadError, ExtractorError) as e:
                exc = e
                if not gateish(str(e)):
                    break                      # private/deleted — retrying won't help
                if i + 1 < len(order):
                    time.sleep(random.uniform(0.5, 1.5))   # human-ish pause
        if info is None and exc is None:
            # extract_info returned nothing without raising — nothing for
            # the fallbacks to key off; the generic error below applies.
            raise ValueError("No media information was returned for this URL.")
        if info is None:
            # X/Twitter: yt-dlp is usually blocked from datacenter IPs, but the
            # public syndication CDN is not — return direct .mp4 variants.
            # fxtwitter covers sensitive-content tweets the CDN tombstones.
            if info is None and platform == "twitter":
                syn = _twitter_anon_fetch(url)
                if syn and (syn["video_options"] or syn["image_options"]):
                    return _syn_response(url, platform, syn)
            # TikTok bot-gates datacenter IPs — third-party fallbacks, in
            # order: tikdownloader.io first (direct CDN urls from its own
            # fetch infrastructure), tikwm last (rate-limited per egress IP).
            if info is None and platform == "tiktok":
                for fb in (_tikdownloader_fallback, _tiktok_fallback):
                    syn = fb(url)
                    if syn and (syn["video_options"] or syn["image_options"]):
                        return _syn_response(url, platform, syn)
            # Image posts make several extractors bail ("No video formats found",
            # "There is no video in this post", …). Layered fallbacks recover the
            # images anyway: platform-native APIs first, then OpenGraph metadata.
            if info is None and platform == "youtube" and yt_gated_error(str(exc)):
                # Bot-check on the default client — alternate clients often pass.
                info = youtube_client_ladder(url, with_cookies=cookies_configured())
            if info is None and platform == "pinterest":
                info = _pinterest_fallback(url)
            if info is None and platform == "instagram":
                info = _instagram_fallback(url)
            if info is None:
                info = _og_fallback(url, platform)
            if info is None:
                raise ValueError(friendly_fetch_error(platform, url, str(exc))) from exc

    if not info:
        raise ValueError("No media information was returned for this URL.")

    # YouTube can "succeed" with only android's 360p when richer clients are
    # gated — escalate for quality before building the options list.
    if platform == "youtube":
        info = _youtube_best_quality(info, url)

    # If the extractor still hands us a playlist, take the first entry.
    if info.get("_type") == "playlist":
        entries = list(info.get("entries") or [])
        if not entries:
            raise ValueError("This playlist appears to be empty.")
        info = entries[0]

    # Snapchat's dedicated extractor returns zero formats these days and the
    # generic one comes back height-less — the page's og:video meta carries
    # the direct mp4 WITH resolution, so prefer it whenever heights are missing.
    if platform == "snapchat" and not any(f.get("height") for f in info.get("formats") or []):
        fmts = _snapchat_page_formats(url)
        if fmts:
            info = {**info, "formats": fmts}

    duration = info.get("duration") or 0
    best_audio = _best_audio(info)
    audio_size = _fmt_size(best_audio, duration) if best_audio else None
    image_options = build_image_options(info)

    # ---- video options ---------------------------------------------------
    video_options = []
    per_height = _pick_best_per_height(info)
    for height in sorted(per_height, reverse=True):  # non-standard heights (e.g. 1280) kept too
        fmt = per_height[height]
        vsize = _fmt_size(fmt, duration)
        total = (vsize or 0) + (audio_size or 0) if (vsize or audio_size) else None
        video_options.append({
            "kind": "video",
            "height": height,
            "label": f"{height}p" + (f" — {config.QUALITY_LABELS[height]}" if height in config.QUALITY_LABELS else ""),
            "fps": fmt.get("fps"),
            "vcodec": (fmt.get("vcodec") or "").split(".")[0] or None,
            "filesize": total,          # estimated combined video+audio
            "is_estimate": True,
        })

    # Some embeds (Snapchat spotlight via the generic extractor) expose a muxed
    # mp4 WITHOUT height metadata — the ladder above drops it and the post
    # degrades to "photo only". Rescue height-less video formats as direct
    # downloads so video posts stay video posts.
    if not video_options:
        seen_h: set = set()
        for f in info.get("formats") or []:
            src = f.get("url")
            vc = f.get("vcodec") or ""
            ext = (f.get("ext") or "").lower()
            is_video = (vc and vc != "none") or ext in ("mp4", "webm", "mkv", "mov")
            if not src or not is_video or (vc == "none"):
                continue
            h = f.get("height")
            if h in seen_h:
                continue
            seen_h.add(h)
            vsize = _fmt_size(f, duration)
            total = (vsize or 0) + (audio_size or 0) if (vsize or audio_size) else None
            video_options.append({
                "kind": "video",
                "height": h,
                "label": f"{h}p" if h else "Video · original quality",
                "fps": f.get("fps"),
                "vcodec": (f.get("vcodec") or "").split(".")[0] or None,
                "filesize": total,
                "is_estimate": True,
                "direct_url": src,
            })

    # ---- audio options ---------------------------------------------------
    audio_options = []
    if best_audio:
        abr = best_audio.get("abr") or 0
        audio_options.append({
            "kind": "audio",
            "audio_format": "mp3",
            "label": "MP3 — 320 kbps (converted)",
            "bitrate": 320,
            "filesize": int(320 * 1000 / 8 * duration) if duration else None,
            "is_estimate": True,
        })
        m4a_size = audio_size if (best_audio.get("ext") in ("m4a", "mp4", "aac")) else None
        audio_options.append({
            "kind": "audio",
            "audio_format": "m4a",
            "label": f"M4A — native best ({int(abr)} kbps)" if abr else "M4A — native best",
            "bitrate": int(abr) or None,
            "filesize": m4a_size or (int(abr * 1000 / 8 * duration) if duration and abr else None),
            "is_estimate": True,
        })

    # Height-less embeds (Snapchat) carry no acodec metadata either — derive
    # audio extraction options from the rescued direct video.
    if not audio_options:
        direct_vids = [v for v in video_options if v.get("direct_url")]
        if direct_vids:
            audio_options = _twitter_audio_for(direct_vids)

    if not video_options and not audio_options and not image_options:
        # Extraction "succeeded" but yielded nothing usable — one more shot
        # via OpenGraph metadata before giving up.
        og = _og_fallback(url, platform)
        if og:
            image_options = build_image_options(og)

    # X/Twitter: yt-dlp is frequently blocked from datacenter IPs, but the
    # public syndication CDN is not. Fill in direct .mp4 variants + photos.
    syn_meta: dict = {}
    if platform == "twitter" and not video_options:
        syn = _twitter_anon_fetch(url)
        if syn:
            video_options = syn["video_options"]
            audio_options = audio_options or syn["audio_options"]
            if not image_options:
                image_options = syn["image_options"]
            syn_meta = syn
    if platform == "tiktok" and not video_options:
        # Third-party fallbacks, in order: tikdownloader first, tikwm last.
        for fb in (_tikdownloader_fallback, _tiktok_fallback):
            syn = fb(url)
            if syn:
                video_options = syn["video_options"]
                audio_options = audio_options or syn["audio_options"]
                if not image_options:
                    image_options = syn["image_options"]
                syn_meta = syn
                break
    if not video_options and info.get("og_videos"):
        # og:video direct mp4s (facebook public pages) when yt-dlp couldn't help
        video_options = info["og_videos"]
        audio_options = audio_options or _twitter_audio_for(video_options)
        syn_meta = info

    # Photo options are for image posts only — a fetched video post should
    # not offer cover-art photo downloads. (TikTok /photo/ posts are the
    # exception: the slides ARE the content, alongside the slideshow video.)
    if video_options and not (platform == "tiktok" and "/photo/" in url):
        image_options = []

    if not video_options and not audio_options and not image_options:
        raise ValueError("No downloadable media streams were found for this URL.")

    content_type = "video" if video_options else ("image" if image_options else "audio")

    meta = PLATFORM_META.get(platform, PLATFORM_META["generic"])
    return {
        "url": url,
        "platform": platform,
        "platform_name": meta["name"],
        "platform_color": meta["color"],
        "title": syn_meta.get("title") or info.get("title") or "Untitled media",
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id") or syn_meta.get("uploader"),
        "duration": duration or syn_meta.get("duration") or 0,
        "thumbnail": info.get("thumbnail") or syn_meta.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or url,
        "content_type": content_type,
        "video_options": video_options,
        "audio_options": audio_options,
        "image_options": image_options,
    }


def fmt_bytes(n: float | None) -> str | None:
    if not n:
        return None
    units = ["B", "KB", "MB", "GB"]
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    return f"{round(n) if n >= 100 else n:.1f} {units[i]}"


# ---------------------------------------------------------------------------
# Image options — genuine image posts, and cover art beside video posts
# ---------------------------------------------------------------------------
_IMAGE_EXT = re.compile(r"\.(jpe?g|png|webp|gif)([?#]|$)", re.I)
_IMAGE_CDN = re.compile(
    r"(pinimg|twimg|twtdata|cdninstagram|fbcdn|fbsbx|fbximages|"
    r"redd\.it|redditmedia|redditstatic|ytimg|googleusercontent|ggpht|"
    r"tiktokcdn|tiktokv|muscdn|ibytedtos|sncdn|sc-cdn|snapchat|pinata)", re.I)
# yt-dlp's `thumbnails` list is curated imagery — accept anything that isn't
# obviously a stream/storyboard so EVERY video post gets a Photos option.
_IMAGE_JUNK = re.compile(r"(storyboard|sprite|\.m3u8|\.mpd|\.mp4|\.webm|/fragments/|smil:)", re.I)


def _is_image_candidate(url: str) -> bool:
    if not url.startswith(("http://", "https://")):
        return False
    return not _IMAGE_JUNK.search(url)


def _pinimg_original_candidates(url: str) -> list[str]:
    """Pinterest serves resized copies (236x, 736x, …); the untouched upload
    lives under /originals/ — probe for it."""
    m = re.match(r"(https?://i\.pinimg\.com/)([^/]+)/(.+)$", url)
    if not m or m.group(2) == "originals":
        return []
    base, rest = m.group(1), m.group(3)
    leaf = rest.rsplit("/", 1)[-1]
    cands = [f"{base}originals/{rest}"]
    if "." not in leaf:
        cands += [f"{base}originals/{rest}.jpg", f"{base}originals/{rest}.png"]
    return cands


def build_image_options(info: dict) -> list[dict]:
    seen: set[str] = set()
    candidates: list[dict] = []
    for t in info.get("thumbnails") or []:
        url = t.get("url")
        if not url or not isinstance(url, str) or url in seen:
            continue
        if not _is_image_candidate(url):
            continue
        seen.add(url)
        candidates.append({
            "url": url,
            "width": t.get("width"),
            "height": t.get("height"),
            "filesize": t.get("filesize") or None,
            "is_original": "/originals/" in url,
        })

    upgraded: list[dict] = candidates
    try:
        with httpx.Client(headers=HTTP_HEADERS, timeout=6, follow_redirects=True) as client:
            upgraded = []
            for c in candidates:
                chosen = c
                for cand in _pinimg_original_candidates(c["url"]):
                    try:
                        head = client.head(cand)
                        if head.status_code == 200:
                            chosen = {
                                "url": str(head.url),
                                "width": c["width"], "height": c["height"],
                                "filesize": int(head.headers.get("content-length") or 0) or None,
                                "is_original": True,
                            }
                            break
                    except httpx.HTTPError:
                        continue
                if chosen is c and chosen.get("filesize") is None and (c.get("width") or 0) >= 600:
                    try:
                        head = client.head(c["url"])
                        if head.status_code == 200 and head.headers.get("content-length"):
                            chosen = {**c, "filesize": int(head.headers["content-length"])}
                    except httpx.HTTPError:
                        pass
                upgraded.append(chosen)
    except Exception:  # noqa: BLE001 — metadata enhancement is best-effort
        upgraded = candidates

    upgraded.sort(key=lambda c: (c.get("width") or 0) * (c.get("height") or 0), reverse=True)

    # After originals-probing, several size variants can collapse onto the same
    # file — dedupe by URL, keeping the richest metadata.
    by_url: dict[str, dict] = {}
    for c in upgraded:
        cur = by_url.get(c["url"])
        if cur is None or ((c.get("width") or 0) * (c.get("height") or 0)
                           > (cur.get("width") or 0) * (cur.get("height") or 0)):
            by_url[c["url"]] = c
    upgraded = sorted(by_url.values(),
                      key=lambda c: (c.get("width") or 0) * (c.get("height") or 0),
                      reverse=True)

    options: list[dict] = []
    for c in upgraded:
        w, h = c.get("width"), c.get("height")
        dims = f"{w}×{h}" if w and h else "full size"
        tag = "Original · " if c.get("is_original") else ""
        size = f" · ~{fmt_bytes(c['filesize'])}" if c.get("filesize") else ""
        options.append({
            "kind": "image",
            "label": f"{tag}{dims}{size}",
            "url": c["url"],
            "width": w,
            "height": h,
            "filesize": c.get("filesize"),
        })
        if len(options) >= 12:      # room for full carousels, not just covers
            break
    return options


def _x_token(tweet_id: str) -> int:
    """Public token for the syndication CDN (anonymous, no cookies)."""
    r = 0
    for ch in str(tweet_id):
        r = (r * 31 + ord(ch)) % 1_000_000_007
    return r


def _snapchat_page_formats(url: str) -> list[dict]:
    """Snapchat's dedicated yt-dlp extractor returns zero formats these days,
    but the spotlight page itself exposes the direct mp4 — with resolution —
    via og:video. Scrape it."""
    try:
        with httpx.Client(headers=HTTP_HEADERS, timeout=15, follow_redirects=True) as c:
            html = c.get(url).text
    except Exception:  # noqa: BLE001
        return []

    def meta(prop: str) -> str:
        m = (re.search(rf'<meta[^>]+property="og:video:{prop}"[^>]+content="([^"]+)"', html)
             or re.search(rf'<meta[^>]+content="([^"]+)"[^>]+property="og:video:{prop}"', html))
        return m.group(1) if m else ""

    m = (re.search(r'<meta[^>]+property="og:video"[^>]+content="([^"]+)"', html)
         or re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:video"', html))
    if not m:
        return []
    src = m.group(1).replace("&amp;", "&")
    h, w = meta("height"), meta("width")
    return [{"url": src, "ext": "mp4",
             "height": int(h) if h.isdigit() else None,
             "width": int(w) if w.isdigit() else None}]


def _twitter_audio_for(videos: list[dict]) -> list[dict]:
    """MP3/M4A extraction options from the best direct .mp4 (muxed audio)."""
    best = videos[0]["direct_url"] if videos else None
    if not best:
        return []
    return [
        {"kind": "audio", "audio_format": "mp3", "label": "MP3 — extract from video",
         "filesize": None, "direct_url": best},
        {"kind": "audio", "audio_format": "m4a", "label": "M4A — extract from video",
         "filesize": None, "direct_url": best},
    ]


def _twitter_fxtwitter(url: str) -> Optional[dict]:
    """Second anonymous X fallback (api.fxtwitter.com). Unlike the syndication
    CDN it also serves tweets X tombstones as sensitive content."""
    m = re.search(r"(?:twitter|x)\.com/([^/]+)/status/(\d+)", url)
    if not m:
        return None
    api = f"https://api.fxtwitter.com/{m.group(1)}/status/{m.group(2)}"
    try:
        with httpx.Client(headers=HTTP_HEADERS, timeout=15, follow_redirects=True) as c:
            d = c.get(api).json()
    except Exception:  # noqa: BLE001
        return None
    t = d.get("tweet") if isinstance(d, dict) else None
    if not isinstance(t, dict):
        return None
    media = t.get("media") or {}
    videos: list[dict] = []
    seen_h: set[int] = set()
    for v in media.get("videos") or []:
        src = v.get("url") or ""
        if not src:
            continue
        h = int(v.get("height") or 0)
        if h in seen_h:
            continue
        seen_h.add(h)
        videos.append({"kind": "video", "height": h,
                       "label": f"{h}p" if h else "Video",
                       "filesize": None, "is_estimate": False, "direct_url": src})
    videos.sort(key=lambda x: x["height"], reverse=True)
    images = [{"kind": "image", "url": p.get("url"), "width": p.get("width"),
               "height": p.get("height"),
               "label": f"{p.get('width')}×{p.get('height')}" if p.get("width") else "Image",
               "filesize": None}
              for p in (media.get("photos") or []) if p.get("url")]
    if not videos and not images:
        return None
    first_vid = (media.get("videos") or [{}])[0]
    author = t.get("author") or {}
    return {"title": (t.get("text") or "X post")[:120],
            "uploader": author.get("name"),
            "thumbnail": first_vid.get("thumbnail_url")
                         or (images[0]["url"] if images else None),
            "duration": (first_vid.get("duration") or 0) / 1000,
            "video_options": videos, "image_options": images,
            "audio_options": _twitter_audio_for(videos)}


def _twitter_anon_fetch(url: str) -> Optional[dict]:
    """Anonymous X media: syndication CDN first, then fxtwitter (which also
    covers sensitive-content tweets the CDN tombstones)."""
    return _twitter_syndication(url) or _twitter_fxtwitter(url)


def _syn_response(url: str, platform: str, syn: dict) -> dict:
    """Shared analyzer response for anonymous fallback sources."""
    vo, io_, ao = syn["video_options"], syn["image_options"], syn["audio_options"]
    if vo:
        io_ = []
    meta = PLATFORM_META.get(platform, PLATFORM_META["generic"])
    return {"url": url, "platform": platform,
            "platform_name": meta["name"], "platform_color": meta["color"],
            "title": syn["title"], "uploader": syn["uploader"],
            "duration": syn["duration"], "thumbnail": syn["thumbnail"],
            "webpage_url": url,
            "content_type": "video" if vo else ("image" if io_ else "audio"),
            "video_options": vo, "audio_options": ao, "image_options": io_}


def _tiktok_fallback(url: str) -> Optional[dict]:
    """TikTok bot-gates yt-dlp from datacenter IPs ("Unexpected response…");
    the public tikwm API still serves watermark-free CDN urls. The free tier
    allows ~1 req/s, so retry gently."""
    data = None
    # GET/POST rotation — the free tier shares one req/s per egress IP and
    # some cloud ASNs get throttled hard (shared egress = other tenants eat
    # the quota), so give it six gently-spaced chances before giving up.
    for attempt in range(6):
        try:
            with httpx.Client(headers=HTTP_HEADERS, timeout=20,
                              follow_redirects=True) as c:
                if attempt % 2 == 0:
                    d = c.get("https://www.tikwm.com/api/?hd=1&url=" + url).json()
                else:
                    d = c.post("https://www.tikwm.com/api/",
                               data={"url": url, "hd": "1"}).json()
            data = d.get("data") if isinstance(d, dict) else None
            if data and (data.get("play") or data.get("images")):
                break
        except Exception:  # noqa: BLE001
            return None
        time.sleep(1.5)
    if not data or not (data.get("play") or data.get("images")):
        return None

    def abs_url(u):
        if not u:
            return None
        return u if u.startswith("http") else "https://www.tikwm.com" + u

    videos = []
    hd, sd = abs_url(data.get("hd_play")), abs_url(data.get("play"))
    if hd:
        videos.append({"kind": "video", "height": None, "label": "HD · no watermark",
                       "filesize": data.get("hd_size"), "is_estimate": False,
                       "direct_url": hd})
    if sd:
        videos.append({"kind": "video", "height": None, "label": "SD · no watermark",
                       "filesize": data.get("size"), "is_estimate": False,
                       "direct_url": sd})
    images = []
    for img in data.get("images") or []:
        u = abs_url(img.get("url") if isinstance(img, dict) else img)
        if u:
            images.append({"kind": "image", "url": u, "width": None,
                           "height": None, "label": "Image", "filesize": None})
    return {"title": (data.get("title") or "TikTok post")[:120],
            "uploader": (data.get("author") or {}).get("nickname"),
            "thumbnail": abs_url(data.get("cover") or data.get("origin_cover")),
            "duration": (data.get("duration") or 0) or None,
            "video_options": videos, "image_options": images,
            "audio_options": _twitter_audio_for(videos)}


def _tikdownloader_fallback(url: str) -> Optional[dict]:
    """tikdownloader.io's ajaxSearch API — second third-party TikTok fallback,
    used only when yt-dlp (even with cookies) AND tikwm both fail. The site's
    backend fetches the post from its own infrastructure and returns an HTML
    fragment with direct watermark-free TikTok CDN urls (plus signed proxy
    variants) that remain downloadable from datacenter IPs."""
    api = "https://tikdownloader.io/api/ajaxSearch"
    try:
        with httpx.Client(headers=HTTP_HEADERS, timeout=45, follow_redirects=True) as c:
            r = c.post(api, data={"q": url, "lang": "en"},
                       headers={"X-Requested-With": "XMLHttpRequest",
                                "Referer": "https://tikdownloader.io/en"})
            d = r.json()
    except Exception:  # noqa: BLE001 — fallbacks must never raise
        return None
    if not isinstance(d, dict) or d.get("status") != "ok" or d.get("msg"):
        return None
    frag = d.get("data") or ""
    if "Download MP4" not in frag:
        return None

    links: list[tuple[str, str]] = []
    for m in re.finditer(r'<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', frag, re.S):
        href = m.group(1).strip()
        if not href.startswith("http"):
            continue
        label = re.sub(r"<[^>]+>", " ", m.group(2))
        label = re.sub(r"\s+", " ", html_mod.unescape(label)).strip().lower()
        links.append((label, href))

    def best(label_re: str, prefer_direct: bool = True) -> Optional[str]:
        """First link matching the label; direct tiktok CDN preferred over the
        site's signed proxy (fewer third-party hops on the file bytes)."""
        matches = [h for lab, h in links if re.search(label_re, lab)]
        if prefer_direct:
            for h in matches:
                if "tiktokcdn" in h:
                    return h
        for h in matches:
            return h
        return None

    sd = best(r"mp4")
    hd = best(r"mp4\s*hd")
    videos = []
    if hd and hd != sd:
        videos.append({"kind": "video", "height": None, "label": "HD · no watermark",
                       "filesize": None, "is_estimate": False, "direct_url": hd})
    if sd:
        videos.append({"kind": "video", "height": None, "label": "SD · no watermark",
                       "filesize": None, "is_estimate": False, "direct_url": sd})
    if not videos:
        return None

    img_m = re.search(r'<img[^>]*src="([^"]+)"', frag)
    img_url = html_mod.unescape(img_m.group(1).strip()) if img_m else None
    images = [{"kind": "image", "url": img_url, "width": None,
               "height": None, "label": "Image", "filesize": None}] if img_m else []

    # Caption is the first long text node in the fragment.
    title = "TikTok post"
    for m in re.finditer(r'>([^<>]{20,200})<', frag):
        text = html_mod.unescape(m.group(1)).strip()
        if text and "adsbygoogle" not in text:
            title = text[:120]
            break
    return {"title": title, "uploader": None,
            "thumbnail": img_url,
            "duration": None,
            "video_options": videos, "image_options": images,
            "audio_options": _twitter_audio_for(videos)}


def _twitter_syndication(url: str) -> Optional[dict]:
    """Anonymous X/Twitter fallback via the public syndication CDN.

    yt-dlp is often blocked from datacenter IPs, but this CDN is not, and it
    returns direct .mp4 variants (muxed with audio) plus photos. Returns
    video/image/audio options carrying `direct_url` for direct download.
    """
    m = re.search(r"status/(\d+)", url)
    if not m:
        return None
    tid = m.group(1)
    api = (f"https://cdn.syndication.twimg.com/tweet-result?id={tid}"
           f"&lang=en&token={_x_token(tid)}")
    try:
        with httpx.Client(headers=HTTP_HEADERS, timeout=15, follow_redirects=True) as c:
            data = c.get(api).json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(data, dict):
        return None

    video = data.get("video") or {}
    videos = []
    seen_h = set()
    for var in (video.get("variants") or []):
        src = var.get("src") or ""
        if not src.endswith(".mp4"):
            continue
        mm = re.search(r"/(\d{2,5})x(\d{2,5})/", src)
        h = int(mm.group(2)) if mm else 0
        if h in seen_h:
            continue
        seen_h.add(h)
        videos.append({
            "kind": "video", "height": h,
            "label": f"{h}p" if h else "Video",
            "filesize": None, "is_estimate": False,
            "direct_url": src,
        })
    videos.sort(key=lambda v: v["height"], reverse=True)

    images = []
    for p in (data.get("photos") or []):
        u = p.get("url") or p.get("media_url_https")
        if u:
            images.append({"kind": "image", "url": u, "width": p.get("width"),
                           "height": p.get("height"),
                           "label": f"{p.get('width')}×{p.get('height')}" if p.get("width") else "Image",
                           "filesize": None})

    audio = _twitter_audio_for(videos)

    if not videos and not images:
        return None
    return {
        "title": (data.get("text") or "X post")[:120],
        "uploader": ((data.get("user") or {}).get("name")),
        "thumbnail": video.get("poster") or (images[0]["url"] if images else None),
        "duration": (video.get("durationMs") or 0) / 1000,
        "video_options": videos, "image_options": images, "audio_options": audio,
    }


def _has_audio(f: dict) -> bool:
    ac = f.get("acodec")
    if ac and ac != "none":
        return True
    # Some extractors (X syndication etc.) omit acodec metadata on muxed
    # containers — a media container with video almost always carries audio.
    return (f.get("ext") in ("mp4", "m4a", "mov", "webm", "mkv")
            and (f.get("vcodec") or "none") != "none")


def _best_audio(info: dict) -> Optional[dict]:
    """Best audio source. Prefers audio-only formats; falls back to the best
    combined format for platforms (TikTok, Snapchat…) that mux audio+video."""
    fmts = info.get("formats") or []
    audio_only = [f for f in fmts
                  if (f.get("acodec") or "none") != "none" and f.get("vcodec") in (None, "none")]
    if audio_only:
        return max(audio_only, key=lambda f: (f.get("abr") or 0, f.get("preference") or 0))
    combined = [f for f in fmts if _has_audio(f)]
    if combined:
        return max(combined, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
    return None
