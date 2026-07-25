# MZ Downloader

A multi-account Telegram media download bot. Users send links (YouTube, Instagram, TikTok, Spotify, SoundCloud, Twitter/X, Facebook, VK) and the bot routes each to the appropriate third-party downloader bot via Telethon proxy accounts, then sends the file back.

## Stack

- **Python** — main language
- **python-telegram-bot 22.8** — bot framework (polling)
- **Telethon 1.44.0** — Telegram user accounts used as download proxies
- **yt-dlp** — YouTube search and download
- **Pillow** — YouTube search result thumbnail grids
- **python-dotenv** — environment variable loading

## Key files

- `bot.py` — main bot entry point, all handlers
- `config.py` — settings loaded from environment
- `downloader.py` — routing logic and download orchestration
- `routing.py` — per-platform bot routing table
- `spotisaver.py` — Spotify album ZIP assembly
- `instagram_caption.py` — `/caption` command handler
- `youtube_search.py` — YouTube search and pagination

## Required secrets (must be set before running)

| Variable | Description |
|---|---|
| `BOT_TOKEN` | Telegram bot token from BotFather |
| `TELEGRAM_ACCOUNTS` | JSON array of Telethon accounts with `api_id`, `api_hash`, `string_session` |

See `.env.example` for the full list of optional configuration variables.

## Running locally

```bash
pip install -r requirements.txt
python bot.py
```

## Deployment

Originally designed for Railway (see `RAILWAY_DEPLOY_FA.md`). Can also be run on Replit once `BOT_TOKEN` and `TELEGRAM_ACCOUNTS` secrets are set.

## User preferences
