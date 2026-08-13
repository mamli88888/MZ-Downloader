from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit


class Platform(str, Enum):
    INSTAGRAM = "instagram"
    YOUTUBE = "youtube"
    TIKTOK = "tiktok"
    TWITTER = "twitter"
    FACEBOOK = "facebook"
    VK = "vk"
    SPOTIFY = "spotify"
    SOUNDCLOUD = "soundcloud"


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
}

BLOCKED_REDIRECT_HOSTS = {"l.instagram.com", "l.facebook.com", "lm.facebook.com", "away.vk.com"}


def _matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def detect_platform(url: str) -> Platform | None:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().strip(".")
    except ValueError:
        return None
    if not host or host in BLOCKED_REDIRECT_HOSTS:
        return None
    if host.endswith("youtube.com") and parsed.path.lower() in {"/redirect", "/attribution_link"}:
        return None
    for platform, domains in PLATFORM_DOMAINS.items():
        if any(_matches(host, domain) for domain in domains):
            return platform
    return None


def providers_for_platform(platform: Platform, settings) -> tuple[str, ...]:
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
    return ()


def all_providers(settings) -> tuple[str, ...]:
    return settings.fallback_bots


def ordered_providers(
    platform: Platform,
    settings,
) -> tuple[str, ...]:
    """Build the ordered list of providers to try for a given platform.

    Cobalt (the COBALT_PROVIDER sentinel from cobalt_gateway) is prepended for
    YouTube and Instagram when COBALT_API_URL is configured and COBALT_PRIORITY
    is enabled. The backup Telegram bots are always appended afterwards so the
    bot still works if cobalt is down or returns an error.

    For non-YouTube/Instagram platforms, cobalt is never inserted.
    """
    from cobalt_gateway import COBALT_PROVIDER

    normal = providers_for_platform(platform, settings)
    fallback = all_providers(settings)

    cobalt_first = (
        settings.cobalt_api_url
        and settings.cobalt_priority
        and platform in {Platform.YOUTUBE, Platform.INSTAGRAM}
    )
    if cobalt_first:
        return tuple(dict.fromkeys((COBALT_PROVIDER, *normal, *fallback)))
    return tuple(dict.fromkeys((*normal, *fallback)))


def is_cobalt_provider(bot_username: str) -> bool:
    """True if the given provider name is the cobalt sentinel."""
    from cobalt_gateway import COBALT_PROVIDER

    return bot_username == COBALT_PROVIDER


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
