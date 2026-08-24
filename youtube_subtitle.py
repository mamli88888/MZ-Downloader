"""Fetch YouTube subtitles via the downsub.com API.

This module provides:

* :class:`YouTubeSubtitleError` / :class:`YouTubeSubtitleNotFound` exceptions
* :func:`fetch_youtube_subtitle` — async entry point that returns SRT bytes
  for a given YouTube URL and language code.

Supported languages:

* ``"fa"`` — Persian (manual, then auto-translated as fallback)
* ``"en"`` — English (manual, then auto-generated as fallback)

Why downsub.com?
----------------
Calling YouTube's ``timedtext`` API or yt-dlp directly from a cloud IP
triggers YouTube's "Sign in to confirm you're not a bot" challenge. The
downsub.com backend runs from a different network position, so it can
extract signed ``timedtext`` URLs from YouTube on our behalf.

Flow:

1. POST ``{"url": <raw_url>, "data": <encrypted>}`` to
   ``https://get.downsub.com/`` with ``Content-Type: application/json``
2. Response JSON contains ``subtitles[]`` and ``subtitlesAutoTrans[]`` with
   AES-encrypted URLs.
3. Decrypt each URL with the project key (CryptoJS-compatible AES-256-CBC
   with EVP_BytesToKey / MD5 KDF).
4. Replace the ``fmt=`` parameter with ``fmt=srt`` and fetch the SRT
   directly from ``youtube.com/api/timedtext`` — these URLs are signed
   by YouTube and do **not** trigger bot detection.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Any
from urllib.parse import urlsplit

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

logger = logging.getLogger(__name__)

# ---- languages ----
LANGUAGE_PERSIAN = "fa"
LANGUAGE_ENGLISH = "en"
SUPPORTED_LANGUAGES = (LANGUAGE_PERSIAN, LANGUAGE_ENGLISH)

LANGUAGE_LABELS = {
    LANGUAGE_PERSIAN: "Persian",
    LANGUAGE_ENGLISH: "English",
}

# Maps our internal language code → list of downsub.com display-name patterns
# (the API returns names like "Persian", "English", "English (auto-generated)").
_DOWNSUB_NAME_PATTERNS: dict[str, tuple[str, ...]] = {
    LANGUAGE_PERSIAN: ("Persian", "Farsi"),
    LANGUAGE_ENGLISH: ("English",),
}

# ---- URL parsing ----
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_youtube_video_id(url: str) -> str:
    """Extract the 11-character YouTube video ID from any YouTube URL form.

    Returns an empty string if the URL is not a recognised YouTube video URL.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return ""
    path = parsed.path or ""
    query = parsed.query or ""

    if host == "youtu.be":
        m = re.match(r"^/([A-Za-z0-9_-]{11})(?:[/?].*)?$", path)
        return m.group(1) if m else ""

    m = re.match(r"^/shorts/([A-Za-z0-9_-]{11})(?:[/?].*)?$", path)
    if m:
        return m.group(1)

    if path.rstrip("/") == "/watch":
        m = re.search(r"(?:^|&)v=([A-Za-z0-9_-]{11})(?:&|$)", query)
        return m.group(1) if m else ""

    m = re.match(r"^/(?:embed|v|vi|live)/([A-Za-z0-9_-]{11})(?:[/?].*)?$", path)
    if m:
        return m.group(1)

    return ""


def is_youtube_url(url: str) -> bool:
    """Return True if *url* points to youtube.com / youtu.be (any path)."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in YOUTUBE_HOSTS


def is_youtube_shorts_url(url: str) -> bool:
    """Return True for ``youtube.com/shorts/{id}`` URLs."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if host not in YOUTUBE_HOSTS:
        return False
    return bool(re.match(r"^/shorts/", parsed.path or "", re.IGNORECASE))


# ---- exceptions ----
class YouTubeSubtitleError(Exception):
    """Generic subtitle fetch failure (network, downsub, parse, ...)."""


class YouTubeSubtitleNotFound(YouTubeSubtitleError):
    """No subtitle available for the requested language."""


