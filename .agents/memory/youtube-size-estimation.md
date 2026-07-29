---
name: YouTube quality-button file sizes
description: Why file sizes on quality-selection buttons are YouTube-only and approximate in this bot.
---

Quality/format buttons in this bot are built from the intermediary Telegram bots' own inline keyboards (see `downloader.py`'s `extract_quality_options()`); those labels never include a file size, and no exact size is knowable until after the file is actually downloaded.

**Why:** Instagram/TikTok/Twitter/Spotify/SoundCloud all route through intermediary bots with no independent size source, so accurate pre-download sizes aren't available for them. YouTube is the only platform where `yt-dlp` can be queried directly (`youtube_search.py`: `estimate_youtube_size()` + `YouTubeSearchService.format_sizes()`) to approximate a size by matching the closest format's height/bitrate — it's still an estimate (adaptive formats add the best audio track's size to the chosen video-only track), which is why the bot sends a one-time disclaimer after showing sizes.

**How to apply:** If asked to add sizes (or other pre-download metadata) for other platforms, there is no existing data source to reuse — it would require a new architecture (e.g., a direct yt-dlp-style path for that platform), not a small extension of the YouTube approach. Don't assume it's a quick follow-up.
