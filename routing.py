from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlsplit


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    TWITTER = "twitter"
    FACEBOOK = "facebook"
    VK = "vk"
    SPOTIFY = "spotify"
    SOUNDCLOUD = "soundcloud"
    PINTEREST = "pinterest"
    # AHM7-only platforms (no dedicated Telegram-bot list; handled
    # entirely by AHM7, with Apify / Telegram-bots as fallbacks).
    REDDIT = "reddit"
    SNAPCHAT = "snapchat"
    CAPCUT = "capcut"
    SNACKVIDEO = "snackvideo"
    DOUYIN = "douyin"
    # Anything yt-dlp supports that doesn't have a dedicated Platform
    # (Vimeo, Dailymotion, Streamable, Twitch, etc.).
    YTDLP_GENERIC = "ytdlp_generic"


@dataclass(frozen=True)
class PlatformInfo:
    label: str
    icon: str


PLATFORM_INFO = {
    Platform.INSTAGRAM: PlatformInfo("اینستاگرام", "📸"),
    Platform.YOUTUBE: PlatformInfo("یوتیوب", "▶️"),
    Platform.TIKTOK: PlatformInfo("تیک‌تاک", "🎵"),
    Platform.TWITTER: PlatformInfo("توییتر / X", "𝕏"),
    Platform.FACEBOOK: PlatformInfo("فیسبوک", "📘"),
    Platform.VK: PlatformInfo("VK", "🔵"),
    Platform.SPOTIFY: PlatformInfo("اسپاتیفای", "🟢"),
    Platform.SOUNDCLOUD: PlatformInfo("ساوندکلاد", "☁️"),
    Platform.PINTEREST: PlatformInfo("پینترست", "📌"),
    Platform.REDDIT: PlatformInfo("ردیت", "🟠"),
    Platform.SNAPCHAT: PlatformInfo("اسنپ‌چت", "👻"),
    Platform.CAPCUT: PlatformInfo("کپ‌کات", "🎬"),
    Platform.SNACKVIDEO: PlatformInfo("اسنک‌ویدیو", "🍫"),
    Platform.DOUYIN: PlatformInfo("دویین", "🎵"),
    Platform.YTDLP_GENERIC: PlatformInfo("لینک", "🔗"),
}

PLATFORM_DOMAINS = {
    Platform.INSTAGRAM: ("instagram.com", "instagr.am"),
    Platform.YOUTUBE: ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    Platform.TIKTOK: ("tiktok.com",),
    Platform.TWITTER: ("twitter.com", "x.com"),
    Platform.FACEBOOK: ("facebook.com", "fb.watch", "fb.com"),
    Platform.VK: ("vk.com", "vk.ru", "vkvideo.ru"),
    Platform.SPOTIFY: ("spotify.com", "spotify.link", "spoti.fi"),
    Platform.SOUNDCLOUD: ("soundcloud.com", "snd.sc"),
    Platform.PINTEREST: ("pinterest.com", "pin.it"),
    Platform.REDDIT: ("reddit.com", "redd.it", "redditmedia.com"),
    Platform.SNAPCHAT: ("snapchat.com", "t.snapchat.com"),
    Platform.CAPCUT: ("capcut.com", "www.capcut.com"),
    Platform.SNACKVIDEO: ("snackvideo.com", "sck.io"),
    Platform.DOUYIN: ("douyin.com", "iesdouyin.com", "v.douyin.com"),
}

# Hosts that are routed to the generic yt-dlp path. These are platforms
# yt-dlp supports without needing a dedicated Platform enum value — the
# bot shows a generic quality menu and lets yt-dlp pick the best format.
YTDLP_GENERIC_DOMAINS = (
    "vimeo.com",
    "dailymotion.com",
    "streamable.com",
    "reddit.com",
    "redd.it",
    "twitch.tv",
    "clips.twitch.tv",
    "odysee.com",
    "rumble.com",
    "bitchute.com",
    "soundcloud.com",  # only used if Platform.SOUNDCLOUD doesn't catch it
    "media.giphy.com",
    "giphy.com",
    "9gag.com",
    "imgur.com",
    "kick.com",
    "bilibili.com",
    "sendvid.com",
    "liveleak.com",
    "bandcamp.com",
    "mixcloud.com",
    "pandora.com",
    "hearthis.at",
)

BLOCKED_REDIRECT_HOSTS = {"l.instagram.com", "l.facebook.com", "lm.facebook.com", "away.vk.com"}


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def detect_platform(url: str) -> Platform | None:
    """Detect the platform for a URL.

    Unknown URLs (http/https) fall through to ``Platform.YTDLP_GENERIC``
    so the bot can attempt yt-dlp on them. Truly invalid input (no scheme,
    blocked redirect host, YouTube redirect URL) returns ``None``.
    """
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().strip(".")
    except ValueError:
        return None
    if not host or host in BLOCKED_REDIRECT_HOSTS:
        return None
    if host.endswith("youtube.com") and parsed.path.lower() in {"/redirect", "/attribution_link"}:
        return None
    # First, check dedicated platforms (precise match).
    for platform, domains in PLATFORM_DOMAINS.items():
        if any(_matches(host, domain) for domain in domains):
            return platform
    # Then, check the yt-dlp-generic list.
    for domain in YTDLP_GENERIC_DOMAINS:
        if _matches(host, domain):
            return Platform.YTDLP_GENERIC
    # Finally, fall back to YTDLP_GENERIC for any other http(s) URL so the
    # user gets a "best effort" attempt instead of "این لینک رو نمی‌شناسم".
    if parsed.scheme in {"http", "https"} and "." in host:
        return Platform.YTDLP_GENERIC
    return None


