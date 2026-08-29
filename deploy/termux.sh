#!/data/data/com.termux/files/usr/bin/bash
# NyxDownloader on Android (Termux) — the free, permanent, RESIDENTIAL-IP host.
# Platforms trust home IPs, so datacenter blanket-blocks don't apply here.
#
#   1. install F-Droid, then Termux from F-Droid
#   2. in Termux:  git clone https://github.com/Azrael-opm/Video_D nyx
#                  cd nyx && bash deploy/termux.sh
#   3. keep the phone plugged in; the script takes a wake-lock so Android
#      won't sleep the process
set -e
cd "$(dirname "$0")/.."

echo "[1/4] system packages…"
pkg update -y
pkg install -y git python ffmpeg cloudflared

echo "[2/4] python deps (strip extras Android can't build)…"
python -m pip install --upgrade pip
sed 's/uvicorn\[standard\]/uvicorn/; /imageio-ffmpeg/d' backend/requirements.txt > /tmp/req.txt
pip install -r /tmp/req.txt

echo "[3/4] starting backend on :8000…"
(cd backend && nohup python -m uvicorn main:app --host 0.0.0.0 --port 8000 \
  > /tmp/nyx.log 2>&1 &)

echo "[4/4] starting cloudflare tunnel (public URL printed below)…"
termux-wake-lock 2>/dev/null || true
sleep 2
cloudflared tunnel --url http://localhost:8000 2>&1 | tee /tmp/tunnel.log &

sleep 5
echo
echo "backend : http://localhost:8000"
echo "public  : $(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/tunnel.log | head -1)"
echo
echo "give that https URL to your users, or set it as RELAY_URL on"
echo "Render/Railway so the cloud hosts borrow this phone's residential IP."
echo "stop with: termux-wake-unlock && pkill -f uvicorn && pkill -f cloudflared"