# ---- cache ----
# Keyed by (video_id, language) → (created_at, srt_bytes)
_CACHE: "OrderedDict[tuple[str, str], tuple[float, bytes]]" = OrderedDict()
CACHE_TTL_SECONDS = 15 * 60
CACHE_MAX_ITEMS = 256


def _cache_get(key: tuple[str, str]) -> bytes:
    cached = _CACHE.get(key)
    if cached is None:
        return b""
    created, payload = cached
    if time.monotonic() - created >= CACHE_TTL_SECONDS:
        _CACHE.pop(key, None)
        return b""
    _CACHE.move_to_end(key)
    return payload


def _cache_put(key: tuple[str, str], payload: bytes) -> None:
    _CACHE[key] = (time.monotonic(), payload)
    _CACHE.move_to_end(key)
    while len(_CACHE) > CACHE_MAX_ITEMS:
        _CACHE.popitem(last=False)


# ---- CryptoJS-compatible AES helpers ----
# downsub.com uses CryptoJS.AES.encrypt with a string passphrase. CryptoJS
# derives the actual key+IV via OpenSSL's EVP_BytesToKey (MD5, 1 iteration,
# 8-byte random salt). The output is JSON: ``{"ct": base64(ct), "iv": hex(iv),
# "s": hex(salt)}``. The outer transport encoding is base64url (Bt() in JS).
_DOWNSUB_KEY = b"zthxw34cdp6wfyxmpad38v52t3hsz6c5"
_DOWNSUB_API = "https://get.downsub.com/"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def _evp_bytes_to_key(
    password: bytes, salt: bytes, key_len: int = 32, iv_len: int = 16
) -> tuple[bytes, bytes]:
    """Replicate CryptoJS's OpenSSLKdf = EVP_BytesToKey(MD5, 1 iteration)."""
    d = b""
    prev = b""
    while len(d) < key_len + iv_len:
        prev = hashlib.md5(prev + password + salt).digest()
        d += prev
    return d[:key_len], d[key_len : key_len + iv_len]


def _crypto_js_encrypt(plaintext: str, key: bytes) -> str:
    """AES-256-CBC encrypt with random salt; return CryptoJS JSON format."""
    salt = os.urandom(8)
    derived_key, iv = _evp_bytes_to_key(key, salt)
    cipher = AES.new(derived_key, AES.MODE_CBC, iv)
    ciphertext = cipher.encrypt(pad(plaintext.encode("utf-8"), 16))
    return json.dumps(
        {
            "ct": base64.b64encode(ciphertext).decode("ascii"),
            "iv": iv.hex(),
            "s": salt.hex(),
        },
        separators=(",", ":"),
    )


def _crypto_js_decrypt(encrypted_json_str: str, key: bytes) -> str:
    """Decrypt a CryptoJS JSON envelope ``{"ct","iv","s"}`` produced with *key*."""
    obj = json.loads(encrypted_json_str)
    salt = bytes.fromhex(obj["s"])
    iv = bytes.fromhex(obj["iv"])
    derived_key, _ = _evp_bytes_to_key(key, salt)
    cipher = AES.new(derived_key, AES.MODE_CBC, iv)
    plaintext = unpad(cipher.decrypt(base64.b64decode(obj["ct"])), 16)
    return plaintext.decode("utf-8")


def _b64url_encode(s: str) -> str:
    """Replicate CryptoJS Bt(): base64url without padding."""
    encoded = base64.b64encode(s.encode("utf-8")).decode("ascii")
    return encoded.replace("+", "-").replace("/", "_").rstrip("=")


def _b64url_decode(s: str) -> str:
    """Inverse of :func:`_b64url_encode`."""
    padded = s.replace("-", "+").replace("_", "/")
    padded += "=" * ((4 - len(padded) % 4) % 4)
    return base64.b64decode(padded).decode("utf-8")


def _downsub_encode(data: str, key: bytes | None = None) -> str:
    """Replicate downsub's ``$encode``: encrypt + base64url."""
    if key is None:
        key = _DOWNSUB_KEY
    json_str = json.dumps(data, separators=(",", ":"))
    encrypted = _crypto_js_encrypt(json_str, key)
    return _b64url_encode(encrypted)


