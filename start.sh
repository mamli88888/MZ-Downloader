#!/usr/bin/env bash
# start.sh — launches both the cobalt API server and the MZ-Downloader bot
# in a single container, using a simple child-process model. If either one
# exits, the other is killed and the container restarts (via Docker's
# `restart: unless-stopped` policy).

set -e

echo "[start] MZ-Downloader + Cobalt container starting..."

# ---------------------------------------------------------------------------
# 1. Start the cobalt API server in the background
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

# Give cobalt a moment to bind its port before we start the bot, so the bot's
# initial COBALT_GATEWAY health probe doesn't fail.
sleep 3

# Trap SIGTERM/SIGINT and forward to both children.
trap 'echo "[start] Shutting down..."; kill -TERM $COBALT_PID 2>/dev/null || true; kill -TERM $BOT_PID 2>/dev/null || true; wait' TERM INT

# ---------------------------------------------------------------------------
# 2. Start the MZ-Downloader bot in the foreground
# ---------------------------------------------------------------------------
echo "[start] Starting MZ-Downloader bot..."
cd /app
# Tell the bot where cobalt is. The bot reads this on startup.
export COBALT_API_URL="${API_URL}"
export FFMPEG_PATH="${FFMPEG_PATH:-/usr/bin/ffmpeg}"

python -u bot.py &
BOT_PID=$!

# Wait for either process to exit. If one dies, we want to restart the whole
# container (Docker's `restart: unless-stopped` will bring it back up).
echo "[start] Both processes started. cobalt PID=${COBALT_PID}, bot PID=${BOT_PID}. Waiting..."
wait -n $COBALT_PID $BOT_PID
EXIT_CODE=$?

echo "[start] One of the processes exited (code=${EXIT_CODE}). Shutting down..."
kill -TERM $COBALT_PID 2>/dev/null || true
kill -TERM $BOT_PID 2>/dev/null || true
wait || true

exit $EXIT_CODE
