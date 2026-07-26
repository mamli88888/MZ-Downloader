FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && adduser --disabled-password --gecos "" appuser \
    && mkdir -p /tmp/mz-downloader \
    && chown -R appuser:appuser /app /tmp/mz-downloader

COPY --chown=appuser:appuser bot.py config.py downloader.py routing.py instagram_caption.py spotisaver.py youtube_search.py users_db.py gofile_uploadv ./

USER appuser

CMD ["python", "-u", "bot.py"]