def _build_downsub_payload(raw_url: str) -> dict[str, str]:
    """Build the JSON body for the get.downsub.com POST request."""
    url_encrypt = _downsub_encode(raw_url)  # inner: default key
    data = _downsub_encode(url_encrypt, raw_url.encode("utf-8"))  # outer: url as key
    return {"url": raw_url, "data": data}


def _decode_downsub_url(encrypted_url: str) -> str:
    """Decrypt a subtitle URL returned by downsub.com (kept for debugging).

    Returns the raw youtube.com/api/timedtext URL. Note: fetching this URL
    directly from a cloud IP triggers YouTube's bot detection; use
    :func:`_fetch_srt_bytes_via_downsub` instead, which routes through
    downsub.com's subtitle proxy.
    """
    decoded = _b64url_decode(encrypted_url)
    plaintext = _crypto_js_decrypt(decoded, _DOWNSUB_KEY)
    try:
        return json.loads(plaintext)
    except (json.JSONDecodeError, TypeError):
        return plaintext.strip('"')


# ---- downsub client ----
def _downsub_fetch_subtitle_list(
    youtube_url: str,
    *,
    proxy_url: str | None,
    timeout: float = 25.0,
) -> dict[str, Any]:
    """Call downsub.com and return the raw JSON response dict."""
    payload = _build_downsub_payload(youtube_url)
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Content-Type": "application/json",
        "Origin": "https://downsub.com",
        "Referer": "https://downsub.com/",
    }
    with httpx.Client(
        http2=True,
        timeout=timeout,
        headers=headers,
        proxy=proxy_url,
    ) as client:
        r = client.post(_DOWNSUB_API, json=payload)
        if r.status_code != 200:
            raise YouTubeSubtitleError(
                f"downsub.com returned HTTP {r.status_code}"
            )
        try:
            body = r.json()
        except Exception as exc:
            raise YouTubeSubtitleError(
                f"downsub.com returned non-JSON response: {exc}"
            ) from exc
    if not isinstance(body, dict):
        raise YouTubeSubtitleError("downsub.com returned unexpected response shape")
    state = body.get("state")
    # state == 2 means "OK with subtitles"; state == 3 means error.
    if state == 3:
        raise YouTubeSubtitleError(
            f"downsub.com reported error state for this URL"
        )
    return body


def _select_subtitle_entry(
    body: dict[str, Any], language: str
) -> dict[str, Any] | None:
    """Pick the best subtitle entry for *language* from the downsub response.

    Preference order:
      1. Manual subtitle matching the language (e.g. "Persian", "English")
      2. Auto-generated (English only) or auto-translated matching the language
    """
    patterns = _DOWNSUB_NAME_PATTERNS.get(language, (language,))
    manual = body.get("subtitles") or []
    auto = body.get("subtitlesAutoTrans") or []

    # 1. Manual subs
    for entry in manual:
        name = (entry.get("name") or "").strip().lower()
        if any(p.lower() in name for p in patterns):
            # Exclude "(auto-generated)" entries from the manual pass
            if "auto" in name:
                continue
            return entry

    # 2. Auto-generated English (only English has auto-generated on YouTube)
    if language == LANGUAGE_ENGLISH:
        for entry in manual:
            name = (entry.get("name") or "").strip().lower()
            if "english" in name and "auto" in name:
                return entry

    # 3. Auto-translated (subtitlesAutoTrans)
    for entry in auto:
        name = (entry.get("name") or "").strip().lower()
        if any(p.lower() in name for p in patterns):
            return entry

    return None


