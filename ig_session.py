"""Helper to build an instagrapi.Client with robust TLS handling.

WHY THIS EXISTS
---------------
Instagram silently drops TLS handshakes whose fingerprint does not match
the official Instagram app. This manifests as:

    SSLError(SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]
        EOF occurred in violation of protocol (_ssl.c:1032)'))

There are two common triggers:

1. urllib3 v2.x changed the default TLS handshake parameters. With
   urllib3 >= 2 the SSL fingerprint of `requests` no longer matches what
   Instagram expects, so the server closes the connection mid-handshake.

2. Networks that throttle/block Instagram (e.g. Iran) often manifest
   the same EOF behaviour — the connection is killed by an upstream
   middlebox, not by Instagram itself.

WORKAROUND
----------
instagrapi uses TWO separate `requests.Session` instances internally:
    * `self.public`  — for public instagram.com endpoints
    * `self.private` — for i.instagram.com API endpoints (login, DMs, etc.)

Both must be replaced with `curl_cffi.requests.Session` instances
impersonating Chrome. We also configure instagrapi's built-in
`public_transport="curl"` mechanism (which mounts a CurlCffiAdapter
on `self.public`) as a secondary safety net.

If `curl_cffi` is not installed, we fall back to plain instagrapi
(which uses `requests`/`urllib3`). In that case you can usually fix
the SSL error by downgrading urllib3:

    pip install "urllib3<2"

If you are in a country that blocks Instagram (Iran, China, Russia),
you ALSO need to set IG_BRIDGE_PROXY to a working proxy/VPN, otherwise
no TLS impersonation will help — the middlebox kills the connection
before the handshake completes.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# The impersonate target we use. chrome120 is widely supported by
# curl_cffi >= 0.7 and is recent enough that Instagram treats it as a
# legitimate client. Update this if curl_cffi deprecates it.
_DEFAULT_IMPERSONATE = "chrome120"


def _build_curl_session(proxy: Optional[str], impersonate: str) -> Any:
    """Build a curl_cffi.requests.Session with proxy + impersonate."""
    from curl_cffi import requests as curl_requests

    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}

    try:
        session = curl_requests.Session(impersonate=impersonate, proxies=proxies)
    except TypeError:
        # Older curl_cffi API
        session = curl_requests.Session(proxies=proxies)
        try:
            session.impersonate = impersonate
        except Exception:
            pass

    # Add a no-op `mount()` method so instagrapi's HTTPAdapter configuration
    # doesn't crash. curl_cffi sessions don't use urllib3 adapters — they
    # use libcurl directly — so mounting an adapter has no effect anyway.
    if not hasattr(session, "mount"):
        session.mount = lambda prefix, adapter: None  # noqa: E731

    # Also add `close()` if missing (instagrapi may call it on shutdown)
    if not hasattr(session, "close"):
        session.close = lambda: None  # noqa: E731

    return session


def _copy_headers_and_cookies(src_session: Any, dst_session: Any) -> None:
    """Copy headers and cookies from src to dst session (best-effort)."""
    try:
        # Headers
        try:
            for k, v in src_session.headers.items():
                dst_session.headers[k] = v
        except Exception:
            pass
        # Cookies
        try:
            cookies = src_session.cookies
            # curl_cffi.Cookies supports .update() and iteration
            try:
                dst_session.cookies.update(cookies)
            except Exception:
                # Fall back to dict-style copy
                for cookie in cookies:
                    try:
                        dst_session.cookies.set(
                            cookie.name, cookie.value, domain=cookie.domain
                        )
                    except Exception:
                        pass
        except Exception:
            pass
    except Exception:
        pass


def build_client(
    proxy: Optional[str] = None,
    impersonate: Optional[str] = None,
) -> Any:
    """Build an instagrapi.Client with the best available TLS handling.

    Args:
        proxy: Optional proxy URL (http/https/socks5). STRONGLY RECOMMENDED
            if you are in a region that blocks Instagram.
        impersonate: curl_cffi impersonate target. Defaults to chrome120.

    Returns:
        A configured instagrapi.Client (not yet logged in). The caller
        is responsible for calling `.login(...)` or `.load_settings(...)`.
    """
    from instagrapi import Client

    impersonate = impersonate or _DEFAULT_IMPERSONATE

    # Check curl_cffi availability FIRST. If not available, there's nothing
    # we can do — return a plain client and let the caller see the SSL error.
    try:
        from curl_cffi import requests as curl_requests  # noqa: F401
    except ImportError:
        logger.warning(
            "curl_cffi is NOT installed. Instagram will very likely reject "
            "your TLS handshake with SSLEOFError. Fix: pip install curl_cffi  "
            '— or downgrade urllib3: pip install "urllib3<2". If you are in '
            "Iran/China/Russia you ALSO need IG_BRIDGE_PROXY set to a working proxy."
        )
        cl = Client(proxy=proxy if proxy else None)
        return cl

    # Build a plain Client first (without instagrapi's built-in curl transport,
    # because that requires the optional `curl_adapter` package which is a
    # separate dependency). We will replace self.public and self.private
    # manually with full curl_cffi sessions below.
    try:
        cl = Client(proxy=proxy if proxy else None)
        logger.info(
            "IG session: building client with curl_cffi sessions "
            "(impersonate=%s, proxy=%s).",
            impersonate,
            "yes" if proxy else "no",
        )
    except Exception as exc:
        logger.warning(
            "Failed to build instagrapi.Client (%s); aborting curl_cffi setup.",
            exc,
        )
        return cl

    # Replace `self.private` with a curl_cffi session. This is the one used
    # for i.instagram.com API endpoints (login, DMs, etc.) — the host that
    # fails with SSLEOFError.
    try:
        old_private = cl.private
        new_private = _build_curl_session(proxy, impersonate)
        _copy_headers_and_cookies(old_private, new_private)
        # Keep instagrapi's verify setting
        new_private.verify = getattr(cl, "tls_verify", True)
        cl.private = new_private
        logger.info("IG session: replaced self.private with curl_cffi session.")
    except Exception as exc:
        logger.warning(
            "Failed to replace self.private with curl_cffi session (%s). "
            "Login will likely fail with SSLEOFError.",
            exc,
        )

    # Also replace `self.public` with a curl_cffi session (used for
    # instagram.com public endpoints like user_info_by_username).
    try:
        old_public = cl.public
        new_public = _build_curl_session(proxy, impersonate)
        _copy_headers_and_cookies(old_public, new_public)
        new_public.verify = getattr(cl, "tls_verify", True)
        cl.public = new_public
        logger.info("IG session: replaced self.public with curl_cffi session.")
    except Exception as exc:
        logger.warning(
            "Failed to replace self.public with curl_cffi session (%s). "
            "Public requests may fail with SSLEOFError.",
            exc,
        )

    # Make sure proxy is set on the instagrapi client (for any code paths
    # that read cl.proxy)
    if proxy:
        try:
            cl.set_proxy(proxy)
        except Exception as exc:
            logger.warning("Failed to set cl.set_proxy(%s): %s", proxy, exc)

    # Patch get_settings() so dump_settings() works with curl_cffi cookies.
    # instagrapi's get_settings() does:
    #     "cookies": requests.utils.dict_from_cookiejar(self.private.cookies)
    # but `requests.utils.dict_from_cookiejar` iterates the cookiejar and
    # accesses `.name`/`.value` on each item. `requests.cookies.RequestsCookieJar`
    # yields Cookie objects (with .name/.value), but `curl_cffi.requests.cookies.Cookies`
    # yields plain strings (cookie names) when iterated, causing:
    #     ERROR: 'str' object has no attribute 'name'
    # Fix: use `.jar` (a real http.cookiejar.CookieJar) when available.
    _patch_get_settings_for_curl_cffi(cl)

    return cl


def _patch_get_settings_for_curl_cffi(cl: Any) -> None:
    """Monkey-patch cl.get_settings so dump_settings works with curl_cffi.

    instagrapi's get_settings uses `requests.utils.dict_from_cookiejar`,
    which iterates the cookiejar expecting Cookie objects with .name/.value.
    curl_cffi's Cookies class yields strings when iterated directly, but
    exposes a proper CookieJar via `.jar`. We swap the cookies attribute
    temporarily before calling the original get_settings.
    """
    original_get_settings = cl.get_settings

    def patched_get_settings() -> dict:
        # Temporarily swap curl_cffi Cookies with their underlying CookieJar
        # (which is compatible with requests.utils.dict_from_cookiejar).
        # We must set `._cookies` directly because `Session.cookies` is a
        # property whose setter re-wraps any input into a Cookies instance.
        swapped = []
        for session_attr in ("private", "public"):
            session = getattr(cl, session_attr, None)
            if session is None:
                continue
            cookies = getattr(session, "cookies", None)
            # curl_cffi Cookies have a .jar attribute (http.cookiejar.CookieJar)
            if cookies is not None and hasattr(cookies, "jar"):
                try:
                    # Save the original Cookies object and swap in the raw jar
                    jar = cookies.jar
                    original_cookies = getattr(session, "_cookies", cookies)
                    session._cookies = jar
                    swapped.append((session_attr, original_cookies))
                except Exception:
                    pass

        try:
            settings = original_get_settings()
        finally:
            # Restore original Cookies objects
            for session_attr, original_cookies in swapped:
                session = getattr(cl, session_attr, None)
                if session is not None:
                    try:
                        session._cookies = original_cookies
                    except Exception:
                        pass

        return settings

    cl.get_settings = patched_get_settings
    logger.info("IG session: patched get_settings() for curl_cffi cookie compatibility.")


def is_ssl_error(exc: BaseException) -> bool:
    """Return True if `exc` looks like the Instagram TLS-fingerprint error.

    Used to print a targeted troubleshooting hint when login fails.
    """
    msg = str(exc).lower()
    return (
        "ssl" in msg
        or "eof" in msg
        or "unexpected_eof" in msg
        or "ssleoferror" in msg
        or "handshake" in msg
    )


# Track how many times we've printed the hint in this process, to avoid
# spamming output when instagrapi retries internally and the error
# callback fires many times.
_hint_printed_count = 0
_HINT_PRINT_LIMIT = 2


def print_troubleshooting_hint(exc: BaseException) -> None:
    """Print a human-readable hint when login fails with SSL-related errors.

    Idempotent: only prints the hint up to _HINT_PRINT_LIMIT times per
    process. instagrapi retries failed requests internally (3 retries by
    default), so without this guard the hint would be printed many times.
    """
    global _hint_printed_count

    if not is_ssl_error(exc):
        return
    if _hint_printed_count >= _HINT_PRINT_LIMIT:
        return
    _hint_printed_count += 1

    # IMPORTANT: do NOT use implicit string concatenation (adjacent string
    # literals) here. Python's lexer concatenates adjacent literals BEFORE
    # the parser sees the `*` operator, so `"=" * 70 + "\n" "FOO"` becomes
    # `"=" * 70 + ("\nFOO")` (correct), but:
    #     "=" * 70 + "\n" "FOO" "\n" "=" * 70
    # becomes:
    #     "=" * 70 + ("\nFOO\n=") * 70
    # which prints the whole hint 70 times! Always use explicit `+`.
    sep = "=" * 70
    msg = (
        "\n" + sep + "\n" +
        "TROUBLESHOOTING: SSL handshake rejected by Instagram\n" +
        sep + "\n" +
        "This happens because Instagram blocks TLS fingerprints that don't\n" +
        "match the official Instagram app. Three fixes, in priority order:\n\n" +
        "1) Install curl_cffi (spoofs Chrome's TLS fingerprint):\n" +
        "       pip install curl_cffi\n" +
        "   Then re-run this script. The session helper auto-detects it\n" +
        "   and replaces BOTH self.public AND self.private with curl_cffi.\n\n" +
        "2) If you are in Iran/China/Russia or any region that throttles\n" +
        "   Instagram, you MUST use a proxy/VPN:\n" +
        "       export IG_BRIDGE_PROXY=http://user:pass@host:port\n" +
        "       # or socks5://host:port\n" +
        "   Without a working proxy, no TLS impersonation will help — the\n" +
        "   middlebox kills the connection before the handshake completes.\n\n" +
        "3) If curl_cffi can't be installed, downgrade urllib3 as a last resort:\n" +
        '       pip install "urllib3<2"\n\n' +
        "If you've done all three and STILL see this error, your proxy may\n" +
        "be dead. Test with:\n" +
        "    curl -x $IG_BRIDGE_PROXY https://i.instagram.com/api/v1/\n" +
        "If curl also fails, the proxy is the problem, not the code.\n" +
        sep + "\n"
    )
    print(msg, flush=True)
