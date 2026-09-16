# NyxDownloader

A high-performance web app that turns a pasted link from **TikTok, Instagram, Snapchat,
Facebook, X (Twitter), Reddit, Pinterest or YouTube** into a clean, **watermark-free** download —
video up to **4K (2160p)**, **original-quality images**, or high-bitrate **MP3 / M4A** audio —
with live progress, tap-to-play 480p previews, per-quality file-size estimates and
server-side rate limiting.

### What you get per post type
| Post type | Offered downloads |
|-----------|-------------------|
| **Video post** | Video (4K→480p) · Audio (MP3/M4A) · Photos (cover art) |
| **Image post** | Photos (original quality) |
| **Image post with music** | Photos · **Video (image + music, auto-composed)** · Audio (the music) |

> ⚠️ **Legal note** — downloading media can breach a platform's Terms of Service and
> copyright law. Use this only for content you own or have explicit permission to save,
> for personal use. Respect creators' rights.

---

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

### Why the browser can't do this alone
Browsers block cross-origin fetches to these platforms (CORS), can't run stream muxers,
and 4K DASH downloads require re-muxing separate video+audio tracks. All of that lives
server-side in **yt-dlp + FFmpeg**; the browser only exchanges small JSON and finally
receives one finished file.

### Request lifecycle
1. **Analyze** — `POST /api/analyze {url}`. yt-dlp extracts metadata *without*
   downloading: title, uploader, duration, thumbnail and every available rendition.
   The backend groups formats by height (2160 → 144), picks the best fps/bitrate per
   height, estimates byte sizes (exact `filesize` when the CDN reports it, otherwise
   `tbr × duration`), and returns video + audio options.
2. **Download** — `POST /api/download {url, mode, height | audio_format}` creates a
   background **Job** (in-memory store + worker threads, capped by
   `MAX_CONCURRENT_DOWNLOADS`). yt-dlp downloads the best streams, FFmpeg merges them
   into MP4 (or converts the best audio track to ~320 kbps MP3 / native M4A).
3. **Poll** — the frontend calls `GET /api/jobs/{id}` every 700 ms and renders the
   progress bar (percent, MB/s, ETA, processing stage).
4. **Stream** — when done, `GET /api/jobs/{id}/file` streams the file with
   `Content-Disposition: attachment`. Files are garbage-collected after `JOB_TTL_SECONDS`.

### Watermark handling
* **TikTok** — yt-dlp's extractor returns the clean (non-watermarked) renditions and we
  pass `extractor_args` that prefer the watermark-free API endpoints.
* **Instagram / Facebook / X / Reddit / YouTube** — the served files are the
  original uploads (Shorts, Reels, tweets…), so there is no watermark to remove.
* **Snapchat** — every anonymous CDN rendition has the ghost logo + username
  burned into the top-left (no clean variant exists). Downloads are passed
  through an ffmpeg `delogo` interpolation pass (box scaled to the frame) that
  removes it; creator-added stickers/sound pills are left untouched.

### Rate limiting
`slowapi` enforces per-client limits (X-Forwarded-For aware for reverse proxies):

| Endpoint          | Default        | Override env        |
|-------------------|----------------|---------------------|
| `POST /api/analyze`  | 15 / minute | `ANALYZE_RATE_LIMIT` |
| `POST /api/download` | 8 / hour    | `DOWNLOAD_RATE_LIMIT`|
| `POST /api/preview`  | 30 / hour   | `PREVIEW_RATE_LIMIT` |

---

## Image posts & "image + music" composites

- **Image posts** — when a link is a photo (e.g. a Pinterest pin), yt-dlp returns no
  video formats but a full ladder of image renditions. The analyzer surfaces these as
  `image_options`, and for Pinterest it additionally probes the CDN's `/originals/`
  path (HEAD requests) to hand you the **untouched upload** rather than a resized copy.
- **Image + music** — some photo posts carry an audio track. For those the UI offers a
  **Video** option: the server downloads the music, fetches the image, and FFmpeg muxes
  them into a playable MP4 slideshow (`-loop 1 -tune stillimage -shortest`). You also
  get the raw image and the standalone audio.
- **Direct image downloads** skip yt-dlp entirely and stream the CDN file with the
  correct `Referer` header so hotlink-protected CDNs (pinimg, twimg, cdninstagram…) respond.