def _fetch_srt_bytes_via_downsub(encrypted_url: str, *, proxy_url: str | None) -> bytes:
    """Download SRT via downsub.com's subtitle proxy.

    downsub.com exposes ``https://subtitle.downsub.com/srt/{enc_url}/`` which
    proxies the actual YouTube timedtext download. This is critical because
    fetching timedtext URLs directly from a cloud IP triggers YouTube's
    bot-detection rate limiting (HTTP 429), even when the URL is signed.
    Routing through downsub's backend avoids the rate limit.
    """
    download_url = f"https://subtitle.downsub.com/srt/{encrypted_url}/"
    headers = {
        "User-Agent": _BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://downsub.com/",
        "Origin": "https://downsub.com",
    }
    with httpx.Client(
        http2=True,
        timeout=25.0,
        headers=headers,
        proxy=proxy_url,
    ) as client:
        r = client.get(download_url)
        if r.status_code == 500:
            # downsub returns 500 when YouTube itself can't serve the subtitle
            # (e.g. auto-gen for a language YouTube doesn't auto-translate to).
            raise YouTubeSubtitleNotFound(
                "Subtitle is not available for this language on this video"
            )
        if r.status_code != 200:
            raise YouTubeSubtitleError(
                f"downsub subtitle proxy returned HTTP {r.status_code}"
            )
        content = r.content
    if not content or len(content) < 20:
        raise YouTubeSubtitleNotFound("Subtitle response was empty")
    # Sanity check: real SRT contains a timestamp like "00:00:" or "-->"
    if b"-->" not in content and b"00:0" not in content:
        raise YouTubeSubtitleNotFound(
            "Subtitle response did not look like an SRT file"
        )
    return content


# ---- async entry point ----
async def fetch_youtube_subtitle(
    youtube_url: str,
    language: str,
    *,
    proxy_url: str | None = None,
    timeout: float = 60.0,
) -> bytes:
    """Asynchronously fetch the SRT subtitle for *youtube_url* in *language*.

    Returns the raw SRT file content as bytes. Raises
    :class:`YouTubeSubtitleNotFound` when no subtitle is available for the
    requested language, or :class:`YouTubeSubtitleError` for any other
    failure (network, downsub crash, invalid URL, ...).

    Results are cached for 15 minutes per (video_id, language) tuple.
    """
    if language not in SUPPORTED_LANGUAGES:
        raise YouTubeSubtitleError(f"Unsupported language: {language!r}")

    video_id = extract_youtube_video_id(youtube_url)
    if not video_id:
        raise YouTubeSubtitleError(
            f"Could not extract video ID from {youtube_url!r}"
        )

    cache_key = (video_id, language)
    cached = _cache_get(cache_key)
    if cached:
        return cached

    try:
        body, entry = await asyncio.wait_for(
            asyncio.to_thread(_downsub_resolve_entry, youtube_url, language, proxy_url),
            timeout=timeout,
        )
    except asyncio.TimeoutError as exc:
        raise YouTubeSubtitleError(
            "Subtitle fetch timed out. downsub.com may be slow or unreachable."
        ) from exc
    except YouTubeSubtitleError:
        raise
    except Exception as exc:
        raise YouTubeSubtitleError(f"Unexpected subtitle fetch failure: {exc}") from exc

    if entry is None:
        raise YouTubeSubtitleNotFound(
            f"No {LANGUAGE_LABELS[language]} subtitle available for this video."
        )

    encrypted_url = entry.get("url")
    if not encrypted_url:
        raise YouTubeSubtitleError("downsub.com returned a subtitle without a URL")

    # Fetch the SRT via downsub.com's subtitle proxy. We deliberately do NOT
    # decrypt the URL and fetch timedtext directly — YouTube's bot detection
    # rate-limits direct timedtext access from cloud IPs (HTTP 429). Routing
    # through ``subtitle.downsub.com`` sidesteps that limit entirely.
    try:
        srt_bytes = await asyncio.wait_for(
            asyncio.to_thread(_fetch_srt_bytes_via_downsub, encrypted_url, proxy_url=proxy_url),
            timeout=30.0,
        )
    except YouTubeSubtitleError:
        raise
    except Exception as exc:
        raise YouTubeSubtitleError(f"SRT download failed: {exc}") from exc

    _cache_put(cache_key, srt_bytes)
    return srt_bytes


def _downsub_resolve_entry(
    youtube_url: str, language: str, proxy_url: str | None
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Synchronous helper: call downsub.com and pick the best subtitle entry."""
    body = _downsub_fetch_subtitle_list(youtube_url, proxy_url=proxy_url)
    entry = _select_subtitle_entry(body, language)
    return body, entry
