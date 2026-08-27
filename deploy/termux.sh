#!/data/data/com.termux/files/usr/bin/bash
# ─────────────────────────────────────────────────────────────
# NyxDownloader on an Android phone (Termux) — the free, permanent,
# RESIDENTIAL-IP host. Platforms trust home IPs, so blanket datacenter
# blocks (TikTok et al.) simply don't apply here.
#
# 1. install F-Droid, then Termux from F-Droid
# 2. in Termux:  git clone https://github.com/Azrael-opm/Video_D nyx
#                bash nyx/deploy/termux.sh
# 3. keep the phone awake-ish: Settings → Developer options →
#    "Stay awake" while charging, and give Termux a wakelock:
#                termux-wake-lock
# ─────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")/.."

echo "[1/4] packages…"
pkg update -y
pkg install -y python pip git ffmpeg cloudflared || pkg install -y python pip git ffmpeg

echo "[2/4] python deps…"
pip install --upgrade pip
# imageio-ffmpeg ships x86_64 binaries (useless on ARM) — install the rest,
# the app falls back to the system ffmpeg from pkg above.
grep -v imageio-ffmpeg backend/requirements.txt > /tmp/req.txt
pip install -r /tmp/req.txt
pip install yt-dlp --upgrade

echo "[3/4] starting backend on :8000…"
(cd backend && nohup python -m uvicorn main:app --host 0.0.0.0 --port 8000 \
  > /tmp/nyx.log 2>&1 &)

echo "[4/4] starting cloudflare tunnel (public URL printed below)…"
sleep 3
cloudflared tunnel --url http://localhost:8000 2>&1 | tee /tmp/tunnel.log &

echo
echo "✔ backend:  http://localhost:8000"
echo "✔ watch the tunnel log above for:  https://XXXX.trycloudflare.com"
echo "  give THAT url to your users, or set it as RELAY_URL on Render/Railway"
echo "  so the cloud hosts borrow this phone's residential IP for fetching."
