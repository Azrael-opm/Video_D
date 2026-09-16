<div align="right">
  <img src="frontend/favicon.svg" height="72" alt="NyxDownloader" />
</div>

# NyxDownloader

[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.10+-3776ab)](https://python.org)
[![yt-dlp](https://img.shields.io/badge/yt--dlp-2025.6.9+-6c5ce7)](https://github.com/yt-dlp/yt-dlp)
[![FFmpeg](https://img.shields.io/badge/FFmpeg-bundled-orange)](https://ffmpeg.org)
[![Code style](https://img.shields.io/badge/code%20style-ruff-black)](https://docs.astral.sh/ruff/)
[![Self-hosted](https://img.shields.io/badge/deploy-self--hosted%20%C2%B7%20Render%20%C2%B7%20Railway%20%C2%B7%20Docker-2f80ed)](#quick-start)

A high-performance, self-hosted web app that turns a pasted link from
**TikTok, Instagram, Snapchat, Facebook, X (Twitter), Reddit, Pinterest or
YouTube** into a clean, **watermark-free** download — video up to **4K (2160p)**,
**original-quality images**, or high-bitrate **MP3 / M4A** audio — with live
progress, tap-to-play 480p previews, per-quality file-size estimates,
server-side rate limiting, and a full admin/ops console.

| Post type | Offered downloads |
|-----------|-------------------|
| **Video post** | Video (4K→480p) · Audio (MP3/M4A) · Photos (cover art) |
| **Image post** | Photos (original quality) |
| **Image post with music** | Photos · **Video (image + music, auto-composed)** · Audio (the music) |

> ⚠️ **Legal note** — downloading media can breach a platform's Terms of
> Service and copyright law. Use this only for content you own or have explicit
> permission to save, for personal use. Respect creators' rights.

---

## Table of contents

- [Platform support](#platform-support)
- [Quick start](#quick-start)
- [Environment variables](#environment-variables)
- [Architecture](#architecture)
- [Watermark handling](#watermark-handling)
- [Rate limiting](#rate-limiting)
- [Image posts & composites](#image-posts--composites)
- [Inline 480p preview](#inline-480p-preview)
- [Recode Studio](#recode-studio-recode--compress--sync--repair)
- [Admin dashboard](#admin-dashboard)
- [Why platforms gate servers — and the real fixes](#why-platforms-gate-servers--and-the-real-fixes)
- [Frontend build (Tailwind)](#frontend-build-tailwind)
- [Code hygiene](#code-hygiene-run-after-every-modification)
- [Project layout](#project-layout)
- [Production hardening](#production-hardening-checklist)
- [Known platform quirks](#known-platform-quirks)

---

## Platform support

What to expect from a datacenter host (Render/Railway/VPS) — the app degrades
gracefully and always tells you *why* a platform is gated:

| Platform | Analyze (metadata) | Download | Notes |
|----------|--------------------|----------|-------|
| **TikTok** | ✅ full ladder | ✅ | Cookies (bundled/your own) unlock the full height ladder; the **tikwm fallback** serves watermark-free CDN URLs when even cookies are bot-checked |
| **X / Twitter** | ✅ | ✅ | yt-dlp direct; public **syndication CDN fallback** for gated IPs (direct .mp4 variants) |
| **YouTube** | ✅ full ladder incl. 4K60 | ⚠️ datacenter IPs get **403 at the video CDN** | Player API tolerates DC IPs (metadata is fine); the CDN doesn't. **Cookies** or a **residential relay** fix it — see [below](#why-platforms-gate-servers--and-the-real-fixes). Previews are disabled for YouTube (ToS) |
| **Instagram** | ✅ | ✅ many Reels anonymously | Stories/private posts need cookies |
| **Reddit** | ⚠️ often 403 from DC IPs | ⚠️ | Cookies or relay lift it |
| **Pinterest** | ✅ | ✅ | Probes the CDN's `/originals/` path for the **untouched upload** |
| **Snapchat** | ✅ Spotlight/Stories | ✅ | Ghost logo removed via ffmpeg `delogo` |
| **Facebook** | ✅ | ✅ public content | Private group posts need cookies |

## Quick start

```bash
cd backend
pip install -r requirements.txt          # FFmpeg comes bundled via imageio-ffmpeg
uvicorn main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000  (UI · /recode · /admin)
```

## Environment variables

All defaults come from `config.py`; dashboard-saved settings (Admin → Settings)
override them at runtime.

| Var | Default | Purpose |
|-----|---------|---------|
| `FFMPEG_PATH` | auto-detected | path to the ffmpeg binary/directory |
| `NYX_DB` | `backend/nyx.sqlite3` | SQLite database location |
| `TURSO_URL` | – | remote Turso/libsql DB (`libsql://…`) — history survives restarts/redeploys on ephemeral hosts |
| `TURSO_TOKEN` | – | access token for `TURSO_URL` |
| `RELAY_URL` | – | comma-separated sibling backends (e.g. a phone tunnel) that take over fetches when this host's egress is gated |
| `COOKIES_FILE` | – | Netscape cookies.txt for login-gated content (IG/X/TikTok/YouTube) |
| `COOKIES_TEXT` | – | raw cookie paste (Netscape export or `a=1; b=2`) — for ephemeral hosts where stored cookies die on restart; `COOKIES_DOMAIN` picks the platform for raw pastes |
| `COOKIES_B64` | – | base64 of a cookies file — same purpose as `COOKIES_TEXT`, shell-safe |
| `COOKIES_PATH` | `backend/cookies.txt` | where dashboard-saved cookies are written |
| `ADMIN_PASSWORD` | `nyx-admin` | `/admin` dashboard password — **change in production** |
| `ANALYZE_RATE_LIMIT` | `15/minute` | per-IP limit for analyze |
| `DOWNLOAD_RATE_LIMIT` | `8/hour` | per-IP limit for downloads |
| `PREVIEW_RATE_LIMIT` | `30/hour` | per-IP limit for 480p previews |
| `RECODE_RATE_LIMIT` | `12/hour` | per-IP limit for recodes |
| `UPLOAD_RATE_LIMIT` | `10/hour` | per-IP limit for Recode Studio uploads |
| `PREVIEW_HEIGHT` | `480` | preview transcode height |
| `MAX_UPLOAD_BYTES` | `800 MB` | Recode Studio per-file upload cap |
| `MAX_DOWNLOAD_BYTES` | `2 GB` | per-file download size cap |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | worker semaphore size |
| `JOB_TTL_SECONDS` | `1800` | how long finished files stick around |
| `TMP_DIR` | `backend/downloads` | media staging directory |
| `BUILD_TAG` | versioned string | shown in `/health` and Admin → Server so you can verify which build each host runs |

## Architecture

```
┌────────────────────────────────┐          ┌─────────────────────────────────────────┐
│  Frontend (vanilla JS + Tailwind) │  HTTP   │  FastAPI backend                        │
│                                │ ──────▶ │                                         │
│  • paste URL                   │         │  POST /api/analyze   → yt-dlp metadata  │
│  • platform auto-detect        │ ◀────── │  POST /api/download  → enqueue job      │
│  • preview card + quality dd   │   JSON  │  POST /api/preview   → 480p tap-to-play │
│  • Video/Photos/Audio tabs     │         │  GET  /api/jobs/{id} → live progress    │
│  • progress bar (polling)      │         │  GET  /api/jobs/{id}/file → stream file │
└────────────────────────────────┘         │  • slowapi rate limiting (per client IP)│
        served from the same origin        │  • thread pool + concurrency semaphore  │
        (no CORS, no client CORS limits)   │  • FFmpeg merge/convert + TTL cleanup   │
                                           └─────────────────────────────────────────┘
```

**Why the browser can't do this alone** — browsers block cross-origin fetches to
these platforms (CORS), can't run stream muxers, and 4K DASH downloads require
re-muxing separate video+audio tracks. All of that lives server-side in
**yt-dlp + FFmpeg**; the browser only exchanges small JSON and finally receives
one finished file.

### Request lifecycle

1. **Analyze** — `POST /api/analyze {url}`. yt-dlp extracts metadata *without*
   downloading: title, uploader, duration, thumbnail and every available
   rendition. The backend groups formats by height (2160 → 144), picks the best
   fps/bitrate per height, estimates byte sizes (exact `filesize` when the CDN
   reports it, otherwise `tbr × duration`), and returns video + audio options.
   Result is cached 5 minutes per URL.
2. **Download** — `POST /api/download {url, mode, height | audio_format}` creates
   a background **Job** (in-memory store + worker threads, capped by
   `MAX_CONCURRENT_DOWNLOADS`). yt-dlp downloads the best streams, FFmpeg merges
   them into MP4 (or converts the best audio track to ~320 kbps MP3 / native M4A).
3. **Poll** — the frontend calls `GET /api/jobs/{id}` every 700 ms and renders the
   progress bar (percent, MB/s, ETA, processing stage).
4. **Stream** — when done, `GET /api/jobs/{id}/file` streams the file with
   `Content-Disposition: attachment`. Files are garbage-collected after
   `JOB_TTL_SECONDS`.

## Watermark handling

* **TikTok** — yt-dlp's extractor returns the clean (non-watermarked) renditions
  and we pass `extractor_args` that prefer the watermark-free API endpoints.
* **Instagram / Facebook / X / Reddit / YouTube** — the served files are the
  original uploads (Shorts, Reels, tweets…), so there is no watermark to remove.
* **Snapchat** — every anonymous CDN rendition has the ghost logo + username
  burned into the top-left (no clean variant exists). Downloads are passed
  through an ffmpeg `delogo` interpolation pass (box scaled to the frame) that
  removes it; creator-added stickers/sound pills are left untouched.

## Rate limiting

Per-client limits (X-Forwarded-For aware for reverse proxies), editable live in
Admin → Settings:

| Endpoint | Default | Override env |
|----------|---------|--------------|
| `POST /api/analyze` | 15 / minute | `ANALYZE_RATE_LIMIT` |
| `POST /api/download` | 8 / hour | `DOWNLOAD_RATE_LIMIT` |
| `POST /api/preview` | 30 / hour | `PREVIEW_RATE_LIMIT` |
| `POST /api/recode/start` | 12 / hour | `RECODE_RATE_LIMIT` |
| `POST /api/recode/upload` | 10 / hour | `UPLOAD_RATE_LIMIT` |

Admin login: 5 / minute. IP bans (Abuse tab) take precedence over everything.

## Image posts & composites

- **Image posts** — when a link is a photo (e.g. a Pinterest pin), yt-dlp returns
  no video formats but a full ladder of image renditions. The analyzer surfaces
  these as `image_options`, and for Pinterest it additionally probes the CDN's
  `/originals/` path (HEAD requests) to hand you the **untouched upload** rather
  than a resized copy.
- **Image + music** — some photo posts carry an audio track. For those the UI
  offers a **Video** option: the server downloads the music, fetches the image,
  and FFmpeg muxes them into a playable MP4 slideshow
  (`-loop 1 -tune stillimage -shortest`). You also get the raw image and the
  standalone audio.
- **Direct image downloads** skip yt-dlp entirely and stream the CDN file with
  the correct `Referer` header so hotlink-protected CDNs (pinimg, twimg,
  cdninstagram…) respond.

## Inline 480p preview

`POST /api/preview` grabs the best **clean** rendition
(`format_note!=watermarked` exclusion) and transcodes it to **480p** H.264
(+faststart) so it plays instantly in the card. The preview **never autoplays** —
a "Play preview" button appears bottom-right once it's ready. Previews are
offered for every supported platform **except YouTube** (ToS), and only when the
post actually has video renditions.
Note: if a watermark is still visible, it is burned into the source pixels
(common on reposted clips) — no downloader can remove it.

## Recode Studio (`/recode`) — compress · sync · repair

A standalone page where users upload their own video (800 MB cap) and run one of
three operations:

| Mode | What it does |
|------|--------------|
| **Compress** | Lands on an **exact target size** (smaller than the original), at the **original resolution**. Two-pass x264 (or single-pass "fast" ABR, ~2× quicker). Live progress parsed from ffmpeg's `-progress` stream. |
| **Sync** | Rebuilds timestamps (`-fflags +genpts`) and forces a constant frame rate (`-fps_mode cfr`) — fixes videos that play slower than their audio because of broken VFR timing. Quality-preserving (CRF 18). |
| **Repair** | Stream-copy container rewrite first (100% original quality); falls back to a gentle re-encode (CRF 16) only for damaged streams. |

Pipeline: `POST /api/recode/upload` (multipart → save + probe) →
`GET /api/recode/plan` (the honest plan: resulting bitrates, estimated size,
bits-per-pixel quality rating `excellent → low`, warnings when the target is
aggressive) → `POST /api/recode/start`.

Output is **upload-ready** for the chosen platform: H.264 High + AAC-LC in MP4,
CFR-forced timestamps and regenerated PTS — files that already meet a platform's
codec/bitrate spec (TikTok ≤4 Mbps, Instagram ≤4.5 Mbps, YouTube ≤12 Mbps…) are
**not re-compressed when posted**.

Guarantees: target size must be < original (422 otherwise), resolution is never
scaled, and the UI states plainly that smaller targets mean more compression.

> **Why no `+faststart` on recode output?** The output is delivered as a plain
> file download (never streamed in-browser), and the flag would make ffmpeg
> silently re-write the entire file a second time after the encode — leaving
> users waiting minutes on a bar frozen at ~98%. The job completes the moment
> the last frame is encoded. (The inline 480p *preview* keeps `+faststart`
> because it is played in the browser.)

## Admin dashboard

Browse to **`/admin`**. Default credentials: **`admin`** / **`nyx-admin`**
(from `ADMIN_PASSWORD`) — change both and enable 2FA before going public.
Everything is persisted in **SQLite** (`NYX_DB`, default `backend/nyx.sqlite3`)
— history survives restarts.

| Tab | What's in it |
|---|---|
| **Overview** | Live stats, jobs/bandwidth charts (24h/3d/7d), **quick actions** (clear disk · clear all jobs · self-test · reset data), disk usage bar vs cap, CSV exports |
| **Jobs** | Full persisted history with **search**, status/platform filters, per-job **cancel** / **retry** / delete, **Clear all jobs**, and a **detailed diagnostic report** modal (structured + copyable, with the ffmpeg log tail) for every failed job |
| **Platform health** | Per-platform success rate (7d) with top error categories, yt-dlp **version-age guidance**, one-click **extractor self-test** (probes real URLs and distinguishes "broken extractor" from "IP blocked") |
| **Abuse** | Top consumer IPs by jobs/bandwidth, one-click **IP bans** (1h/24h/7d/permanent) enforced by middleware, recent-failures feed |
| **Settings** | Live-editable rate limits, TTL, size caps, preview height, **disk cap** — applied instantly, no restart. **Reset to defaults** genuinely deletes every stored override (env-var defaults from `config.py` then apply) |
| **Security** | Multi-admin accounts, **TOTP 2FA** setup, per-platform cookie management, active sessions with revoke, full **audit log** |

Maintenance endpoints behind the quick-action buttons:
`POST /api/admin/jobs/clear` · `POST /api/admin/disk/clear` ·
`POST /api/admin/data/reset` · `POST /api/admin/settings/reset`.
Every action is audit-logged. Data reset wipes jobs, deliveries, audit, bans,
health checks, lifetime counters **and** staged files — admins, sessions and
settings survive.

**Ops behaviors**

- **Disk watchdog** — banner at 80% of the cap; oldest finished files are evicted automatically past it
- Jobs that were mid-flight when the server died are marked *"Server restarted mid-job"* on boot
- Admin sessions expire after 12h; login is rate-limited 5/minute

### Deployment checklist

- [ ] `ADMIN_PASSWORD=<strong>` + rename the default admin or add your own account
- [ ] Enable 2FA (Security tab)
- [ ] Set `disk_cap_gb` for your disk, and rate limits for your hardware
- [ ] Serve behind HTTPS (nginx/Caddy); consider IP-restricting `/admin` at the proxy
- [ ] Keep yt-dlp fresh: `pip install -U yt-dlp` (the Health tab warns after 14 days)
- [ ] Back up `nyx.sqlite3` if you care about history (snapshot it to a separate branch — e.g. `nyx-data` — never to `main`)
- [ ] On ephemeral-disk hosts (Render free), dashboard-saved cookies die on restart — the bundled `seed_cookies/` (and `COOKIES_B64`/`COOKIES_TEXT`) keep login-gated platforms unlocked across redeploys

### Persistent history on ephemeral hosts (Render / Railway free)

Everything (jobs, audit, bans, settings, sessions) lives in a local SQLite file
by default — on ephemeral hosts it dies with every restart/redeploy. To make it
survive: create a free Turso database (turso.tech), then set `TURSO_URL`
(`libsql://…`) + `TURSO_TOKEN`. All hosts sharing those vars share one live
database. Without either option the app silently keeps local SQLite.

Default cookies ship bundled in `backend/seed_cookies/` — one file per platform,
named `<domain>.txt` — and re-apply on every boot. Dashboard-saved values win
over the bundled seeds.

## Why platforms gate servers — and the real fixes

Every "requires login / bot check / 403 blocked" error across **all** platforms
(Reddit 403s, Instagram login walls, YouTube's "confirm you're not a bot", the
silent 360p quality cap) shares one root cause: **your server's IP is a
datacenter IP**, and platforms trust those far less than home IPs. Railway,
Render, AWS, GCP — all datacenter. The same code on a home connection gets full
quality everywhere, anonymously.

No code rewrite changes this: it is the platform's decision about the IP, made
server-side at YouTube/Reddit/Instagram. (PO-token generators were tested too —
they "may help" per their own docs and did not lift the gate in live tests.)

**Fix 1 — cookies (5 minutes, works on any host).** Export your logged-in
session per platform (Admin → Security, or `COOKIES_TEXT`/`COOKIES_B64` env on
ephemeral hosts). Requests then arrive as *you*, bypassing the anonymous wall.
The app only attaches cookies **after** the anonymous attempt is gated, keeping
the account out of every request that doesn't need it.

**Fix 2 — residential egress (fixes every platform at once, free).** Run the
backend on your own PC and expose it with a Cloudflare tunnel. On Windows it's
two double-clicks: install cloudflared once (`winget install
Cloudflare.cloudflared`), then run `run-home.bat` — it installs deps, starts the
backend and the tunnel, and prints a public `https://….trycloudflare.com` URL to
share. Your home IP is residential → YouTube serves the full 4K ladder, Reddit
stops 403-ing; only Instagram stories stay account-gated (cookies unlock those).
Keep Render/Railway as fallbacks. Use a **named tunnel** for a stable URL.

**Fix 2b — the phone IS the server (free + permanent + no PC left on).**
Platforms blanket-block datacenter ASNs no matter which free host you pick; they
fully trust residential IPs. An old Android phone on home WiFi is the cheapest
residential host that exists: install Termux, then `bash deploy/termux.sh` — it
installs python+ffmpeg+cloudflared, boots the backend and prints a public tunnel
URL.

**The no-money stack (relay).** Keep the cloud host (Render free — free forever)
as the always-on public front, and set `RELAY_URL` on it pointing at the
phone/PC tunnel. When a fetch is gated, the whole job (download + encode) is
forwarded to the sibling on the residential IP and the file is streamed back —
the user never notices. Swap any leg for another host whenever one gets blocked
— the architecture is host-agnostic.

## Frontend build (Tailwind)

The UI ships with a **precompiled** `frontend/tailwind.css` — no CDN, no build
step needed to run. If you edit classes in any page, rebuild the stylesheet:

```bash
cd tailwind-src
npm install                    # installs tailwindcss CLI (one-time)
npx tailwindcss -i input.css -o ../frontend/tailwind.css --minify
```

`tailwind-src/tailwind.config.js` holds the theme tokens (colors are
CSS-variable driven, so dark mode inverts automatically).

After rebuilding, run the class guard — it exits non-zero if any class used in
the markup/JS is missing from the compiled CSS (the silent "glued layout" bug
class can never ship again):

```bash
python3 frontend/check_classes.py
```

## Code hygiene (run after EVERY modification)

`bash hygiene.sh` runs the Code Hygiene Protocol over backend + frontend and
exits non-zero until the tree is pristine:

| Phase | What it does |
|---|---|
| **1 — Scan** | Python syntax · ruff (unused imports, undefined names, bugbear real-bug patterns) · frontend JS syntax (all pages + app.js) · class guard (markup classes ⊆ compiled CSS) · scope-aware duplicate-definition check |
| **2 — Clean** | Auto-fixes safe findings (unused imports, missing `raise … from`) — idempotent |
| **3 — Verify** | Import/build test (whole module graph loads — catches circular imports) · dead-config check (every env default is consumed) · final ruff gate |

Gate before any submit: syntax ✅ · no redundancy ✅ · no deprecated methods ✅
· clean formatting ✅ · working features ✅ (the import test + class guard are
the automated half; exercise the touched feature in a browser for the rest).

CI variant: `bash hygiene.sh --check` (verify only, never edits files).

## Project layout

```
nyx-downloader/
├── backend/
│   ├── main.py            # FastAPI app, routes, static hosting, rate limiter, admin API
│   ├── extractors.py      # platform detection + yt-dlp analysis/format selection + fallbacks
│   ├── jobs.py            # job engine: worker threads, progress hooks, relay, cleanup
│   ├── db.py              # SQLite/Turso persistence: jobs, settings, audit, sessions
│   ├── auth.py            # PBKDF2 hashing, RFC-6238 TOTP, dynamic per-IP rate guard
│   ├── uploads.py         # Recode Studio upload store + plan/size math
│   ├── config.py          # env-driven config (ffmpeg, limits, TTL, caps)
│   ├── entrypoint.sh      # container entrypoint
│   ├── seed_cookies/      # bundled per-platform cookies, re-applied on every boot
│   └── requirements.txt
├── frontend/
│   ├── index.html         # Apple-style Tailwind UI (downloader)
│   ├── recode.html        # Recode Studio (compress / sync / repair)
│   ├── admin.html         # admin dashboard (production console)
│   ├── app.js             # state machine, API client, polling, SVG brand icons
│   ├── tailwind.css       # precompiled stylesheet (no build step to run)
│   ├── check_classes.py   # class guard: markup classes must exist in compiled CSS
│   └── favicon.svg
├── tailwind-src/          # Tailwind build source (only needed to recompile CSS)
├── deploy/termux.sh       # run the backend on an Android phone (residential egress)
├── hygiene.sh             # Code Hygiene Protocol (scan / clean / verify gate)
├── Dockerfile · render.yaml · railway.json
└── run-home.bat · start-backend.bat   # Windows: home-PC + Cloudflare tunnel
```

## Production hardening checklist

- Run behind **nginx/Caddy + HTTPS**; put `uvicorn --workers N` behind it (keep
  job state in **Redis** if you use >1 worker — the `Job` dataclass is
  JSON-ready).
- Add **auth/API keys** on top of the rate limiter, and a CDN in front of file
  delivery.
- Pin `yt-dlp` and upgrade it frequently (`pip install -U yt-dlp`) — extractors
  break whenever platforms change; a cron job or health-check keeps this alive.
- Consider a **job queue** (Celery/RQ) and object storage (S3 presigned URLs) at
  scale.
- Log every request and enforce your jurisdiction's copyright/ToS obligations.

## Known platform quirks

- **Instagram**: many Reels work anonymously; stories/private posts need cookies.
- **Snapchat**: public Spotlight links work via yt-dlp's `SnapchatSpotlight`
  extractor; profile stories may be gated.
- **YouTube**: datacenter IPs get a 403 at the video CDN even when metadata
  works — cookies resolve it, or relay to a residential IP.
- **Reddit**: datacenter IPs are frequently 403'd — cookies or relay.
- **Facebook**: private group posts require cookies.
