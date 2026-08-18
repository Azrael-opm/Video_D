FROM python:3.12-slim

# FFmpeg — required for merging, transcoding and the Recode Studio
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

ENV PYTHONUNBUFFERED=1
EXPOSE 8000

WORKDIR /app/backend
# entrypoint self-updates yt-dlp first (skip with NO_YTDLP_UPDATE=1)
CMD ["sh", "entrypoint.sh"]
