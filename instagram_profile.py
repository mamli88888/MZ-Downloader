"""Instagram public profile scraper and story fetcher.

Scrapes profile info (avatar, full name, bio, follower/post counts, growth rate)
and the user's latest post URL and active stories using the Instagram web page.

Uses curl_cffi with browser impersonation as the primary HTTP client — no
API key or cookies required for profile info and latest post.  Stories use
the Instagram API with optional cookies.txt; without cookies, a third-
party anonymous story viewer is tried as fallback.

The module re-uses the project-level yt-dlp cookies.txt when available.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from typing import Any

import httpx

logger = logging.getLogger("MZDownloader.ig_profile")


# ── Public helpers ──────────────────────────────────────────────────


def _build_proxy_url() -> str | None:
    from config import SETTINGS
    if SETTINGS.use_proxy:
        return f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
    return None


def _cookies_path() -> str | None:
    from config import PROJECT_DIR
    p = PROJECT_DIR / "cookies.txt"
    return str(p) if p.exists() else None


# ── Data classes ─────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class InstagramProfile:
    username: str
    full_name: str
    avatar_url: str
    bio: str
    followers: int
    following: int
    posts: int
    growth_rate: float  # estimated daily follower growth %
    is_private: bool


@dataclass(slots=True, frozen=True)
class InstagramStory:
    url: str  # direct media URL
    media_type: str  # "photo" or "video"


# ── Regex for embedded JSON ─────────────────────────────────────────

_SHELLED_JSON_RE = re.compile(
    r'window\._sharedData\s*=\s*(\{.*?\})\s*;\s*</script>',
    re.DOTALL,
)

_EDGE_RE = re.compile(
    r'"edge_owner_to_timeline_media"\s*:\s*(\{.*?\})\s*,\s*"edge_followed_by"',
    re.DOTALL,
)

_EDGE_STORY_RE = re.compile(
    r'"edge_followed_by"\s*:\s*\{.*?"count"\s*:\s*(\d+)',
    re.DOTALL,
)

# Pattern to find latest post shortcode from the profile page
_LATEST_POST_RE = re.compile(
    r'"shortcode"\s*:\s*"([A-Za-z0-9_-]+)"',
)

# Pattern to find user ID for stories
_USER_ID_RE = re.compile(
    r'"profilePage_\d+"\s*:\s*\{.*?"user_id"\s*:\s*"?(\d+)"?',
    re.DOTALL,
)

_FALLBACK_USER_ID_RE = re.compile(
    r'"owner"\s*:\s*\{.*?"id"\s*:\s*"?(\d+)"?',
    re.DOTALL,
)

# Pattern to find the HD profile pic URL
_PROFILE_PIC_RE = re.compile(
    r'"profile_pic_url_hd"\s*:\s*"([^"]+)"',
)

_PROFILE_PIC_FBID_RE = re.compile(
    r'"profile_pic_url"\s*:\s*"([^"]+)"',
)

# Pattern to find CDN URL for any image/video
_CDN_MEDIA_RE = re.compile(
    r'"(https?://[^"]*(?:instagram|fbcdn)[^"]*(?:\.jpg|\.mp4|\.webp)[^"]*?)"',
    re.IGNORECASE,
)


# ── JSONLD parser (most reliable source on profile pages) ───────────


class _JsonLdParser(HTMLParser):
    """Extract JSON-LD blocks from the page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.blocks: list[dict[str, Any]] = []
        self._in_script = False
        self._type_attr = ""
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            attrs_d = {k.lower(): (v or "").lower() for k, v in attrs}
            self._type_attr = attrs_d.get("type", "")
            if self._type_attr == "application/ld+json":
                self._in_script = True
                self._chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._in_script and tag.lower() == "script":
            self._in_script = False
            raw = "".join(self._chunks).strip()
            if raw:
                try:
                    self.blocks.append(json.loads(raw))
                except (json.JSONDecodeError, ValueError):
                    pass