## Inline 480p preview

`POST /api/preview` grabs the best **clean** rendition (`format_note!=watermarked`
exclusion) and transcodes it to **480p** H.264 (+faststart) so it plays instantly in
the card. The preview **never autoplays** — a "Play preview" button appears
bottom-right once it's ready. Previews are offered for every supported platform
**except YouTube** (ToS), and only when the post actually has video renditions.
Note: if a watermark is still visible, it is burned into the source pixels (common on
reposted clips) — no downloader can remove it.

## Recode Studio (`/recode`) — compress your own videos

A standalone page where users upload their own video and compress it to an
**exact target size**, smaller than the original, at the **original resolution**:

1. `POST /api/recode/upload` (multipart, 800 MB cap) → saves + probes
   (duration, resolution, fps, codec, bitrate)
2. `GET /api/recode/plan?upload_id&target_mb&preset` → the honest plan:
   resulting bitrates, estimated size, and a quality rating derived from
   bits-per-pixel (`excellent → low`), with warnings when the target is aggressive
3. `POST /api/recode/start` → **two-pass x264** encode that lands on the target
   size (`-pass 1` analysis + `-pass 2` with `-maxrate` headroom), with live
   progress parsed from ffmpeg's `-progress` stream

Upload-ready output for the chosen platform: H.264 High + AAC-LC in MP4,
CFR-forced timestamps and regenerated PTS — files that already meet
a platform's codec/bitrate spec (TikTok ≤4 Mbps, Instagram ≤4.5 Mbps, YouTube
≤12 Mbps…) are **not re-compressed when posted**.

Recode output is delivered as a plain file download (never streamed in-browser),
so it is encoded **without `+faststart`**: the flag would otherwise make ffmpeg
silently re-write the entire file a second time after the encode, which left
users waiting minutes on a bar frozen at ~98%. The job now completes the
moment the last frame is encoded. (The inline 480p *preview* still uses
`+faststart` because it is played in the browser.)

Guarantees: target size must be < original (422 otherwise), resolution is never
scaled, and the UI states plainly that smaller targets mean more compression.

## Recode (downloaded files)

`POST /api/recode {job_id, preset}` re-encodes a finished video download:

- **Container repair** — `-fflags +genpts` rebuilds missing timestamps (fixes files
  with no duration metadata), `-fps_mode cfr` forces a constant frame rate, which
  fixes the "video plays slower than the audio" drift caused by broken VFR timing.
- **Upload-ready presets** — `tiktok`, `instagram`, `youtube`, `facebook`,
  `snapchat`: CRF-driven H.264 High + AAC 160–192k with each platform's recommended
  bitrate ceiling. Resolution is **never scaled**; the encoder also caps at the
  source's own bitrate, so the output is always ≤ the input size.

## Admin dashboard (production console)

Browse to **`/admin`**. Default credentials: username **`admin`**, password
**`nyx-admin`** (from `ADMIN_PASSWORD` env) — change both and enable 2FA before
going public. Everything is persisted in **SQLite** (`backend/nyx.sqlite3`,
override with `NYX_DB` env) — history survives restarts.

| Tab | What's in it |
|---|---|
| **Overview** | Live stats, jobs/bandwidth charts (24h/3d/7d), **quick actions** (clear disk · clear all jobs · self-test · reset data), disk usage bar vs cap, CSV exports |
| **Jobs** | Full persisted history with **search**, status/platform filters, per-job **cancel** / **retry** / delete, **Clear all jobs**, and a **detailed diagnostic report** modal (structured + copyable, with the ffmpeg log tail) for every failed job |
| **Platform health** | Per-platform success rate (7d) with top error categories, yt-dlp **version-age guidance**, one-click **extractor self-test** (probes real TikTok/Pinterest/YouTube URLs and distinguishes "broken extractor" from "IP blocked") |
| **Abuse** | Top consumer IPs by jobs/bandwidth, one-click **IP bans** (1h/24h/7d/permanent) enforced by middleware, recent-failures feed |
| **Settings** | Live-editable rate limits, TTL, size caps, preview height, **disk cap** — applied instantly, no restart. **Reset to defaults** genuinely deletes every stored override |
| **Security** | Multi-admin accounts, **TOTP 2FA** setup, active sessions with revoke, full **audit log** (logins, bans, deletes, config changes) |

