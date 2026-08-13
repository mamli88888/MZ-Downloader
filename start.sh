#!/usr/bin/env bash
# start.sh — launches cobalt API + bgutil PO Token server + MZ-Downloader bot
# in a single container, using a simple child-process model. If any one exits,
# the others are killed and the container restarts (via Docker's
# `restart: unless-stopped` policy or Railway's restart policy).
#
# Startup order:
#   1. bgutil PO Token server  (port 4416) — needed by yt-dlp for YouTube
#      bot-detection bypass. Optional: if it fails, yt-dlp still works but
#      YouTube may return "Sign in to confirm you're not a bot" errors.
#   2. cobalt API              (port 9000) — needed for Instagram/TikTok/etc.
#      and as a YouTube fallback. If it fails, the bot auto-disables the
#      cobalt gateway and falls back to Telegram bots.
#   3. MZ-Downloader bot       (Python)    — the Telegram bot itself.
#
# The bot's post_init probe checks both services and degrades gracefully.

set -e

echo "[start] MZ-Downloader + Cobalt + bgutil container starting..."

# ---------------------------------------------------------------------------
# 1. Start the bgutil PO Token server in the background
# ---------------------------------------------------------------------------
# bgutil generates Proof-of-Origin (PO) Tokens that YouTube requires to
# download videos without the "Sign in to confirm you are not a bot"
# challenge. yt-dlp auto-discovers the bgutil plugin (installed via
# `pip install bgutil-ytdlp-pot-provider`) and calls this server.
BGUTIL_DIR="/opt/bgutil"
BGUTIL_PORT="${BGUTIL_PORT:-4416}"
BGUTIL_TOKEN_TTL="${BGUTIL_TOKEN_TTL:-6}"

if [ -f "${BGUTIL_DIR}/build/main.js" ]; then
    echo "[start] Starting bgutil PO Token server on port ${BGUTIL_PORT}..."
    cd "${BGUTIL_DIR}"
    TOKEN_TTL="${BGUTIL_TOKEN_TTL}" node build/main.js --port "${BGUTIL_PORT}" &
    BGUTIL_PID=$!
    echo "[start] bgutil started (PID=${BGUTIL_PID})"

    # Poll bgutil's /ping endpoint for up to 30s. If it doesn't come up,
    # we proceed anyway — yt-dlp will work but YouTube bot-detection bypass
    # will be degraded (the bot logs a warning but doesn't crash).
    echo "[start] Waiting for bgutil PO Token server to be ready (up to 30s)..."
    BGUTIL_READY=0
    for i in $(seq 1 30); do
        if curl -sf --max-time 1 "http://127.0.0.1:${BGUTIL_PORT}/ping" >/dev/null 2>&1; then
            echo "[start] bgutil PO Token server ready after ${i}s"
            BGUTIL_READY=1
            break
        fi
        sleep 1
    done
    if [ "$BGUTIL_READY" -ne 1 ]; then
        echo "[start] WARNING: bgutil PO Token server not ready after 30s. YouTube bot-detection bypass will be degraded."
        # Don't kill the process — it might still come up. yt-dlp will handle the failure.
    fi
else
    echo "[start] bgutil server not found at ${BGUTIL_DIR}/build/main.js — skipping (YouTube bot-detection bypass will be degraded)"
    BGUTIL_PID=""
fi

# ---------------------------------------------------------------------------
# 2. Start the cobalt API server in the background
# ---------------------------------------------------------------------------
# Cobalt reads its config from environment variables. The defaults below are
# safe for a single-tenant instance bound to localhost — override them via
# `docker run -e ...` or `environment:` in docker-compose.yml.
export API_URL="${COBALT_API_URL:-http://127.0.0.1:9000/}"
export API_PORT="${COBALT_API_PORT:-9000}"
export API_LISTEN_ADDRESS="${API_LISTEN_ADDRESS:-127.0.0.1}"
# Cobalt's default duration limit (3 hours) is fine for most use cases.
export DURATION_LIMIT="${DURATION_LIMIT:-10800}"
# Rate limits — generous since this is a single-user instance.
export RATELIMIT_MAX="${RATELIMIT_MAX:-200}"
export TUNNEL_RATELIMIT_MAX="${TUNNEL_RATELIMIT_MAX:-400}"

# If the user supplied a cookies.json (for Instagram / YouTube member content),
# mount it at /cookies.json and point cobalt at it.
if [ -n "${COOKIE_PATH:-}" ] && [ -f "${COOKIE_PATH}" ]; then
    export COOKIE_PATH="${COOKIE_PATH}"
fi

echo "[start] Starting cobalt API on ${API_LISTEN_ADDRESS}:${API_PORT}..."
cd /opt/cobalt/api
# `node src/cobalt.js` runs the API in the foreground; we background it.
node src/cobalt.js &
COBALT_PID=$!

# Poll cobalt's / endpoint for up to 60s before starting the bot, so the bot's
# initial COBALT_GATEWAY health probe doesn't fail and trigger a fallback to
# the Telegram bots. If cobalt doesn't come up in 60s we proceed anyway —
# the bot's own startup probe will disable cobalt gracefully.
echo "[start] Waiting for cobalt API to be ready (up to 60s)..."
COBALT_READY=0
for i in $(seq 1 60); do
    if curl -sf --max-time 1 "http://${API_LISTEN_ADDRESS}:${API_PORT}/" >/dev/null 2>&1; then
        echo "[start] Cobalt API ready after ${i}s"
        COBALT_READY=1
        break
    fi
    sleep 1
done
if [ "$COBALT_READY" -ne 1 ]; then
    echo "[start] WARNING: cobalt API not ready after 60s, starting bot anyway (cobalt will be auto-disabled by bot's startup probe)"
fi

# Trap SIGTERM/SIGINT and forward to all children.
trap 'echo "[start] Shutting down..."; kill -TERM $COBALT_PID $BGUTIL_PID $BOT_PID 2>/dev/null || true; wait' TERM INT

# ---------------------------------------------------------------------------
# 3. Start the MZ-Downloader bot in the foreground
# ---------------------------------------------------------------------------
echo "[start] Starting MZ-Downloader bot..."
cd /app
# Tell the bot where cobalt + bgutil are. The bot reads these on startup.
export COBALT_API_URL="${API_URL}"
export FFMPEG_PATH="${FFMPEG_PATH:-/usr/bin/ffmpeg}"
export YTDLP_ENABLED="${YTDLP_ENABLED:-true}"
export YTDLP_BGUTIL_BASE_URL="${YTDLP_BGUTIL_BASE_URL:-http://127.0.0.1:4416}"

python -u bot.py &
BOT_PID=$!

# Wait for any process to exit. If one dies, we want to restart the whole
# container (Docker's `restart: unless-stopped` will bring it back up).
echo "[start] All processes started. cobalt PID=${COBALT_PID}, bgutil PID=${BGUTIL_PID:-none}, bot PID=${BOT_PID}. Waiting..."
wait -n $COBALT_PID $BOT_PID 2>/dev/null
EXIT_CODE=$?
# Also wait for bgutil if it was started (ignore its exit code — it's optional)
if [ -n "$BGUTIL_PID" ]; then
    kill -TERM $BGUTIL_PID 2>/dev/null || true
fi

echo "[start] One of the processes exited (code=${EXIT_CODE}). Shutting down..."
kill -TERM $COBALT_PID 2>/dev/null || true
kill -TERM $BGUTIL_PID 2>/dev/null || true
kill -TERM $BOT_PID 2>/dev/null || true
wait || true

exit $EXIT_CODE
