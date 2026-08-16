@echo off
setlocal
title NyxDownloader — home hosting
cd /d "%~dp0"

echo ============================================
echo   NyxDownloader — home hosting (residential IP)
echo ============================================

where python >nul 2>&1 || (
  echo [!] Python not found. Install Python 3.10+ from python.org (tick "Add to PATH").
  pause & exit /b 1
)

echo [1/3] Installing dependencies...
python -m pip install -r backend\requirements.txt --quiet --disable-pip-version-check

echo [2/3] Starting backend on http://localhost:8000 ...
start "NyxBackend" /min "%~dp0start-backend.bat"

echo [3/3] Starting Cloudflare tunnel...
where cloudflared >nul 2>&1 || (
  echo.
  echo [!] cloudflared not found. Install it ONCE with:
  echo       winget install Cloudflare.cloudflared
  echo The backend is already running locally at http://localhost:8000
  pause & exit /b 1
)

echo.
echo A PUBLIC URL like https://random-words.trycloudflare.com will appear below.
echo Share THAT with your users. Keep this window open while hosting.
echo.
cloudflared tunnel --url http://localhost:8000

pause
