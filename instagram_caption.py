from __future__ import annotations

import re
import time
from collections import OrderedDict
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx


FORM_URL = "https://instaspeeder.com/copy-instagram-caption/"
ENDPOINT_URL = "https://instaspeeder.com/app/instagram/getCaptionRapid.php"
ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com", "instagr.am", "www.instagr.am"}
POST_PATH_PATTERN = re.compile(r"^/(p|reel|tv)/([A-Za-z0-9_-]+)/?$")
CACHE_TTL_SECONDS = 15 * 60
CACHE_MAX_ITEMS = 256


class InstagramCaptionError(RuntimeError):
    pass


class InstagramCaptionNotFound(InstagramCaptionError):
    pass


class _ResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []
        self.all_text: list[str] = []
        self._capture = False
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.lower(): value or "" for key, value in attrs}
        marker = " ".join((attributes.get("id", ""), attributes.get("class", ""), attributes.get("name", ""))).lower()
        if attributes.get("data-caption"):
            self.candidates.append(attributes["data-caption"])
        if tag.lower() == "input" and "caption" in marker and attributes.get("value"):
            self.candidates.append(attributes["value"])
        self._capture = tag.lower() in {"textarea", "pre"} or "caption" in marker
        if self._capture:
            self._chunks = []

    def handle_data(self, data: str) -> None:
        clean = data.strip()
        if clean:
            self.all_text.append(clean)
        if self._capture:
            self._chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._capture and tag.lower() in {"textarea", "pre", "p", "div", "span"}:
            value = "".join(self._chunks).strip()
            if value:
                self.candidates.append(value)
            self._capture = False
            self._chunks = []


_CACHE: OrderedDict[str, tuple[float, str]] = OrderedDict()


def canonical_instagram_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().strip(".")
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise InstagramCaptionError("Invalid Instagram URL") from exc
    if parsed.scheme.lower() != "https" or host not in ALLOWED_HOSTS:
        raise InstagramCaptionError("Only public HTTPS Instagram post URLs are supported")
    if parsed.username or parsed.password or port not in {None, 443}:
        raise InstagramCaptionError("Unsafe Instagram URL")
    match = POST_PATH_PATTERN.fullmatch(parsed.path.rstrip("/") + "/")
    if not match:
        raise InstagramCaptionError("Instagram URL is not a post, reel, or IGTV link")
    kind, shortcode = match.groups()
    return f"https://www.instagram.com/{kind}/{shortcode}/"


def _clean(value: str) -> str:
    clean = unescape(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    blocked = (
        "post is private or not found",
        "verify you are not robot",
        "recaptcha",
        "uppps",
        "error happened",
        "copy caption",
        "search",
    )
    if not clean or any(marker in clean.casefold() for marker in blocked):
        return ""
    return clean


def extract_instagram_caption(document: str) -> str:
    parser = _ResultParser()
    parser.feed(document)
    candidates = [_clean(value) for value in parser.candidates]
    candidates = [value for value in candidates if value]
    if not candidates:
        fallback = [_clean(value) for value in parser.all_text]
        candidates = [value for value in fallback if len(value) >= 2]
    if not candidates:
        raise InstagramCaptionNotFound("Instaspeeder returned no caption")
    # The caption is normally the longest text field in the result fragment.
    return max(candidates, key=len)


def _cache_get(key: str) -> str:
    cached = _CACHE.get(key)
    if cached is None:
        return ""
    created, caption = cached
    if time.monotonic() - created >= CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return ""
    _CACHE.move_to_end(key)
    return caption


def _cache_put(key: str, caption: str) -> None:
    _CACHE[key] = (time.monotonic(), caption)
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX_ITEMS:
        _CACHE.popitem(last=False)


async def fetch_instagram_caption(
    url: str,
    *,
    proxy_url: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> str:
    canonical = canonical_instagram_url(url)
    cached = _cache_get(canonical)
    if cached:
        return cached
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": FORM_URL,
        "X-Requested-With": "XMLHttpRequest",
    }
    async with httpx.AsyncClient(
        headers=headers,
        proxy=proxy_url,
        transport=transport,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=False,
        trust_env=False,
    ) as client:
        try:
            form = await client.get(FORM_URL)
            form.raise_for_status()
            response = await client.post(ENDPOINT_URL, data={"url": canonical, "token": ""})
            response.raise_for_status()
            caption = extract_instagram_caption(response.text)
        except httpx.HTTPError as exc:
            raise InstagramCaptionError("Instaspeeder request failed") from exc
        _cache_put(canonical, caption)
        return caption
