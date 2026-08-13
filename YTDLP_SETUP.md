# yt-dlp + bgutil PO Token — Setup Guide

This document explains how to use the new yt-dlp YouTube downloader that
bypasses YouTube's "Sign in to confirm you are not a bot" challenge.

## Architecture

```
User → Telegram bot → YtDlpGateway
                          │
                          ▼
                  yt-dlp (with bgutil plugin)
                          │
                          ▼
                  bgutil PO Token server (HTTP, port 4416)
                          │
                          ▼
                  (optional proxy if in Iran)
                          │
                          ▼
                      YouTube
```

The `bgutil-ytdlp-pot-provider` plugin (installed via `pip`) auto-discovers
yt-dlp's plugin entry point. yt-dlp calls the bgutil HTTP server to generate
Proof-of-Origin (PO) Tokens, which YouTube now requires for most downloads.

## Quick start (Docker)

The updated `docker-compose.yml` already includes the bgutil container.
Just run:

```bash
cp .env.example .env
# Edit .env to set BOT_TOKEN, TELEGRAM_ACCOUNTS, and (optionally) cookies.
docker compose up -d --build
```

Verify the bgutil server is healthy:

```bash
docker compose ps bgutil-pot-provider
# Should show "healthy" within ~15s of starting.
```

## Quick start (without Docker)

If you run the bot directly with `python bot.py` (e.g. on Railway, Replit,
or a VPS without Docker), you need to run the bgutil server separately.

### Option A — Run bgutil as a Docker container on the same host

```bash
docker compose -f docker-compose.bgutil.yml up -d
curl http://127.0.0.1:4416/ping   # should return "pong"
```

### Option B — Run bgutil from source (Node.js)

```bash
git clone https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git
cd bgutil-ytdlp-pot-provider
npm install
npm start
# Server listens on http://127.0.0.1:4416
```

Then in `.env`:

```
YTDLP_ENABLED=true
YTDLP_BGUTIL_BASE_URL=http://127.0.0.1:4416
```

## Cookies (optional but recommended)

Cookies help with:
- Age-restricted videos (18+)
- Members-only videos
- Extra bot-detection bypass when PO Token alone is not enough
- Persistent session (less chance of being challenged)

### How to get cookies.txt

1. Install the **"Get cookies.txt LOCALLY"** browser extension
   (Chrome / Firefox / Edge).
2. Open https://www.youtube.com in your browser and sign in with a
   throwaway Google account (NOT your main account — yt-dlp usage can
   get the account banned).
3. Click the extension icon and select "Export → cookies.txt for this site".
4. Save the file as `cookies.txt` next to `bot.py` (or set
   `YTDLP_COOKIES_FILE` to its path).

### Refresh schedule

Cookies expire after a few months. When yt-dlp starts returning 401 errors
on videos that used to work, just re-export `cookies.txt` from your browser
and restart the bot. No code changes needed.

## Maintenance

### Weekly update (recommended)

YouTube rotates its bot-detection scripts every few weeks. The bgutil
maintainers usually patch within 24-48 hours. To stay current:

```bash
# Docker Compose (with watchtower this happens automatically once per day):
docker compose pull bgutil-pot-provider
docker compose up -d bgutil-pot-provider

# Also update the Python plugin:
pip install -U bgutil-ytdlp-pot-provider yt-dlp
```

The included `watchtower` container in `docker-compose.yml` auto-pulls the
latest bgutil image once per day and restarts the container. You can
disable it by removing the `watchtower` service.

### When downloads start failing

1. Check bgutil server health: `curl http://127.0.0.1:4416/ping`
2. Check bot logs for `bgutil PO Token server probe failed`
3. Update everything:
   ```bash
   docker compose pull
   pip install -U yt-dlp bgutil-ytdlp-pot-provider
   docker compose up -d --force-recreate
   ```
4. If still failing, check https://github.com/yt-dlp/yt-dlp/issues for
   recent bot-detection changes.

## Running inside Iran

The bgutil server needs to reach YouTube from a non-Iranian IP. Two options:

### Option 1 — Run bgutil on a foreign VPS

Run `docker-compose.bgutil.yml` on a Hetzner/DigitalOcean/Vultr VPS, then
point the bot at it:

```
YTDLP_BGUTIL_BASE_URL=http://your-foreign-vps:4416
```

Make sure to firewall port 4416 so only the bot can reach it.

### Option 2 — Use HTTP_PROXY for the bgutil container

Edit `docker-compose.yml` (or `docker-compose.bgutil.yml`):

```yaml
  bgutil-pot-provider:
    environment:
      HTTP_PROXY: "http://user:pass@your-proxy:3128"
      HTTPS_PROXY: "http://user:pass@your-proxy:3128"
```

The bot itself also needs `USE_PROXY=true` in `.env` so yt-dlp's direct
YouTube requests go through the proxy too.

## Configuration reference

| Variable | Default | Description |
|----------|---------|-------------|
| `YTDLP_ENABLED` | `false` | Master switch. Set to `true` to route YouTube to yt-dlp + bgutil. |
| `YTDLP_BGUTIL_BASE_URL` | `http://127.0.0.1:4416` | bgutil HTTP server URL. Use `http://bgutil-pot-provider:4416` inside Docker Compose. |
| `YTDLP_COOKIES_FILE` | `cookies.txt` | Path to cookies.txt (relative to project root or absolute). File may not exist — yt-dlp will work without it. |
| `YTDLP_PLAYER_CLIENTS` | `mweb,web` | Comma-separated YouTube InnerTube clients. `mweb` (mobile web) rarely triggers bot detection; `web` (desktop) gives higher quality. |

## What changed in this update

### New files
- `ytdlp_gateway.py` — yt-dlp + bgutil gateway (mirrors CobaltGateway's interface)
- `docker-compose.bgutil.yml` — standalone bgutil server (for non-Docker bot deployments)
- `YTDLP_SETUP.md` — this document

### Modified files
- `bot.py` — instantiates `YtDlpGateway`, routes YouTube to it, handles selection callbacks
- `routing.py` — `ordered_providers()` prepends `ytdlp` for YouTube; added `is_ytdlp_provider()` and `is_api_provider()` helpers
- `config.py` — added `ytdlp_enabled`, `ytdlp_bgutil_base_url`, `ytdlp_cookies_file`, `ytdlp_player_clients` settings
- `requirements.txt` — added `bgutil-ytdlp-pot-provider==1.3.1`
- `docker-compose.yml` — added `bgutil-pot-provider` and `watchtower` services
- `.env.example` — documented the new `YTDLP_*` variables

### Unchanged behaviour
- Cobalt still handles Instagram (and YouTube as a fallback if yt-dlp fails).
- Telegram backup bots still work as the last-resort fallback.
- Pixeldrain uploads for files ≥30MB still work.
- Subtitle downloads, search, shazam, spotisaver — all unchanged.