def providers_for_platform(platform: Platform, settings) -> tuple[str, ...]:
    """Return the Telegram downloader bots that can handle this platform.

    Platforms handled entirely by SOCIAL_GATEWAY (Pinterest, generic
    yt-dlp URLs) have no Telegram fallback — they return an empty tuple,
    so the bot skips the Telegram-worker acquisition path entirely.
    """
    if platform in {Platform.INSTAGRAM, Platform.YOUTUBE}:
        return settings.instagram_youtube_bots
    if platform == Platform.TIKTOK:
        return settings.tiktok_bots
    if platform == Platform.TWITTER:
        return settings.twitter_bots
    if platform in {Platform.FACEBOOK, Platform.VK}:
        return (settings.primary_bot,)
    if platform == Platform.SPOTIFY:
        return settings.spotify_track_bots
    if platform == Platform.SOUNDCLOUD:
        return (settings.soundcloud_bot,)
    # Reddit / Snapchat / CapCut / SnackVideo / Douyin — handled
    # primarily by AHM7, with Apify and the union Telegram-bots fallback
    # as backups. No dedicated Telegram-bot list exists for them, so the
    # `_process_url` `providers` list (which already unions in
    # `all_providers(SETTINGS)`) is what they fall back through.
    # Pinterest / YTDLP_GENERIC — handled entirely by yt-dlp via
    # SOCIAL_GATEWAY. No Telegram fallback.
    return ()


def all_providers(settings) -> tuple[str, ...]:
    return settings.fallback_bots


def spotify_resource_type(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    parts = [part.lower() for part in parsed.path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"track", "album", "playlist"}:
        return parts[0]
    return None


def platform_info(platform: Platform) -> PlatformInfo:
    return PLATFORM_INFO[platform]


def is_instagram_reel(url: str) -> bool:
    """Return True if the URL is an Instagram Reel (/reel/, /reels/, or /tv/)."""
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().strip(".")
        path = parsed.path.lower()
    except ValueError:
        return False
    if not (host == "instagram.com" or host.endswith(".instagram.com")):
        return False
    return path.startswith("/reel/") or path.startswith("/reels/") or path.startswith("/tv/")


def is_instagram_image_post(url: str) -> bool:
    """Return True if the URL is an Instagram image carousel post.

    Instagram adds the ``img_index`` query parameter when a user shares a
    specific slide of a multi-image carousel. Its presence is a strong,
    URL-only signal of an image (not video) post.

    These posts are routed through Apify as their *primary* downloader
    because the AHM7 ``alldl`` endpoint only returns ``videoUrl`` /
    ``audioUrl`` — it cannot serve photo carousels. Apify's
    ``instagram-scraper`` Actor extracts ``displayUrl`` / ``images`` /
    ``childPosts`` recursively, so it handles both single-image and
    multi-slide carousel posts.

    Reels (``/reel/``, ``/reels/``, ``/tv/``) are always video and return
    ``False`` — they keep AHM7 as their primary downloader.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().strip(".")
    if not (host == "instagram.com" or host.endswith(".instagram.com")):
        return False
    path = parsed.path.lower()
    if not path.startswith("/p/"):
        return False
    query = parse_qs(parsed.query)
    return any(key.lower() == "img_index" for key in query)


def is_instagram_post_page(url: str) -> bool:
    """Return True for EVERY Instagram post page (path starts with ``/p/``).

    ``is_instagram_image_post`` needs the ``img_index`` query parameter, but
    most shared carousel links do NOT carry it — so from the URL alone a
    photo carousel is indistinguishable from a video post.  AHM7's
    ``alldl`` endpoint only ever returns ONE ``videoUrl``/``audioUrl`` —
    on a carousel that is the FIRST slide, served as a "video", with no
    photo option in the menu at all.

    Therefore every ``/p/`` post is routed through Apify's
    ``instagram-scraper`` Actor as its PRIMARY downloader: the Actor
    resolves ``displayUrl`` / ``images`` / ``childPosts`` recursively, so
    carousels come out complete (all slides, in order).  Reels
    (``/reel/``, ``/reels/``, ``/tv/``) are always a single video and keep
    AHM7 as their primary downloader.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().strip(".")
    if not (host == "instagram.com" or host.endswith(".instagram.com")):
        return False
    return parsed.path.lower().startswith("/p/")
