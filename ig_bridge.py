"""Instagram DM bridge — listens for DMs on an Instagram page account and
forwards Instagram content URLs to the Telegram bot for downloading.

Architecture
------------
- The bot operator runs an Instagram account (the "page") whose DMs this
  module polls via `instagrapi`.
- A Telegram user obtains a 6-character pairing code via the bot's `/ig`
  command and sends it to the page's DM.
- When this module sees a pairing code in a DM, it calls
  `ig_pairings.claim_code(code, ig_user_id, ig_username)` to link the IG
  sender to a Telegram user.
- When a paired IG user sends a message containing an Instagram URL (or
  forwards a reel/post), this module extracts the URL and invokes the
  `on_url` callback, which triggers the existing Telegram-bot download flow.

instagrapi is synchronous, so all IG API calls are run in a thread executor.
The poll loop runs as an asyncio task inside the bot process.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import ig_pairings

logger = logging.getLogger(__name__)

# How often to poll for new DMs (seconds). Instagram rate-limits aggressive
# polling, so keep this >= 5s.
POLL_INTERVAL_SECONDS = 8

# How many threads to fetch per poll cycle.
POLL_THREAD_AMOUNT = 20

# Cap on seen-message-id cache size (FIFO eviction).
SEEN_IDS_CAP = 5000

# Regex matching Instagram content URLs in DM text.
# Matches: instagram.com/reel/<code>, /reels/, /p/<code>, /tv/<code>,
# /stories/<user>/<id>/, and instagr.am short links.
IG_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:instagram\.com|instagr\.am)"
    r"/(reel|reels|p|tv|stories)/[\w\-]+/?",
    re.IGNORECASE,
)

# Regex matching a standalone 6-char pairing code (from our alphabet).
# Must be a separate word so we don't match random substrings.
CODE_RE = re.compile(r"(?<![A-Z2-9])([A-Z2-9]{6})(?![A-Z2-9])")


@dataclass
class IGBridgeConfig:
    """Configuration for the Instagram bridge."""
    enabled: bool = False
    username: str = ""
    password: str = ""
    session_file: str = "ig_session.json"
    proxy: str = ""  # e.g. "http://user:pass@host:port" or "socks5://host:port"

    @property
    def session_path(self) -> Path:
        p = Path(self.session_file)
        return p if p.is_absolute() else Path(__file__).resolve().parent / p


@dataclass
class IGBridgeStats:
    """Runtime stats for observability."""
    started_at: float = 0.0
    poll_cycles: int = 0
    dm_received: int = 0
    pairings_made: int = 0
    pairings_failed: int = 0
    urls_forwarded: int = 0
    last_poll_at: float = 0.0
    last_error: str = ""


class InstagramBridge:
    """Polls an Instagram account's DMs and bridges content to Telegram.

    Lifecycle:
        bridge = InstagramBridge(config, on_url=callback)
        await bridge.start()    # starts the poll loop as a background task
        ...
        await bridge.stop()     # cancels the poll loop
    """

    def __init__(
        self,
        config: IGBridgeConfig,
        on_url: Callable[[int, str, dict], Awaitable[None]],
        on_pairing_success: Optional[Callable[[int, int, str], Awaitable[None]]] = None,
    ) -> None:
        """
        Args:
            config: Instagram account configuration.
            on_url: async callback(tg_user_id, url, extra_info) invoked when a
                paired IG user sends a URL. `extra_info` is a dict with at
                least: ig_username (str), ig_msg_id (str), ig_thread_id (str).
            on_pairing_success: optional async callback(tg_user_id, ig_user_id,
                ig_username) invoked when a pairing code is successfully claimed.
        """
        self.config = config
        self.on_url = on_url
        self.on_pairing_success = on_pairing_success
        self.client: Any = None  # instagrapi.Client
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._seen_msg_ids: set[str] = set()
        self._seen_msg_queue: list[str] = []  # FIFO for eviction
        self._last_thread_seen: dict[str, int] = {}  # thread_id -> last message timestamp
        self.stats = IGBridgeStats()
        self._my_user_id: Optional[int] = None

    async def start(self) -> bool:
        """Connect to Instagram and start the poll loop. Returns True on success."""
        if not self.config.enabled:
            logger.info("IG bridge disabled; not starting.")
            return False
        if not self.config.username:
            logger.error("IG bridge enabled but IG_BRIDGE_USERNAME is empty.")
            return False

        try:
            ok = await self._login()
        except Exception as exc:
            logger.error("IG bridge login failed: %s", exc, exc_info=True)
            self.stats.last_error = f"login: {exc}"
            return False
        if not ok:
            return False

        self.stats.started_at = time.time()
        self._task = asyncio.create_task(self._poll_loop(), name="ig-bridge-poll")
        logger.info(
            "IG bridge started for @%s (my IG user_id=%s); polling every %ss.",
            self.config.username, self._my_user_id, POLL_INTERVAL_SECONDS,
        )
        return True

    async def _login(self) -> bool:
        """Log in to Instagram (load session or fresh login). Runs in executor."""
        import ig_session

        loop = asyncio.get_event_loop()

        def _do_login() -> bool:
            cl = ig_session.build_client(proxy=self.config.proxy or None)

            session_path = self.config.session_path
            logger.info(
                "IG bridge: looking for session file at %s (exists=%s), "
                "username=@%s, proxy=%s",
                session_path,
                session_path.exists(),
                self.config.username,
                self.config.proxy or "(none)",
            )

            if session_path.exists():
                try:
                    cl.load_settings(str(session_path))
                    # Session loaded — verify by getting own user
                    me = cl.user_info_by_username(self.config.username)
                    if me is None:
                        raise RuntimeError("user_info_by_username returned None")
                    logger.info(
                        "IG session loaded from %s (verified, user_id=%s).",
                        session_path, cl.user_id,
                    )
                    self.client = cl
                    self._my_user_id = cl.user_id
                    return True
                except Exception as e:
                    logger.warning(
                        "IG session load failed (%s); falling back to fresh login.", e,
                    )
                    ig_session.print_troubleshooting_hint(e)
                    cl = ig_session.build_client(proxy=self.config.proxy or None)
            else:
                logger.error(
                    "IG session file NOT FOUND at %s. "
                    "You must run ig_login.py on your LOCAL machine (with a proxy "
                    "if in Iran) to generate ig_session.json, then copy that file "
                    "to the server. Fresh login from a data-center IP will be "
                    "rejected by Instagram (HTTP 400).",
                    session_path,
                )

            # Fresh login (will almost certainly fail on a server IP — Instagram
            # rejects logins from data-center IPs as suspicious)
            if not self.config.password:
                logger.error(
                    "No usable IG session at %s and no IG_BRIDGE_PASSWORD set. "
                    "Run ig_login.py locally first to create a session file.",
                    session_path,
                )
                return False
            logger.warning(
                "Attempting fresh Instagram login from this server's IP. "
                "This will likely fail with HTTP 400 if this is a data-center IP "
                "(Railway, Heroku, AWS, etc). The correct approach is: run "
                "ig_login.py locally, then copy ig_session.json to the server."
            )
            try:
                cl.login(self.config.username, self.config.password)
            except Exception as exc:
                # HTTP 400 from accounts/login/ = Instagram rejected the login.
                # Common causes: data-center IP, missing 2FA, wrong password,
                # account temporarily locked, or rate-limited.
                msg = str(exc).lower()
                if "400" in msg or "bad request" in msg:
                    logger.error(
                        "Instagram returned HTTP 400 on login. This typically means:\n"
                        "  1) You are running on a data-center IP (Railway/AWS/etc) — "
                        "Instagram rejects fresh logins from such IPs.\n"
                        "  2) Two-factor authentication is enabled on the account "
                        "(fresh login via the bridge does not handle 2FA).\n"
                        "  3) The password is wrong, OR the account is temporarily "
                        "locked due to suspicious activity.\n\n"
                        "FIX: Run ig_login.py on your LOCAL machine (it handles 2FA "
                        "and challenges interactively), then copy the generated "
                        "ig_session.json to the server. Do NOT attempt fresh login "
                        "on the server."
                    )
                else:
                    ig_session.print_troubleshooting_hint(exc)
                raise
            cl.dump_settings(str(session_path))
            logger.info("IG fresh login OK; session saved to %s", session_path)
            self.client = cl
            self._my_user_id = cl.user_id
            return True

        return await loop.run_in_executor(None, _do_login)

    async def stop(self) -> None:
        """Stop the poll loop and disconnect."""
        self._stop_event.set()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        # Client is sync; nothing to await for disconnect
        logger.info("IG bridge stopped.")

    async def _poll_loop(self) -> None:
        """Background poll loop."""
        while not self._stop_event.is_set():
            try:
                await self._poll_once()
                self.stats.poll_cycles += 1
                self.stats.last_poll_at = time.time()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("IG poll error: %s", exc, exc_info=True)
                self.stats.last_error = f"poll: {exc}"
            # Wait for next cycle (cancellable)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _poll_once(self) -> None:
        """Fetch recent DM threads and process new messages."""
        if not self.client:
            return
        loop = asyncio.get_event_loop()

        def _fetch_threads():
            return self.client.direct_threads(selected_filter="", amount=POLL_THREAD_AMOUNT)

        try:
            threads = await loop.run_in_executor(None, _fetch_threads)
        except Exception as exc:
            logger.warning("direct_threads failed: %s", exc)
            self.stats.last_error = f"direct_threads: {exc}"
            return

        # Periodic cleanup
        if self.stats.poll_cycles % 50 == 0:
            ig_pairings.cleanup_expired()
            self._evict_seen_ids()

        for thread in threads:
            try:
                await self._process_thread(thread)
            except Exception as exc:
                logger.warning("Thread %s processing error: %s", getattr(thread, "id", "?"), exc)

    def _evict_seen_ids(self) -> None:
        """Keep _seen_msg_ids under SEEN_IDS_CAP."""
        while len(self._seen_msg_ids) > SEEN_IDS_CAP and self._seen_msg_queue:
            old = self._seen_msg_queue.pop(0)
            self._seen_msg_ids.discard(old)

    async def _process_thread(self, thread: Any) -> None:
        """Process new messages in a single DM thread."""
        thread_id = str(getattr(thread, "id", ""))
        messages = list(getattr(thread, "messages", []) or [])
        if not messages:
            return

        # Sort by timestamp ascending so we process in order
        def _msg_ts(m: Any) -> int:
            ts = getattr(m, "timestamp", None)
            if ts is None:
                return 0
            try:
                return int(ts)
            except (TypeError, ValueError):
                return 0

        messages.sort(key=_msg_ts)

        last_seen = self._last_thread_seen.get(thread_id, 0)
        new_msgs = [m for m in messages if _msg_ts(m) > last_seen]
        if not new_msgs:
            return
        self._last_thread_seen[thread_id] = _msg_ts(new_msgs[-1])

        for msg in new_msgs:
            # Skip our own messages
            try:
                sender_id = int(getattr(msg, "user_id", 0))
            except (TypeError, ValueError):
                sender_id = 0
            if sender_id == 0 or sender_id == self._my_user_id:
                continue

            msg_id = str(getattr(msg, "id", ""))
            if msg_id and msg_id in self._seen_msg_ids:
                continue
            if msg_id:
                self._seen_msg_ids.add(msg_id)
                self._seen_msg_queue.append(msg_id)

            self.stats.dm_received += 1
            try:
                await self._handle_message(msg, thread)
            except Exception as exc:
                logger.exception("Error handling IG message %s: %s", msg_id, exc)

    async def _handle_message(self, msg: Any, thread: Any) -> None:
        """Handle a single DM message: pairing code, URL, or forwarded media."""
        thread_id = str(getattr(thread, "id", ""))
        sender_ig_id = int(getattr(msg, "user_id", 0))

        # Look up sender's IG username (best-effort)
        ig_username = ""
        try:
            loop = asyncio.get_event_loop()
            user_info = await loop.run_in_executor(
                None, lambda: self.client.user_info_by_id(sender_ig_id)
            )
            ig_username = getattr(user_info, "username", "") or ""
        except Exception:
            pass

        text = getattr(msg, "text", "") or ""

        # 1) Pairing code?
        code_match = CODE_RE.search(text)
        if code_match:
            code = code_match.group(1)
            tg_user_id = ig_pairings.claim_code(code, sender_ig_id, ig_username)
            if tg_user_id is not None:
                logger.info(
                    "IG pairing success: code=%s ig_user=%s(%s) -> tg_user=%s",
                    code, sender_ig_id, ig_username, tg_user_id,
                )
                self.stats.pairings_made += 1
                await self._send_dm(
                    thread_id,
                    "✅ Pairing successful! Your Instagram is now linked to your Telegram bot.\n\n"
                    "Now send me any Instagram reel, post, or URL — I'll download it and send it to you in Telegram.",
                )
                if self.on_pairing_success is not None:
                    try:
                        await self.on_pairing_success(tg_user_id, sender_ig_id, ig_username)
                    except Exception as exc:
                        logger.warning("on_pairing_success callback error: %s", exc)
            else:
                self.stats.pairings_failed += 1
                logger.info(
                    "IG pairing failed: code=%s ig_user=%s(%s) — invalid/expired/already-claimed",
                    code, sender_ig_id, ig_username,
                )
                await self._send_dm(
                    thread_id,
                    "❌ That pairing code is invalid, expired, or already used.\n"
                    "Get a fresh code from the Telegram bot with /ig and try again.",
                )
            return

        # 2) Already paired?
        tg_user_id = ig_pairings.get_tg_user_id(sender_ig_id)
        if tg_user_id is None:
            # Greet unpaired users and prompt them to pair
            await self._send_dm(
                thread_id,
                "👋 Hi! I'm a bridge to the MZ Downloader Telegram bot.\n\n"
                "To use me, get a pairing code from the bot with the /ig command, "
                "then send that 6-character code to me here in DM.",
            )
            return

        # 3) URL in text?
        url_match = IG_URL_RE.search(text)
        if url_match:
            url = url_match.group(0)
            await self._forward_url(tg_user_id, url, ig_username, msg, thread_id)
            return

        # 4) Forwarded reel/post? (instagrapi exposes `media_share` on the message)
        media_share = getattr(msg, "media_share", None)
        if media_share is not None:
            url = self._extract_url_from_media_share(media_share)
            if url:
                await self._forward_url(tg_user_id, url, ig_username, msg, thread_id)
                return

        # 5) Forwarded story? (instagrapi exposes `story_share` or `reel_share`)
        for attr in ("story_share", "reel_share"):
            share = getattr(msg, attr, None)
            if share is not None:
                # Stories don't have a public URL; tell the user to send a reel/post URL instead.
                await self._send_dm(
                    thread_id,
                    "⚠️ Stories can't be downloaded through this bridge. "
                    "Please send me a reel or post URL instead "
                    "(e.g. https://www.instagram.com/reel/ABC123/).",
                )
                return

        # 6) Couldn't understand the message
        await self._send_dm(
            thread_id,
            "❓ I couldn't find an Instagram link in your message. "
            "Send me a reel, post, or URL like:\n"
            "https://www.instagram.com/reel/CxYz123/\n"
            "https://www.instagram.com/p/AbCdEf/",
        )

    def _extract_url_from_media_share(self, media: Any) -> Optional[str]:
        """Build an Instagram URL from a forwarded Media object."""
        pk = getattr(media, "pk", None)
        code = getattr(media, "code", None) or getattr(media, "shortcode", None)
        media_type = getattr(media, "media_type", 0)
        # 1 = photo, 2 = video, 8 = album, 14 = reel (IGTV-like)

        # If we have a code, build a clean URL
        if code:
            if media_type == 2:
                return f"https://www.instagram.com/reel/{code}/"
            return f"https://www.instagram.com/p/{code}/"

        # Fall back to PK-based URL (works for some endpoints)
        if pk:
            if media_type == 2:
                return f"https://www.instagram.com/reel/{pk}/"
            return f"https://www.instagram.com/p/{pk}/"

        return None

    async def _forward_url(
        self,
        tg_user_id: int,
        url: str,
        ig_username: str,
        msg: Any,
        thread_id: str,
    ) -> None:
        """Send URL to the Telegram bot via the on_url callback and acknowledge in DM."""
        logger.info(
            "IG bridge forwarding URL %s from @%s (ig_user_id=%s) -> tg_user=%s",
            url, ig_username, getattr(msg, "user_id", "?"), tg_user_id,
        )
        self.stats.urls_forwarded += 1

        # Acknowledge in IG DM
        await self._send_dm(
            thread_id,
            f"📥 Got it! Downloading:\n{url}\n\nYou'll receive it in Telegram shortly.",
        )

        # Trigger the Telegram side
        try:
            await self.on_url(
                tg_user_id,
                url,
                {
                    "ig_username": ig_username,
                    "ig_msg_id": str(getattr(msg, "id", "")),
                    "ig_thread_id": thread_id,
                },
            )
        except Exception as exc:
            logger.exception("on_url callback failed for url=%s: %s", url, exc)
            await self._send_dm(
                thread_id,
                "⚠️ Something went wrong triggering the Telegram bot. Please try again later.",
            )

    async def _send_dm(self, thread_id: str, text: str) -> None:
        """Send a DM to a thread (best-effort, never raises)."""
        if not self.client or not thread_id:
            return
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self.client.direct_send(text=text, thread_ids=[thread_id]),
            )
        except Exception as exc:
            logger.warning("IG direct_send to thread %s failed: %s", thread_id, exc)
