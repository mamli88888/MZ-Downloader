"""Cloud-download integrations for the five new platforms (1404 upgrade).

Spotify      → maximedupre/spotify-downloader           (trackUrls + fast_links → direct MP3 downloadUrl)
SoundCloud   → easyapi/soundcloud-mp3-downloader       (direct signed CDN MP3 links)
Twitter / X  → apidojo/tweet-scraper                   (tweet URL → videos with bitrate variants, photos, text)
Facebook     → apple_yang/facebook-video-audio-downloader (direct videoUrl / audioUrl)
Pinterest    → easyapi/pinterest-video-downloader (videos) + fatihtahta/pinterest-scraper-search (original images)

All input shapes AND output fields below were verified against each actor's
published documentation (README input/output examples and the published
input schemas). Dataset output shapes still vary, so extraction uses a
recursive, key-preference-scored normalizer — the same robust technique the
repository already uses for Instagram carousels.

v2 (fix round): replaced metadata-only actors with real downloaders and
corrected the Spotify input contract (track_urls must be objects).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable
from urllib.parse import urlsplit

from downloader import MediaKind, QualityOption
from routing import Platform

logger = logging.getLogger("MZDownloader.apify_platforms")

SPOTIFY_ACTOR_ID = "maximedupre/spotify-downloader"
SOUNDCLOUD_ACTOR_ID = "easyapi/soundcloud-mp3-downloader"
TWITTER_ACTOR_ID = "apidojo/tweet-scraper"
FACEBOOK_ACTOR_ID = "apple_yang/facebook-video-audio-downloader"
PINTEREST_IMAGE_ACTOR_ID = "fatihtahta/pinterest-scraper-search"
PINTEREST_VIDEO_ACTOR_ID = "easyapi/pinterest-video-downloader"

NEW_APIFY_PLATFORMS: frozenset[Platform] = frozenset(
    {
        Platform.SPOTIFY,
        Platform.SOUNDCLOUD,
        Platform.TWITTER,
        Platform.FACEBOOK,
        Platform.PINTEREST,
    }
)

# Conservative per-platform start rates (runs per minute) so free-tier
# accounts stay inside the platform's own rate limits.
PLATFORM_RATE_PER_MINUTE: dict[Platform, float] = {
    Platform.SPOTIFY: 6.0,
    Platform.SOUNDCLOUD: 6.0,
    Platform.TWITTER: 10.0,
    Platform.FACEBOOK: 6.0,
    Platform.PINTEREST: 6.0,
}

_AUDIO_EXT = (".mp3", ".m4a", ".ogg", ".opus", ".wav", ".flac", ".aac")
_VIDEO_EXT = (".mp4", ".webm", ".mov", ".mkv", ".m4v")
_IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")

# Key fragments that signal a *preferred* media field, and fragments that
# signal a preview / placeholder that must be avoided.
_PREFERRED_KEY_PARTS = ("download", "original", "hd", "playable", "progressive", "fullimage", "full_image", "source", "video", "audio")
_AVOID_KEY_PARTS = ("thumbnail", "preview", "placeholder", "profile", "avatar", "favicon", "sprite", "logo", "small", "thumb")


def _fingerprint(payload: dict[str, Any]) -> str:
    import json

    return "apify:" + json.dumps(payload, separators=(",", ":"), sort_keys=True)


def fingerprint_decode(fingerprint: str) -> dict[str, Any] | None:
    import json

    if not fingerprint.startswith("apify:"):
        return None
    try:
        value = json.loads(fingerprint[len("apify:"):])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _as_http_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    return candidate if urlsplit(candidate).scheme in {"http", "https"} else None


def spotify_track_url(url: str) -> bool:
    """The Spotify actor only accepts single-track URLs (verified: rejects
    anything without ``/track/``; albums/playlists stay on the existing chain)."""
    try:
        parts = [p.lower() for p in urlsplit(url).path.split("/") if p]
    except ValueError:
        return False
    return len(parts) >= 2 and parts[0] == "track"


def soundcloud_track_url(url: str) -> bool:
    """The MP3 downloader actor accepts track URLs (``/artist/track``).
    Playlists (``/artist/sets/…``), profiles and likes pages are routed to
    the existing yt-dlp chain."""
    try:
        parts = [p.lower() for p in urlsplit(url).path.split("/") if p]
    except ValueError:
        return False
    if len(parts) != 2:
        return False
    return parts[0] not in {"you", "sets", "stations"} and "sets" not in parts


def _duration_to_seconds(value: Any) -> float | None:
    """Parse '3:20' / '1:02:13' style durations into seconds."""
    if isinstance(value, (int, float)) and value > 0:
        # Already seconds (or ms — caller decides via magnitude).
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    pieces = value.strip().split(":")
    if not all(re.fullmatch(r"\d{1,3}", p) for p in pieces) or len(pieces) > 3:
        return None
    total = 0.0
    for piece in pieces:
        total = total * 60 + int(piece)
    return total or None


# ───────────────────────────── Quality menus ─────────────────────────────

def new_platform_options(platform: Platform) -> tuple[QualityOption, ...]:
    if platform == Platform.SPOTIFY:
        return (
            QualityOption(
                label="MP3 320kbps (متادیتا کامل)",
                row=0,
                column=0,
                fingerprint=_fingerprint({"platform": "spotify", "kind": "audio", "quality": "320"}),
                expected_kind=MediaKind.AUDIO,
                expected_bitrate_kbps=320,
            ),
        )
    if platform == Platform.SOUNDCLOUD:
        return (
            QualityOption(
                label="بهترین کیفیت موجود (MP3)",
                row=0,
                column=0,
                fingerprint=_fingerprint({"platform": "soundcloud", "kind": "audio", "quality": "best"}),
                expected_kind=MediaKind.AUDIO,
                expected_bitrate_kbps=320,
            ),
        )
    if platform == Platform.TWITTER:
        return (
            QualityOption(
                label="ویدیو — بالاترین کیفیت",
                row=0,
                column=0,
                fingerprint=_fingerprint({"platform": "twitter", "kind": "video", "quality": "best"}),
                expected_kind=MediaKind.VIDEO,
                expected_height=1080,
            ),
            QualityOption(
                label="ویدیو — کم‌حجم",
                row=0,
                column=1,
                fingerprint=_fingerprint({"platform": "twitter", "kind": "video", "quality": "small"}),
                expected_kind=MediaKind.VIDEO,
                expected_height=480,
            ),
            QualityOption(
                label="همه تصاویر",
                row=1,
                column=0,
                fingerprint=_fingerprint({"platform": "twitter", "kind": "photo", "quality": "photos"}),
                expected_kind=MediaKind.PHOTO,
            ),
            QualityOption(
                label="متن و جزییات توییت",
                row=1,
                column=1,
                fingerprint=_fingerprint({"platform": "twitter", "kind": "text", "quality": "text"}),
                expected_kind=None,
            ),
        )
    if platform == Platform.FACEBOOK:
        return (
            QualityOption(
                label="ویدیو (بهترین کیفیت)",
                row=0,
                column=0,
                fingerprint=_fingerprint({"platform": "facebook", "kind": "video", "quality": "video"}),
                expected_kind=MediaKind.VIDEO,
                expected_height=1080,
            ),
            QualityOption(
                label="فقط صدا (MP3)",
                row=0,
                column=1,
                fingerprint=_fingerprint({"platform": "facebook", "kind": "audio", "quality": "audio"}),
                expected_kind=MediaKind.AUDIO,
                expected_bitrate_kbps=192,
            ),
        )
    if platform == Platform.PINTEREST:
        return (
            QualityOption(
                label="تصاویر با کیفیت اصلی",
                row=0,
                column=0,
                fingerprint=_fingerprint({"platform": "pinterest", "kind": "photo", "quality": "images"}),
                expected_kind=MediaKind.PHOTO,
            ),
            QualityOption(
                label="ویدیوی پین",
                row=0,
                column=1,
                fingerprint=_fingerprint({"platform": "pinterest", "kind": "video", "quality": "video"}),
                expected_kind=MediaKind.VIDEO,
                expected_height=1080,
            ),
        )
    return ()


def new_platform_size_hint(option: QualityOption) -> str:
    payload = fingerprint_decode(option.fingerprint) or {}
    quality = str(payload.get("quality", ""))
    if option.expected_kind == MediaKind.AUDIO:
        kbps = option.expected_bitrate_kbps or 128
        per_min = kbps * 1000 / 8 * 60 / (1024 * 1024)
        rendered = f"{per_min:.1f}".rstrip("0").rstrip(".")
        return f"≈{rendered}MB/min"
    if quality in {"best", "video"}:
        return "بهترین کیفیت"
    if quality == "small":
        return "کم‌حجم"
    return "حجم اصلی" if option.expected_kind == MediaKind.PHOTO else "حجم تقریبی"


# ─────────────────────────── Actor input builders ───────────────────────────

def build_actor_request(
    platform: Platform,
    url: str,
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any], MediaKind | None, bool]:
    """Map a decoded fingerprint to (actor_id, actor_input, expected_kind, extract_mp3_from_video)."""
    kind = payload.get("kind")

    if platform == Platform.SPOTIFY:
        # Verified input contract (actor README): trackUrls is a plain string
        # array; resolutionMode=fast_links returns a direct CDN MP3 link in
        # media.downloadUrl (no storage download needed).
        return (
            SPOTIFY_ACTOR_ID,
            {
                "trackUrls": [url],
                "resolutionMode": "fast_links",
            },
            MediaKind.AUDIO,
            False,
        )

    if platform == Platform.SOUNDCLOUD:
        # Verified input contract: {"links": ["https://soundcloud.com/artist/track"]}
        return (
            SOUNDCLOUD_ACTOR_ID,
            {"links": [url]},
            MediaKind.AUDIO,
            False,
        )

    if platform == Platform.TWITTER:
        if kind == "text":
            return (
                TWITTER_ACTOR_ID,
                {"startUrls": [{"url": url, "method": "GET"}], "maxItems": 40},
                None,
                False,
            )
        if kind == "photo":
            return (
                TWITTER_ACTOR_ID,
                {"startUrls": [{"url": url, "method": "GET"}], "maxItems": 40, "onlyImage": True},
                MediaKind.PHOTO,
                False,
            )
        return (
            TWITTER_ACTOR_ID,
            {"startUrls": [{"url": url, "method": "GET"}], "maxItems": 40, "onlyVideo": True},
            MediaKind.VIDEO,
            False,
        )

    if platform == Platform.FACEBOOK:
        # Verified input contract: {"videoUrls": ["..."]}
        if kind == "audio":
            return (
                FACEBOOK_ACTOR_ID,
                {"videoUrls": [url]},
                MediaKind.AUDIO,
                False,  # audioUrl used directly; conversion flag only if missing
            )
        return (
            FACEBOOK_ACTOR_ID,
            {"videoUrls": [url]},
            MediaKind.VIDEO,
            False,
        )

    if platform == Platform.PINTEREST:
        if kind == "video":
            # Verified input contract: {"links": [".../pin/..."]}
            return (
                PINTEREST_VIDEO_ACTOR_ID,
                {"links": [url]},
                MediaKind.VIDEO,
                False,
            )
        return (
            PINTEREST_IMAGE_ACTOR_ID,
            {"startUrls": [{"url": url, "method": "GET"}], "type": "all-pins"},
            MediaKind.PHOTO,
            False,
        )

    raise ValueError(f"Unsupported new cloud platform: {platform}")


# ─────────────────────── Output normalization ───────────────────────

@dataclass
class NormalizedResult:
    media_specs: list[tuple[str, MediaKind]] = field(default_factory=list)
    text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    audio_fallback_video: bool = False  # video present, audioUrl missing → convert


def _score_key(key: str) -> int:
    lowered = key.lower()
    score = 0
    if any(part in lowered for part in _PREFERRED_KEY_PARTS):
        score += 4
    if any(part in lowered for part in _AVOID_KEY_PARTS):
        score -= 6
    return score


def _kind_for_url(url: str) -> MediaKind | None:
    path = urlsplit(url).path.lower()
    if path.endswith(_AUDIO_EXT):
        return MediaKind.AUDIO
    if path.endswith(_VIDEO_EXT):
        return MediaKind.VIDEO
    if path.endswith(_IMAGE_EXT):
        return MediaKind.PHOTO
    return None


def _twitter_variant_pick(variants: Iterable[dict[str, Any]], want_best: bool) -> str | None:
    """Pick an mp4 variant by bitrate; tolerate missing/zero bitrates; skip HLS."""
    candidates: list[tuple[int, str]] = []
    for variant in variants:
        if not isinstance(variant, dict):
            continue
        url = _as_http_url(variant.get("url"))
        if url is None:
            continue
        content_type = str(variant.get("content_type") or variant.get("contentType") or "").lower()
        if "mpegurl" in content_type or url.split("?")[0].endswith(".m3u8"):
            continue  # HLS playlist — prefer progressive mp4 variants
        if "mp4" not in content_type and not url.split("?")[0].endswith(".mp4"):
            continue
        try:
            bitrate = int(variant.get("bitrate") or 0)
        except (TypeError, ValueError):
            bitrate = 0
        candidates.append((bitrate, url))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return (candidates[-1] if want_best else candidates[0])[1]


def _collect_media(node: Any, wanted_kinds: frozenset[MediaKind]) -> list[tuple[str, MediaKind, int]]:
    """Recursively collect candidate media URLs with a preference score."""
    found: dict[str, tuple[MediaKind, int]] = {}

    def visit(value: Any, key_hint: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, (dict, list)):
                    visit(child, str(key))
                elif isinstance(child, str):
                    url = _as_http_url(child)
                    if url is not None:
                        kind = _kind_for_url(url)
                        if kind is None and key_hint:
                            lowered_key = key_hint.lower()
                            if "video" in lowered_key:
                                kind = MediaKind.VIDEO
                            elif "image" in lowered_key or "photo" in lowered_key or "cover" in lowered_key:
                                kind = MediaKind.PHOTO
                            elif "audio" in lowered_key or "download" in lowered_key:
                                kind = MediaKind.AUDIO
                        if kind is not None and kind in wanted_kinds:
                            score = _score_key(key) + _score_key(key_hint)
                            existing = found.get(url)
                            if existing is None or score > existing[1]:
                                found[url] = (kind, score)
            variants = value.get("variants")
            if isinstance(variants, list):
                for variant in variants:
                    visit(variant, "variant")
        elif isinstance(value, list):
            for child in value:
                visit(child, key_hint)

    visit(node)
    scored = [(url, kind, score) for url, (kind, score) in found.items()]
    scored.sort(key=lambda item: -item[2])
    return scored


def _medias_list(item: dict[str, Any]) -> list[Any]:
    """easyapi-family output wraps media in item.medias OR item.result.medias."""
    for container in (item, item.get("result") if isinstance(item.get("result"), dict) else None):
        if isinstance(container, dict) and isinstance(container.get("medias"), list):
            return container["medias"]
    return []


def _pick_best_media(medias: list[Any]) -> tuple[str, int] | None:
    """Pick the best (url, area) from an easyapi-style medias array."""
    best: tuple[str, int] | None = None
    for media in medias:
        if not isinstance(media, dict):
            continue
        url = _as_http_url(media.get("url"))
        if url is None:
            continue
        try:
            area = int(media.get("width") or 0) * int(media.get("height") or 0)
        except (TypeError, ValueError):
            area = 0
        if best is None or area > best[1]:
            best = (url, area)
    return best


def extract_new_media(
    platform: Platform,
    items: list[dict[str, Any]],
    payload: dict[str, Any],
) -> NormalizedResult:
    kind = payload.get("kind")
    quality = str(payload.get("quality", ""))
    result = NormalizedResult()

    if platform == Platform.SPOTIFY:
        # Verified output (actor README): {status, trackName, artistNames[],
        # albumName, durationMs, durationText, coverImageUrl,
        # media.downloadUrl (direct mp3), availability.downloadContentLength}
        for item in items:
            result.metadata.update(_spotify_metadata(item))
            media_block = item.get("media") if isinstance(item.get("media"), dict) else {}
            direct = _as_http_url(media_block.get("downloadUrl"))
            if direct:
                result.media_specs.append((direct, MediaKind.AUDIO))
                # Exact byte size straight from the actor — feeds the size audit.
                availability = item.get("availability") if isinstance(item.get("availability"), dict) else {}
                length = availability.get("downloadContentLength")
                if isinstance(length, int) and length > 0:
                    result.metadata["expected_bytes"] = length
                break
            # Fallbacks for schema drift / save_files mode.
            scored = _collect_media(item, frozenset({MediaKind.AUDIO}))
            if scored:
                result.media_specs.append((scored[0][0], MediaKind.AUDIO))
                break
            saved = item.get("savedFile") if isinstance(item.get("savedFile"), dict) else {}
            saved_url = _as_http_url(saved.get("url") or saved.get("directUrl"))
            if saved_url:
                result.media_specs.append((saved_url, MediaKind.AUDIO))
                break
        return result

    if platform == Platform.SOUNDCLOUD:
        # Verified output: [{url, title, thumbnail, duration "4:41",
        # medias: [{url: <signed cf-media .mp3>, ...}]}] (medias may also be
        # nested under result.medias).
        for item in items:
            container = item.get("result") if isinstance(item.get("result"), dict) else item
            result.metadata.update(_soundcloud_metadata(container))
            medias = _medias_list(item)
            picked = _pick_best_media(medias) if medias else None
            if picked:
                result.media_specs.append((picked[0], MediaKind.AUDIO))
            else:
                scored = [entry for entry in _collect_media(item, frozenset({MediaKind.AUDIO}))]
                if scored:
                    result.media_specs.append((scored[0][0], MediaKind.AUDIO))
            if result.media_specs:
                break
        return result

    if platform == Platform.TWITTER:
        if kind == "text":
            for item in items:
                text_parts: list[str] = []
                for key in ("text", "full_text", "tweet"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        text_parts.append(value.strip())
                author = item.get("author")
                if isinstance(author, dict):
                    handle = author.get("userName") or author.get("screen_name") or ""
                    name = author.get("name") or ""
                    if handle or name:
                        text_parts.append(f"— {name} (@{handle})" if handle else f"— {name}")
                if item.get("createdAt") or item.get("created_at"):
                    text_parts.append(str(item.get("createdAt") or item.get("created_at")))
                stats_line = []
                for stat_key, icon in (("retweetCount", "🔁"), ("favoriteCount", "❤️"), ("replyCount", "💬"), ("quoteCount", "🗞")):
                    value = item.get(stat_key)
                    if isinstance(value, int):
                        stats_line.append(f"{icon} {value}")
                if stats_line:
                    text_parts.append(" ".join(stats_line))
                if text_parts:
                    result.text = "\n".join(text_parts)[:3800]
                    break
            return result

        if kind == "photo":
            for item in items:
                media_block = item.get("media") if isinstance(item.get("media"), dict) else {}
                photos = media_block.get("photos") if isinstance(media_block, dict) else None
                if isinstance(photos, list) and photos:
                    for photo in photos:
                        url = _as_http_url(photo.get("url") if isinstance(photo, dict) else photo)
                        if url:
                            result.media_specs.append((url, MediaKind.PHOTO))
                if not result.media_specs:
                    scored = _collect_media(item, frozenset({MediaKind.PHOTO}))
                    result.media_specs.extend((url, mkind) for url, mkind, _ in scored[:4])
                if result.media_specs:
                    break
            return result

        # video-best / video-small
        want_best = quality != "small"
        for item in items:
            media_block = item.get("media") if isinstance(item.get("media"), dict) else {}
            videos = media_block.get("videos") if isinstance(media_block, dict) else None
            if isinstance(videos, list) and videos:
                for video in videos:
                    if isinstance(video, dict):
                        picked = _twitter_variant_pick(video.get("variants") or [], want_best)
                        if picked is None:
                            picked_url = _as_http_url(video.get("videoUrl") or video.get("url"))
                            picked = picked_url or ""
                        if picked:
                            result.media_specs.append((picked, MediaKind.VIDEO))
                if result.media_specs:
                    break
            scored = [entry for entry in _collect_media(item, frozenset({MediaKind.VIDEO})) if entry[0].endswith(_VIDEO_EXT) or "video" in entry[0]]
            if scored:
                result.media_specs.append((scored[0][0], MediaKind.VIDEO))
                break
        return result

    if platform == Platform.FACEBOOK:
        # Verified output: {url, title, videoUrl <direct>, audioUrl <direct>?}
        want_audio = kind == "audio"
        for item in items:
            container = item.get("result") if isinstance(item.get("result"), dict) else item
            title_value = container.get("title") or container.get("name")
            if isinstance(title_value, str) and title_value.strip():
                result.metadata["title"] = title_value.strip()[:200]
            audio_url = _as_http_url(container.get("audioUrl") or container.get("audio_url"))
            video_url = _as_http_url(container.get("videoUrl") or container.get("video_url"))
            if want_audio:
                if audio_url:
                    result.media_specs.append((audio_url, MediaKind.AUDIO))
                elif video_url:
                    # Actor returned only the video — flag MP3 extraction.
                    result.media_specs.append((video_url, MediaKind.VIDEO))
                    result.audio_fallback_video = True
            else:
                if video_url:
                    result.media_specs.append((video_url, MediaKind.VIDEO))
                else:
                    scored = _collect_media(item, frozenset({MediaKind.VIDEO}))
                    if scored:
                        result.media_specs.append((scored[0][0], MediaKind.VIDEO))
            if result.media_specs:
                break
        return result

    if platform == Platform.PINTEREST:
        wanted = frozenset({MediaKind.VIDEO}) if kind == "video" else frozenset({MediaKind.PHOTO})
        for item in items:
            container = item.get("result") if isinstance(item.get("result"), dict) else item
            if kind == "video":
                # easyapi video downloader: result.medias[].url (direct file).
                medias = _medias_list(item)
                picked = _pick_best_media(medias) if medias else None
                if picked:
                    result.media_specs.append((picked[0], MediaKind.VIDEO))
                else:
                    scored = _collect_media(item, wanted)
                    result.media_specs.extend((url, mkind) for url, mkind, _ in scored[:2])
            else:
                # fatihtahta scraper: images[] ranked by width/height — pick
                # the largest original for the "original quality" option.
                images = item.get("images")
                if isinstance(images, list) and images:
                    best: tuple[int, str] | None = None
                    for image in images:
                        url = _as_http_url(image.get("url") if isinstance(image, dict) else image)
                        if url is None:
                            continue
                        try:
                            area = int(image.get("width") or 0) * int(image.get("height") or 0)
                        except (TypeError, ValueError):
                            area = 0
                        if best is None or area > best[0]:
                            best = (area, url)
                    if best:
                        result.media_specs.append((best[1], MediaKind.PHOTO))
                if not result.media_specs:
                    scored = _collect_media(item, wanted)
                    result.media_specs.extend((url, mkind) for url, mkind, _ in scored[:4])
            if result.media_specs:
                title_value = container.get("title") or container.get("grid_title")
                if isinstance(title_value, str) and title_value.strip():
                    result.metadata["title"] = title_value.strip()[:200]
                break
        return result

    return result


def _spotify_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    title = item.get("trackName") or item.get("track_title") or item.get("title") or item.get("name")
    if isinstance(title, str) and title.strip():
        metadata["title"] = title.strip()[:200]
    artists_value = item.get("artistNames") or item.get("artist") or item.get("artists")
    if isinstance(artists_value, list):
        metadata["artists"] = ", ".join(str(a) for a in artists_value[:4])
    elif isinstance(artists_value, str) and artists_value.strip():
        metadata["artists"] = artists_value.strip()[:200]
    album_value = item.get("albumName") or item.get("album")
    if isinstance(album_value, str) and album_value.strip():
        metadata["album"] = album_value.strip()[:150]
    elif isinstance(album_value, dict):
        album_name = album_value.get("name") or album_value.get("title")
        if isinstance(album_name, str) and album_name.strip():
            metadata["album"] = album_name.strip()[:150]
    cover = _as_http_url(item.get("coverImageUrl") or item.get("cover_image") or item.get("cover"))
    if cover:
        metadata["cover_url"] = cover
    duration_ms = item.get("durationMs")
    if isinstance(duration_ms, (int, float)) and duration_ms > 0:
        metadata["duration_ms"] = int(duration_ms)
    else:
        seconds = _duration_to_seconds(item.get("durationText") or item.get("duration"))
        if seconds and seconds < 3600 * 3:  # guard: "duration" fields are mm:ss
            metadata["duration_ms"] = int(seconds * 1000)
    return metadata


def _soundcloud_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    title = item.get("title") or item.get("name")
    if isinstance(title, str) and title.strip():
        metadata["title"] = title.strip()[:200]
    cover = _as_http_url(item.get("thumbnail") or item.get("artwork_url") or item.get("artworkUrl"))
    if cover:
        metadata["cover_url"] = cover
    seconds = _duration_to_seconds(item.get("duration"))
    if seconds:
        metadata["duration_ms"] = int(seconds * 1000)
    return metadata


def build_media_caption(metadata: dict[str, Any]) -> str:
    """Render extracted metadata as a compact HTML-free caption body."""
    lines: list[str] = []
    if metadata.get("title"):
        lines.append(f"🎵 {metadata['title']}")
    if metadata.get("artists"):
        lines.append(f"🎤 {metadata['artists']}")
    if metadata.get("album"):
        lines.append(f"💿 {metadata['album']}")
    duration_ms = metadata.get("duration_ms")
    if isinstance(duration_ms, int) and duration_ms > 0:
        seconds = duration_ms // 1000
        lines.append(f"⏱ {seconds // 60}:{seconds % 60:02d}")
    return "\n".join(lines)


_IS_HLS_RE = re.compile(r"\.m3u8(\?|$)|\.mpd(\?|$)", re.IGNORECASE)


def is_hls_url(url: str) -> bool:
    return bool(_IS_HLS_RE.search(url.split("?")[0] + "?"))