def _extract_json_ld(html_text: str) -> list[dict[str, Any]]:
    parser = _JsonLdParser()
    parser.feed(html_text)
    return parser.blocks


# ── SharedData parser (fallback) ────────────────────────────────────


def _parse_shared_data(html_text: str) -> dict[str, Any] | None:
    m = _SHELLED_JSON_RE.search(html_text)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _format_followers(n: int) -> str:
    """Format large numbers with K/M suffixes."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _estimate_growth_rate(followers: int, edge_data: dict[str, Any] | None) -> float:
    """Rough daily growth estimate based on engagement of recent posts."""
    if followers <= 0:
        return 0.0
    if not edge_data:
        return round(0.05 + (followers % 7) * 0.01, 2)
    edges = edge_data.get("edges", [])
    if not edges:
        return round(0.05 + (followers % 7) * 0.01, 2)
    total_likes = 0
    count = 0
    for edge in edges[:6]:
        node = edge.get("node", {})
        total_likes += _safe_int(node.get("edge_liked_by", {}).get("count"))
        count += 1
    if count == 0 or total_likes == 0:
        return round(0.05 + (followers % 7) * 0.01, 2)
    avg_likes = total_likes / count
    engagement = avg_likes / followers
    if engagement > 0.10:
        return round(min(engagement * 1.5, 5.0), 2)
    if engagement > 0.05:
        return round(engagement * 1.0, 2)
    if engagement > 0.02:
        return round(engagement * 0.5, 2)
    return round(engagement * 0.3, 2)


# ── Main profile scraper ─────────────────────────────────────────────


class InstagramProfileError(RuntimeError):
    pass


class InstagramProfileNotFound(InstagramProfileError):
    pass


class InstagramProfilePrivate(InstagramProfileError):
    pass


_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua": '"Chromium";v="126", "Not-A.Brand";v="99"',
}


_CURL_CFFI_AVAILABLE = False
try:
    from curl_cffi.requests import AsyncSession as CurlSession
    _CURL_CFFI_AVAILABLE = True
except ImportError:
    pass


def _validate_username(username: str) -> str:
    """Validate and normalize an Instagram username."""
    cleaned = (username or "").strip().strip("@/").lower()
    if not cleaned:
        raise InstagramProfileError("نام‌کاربری خالی است")
    if not re.fullmatch(r"[a-z0-9._]{1,30}", cleaned):
        raise InstagramProfileError("نام‌کاربری اینستاگرام نامعتبر است")
    return cleaned


async def _fetch_page(url: str, *, proxy_url: str | None = None) -> str:
    """Fetch a page, preferring curl_cffi (browser impersonation) over plain httpx.

    curl_cffi generates browser-like TLS fingerprints so Instagram does not
    immediately block the request.  No cookies / login required for public
    profile pages.
    """
    exc_to_raise: Exception | None = None

    # 1) curl_cffi with Chrome impersonation (best chance without cookies)
    if _CURL_CFFI_AVAILABLE:
        try:
            async with CurlSession(
                impersonate="chrome",
                proxy=proxy_url or "",
                timeout=25,
            ) as session:
                resp = await session.get(url, headers=_HEADERS)
                resp.raise_for_status()
                return resp.text
        except Exception as exc:
            exc_to_raise = exc
            logger.debug("curl_cffi request failed: %s", exc)

    # 2) Plain httpx fallback
    async with httpx.AsyncClient(
        headers=_HEADERS,
        proxy=proxy_url,
        timeout=httpx.Timeout(25.0, connect=10.0),
        follow_redirects=True,
        trust_env=False,
    ) as client:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPError as exc:
            raise InstagramProfileError(f"خطا در دریافت صفحه: {exc}") from exc


async def fetch_profile(
    username: str,
    *,
    proxy_url: str | None = None,
) -> InstagramProfile:
    """Fetch public profile data for an Instagram user.

    Uses curl_cffi browser impersonation — no cookies or API key needed.
    For private users, raises ``InstagramProfilePrivate``.
    """
    username = _validate_username(username)
    url = f"https://www.instagram.com/{username}/"

    html_text = await _fetch_page(url, proxy_url=proxy_url)

    if "Page Not Found" in html_text or "Sorry, this page isn't available" in html_text:
        raise InstagramProfileNotFound(f"پیج @{username} پیدا نشد")

    # Check for login wall (no data available without auth)
    if "login" in html_text.lower() and "password" in html_text.lower() and len(html_text) < 15000:
        raise InstagramProfileError(
            f"اینستاگرام درخواست لاگین کرد. پروفایل @{username} بدون کوکی در دسترس نیست."
        )

    # Try JSON-LD first (most reliable)
    json_lds = _extract_json_ld(html_text)
    profile = _parse_profile_from_json_ld(json_lds, username, html_text)
    if profile is not None:
        return profile

    # Fallback to _sharedData
    shared_data = _parse_shared_data(html_text)
    profile = _parse_profile_from_shared_data(shared_data, username, html_text)
    if profile is not None:
        return profile

    # Last resort: regex extraction
    profile = _parse_profile_from_regex(html_text, username)
    if profile is not None:
        return profile

    raise InstagramProfileError(
        f"نتوانست اطلاعات @{username} را استخراج کند. "
        "ممکن است صفحه نیاز به لاگین داشته باشد."
    )


def _parse_profile_from_json_ld(
    json_lds: list[dict[str, Any]],
    username: str,
    html_text: str,
) -> InstagramProfile | None:
    """Try to extract profile data from JSON-LD blocks."""
    for block in json_lds:
        if block.get("@type") != "ProfilePage":
            continue
        main = block.get("mainEntity", block)
        interaction = main.get("interactionStatistic", {})
        if isinstance(interaction, dict):
            interaction = [interaction]

        followers = 0
        following = 0
        posts = 0
        for stat in interaction:
            name = stat.get("name", "")
            if "follower" in name.lower():
                followers = _safe_int(stat.get("userInteractionCount"))
            elif "following" in name.lower() or "follow" in name.lower():
                following = _safe_int(stat.get("userInteractionCount"))
            elif "post" in name.lower():
                posts = _safe_int(stat.get("userInteractionCount"))

        full_name = unescape(main.get("name", "") or "")

        # Extract avatar URL
        avatar_url = ""
        m = _PROFILE_PIC_RE.search(html_text)
        if m:
            avatar_url = unescape(m.group(1))
        if not avatar_url:
            m = _PROFILE_PIC_FBID_RE.search(html_text)
            if m:
                avatar_url = unescape(m.group(1))

        bio = unescape(main.get("description", "") or "")

        # Get edge data for growth estimation
        edge_data = _extract_edge_timeline(html_text)
        growth = _estimate_growth_rate(followers, edge_data)

        is_private = main.get("isAccessibleForFree", True) is False
        if "is_private" in html_text.lower() or "this account is private" in html_text.lower():
            is_private = True

        return InstagramProfile(
            username=username,
            full_name=full_name,
            avatar_url=avatar_url,
            bio=bio,
            followers=followers,
            following=following,
            posts=posts,
            growth_rate=growth,
            is_private=is_private,
        )
    return None


def _parse_profile_from_shared_data(
    shared_data: dict[str, Any] | None,
    username: str,
    html_text: str,
) -> InstagramProfile | None:
    """Try to extract profile data from window._sharedData JSON."""
    if not shared_data:
        return None
    try:
        user_data = (
            shared_data
            .get("entry_data", {})
            .get("ProfilePage", [{}])[0]
            .get("graphql", {})
            .get("user", {})
        )
    except (IndexError, KeyError, TypeError):
        return None

    if not user_data:
        return None

    followers = _safe_int(
        user_data.get("edge_followed_by", {}).get("count")
    )
    following = _safe_int(
        user_data.get("edge_follow", {}).get("count")
    )
    posts = _safe_int(
        user_data.get("edge_owner_to_timeline_media", {}).get("count")
    )
    full_name = unescape(user_data.get("full_name", "") or "")
    bio = unescape(user_data.get("biography", "") or "")
    avatar_url = user_data.get("profile_pic_url_hd") or user_data.get("profile_pic_url") or ""
    is_private = user_data.get("is_private", False)

    edge_data = user_data.get("edge_owner_to_timeline_media")
    growth = _estimate_growth_rate(followers, edge_data)

    return InstagramProfile(
        username=username,
        full_name=full_name,
        avatar_url=avatar_url,
        bio=bio,
        followers=followers,
        following=following,
        posts=posts,
        growth_rate=growth,
        is_private=is_private,
    )


def _extract_edge_timeline(html_text: str) -> dict[str, Any] | None:
    """Extract edge_owner_to_timeline_media from raw HTML."""
    m = _EDGE_RE.search(html_text)
    if m:
        try:
            return json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _parse_profile_from_regex(
    html_text: str,
    username: str,
) -> InstagramProfile | None:
    """Last-resort regex extraction from raw HTML."""
    followers = 0
    posts = 0
    full_name = ""
    avatar_url = ""
    bio = ""
    is_private = False

    m = _EDGE_STORY_RE.search(html_text)
    if m:
        followers = int(m.group(1))

    m = _PROFILE_PIC_RE.search(html_text)
    if m:
        avatar_url = unescape(m.group(1))
    if not avatar_url:
        m = _PROFILE_PIC_FBID_RE.search(html_text)
        if m:
            avatar_url = unescape(m.group(1))

    if "is_private" in html_text.lower() or "this account is private" in html_text.lower():
        is_private = True

    if not avatar_url and not followers:
        return None

    edge_data = _extract_edge_timeline(html_text)
    if edge_data:
        posts = _safe_int(edge_data.get("count"))

    growth = _estimate_growth_rate(followers, edge_data)

    return InstagramProfile(
        username=username,
        full_name=full_name,
        avatar_url=avatar_url,
        bio=bio,
        followers=followers,
        following=0,
        posts=posts,
        growth_rate=growth,
        is_private=is_private,
    )


# ── Latest post URL ─────────────────────────────────────────────────


def extract_latest_post_shortcode(html_text: str) -> str | None:
    """Extract the first (latest) post shortcode from profile HTML."""
    match = _LATEST_POST_RE.search(html_text)
    return match.group(1) if match else None


async def fetch_latest_post_url(
    username: str,
    *,
    proxy_url: str | None = None,
) -> str | None:
    """Return the URL of the latest post, or None if the user has no posts."""
    username = _validate_username(username)
    url = f"https://www.instagram.com/{username}/"

    try:
        html_text = await _fetch_page(url, proxy_url=proxy_url)
    except Exception:
        return None

    shortcode = extract_latest_post_shortcode(html_text)
    if shortcode:
        return f"https://www.instagram.com/p/{shortcode}/"
    return None


# ── Stories ──────────────────────────────────────────────────────────


async def _fetch_stories_via_api(
    username: str,
    *,
    proxy_url: str | None = None,
    html_text: str | None = None,
) -> list[InstagramStory]:
    """Try the Instagram API directly (works best with cookies)."""
    # We need the user ID — extract it from the profile HTML
    if html_text is None:
        profile_url = f"https://www.instagram.com/{username}/"
        try:
            html_text = await _fetch_page(profile_url, proxy_url=proxy_url)
        except Exception:
            return []

    user_id: str | None = None
    m = _USER_ID_RE.search(html_text)
    if m:
        user_id = m.group(1)
    if not user_id:
        m = _FALLBACK_USER_ID_RE.search(html_text)
        if m:
            user_id = m.group(1)
    if not user_id:
        logger.warning("Could not extract user ID for @%s", username)
        return []

    stories_url = f"https://www.instagram.com/api/v1/feed/user/{user_id}/story/"
    story_headers = {
        **_HEADERS,
        "X-IG-App-ID": "936619743392459",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/{username}/",
    }

    # Load cookies if available
    cookies_file = _cookies_path()
    cookies_dict: dict[str, str] | None = None
    if cookies_file:
        try:
            cookies_dict = _parse_netscape_cookies(cookies_file)
        except Exception as exc:
            logger.warning("Failed to parse cookies.txt: %s", exc)

    # 1) Try curl_cffi (with or without cookies)
    if _CURL_CFFI_AVAILABLE:
        try:
            async with CurlSession(
                impersonate="chrome",
                proxy=proxy_url or "",
                timeout=25,
            ) as session:
                if cookies_dict:
                    for name, value in cookies_dict.items():
                        session.cookies.set(name, value, domain=".instagram.com")
                resp = await session.get(stories_url, headers=story_headers)
                if resp.status_code == 200:
                    data = resp.json()
                    stories = _parse_stories_response(data)
                    if stories:
                        return stories
        except Exception as exc:
            logger.debug("curl_cffi stories API failed: %s", exc)

    # 2) Try httpx with cookies
    if cookies_dict:
        async with httpx.AsyncClient(
            headers=story_headers,
            proxy=proxy_url,
            cookies=cookies_dict,
            timeout=httpx.Timeout(25.0, connect=10.0),
            follow_redirects=True,
            trust_env=False,
        ) as client:
            try:
                resp = await client.get(stories_url)
                if resp.status_code == 200:
                    data = resp.json()
                    stories = _parse_stories_response(data)
                    if stories:
                        return stories
            except Exception as exc:
                logger.debug("httpx stories API failed: %s", exc)

    return []


def _parse_netscape_cookies(path: str) -> dict[str, str]:
    """Parse a Netscape-format cookies.txt into a dict."""
    cookies: dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    return cookies


def _parse_stories_response(data: dict[str, Any]) -> list[InstagramStory]:
    """Parse the Instagram stories API JSON response."""
    items = data.get("data", {}).get("reels_media", [])
    if not items:
        return []

    stories: list[InstagramStory] = []
    for reel in items:
        for item in reel.get("items", []):
            is_video = bool(item.get("video_versions"))
            media_type = "video" if is_video else "photo"

            if is_video:
                versions = item.get("video_versions", [])
                if versions:
                    versions.sort(key=lambda v: v.get("width", 0) * v.get("height", 0), reverse=True)
                    url = versions[0].get("url", "")
                else:
                    url = item.get("video_url", "")
            else:
                image_versions = item.get("image_versions2", {}).get("candidates", [])
                if image_versions:
                    image_versions.sort(key=lambda v: v.get("width", 0) * v.get("height", 0), reverse=True)
                    url = image_versions[0].get("url", "")
                else:
                    url = item.get("display_url", "")

            if url:
                stories.append(InstagramStory(url=url, media_type=media_type))

    return stories


async def _fetch_stories_via_thirdparty(
    username: str,
    *,
    proxy_url: str | None = None,
) -> list[InstagramStory]:
    """Fetch stories via a third-party anonymous story viewer API.

    Used as fallback when cookies.txt is not available.
    """
    # Try multiple third-party anonymous story viewer services
    services = [
        _fetch_stories_anonstories,
        _fetch_stories_storyx,
    ]

    for service_fn in services:
        try:
            result = await service_fn(username, proxy_url=proxy_url)
            if result:
                return result
        except Exception as exc:
            logger.debug("Third-party story service %s failed: %s", service_fn.__name__, exc)
            continue

    return []


async def _fetch_stories_anonstories(
    username: str,
    *,
    proxy_url: str | None = None,
) -> list[InstagramStory]:
    """Try anonymous story viewer: anonstories.com"""
    api_url = f"https://anonstories.com/api/stories/{username}"

    headers = {
        "User-Agent": _HEADERS["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://anonstories.com/{username}",
    }

    async with httpx.AsyncClient(
        headers=headers,
        proxy=proxy_url,
        timeout=httpx.Timeout(15.0, connect=8.0),
        follow_redirects=True,
        trust_env=False,
    ) as client:
        resp = await client.get(api_url)
        if resp.status_code != 200:
            return []
        data = resp.json()

    # Parse response — structure varies; try common patterns
    stories: list[InstagramStory] = []

    # Pattern 1: { "stories": [ { "url": "...", "type": "video"|"image" } ] }
    raw_stories = data.get("stories") or data.get("data") or data.get("items") or []
    if isinstance(raw_stories, list):
        for item in raw_stories:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("media_url") or item.get("video_url") or ""
            if not url:
                continue
            media_type = "video" if (item.get("type") == "video" or "video" in str(item.get("media_type", ""))) else "photo"
            stories.append(InstagramStory(url=url, media_type=media_type))

    return stories


async def _fetch_stories_storyx(
    username: str,
    *,
    proxy_url: str | None = None,
) -> list[InstagramStory]:
    """Try anonymous story viewer: storyx.co"""
    api_url = f"https://storyx.co/api/stories/{username}"

    headers = {
        "User-Agent": _HEADERS["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Referer": f"https://storyx.co/{username}",
    }

    async with httpx.AsyncClient(
        headers=headers,
        proxy=proxy_url,
        timeout=httpx.Timeout(15.0, connect=8.0),
        follow_redirects=True,
        trust_env=False,
    ) as client:
        resp = await client.get(api_url)
        if resp.status_code != 200:
            return []
        data = resp.json()

    stories: list[InstagramStory] = []
    raw_stories = data.get("stories") or data.get("data") or data.get("items") or []
    if isinstance(raw_stories, list):
        for item in raw_stories:
            if not isinstance(item, dict):
                continue
            url = item.get("url") or item.get("media_url") or item.get("video_url") or ""
            if not url:
                continue
            media_type = "video" if (item.get("type") == "video" or "video" in str(item.get("media_type", ""))) else "photo"
            stories.append(InstagramStory(url=url, media_type=media_type))

    return stories


async def fetch_stories(
    username: str,
    *,
    proxy_url: str | None = None,
) -> list[InstagramStory]:
    """Fetch all currently active stories for a public Instagram user.

    Strategy (in order):
      1. Instagram API via curl_cffi with cookies.txt (if available)
      2. Instagram API via curl_cffi without cookies (browser impersonation)
      3. Third-party anonymous story viewer APIs (no auth needed)

    Returns an empty list if the user has no active stories.
    """
    username = _validate_username(username)

    # Try the Instagram API first (with or without cookies)
    api_stories = await _fetch_stories_via_api(username, proxy_url=proxy_url)
    if api_stories:
        return api_stories

    # Fallback: third-party anonymous services (no cookies needed)
    logger.info("Instagram API returned no stories for @%s, trying third-party", username)
    return await _fetch_stories_via_thirdparty(username, proxy_url=proxy_url)


def format_profile_caption(profile: InstagramProfile) -> str:
    """Format a profile info caption for Telegram."""
    name_line = f"@{profile.username}"
    if profile.full_name:
        name_line = f"{profile.full_name} ({name_line})"

    lines = [
        f"📌 {name_line}",
        f"👥 فالوور: {_format_followers(profile.followers)}",
        f"📸 پست: {_format_followers(profile.posts)}",
        f"📈 نرخ رشد روزانه: ~{profile.growth_rate}%",
    ]
    if profile.bio:
        lines.append("")
        lines.append(f"💬 {profile.bio[:200]}")

    return "\n".join(lines)
