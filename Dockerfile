FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg is required by yt-dlp's FFmpegExtractAudio postprocessor (used
# by SoundCloud / Pinterest / Twitter / Facebook / generic audio downloads).
# Without it, yt-dlp can download video but cannot convert to MP3.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && adduser --disabled-password --gecos "" appuser \
    && mkdir -p /tmp/mz-downloader \
    && chown -R appuser:appuser /app /tmp/mz-downloader

COPY --chown=appuser:appuser bot.py config.py downloader.py routing.py instagram_caption.py spotisaver.py youtube_search.py social_gateway.py apify_gateway.py apify_platforms.py users_db.py pixeldrain_upload.py mz_shazam_search.py youtube_subtitle.py instagram_profile.py feature_flags.py store.py token_alerts.py user_features.py ai_service.py perf.py media_size.py structured_logging.py ahm7_gateway.py yoinku_gateway.py ./
COPY --chown=appuser:appuser migrations ./migrations

USER appuser

CMD ["python", "-u", "bot.py"]