Maintenance endpoints behind the quick-action buttons:
`POST /api/admin/jobs/clear` · `POST /api/admin/disk/clear` ·
`POST /api/admin/data/reset` · `POST /api/admin/settings/reset`.
Every action is audit-logged. Data reset wipes jobs, deliveries, audit, bans,
health checks, lifetime counters **and** staged files — admins, sessions and settings survive.

Ops behaviors:
- **Disk watchdog** — banner at 80% of the cap; oldest finished files are evicted
  automatically past it
- Jobs that were mid-flight when the server died are marked
  *"Server restarted mid-job"* on boot
- Admin sessions expire after 12h; login is rate-limited 5/minute

### Deployment checklist
1. `ADMIN_PASSWORD=<strong>` + rename the default admin or add your own account
2. Enable 2FA (Security tab)
3. Set `disk_cap_gb` for your disk, and rate limits for your hardware
4. Serve behind HTTPS (nginx/Caddy); consider IP-restricting `/admin` at the proxy
5. Keep yt-dlp fresh: `pip install -U yt-dlp` (the Health tab warns after 14 days)
6. Back up `nyx.sqlite3` if you care about history
7. On ephemeral-disk hosts (Render free) the SQLite-stored cookies die on every
   restart — set `COOKIES_B64` (or `COOKIES_TEXT`) so login-gated platforms stay
   unlocked across restarts

### Persistent history on ephemeral hosts (Render / Railway free)

Everything (jobs, audit, bans, settings, sessions) lives in a local SQLite
file by default — on ephemeral hosts it dies with every restart/redeploy.
To make it survive: create a free Turso database (turso.tech), then set
`TURSO_URL` (`libsql://…`) + `TURSO_TOKEN`. All hosts sharing those vars share
one live database. Without either option the app silently keeps local SQLite.

(Instagram default cookies ship bundled in `backend/seed_cookies.txt` and
re-apply on every boot.)

---

## Frontend build (Tailwind)

The UI ships with a **precompiled** `frontend/tailwind.css` — no CDN, no build step
needed to run. If you edit classes in any page, rebuild the stylesheet:

```bash
cd tailwind-src
npm install                    # installs tailwindcss CLI (one-time)
npx tailwindcss -i input.css -o ../frontend/tailwind.css --minify
```

`tailwind-src/tailwind.config.js` holds the theme tokens (colors are CSS-variable
driven, so dark mode inverts automatically).

After rebuilding, run the class guard — it exits non-zero if any class used in
the markup/JS is missing from the compiled CSS (the silent "glued layout" bug
class can never ship again):

```bash
python3 frontend/check_classes.py
```

## Project layout

```
nyx-downloader/
├── backend/
│   ├── main.py            # FastAPI app, routes, static hosting, rate limiter
│   ├── extractors.py      # platform detection + yt-dlp analysis/format selection
│   ├── jobs.py            # job engine: worker threads, progress hooks, cleanup
│   ├── config.py          # env-driven config (ffmpeg, limits, TTL, quality ladder)
│   └── requirements.txt
└── frontend/
    ├── index.html         # Apple-style Tailwind UI (Play CDN + custom styles)
    └── app.js             # state machine, API client, polling, SVG brand icons
```

## Running it

```bash
cd backend
pip install -r requirements.txt          # FFmpeg comes bundled via imageio-ffmpeg
uvicorn main:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

### Environment variables
| Var | Default | Purpose |
|-----|---------|---------|
| `FFMPEG_PATH` | auto-detected | path to the ffmpeg binary/directory |
| `COOKIES_FILE` | – | Netscape cookies.txt for login-gated content (some IG/X/TikTok) |
| `COOKIES_B64` | – | base64 of a cookies file — for ephemeral-disk hosts (Render free) where the dashboard-saved cookies die on restart; env survives |
| `TURSO_URL` | – | remote Turso/libsql DB (`libsql://…`) — history survives restarts/redeploys on ephemeral hosts |
| `TURSO_TOKEN` | – | access token for `TURSO_URL` |
| `COOKIES_TEXT` | – | raw cookie paste (Netscape export or `a=1; b=2`), same purpose as `COOKIES_B64`; `COOKIES_DOMAIN` picks the platform for raw pastes |
| `ANALYZE_RATE_LIMIT` | `15/minute` | slowapi limit for analyze |
| `DOWNLOAD_RATE_LIMIT` | `8/hour` | slowapi limit for downloads |
| `PREVIEW_RATE_LIMIT` | `30/hour` | slowapi limit for 480p previews |
| `PREVIEW_HEIGHT` | `480` | preview transcode height |
| `RECODE_RATE_LIMIT` | `12/hour` | slowapi limit for recodes |
| `ADMIN_PASSWORD` | `nyx-admin` | `/admin` dashboard password — change in production |
| `MAX_CONCURRENT_DOWNLOADS` | `3` | worker semaphore size |
| `JOB_TTL_SECONDS` | `1800` | how long finished files stick around |
| `MAX_DOWNLOAD_BYTES` | `2 GB` | per-file size cap |
| `TMP_DIR` | `backend/downloads` | media staging directory |

