"""
NyxDownloader — platform detection & media analysis.

All heavy lifting is delegated to yt-dlp, which natively supports every
target platform (TikTok, Instagram, Snapchat, Facebook, X/Twitter, Reddit,
YouTube) plus thousands more.

Watermark notes
---------------
* TikTok   — yt-dlp's TikTok extractor returns the clean (watermark-free)
             rendition by default; we also pass extractor args that prefer
             the no-watermark API endpoints.
* Others   — Reels, Shorts, tweets, Reddit/FB/Snap videos are served as
             the original uploaded file, so there is no watermark to strip.
"""
from __future__ import annotations

import html as html_mod
import json
import re
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


def _tiktok_photo_extract(url: str) -> Optional[dict]:
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
        ydl = YoutubeDL(base_opts())
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

    if slides:
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

    if not images:
        return None

    title = (props.get("og:title") or props.get("twitter:title") or [""])[0]
    uploader = (props.get("og:site_name") or [""])[0]
    return {
        "id": urlparse(url).path.strip("/") or "post",
        "title": title or "Media post",
        "uploader": uploader or None,
        "duration": None,
        "thumbnail": images[0]["url"],
        "thumbnails": images,
        "formats": [],
        "webpage_url": url,
    }


# ---------------------------------------------------------------------------
# yt-dlp option builders
# ---------------------------------------------------------------------------
def base_opts(*, skip_download: bool = True) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,          # a single post, never a whole playlist
        "skip_download": skip_download,
        "socket_timeout": config.SOCKET_TIMEOUT,
        "retries": config.RETRIES,
        "extractor_retries": 2,
        # Prefer watermark-free renditions on TikTok where the extractor
        # exposes the choice.
        "extractor_args": {"tiktok": ["api_hostname=api16-normal-c-useast1a.tiktokv.com",
                                       "app_version=39.1.3"]},
    }
    if config.FFMPEG_PATH:
        opts["ffmpeg_location"] = config.FFMPEG_PATH
    if config.COOKIES_FILE:
        opts["cookiefile"] = config.COOKIES_FILE
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
    if platform == "tiktok" and "/photo/" in url:
        info = _tiktok_photo_extract(url)

    if info is None:
        try:
            with YoutubeDL(base_opts()) as ydl:
                info = ydl.extract_info(normalize_media_url(url), download=False)
        except (DownloadError, ExtractorError) as exc:
            # Image posts make several extractors bail ("No video formats found",
            # "There is no video in this post", …). Layered fallbacks recover the
            # images anyway: platform-native APIs first, then OpenGraph metadata.
            if platform == "pinterest":
                info = _pinterest_fallback(url)
            if info is None and platform == "instagram":
                info = _instagram_fallback(url)
            if info is None:
                info = _og_fallback(url, platform)
            if info is None:
                msg = _strip_ansi(str(exc))
                if platform == "instagram" and "/stories/" in url:
                    # Stories are login-gated for everyone, anonymously — always.
                    raise ValueError(
                        "Instagram stories ALWAYS require a logged-in session, even for "
                        "public accounts — that's Instagram's rule, not a bug. Paste your "
                        "own Instagram cookies in Admin → Security (or set COOKIES_FILE) "
                        "and stories you can see will download."
                    ) from exc
                if "login" in msg.lower() or "cookies" in msg.lower() or "private" in msg.lower():
                    raise ValueError(
                        "This media is private or requires a login to access. "
                        "Paste your cookies in Admin → Security (or set COOKIES_FILE) to unlock it."
                    ) from exc
                if "unsupported url" in msg.lower():
                    raise ValueError("This URL isn't supported. Check that it's a direct link to the post/video.") from exc
                raise ValueError(f"Couldn't fetch this media: {msg.splitlines()[0][:300]}") from exc

    if not info:
        raise ValueError("No media information was returned for this URL.")

    # If the extractor still hands us a playlist, take the first entry.
    if info.get("_type") == "playlist":
        entries = list(info.get("entries") or [])
        if not entries:
            raise ValueError("This playlist appears to be empty.")
        info = entries[0]

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

    if not video_options and not audio_options and not image_options:
        # Extraction "succeeded" but yielded nothing usable — one more shot
        # via OpenGraph metadata before giving up.
        og = _og_fallback(url, platform)
        if og:
            image_options = build_image_options(og)

    # Photo options are for image posts only — a fetched video post should
    # not offer cover-art photo downloads.
    if video_options:
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
        "title": info.get("title") or "Untitled media",
        "uploader": info.get("uploader") or info.get("channel") or info.get("uploader_id"),
        "duration": duration,
        "thumbnail": info.get("thumbnail"),
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


def _best_audio(info: dict) -> Optional[dict]:
    """Best audio source. Prefers audio-only formats; falls back to the best
    combined format for platforms (TikTok, Snapchat…) that mux audio+video."""
    fmts = info.get("formats") or []
    audio_only = [f for f in fmts
                  if (f.get("acodec") or "none") != "none" and f.get("vcodec") in (None, "none")]
    if audio_only:
        return max(audio_only, key=lambda f: (f.get("abr") or 0, f.get("preference") or 0))
    combined = [f for f in fmts if (f.get("acodec") or "none") != "none"]
    if combined:
        return max(combined, key=lambda f: (f.get("abr") or f.get("tbr") or 0))
    return None
