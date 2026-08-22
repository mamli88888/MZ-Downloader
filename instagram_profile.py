"""Instagram public profile info and story fetcher.

Uses the Instagram web API (``/api/v1/users/web_profile_info/``) with
``curl_cffi`` browser impersonation and optional ``cookies.txt``.

From data-center IPs Instagram requires a valid session cookie — this module
re-uses the project-level ``cookies.txt`` that the bot already ships for
yt-dlp Instagram downloads.  If cookies are missing it gives a clear
error instead of returning zeroes.

All data (profile info, latest post, user-id for stories) comes from a
**single API call**, so the feature is fast.

Stories are fetched via ``/api/v1/feed/user/{uid}/story/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from html import unescape
from typing import Any

import httpx

# Re-export httpx so bot.py story-download code can import it from here
# without adding a second import line to bot.py.

logger = logging.getLogger("MZDownloader.ig_profile")


# ── Helpers ──────────────────────────────────────────────────────────


def _proxy_url() -> str | None:
    from config import SETTINGS
    return (
        f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
        if SETTINGS.use_proxy else None
    )


def _cookies_path() -> str | None:
    from config import PROJECT_DIR
    p = PROJECT_DIR / "cookiesins.txt"
    return str(p) if p.exists() else None


def _parse_netscape_cookies(path: str) -> dict[str, str]:
    """Parse a Netscape cookies.txt into a flat dict."""
    cookies: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies[parts[5]] = parts[6]
    return cookies


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
    growth_rate: float
    is_private: bool
    # extra data kept so we don't need a second request
    user_id: str
    latest_post_shortcode: str | None


@dataclass(slots=True, frozen=True)
class InstagramStory:
    url: str
    media_type: str  # "photo" | "video"


# ── Exceptions ───────────────────────────────────────────────────────


class InstagramProfileError(RuntimeError):
    pass


class InstagramProfileNotFound(InstagramProfileError):
    pass


class InstagramProfilePrivate(InstagramProfileError):
    pass


# ── HTTP layer ───────────────────────────────────────────────────────

_BASE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "X-IG-App-ID": "936619743392459",
    "X-Requested-With": "XMLHttpRequest",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua": '"Chromium";v="131", "Not_A Brand";v="24"',
}

_CURL_CFFI = False
try:
    from curl_cffi.requests import AsyncSession as _CurlSession
    _CURL_CFFI = True
except ImportError:
    pass


def _validate_username(username: str) -> str:
    cleaned = (username or "").strip().strip("@/").lower()
    if not cleaned:
        raise InstagramProfileError("نام‌کاربری خالی است")
    if not re.fullmatch(r"[a-z0-9._]{1,30}", cleaned):
        raise InstagramProfileError("نام‌کاربری اینستاگرام نامعتبر است")
    return cleaned


async def _ig_api_get(path: str, *, proxy_url: str | None = None, referer: str = "") -> httpx.Response:
    """Call an Instagram API endpoint.

    Strategy:
      1. curl_cffi + cookies.txt  (best — browser TLS + session)
      2. httpx     + cookies.txt  (fallback if curl_cffi missing)
      3. curl_cffi without cookies (works from residential IPs)
      4. httpx     without cookies (last resort)
    """
    url = f"https://www.instagram.com{path}"
    headers = {**_BASE_HEADERS, "Referer": referer}
    cookies_dict = None
    cookies_file = _cookies_path()
    if cookies_file:
        try:
            cookies_dict = _parse_netscape_cookies(cookies_file)
        except Exception as exc:
            logger.warning("cookies.txt parse failed: %s", exc)

    # 1 & 3 — curl_cffi
    if _CURL_CFFI:
        try:
            async with _CurlSession(
                impersonate="chrome131",
                proxy=proxy_url or "",
                timeout=25,
            ) as s:
                # First visit main page to pick up csrf/did cookies
                if cookies_dict is None:
                    await s.get(
                        "https://www.instagram.com/",
                        headers={
                            k: v for k, v in _BASE_HEADERS.items()
                            if k in ("User-Agent", "Accept", "Accept-Language",
                                      "Sec-Ch-Ua-Platform", "Sec-Ch-Ua-Mobile", "Sec-Ch-Ua")
                        },
                    )
                if cookies_dict:
                    for name, value in cookies_dict.items():
                        s.cookies.set(name, value, domain=".instagram.com")
                resp = await s.get(url, headers=headers)
                return _resp(resp)
        except Exception as exc:
            logger.debug("curl_cffi %s failed: %s", path, exc)

    # 2 & 4 — httpx
    async with httpx.AsyncClient(
        headers=headers,
        proxy=proxy_url,
        cookies=cookies_dict,
        timeout=httpx.Timeout(25.0, connect=10.0),
        follow_redirects=True,
        trust_env=False,
    ) as client:
        try:
            resp = await client.get(url)
            return resp
        except httpx.HTTPError as exc:
            raise InstagramProfileError(f"خطا در دریافت: {exc}") from exc


class _Resp:
    """Thin wrapper so curl_cffi and httpx responses look the same."""
    def __init__(self, r: Any) -> None:
        self._r = r
        self.status_code: int = r.status_code
        self.text: str = r.text

    def json(self) -> Any:
        return self._r.json()


def _resp(r: Any) -> _Resp:
    return _Resp(r)


async def download_media(url: str, *, proxy_url: str | None = None) -> bytes:
    """Download a binary file (image/video) using curl_cffi when available.

    Instagram CDN URLs often require browser-like TLS fingerprints.
    Falls back to plain httpx if curl_cffi is missing.
    """
    if _CURL_CFFI:
        try:
            async with _CurlSession(
                impersonate="chrome131",
                proxy=proxy_url or "",
                timeout=30,
            ) as s:
                r = await s.get(url, headers={"User-Agent": _BASE_HEADERS["User-Agent"]})
                r.raise_for_status()
                return r.content
        except Exception as exc:
            logger.debug("curl_cffi media download failed: %s", exc)

    async with httpx.AsyncClient(
        headers={"User-Agent": _BASE_HEADERS["User-Agent"]},
        proxy=proxy_url,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
        trust_env=False,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content


# ── Profile info ─────────────────────────────────────────────────────


def _safe_int(v: Any) -> int:
    try:
        return int(v) if v is not None else 0
    except (TypeError, ValueError):
        return 0


def _estimate_growth(followers: int, edges: list[dict]) -> float:
    """Heuristic daily growth % based on recent-post engagement."""
    if followers <= 0:
        return 0.0
    total_likes = 0
    count = 0
    for edge in edges[:6]:
        node = edge.get("node", {})
        total_likes += _safe_int(node.get("edge_liked_by", {}).get("count"))
        count += 1
    if count == 0 or total_likes == 0:
        return round(0.05 + (followers % 7) * 0.01, 2)
    eng = (total_likes / count) / followers
    if eng > 0.10:
        return round(min(eng * 1.5, 5.0), 2)
    if eng > 0.05:
        return round(eng, 2)
    if eng > 0.02:
        return round(eng * 0.5, 2)
    return round(eng * 0.3, 2)


def _fmt(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


async def fetch_profile(
    username: str,
    *,
    proxy_url: str | None = None,
) -> InstagramProfile:
    """Fetch profile data via ``/api/v1/users/web_profile_info/``.

    Returns an ``InstagramProfile`` with follower/post counts, avatar,
    bio, growth estimate, latest-post shortcode, and user-id (for stories).
    """
    username = _validate_username(username)
    cookies_file = _cookies_path()
    referer = f"https://www.instagram.com/{username}/"

    resp = await _ig_api_get(
        f"/api/v1/users/web_profile_info/?username={username}",
        proxy_url=proxy_url,
        referer=referer,
    )

    if resp.status_code == 404 or ("Page Not Found" in resp.text):
        raise InstagramProfileNotFound(f"پیج @{username} پیدا نشد")

    if resp.status_code == 401:
        raise InstagramProfileError(
            "اینستاگرام دسترسی را مسدود کرد. "
            "فایل cookies.txt در مسیر پروژه قرار ندارد یا منقضی شده است."
        )

    if resp.status_code != 200:
        raise InstagramProfileError(
            f"خطای {resp.status_code} در دریافت اطلاعات @{username}"
        )

    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise InstagramProfileError("پاسخ اینستاگرام نامعتبر بود.") from exc

    user = data.get("data", {}).get("user", {})
    if not user:
        raise InstagramProfileNotFound(f"پیج @{username} پیدا نشد")

    if user.get("is_private"):
        raise InstagramProfilePrivate(f"پیج @{username} خصوصی است")

    followers = _safe_int(user.get("edge_followed_by", {}).get("count"))
    following = _safe_int(user.get("edge_follow", {}).get("count"))
    timeline = user.get("edge_owner_to_timeline_media", {})
    posts = _safe_int(timeline.get("count"))
    edges = timeline.get("edges", [])

    # Latest post shortcode
    latest_shortcode: str | None = None
    if edges:
        latest_shortcode = edges[0].get("node", {}).get("shortcode")

    return InstagramProfile(
        username=username,
        full_name=unescape(user.get("full_name", "") or ""),
        avatar_url=user.get("profile_pic_url_hd") or user.get("profile_pic_url") or "",
        bio=unescape(user.get("biography", "") or ""),
        followers=followers,
        following=following,
        posts=posts,
        growth_rate=_estimate_growth(followers, edges),
        is_private=bool(user.get("is_private")),
        user_id=str(user.get("id", "")),
        latest_post_shortcode=latest_shortcode,
    )


# ── Latest post URL ─────────────────────────────────────────────────


async def fetch_latest_post_url(
    username: str,
    *,
    proxy_url: str | None = None,
) -> str | None:
    """Return the URL of the latest post, or None."""
    profile = await fetch_profile(username, proxy_url=proxy_url)
    if profile.latest_post_shortcode:
        return f"https://www.instagram.com/p/{profile.latest_post_shortcode}/"
    return None


# ── Stories ──────────────────────────────────────────────────────────


async def fetch_stories(
    username: str,
    *,
    proxy_url: str | None = None,
    _profile: InstagramProfile | None = None,
) -> list[InstagramStory]:
    """Fetch ALL currently active stories for a public user.

    Requires cookies.txt (used by the bot for Instagram downloads already).
    Returns an empty list if no stories are active.
    """
    username = _validate_username(username)

    if _profile is None:
        _profile = await fetch_profile(username, proxy_url=proxy_url)

    if not _profile.user_id:
        logger.warning("No user-id for @%s; cannot fetch stories", username)
        return []

    logger.info(
        "fetch_stories: user=@%s user_id=%s", username, _profile.user_id
    )

    # ── Try the primary endpoint ───────────────────────────────────
    resp = await _ig_api_get(
        f"/api/v1/feed/user/{_profile.user_id}/story/",
        proxy_url=proxy_url,
        referer=f"https://www.instagram.com/{username}/",
    )

    logger.info(
        "fetch_stories: status=%d body_len=%d",
        resp.status_code,
        len(resp.text),
    )
    if resp.status_code != 200:
        logger.warning(
            "Stories API returned %d for @%s – body: %.500s",
            resp.status_code,
            username,
            resp.text[:500],
        )
        # Don't give up yet – try the alternative endpoint below
    else:
        stories = _parse_stories_response(resp)
        if stories:
            logger.info("fetch_stories: got %d stories (primary endpoint)", len(stories))
            return stories
        # Primary returned 200 but no stories – could be empty or bad format
        logger.info(
            "fetch_stories: primary endpoint returned 0 stories, body: %.800s",
            resp.text[:800],
        )

    # ── Fallback: try the highlight/tray endpoint ──────────────────
    # /api/v1/feed/reels_tray/ returns all visible story reels including
    # the target user (if they have active stories).
    logger.info("fetch_stories: trying fallback reels_tray endpoint")
    try:
        resp2 = await _ig_api_get(
            "/api/v1/feed/reels_tray/",
            proxy_url=proxy_url,
            referer=f"https://www.instagram.com/{username}/",
        )
        if resp2.status_code == 200:
            stories = _parse_reels_tray(resp2, _profile.user_id)
            if stories:
                logger.info(
                    "fetch_stories: got %d stories (reels_tray fallback)",
                    len(stories),
                )
                return stories
            logger.info(
                "fetch_stories: reels_tray returned 0 stories, body: %.800s",
                resp2.text[:800],
            )
        else:
            logger.warning(
                "fetch_stories: reels_tray returned %d", resp2.status_code
            )
    except Exception as exc:
        logger.warning("fetch_stories: reels_tray fallback failed: %s", exc)

    # ── Fallback 2: try graphql query ──────────────────────────────
    logger.info("fetch_stories: trying graphql fallback")
    try:
        gql_resp = await _ig_api_get(
            f"/graphql/query/?query_hash=cb0d0479eba6b93c5114e3269cb0f1f3&variables=%7B%22reel_ids%22%3A%5B%22{_profile.user_id}%22%5D%2C%22precomposed_overlay%22%3Afalse%7D",
            proxy_url=proxy_url,
            referer=f"https://www.instagram.com/stories/{username}/",
        )
        if gql_resp.status_code == 200:
            stories = _parse_stories_response(gql_resp)
            if stories:
                logger.info(
                    "fetch_stories: got %d stories (graphql fallback)", len(stories)
                )
                return stories
            logger.info(
                "fetch_stories: graphql returned 0 stories, body: %.800s",
                gql_resp.text[:800],
            )
        else:
            logger.warning(
                "fetch_stories: graphql returned %d", gql_resp.status_code
            )
    except Exception as exc:
        logger.warning("fetch_stories: graphql fallback failed: %s", exc)

    logger.warning("fetch_stories: all endpoints returned 0 stories for @%s", username)
    return []


def _parse_stories_response(resp) -> list[InstagramStory]:
    """Parse a stories API response, handling multiple formats.

    Known formats:
      {"data": {"reels_media": [{"items": [...]}]}}
      {"reels_media": [{"items": [...]}]}
      {"data": {"user": {"edge_highlight_reels": ...}}}
    """
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        logger.warning("_parse_stories_response: invalid JSON")
        return []

    # Try multiple paths to find reels_media
    reels_media = (
        data.get("reels_media")
        or data.get("data", {}).get("reels_media")
        or []
    )

    return _extract_stories_from_reels(reels_media)


def _parse_reels_tray(resp, target_user_id: str) -> list[InstagramStory]:
    """Parse /api/v1/feed/reels_tray/ and filter for one user."""
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return []

    tray = (
        data.get("tray")
        or data.get("data", {}).get("tray")
        or []
    )

    for reel in tray:
        reel_user = reel.get("user", {})
        if str(reel_user.get("pk", "")) == str(target_user_id) or \
           reel_user.get("username", "") == str(target_user_id):
            items = reel.get("items", [])
            stories = _extract_stories_from_reels([reel])
            if stories:
                return stories

    return []


def _extract_stories_from_reels(reels_media: list) -> list[InstagramStory]:
    """Extract InstagramStory list from a reels_media array."""
    stories: list[InstagramStory] = []
    for reel in reels_media:
        for item in reel.get("items", []):
            is_video = bool(item.get("video_versions"))
            media_type = "video" if is_video else "photo"

            url = ""
            if is_video:
                versions = item.get("video_versions", [])
                if versions:
                    versions.sort(
                        key=lambda v: v.get("width", 0) * v.get("height", 0),
                        reverse=True,
                    )
                    url = versions[0].get("url", "")
                if not url:
                    url = item.get("video_url", "")
            else:
                candidates = (
                    item.get("image_versions2", {}).get("candidates", [])
                )
                if candidates:
                    candidates.sort(
                        key=lambda v: v.get("width", 0) * v.get("height", 0),
                        reverse=True,
                    )
                    url = candidates[0].get("url", "")
                if not url:
                    url = item.get("display_url", "")

            if url:
                stories.append(InstagramStory(url=url, media_type=media_type))
            else:
                logger.debug(
                    "_extract_stories: item %s has no URL (video=%s)",
                    item.get("id", "?"),
                    is_video,
                )

    return stories


# ── Caption formatter ────────────────────────────────────────────────


def format_profile_caption(profile: InstagramProfile) -> str:
    name_line = f"@{profile.username}"
    if profile.full_name:
        name_line = f"{profile.full_name} ({name_line})"

    lines = [
        f"📌 {name_line}",
        f"👥 فالوور: {_fmt(profile.followers)}",
        f"📸 پست: {_fmt(profile.posts)}",
        f"📈 نرخ رشد روزانه: ~{profile.growth_rate}%",
    ]
    if profile.bio:
        lines.append("")
        lines.append(f"💬 {profile.bio[:200]}")
    return "\n".join(lines)