## Production hardening checklist
- Run behind **nginx/Caddy + HTTPS**; put `uvicorn --workers N` behind it (keep job
  state in **Redis** if you use >1 worker — the `Job` dataclass is JSON-ready).
- Add **auth/API keys** on top of the rate limiter, and a CDN in front of file delivery.
- Pin `yt-dlp` and upgrade it frequently (`pip install -U yt-dlp`) — extractors break
  whenever platforms change; a cron job or health-check keeps this alive.
- Consider a **job queue** (Celery/RQ) and object storage (S3 presigned URLs) at scale.
- Log every request and enforce your jurisdiction's copyright/ToS obligations.

## Known platform quirks
- **Instagram**: many Reels work anonymously; stories/private posts need `COOKIES_FILE`.
- **Snapchat**: public Spotlight/Story links work; some deep links are login-gated.
- **YouTube**: datacenter IPs occasionally get a bot check; cookies resolve it.
- **Facebook**: private group posts require cookies.
- **Snapchat**: public Spotlight links work; yt-dlp's `SnapchatSpotlight` extractor
  handles them, profile stories may be gated.

## Why platforms gate servers — and the two real fixes

Every "requires login / bot check / 403 blocked" error across **all** platforms
(Reddit 403s, Instagram login walls, YouTube's "confirm you're not a bot", the
silent 360p quality cap) shares one root cause: **your server's IP is a
datacenter IP**, and platforms trust those far less than home IPs. Railway,
Render, AWS, GCP — all datacenter. The same code on a home connection gets
full quality everywhere, anonymously.

No code rewrite changes this: it is the platform's decision about the IP, made
server-side at YouTube/Reddit/Instagram. (PO-token generators were tested too —
they "may help" per their own docs and did not lift the gate in live tests.)

**Fix 1 — cookies (5 minutes, works on any host).** Export your logged-in
session per platform (Admin → Security, or `COOKIES_TEXT`/`COOKIES_B64` env on
ephemeral hosts). Requests then arrive as *you*, bypassing the anonymous wall.

**Fix 2 — residential egress (fixes every platform at once, free).** Run the
backend on your own PC and expose it with a Cloudflare quick tunnel. On
Windows it's two double-clicks: install cloudflared once
(`winget install Cloudflare.cloudflared`), then run `run-home.bat` — it installs
deps, starts the backend and the tunnel, and prints a public
`https://….trycloudflare.com` URL to share. Your home IP is residential →
YouTube serves the full 4K ladder, Reddit stops 403-ing; only Instagram stories
stay account-gated (cookies unlock those). Keep Railway/Render as fallbacks.

**Fix 2b — the phone IS the server (free + permanent + no PC left on).**
Platforms blanket-block datacenter ASNs no matter which free host you pick;
they fully trust residential IPs. An old Android phone on home WiFi is the
cheapest residential host that exists: install Termux, then
`bash deploy/termux.sh` — it installs python+ffmpeg+cloudflared, boots the
backend and prints a public tunnel URL. Keep the cloud hosts as the always-on
public front and set `RELAY_URL` on them pointing at the phone, so all
platform fetching rides the residential IP while the cloud serves UI/files.

**After the Railway trial ends (no-money stack):** Render free (public UI,
sleeps/wakes fine) + phone via Termux (fetch leg). Relay glues them together;
swap any leg for another free PaaS whenever one gets blocked — the architecture
is host-agnostic.
