"""فالو‌بک خودکار + پیام خوش‌آمد دایرکت برای فالوورهای جدید پیج.

طراحی و نکات مهم:
- از همان کلاینت و سشن «پل دایرکت» (ig_dm) استفاده می‌کند؛ هیچ لاگین دومی
  انجام نمی‌شود (بدون Conflict و بدون دستگاه/سشن جدا).
- هر IG_FOLLOW_POLL_SECONDS ثانیه، فالوورهای جدید با ترتیب
  date_followed_latest (جدیدترین اول) بررسی می‌شوند.
- **قانون baseline**: اولین اجرا فقط فهرست فالوورهای فعلی را ثبت می‌کند و
  هیچ‌کس را فالو‌بک نمی‌کند؛ فقط فالوورهای «بعد از آن لحظه» فالو‌بک و
  پیام خوش‌آمد می‌گیرند (تا هرگز فالوورهای قدیمی انبوه فالو نشوند).
- سقف ساعتی/روزانه + فاصلهٔ بین هر فالو (محافظت در برابر محدودیت اینستاگرام).
- وضعیت در ig_follow_state.json ذخیره می‌شود؛ با IG_FOLLOW_STATE_B64 می‌توان
  بین دپلوی‌ها منتقلش کرد (دستور /igfollowstate در تلگرام برای خروجی base64).
- هیچ خطایی نباید به ربات اصلی یا پل دایرکت سرایت کند.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("MZDownloader.ig_follow")

PROJECT_DIR = Path(__file__).resolve().parent
IG_FOLLOW_STATE_FILE = Path(
    os.getenv("IG_FOLLOW_STATE_FILE", str(PROJECT_DIR / "ig_follow_state.json"))
)

# ── پیکربندی از متغیرهای محیطی ──


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(str(os.getenv(name, "") or default).strip() or default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(str(os.getenv(name, "") or default).strip() or default))
    except (TypeError, ValueError):
        return default


FOLLOW_ENABLED = os.getenv("IG_FOLLOWBACK_ENABLED", "1").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}
FOLLOW_POLL_SECONDS = _env_int("IG_FOLLOW_POLL_SECONDS", 90, minimum=30)
FOLLOWER_SAMPLE = _env_int("IG_FOLLOWER_SAMPLE", 200, minimum=20)
FOLLOW_COOLDOWN = _env_float("IG_FOLLOWBACK_COOLDOWN", 5.0, minimum=1.0)
HOURLY_CAP = _env_int("IG_FOLLOWBACK_HOURLY_CAP", 40, minimum=1)
DAILY_CAP = _env_int("IG_FOLLOWBACK_DAILY_CAP", 150, minimum=1)
MAX_KNOWN = 5000   # سقف حافظهٔ فالوورهای شناخته‌شده (حذف قدیمی‌ها)
MAX_WELCOMED = 3000
DM_MAX_TRIES = 3

DEFAULT_DM_TEXT = (
    "سلام 👋 فالو‌ات کردم! ✅\n\n"
    "این پیج ربات دانلوده — هر ریلز/پستی که همین‌جا Share کنی یا لینکش رو "
    "بفرستی، فایلش رو همین‌جا برات می‌فرستم 📥"
)


def _dm_text() -> str:
    return (os.getenv("IG_FOLLOWBACK_DM_TEXT", "") or "").strip() or DEFAULT_DM_TEXT


# ── وضعیت ماندگار ──


class FollowState:
    """فالوورهای شناخته‌شده + سقف‌ها + صف پیام‌های در انتظار (JSON محلی)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.known: dict[str, dict[str, Any]] = {}
        self.welcomed: list[str] = []
        self.dm_failed: list[str] = []
        self.pending_dm: list[dict[str, Any]] = []
        self.baseline_done = False
        self.hourly: dict[str, Any] = {"window": 0.0, "count": 0}
        self.daily: dict[str, Any] = {"day": "", "count": 0}
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self.known = data.get("known", {}) or {}
                self.welcomed = data.get("welcomed", []) or []
                self.dm_failed = data.get("dm_failed", []) or []
                self.pending_dm = data.get("pending_dm", []) or []
                self.baseline_done = bool(data.get("baseline_done", False))
                self.hourly = data.get("hourly", self.hourly) or self.hourly
                self.daily = data.get("daily", self.daily) or self.daily
        except Exception as exc:  # noqa: BLE001
            logger.warning("ig-follow: could not load %s: %s", self._path.name, exc)

    def save(self) -> None:
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(
                    {
                        "known": self.known,
                        "welcomed": self.welcomed[-MAX_WELCOMED:],
                        "dm_failed": self.dm_failed[-MAX_WELCOMED:],
                        "pending_dm": self.pending_dm[-500:],
                        "baseline_done": self.baseline_done,
                        "hourly": self.hourly,
                        "daily": self.daily,
                    },
                    ensure_ascii=False,
                    indent=1,
                ),
                encoding="utf-8",
            )
            tmp.replace(self._path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ig-follow: could not save %s: %s", self._path.name, exc)

    # ── helpers ──

    def remember(self, pk: str, username: str) -> None:
        self.known[str(pk)] = {"u": str(username or ""), "ts": int(time.time())}
        if len(self.known) > MAX_KNOWN:
            for old_pk, _ in sorted(
                self.known.items(), key=lambda kv: kv[1].get("ts", 0)
            )[: len(self.known) - MAX_KNOWN]:
                self.known.pop(old_pk, None)

    def remembered(self, pk: str) -> bool:
        return str(pk) in self.known

    def is_welcomed(self, pk: str) -> bool:
        pk = str(pk)
        return pk in self.welcomed or pk in self.dm_failed

    def mark_welcomed(self, pk: str) -> None:
        pk = str(pk)
        if pk not in self.welcomed:
            self.welcomed.append(pk)
        self.pending_dm = [p for p in self.pending_dm if str(p.get("pk")) != pk]

    def mark_dm_failed(self, pk: str) -> None:
        pk = str(pk)
        if pk not in self.dm_failed:
            self.dm_failed.append(pk)
        self.pending_dm = [p for p in self.pending_dm if str(p.get("pk")) != pk]

    def queue_dm(self, pk: str, username: str) -> None:
        if any(str(p.get("pk")) == str(pk) for p in self.pending_dm):
            return
        self.pending_dm.append(
            {"pk": str(pk), "u": str(username or ""), "tries": 0}
        )

    def allow_by_caps(self) -> bool:
        """سقف ساعتی/روزانه — شمارنده‌ها را هم به‌روز می‌کند (بدون مصرف)."""
        now = time.time()
        if now - float(self.hourly.get("window", 0.0)) >= 3600:
            self.hourly = {"window": now, "count": 0}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if str(self.daily.get("day", "")) != today:
            self.daily = {"day": today, "count": 0}
        return (
            int(self.hourly.get("count", 0)) < HOURLY_CAP
            and int(self.daily.get("count", 0)) < DAILY_CAP
        )

    def consume_follow(self) -> None:
        now = time.time()
        if now - float(self.hourly.get("window", 0.0)) >= 3600:
            self.hourly = {"window": now, "count": 0}
        self.hourly["count"] = int(self.hourly.get("count", 0)) + 1
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if str(self.daily.get("day", "")) != today:
            self.daily = {"day": today, "count": 0}
        self.daily["count"] = int(self.daily.get("count", 0)) + 1


_STATE: FollowState | None = None


def _state() -> FollowState:
    global _STATE
    if _STATE is None:
        _seed_state_from_env()
        _STATE = FollowState(IG_FOLLOW_STATE_FILE)
    return _STATE


def _seed_state_from_env() -> bool:
    """اگر IG_FOLLOW_STATE_B64 تنظیم شده باشد، فایل وضعیت را بازسازی می‌کند."""
    raw_b64 = (os.getenv("IG_FOLLOW_STATE_B64", "") or "").strip()
    if not raw_b64:
        return False
    try:
        raw = base64.b64decode(raw_b64)
        json.loads(raw.decode("utf-8"))  # فقط اعتبارسنجی
        if IG_FOLLOW_STATE_FILE.exists():
            return False  # فایل تازه‌تر از env برنده است
        IG_FOLLOW_STATE_FILE.write_bytes(raw)
        logger.info(
            "ig-follow: state seeded from IG_FOLLOW_STATE_B64 → %s",
            IG_FOLLOW_STATE_FILE.name,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("ig-follow: IG_FOLLOW_STATE_B64 invalid: %s", exc)
        return False


def follow_state_b64() -> str | None:
    """محتوای فایل وضعیت به‌صورت base64 (برای انتقال بین دپلوی‌ها)."""
    try:
        if not IG_FOLLOW_STATE_FILE.exists():
            return None
        raw = IG_FOLLOW_STATE_FILE.read_bytes()
        if not raw.strip():
            return None
        json.loads(raw.decode("utf-8"))
        return base64.b64encode(raw).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.warning("ig-follow: could not export state: %s", exc)
        return None


def feature_enabled_followback() -> bool:
    return FOLLOW_ENABLED


# ── حلقهٔ پس‌زمینه ──


async def _fetch_newest_followers(bridge: Any, client: Any) -> list[Any]:
    """فالوورهای پیج — جدیدترین اول؛ اگر order پذیرفته نشد بدون order."""
    try:
        return await bridge._run_ig(
            lambda: client.user_followers_v1(
                str(client.user_id), amount=FOLLOWER_SAMPLE, order="date_followed_latest"
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ig-follow: ordered fetch failed (%s) → plain fetch", type(exc).__name__
        )
        return await bridge._run_ig(
            lambda: client.user_followers_v1(str(client.user_id), amount=FOLLOWER_SAMPLE)
        )


async def _send_welcome(bridge: Any, client: Any, pk: str, username: str) -> bool:
    """پیام خوش‌آمد دایرکت — بعد از فالو‌بک."""
    try:
        await bridge._run_ig(
            lambda: client.direct_send(_dm_text(), user_ids=[int(pk)])
        )
        logger.info("ig-follow: welcome DM sent to @%s (%s)", username or "?", pk)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ig-follow: welcome DM failed for @%s (%s): %s",
            username or "?",
            pk,
            type(exc).__name__,
        )
        return False


async def _process_new_followers(
    bridge: Any, client: Any, st: FollowState, followers: list[Any]
) -> None:
    """تفاوت فالوورها → فالو‌بک + پیام خوش‌آمد با سقف و فاصلهٔ امن."""
    ig_exc = getattr(bridge, "_ig_exc", None)
    throttled_type = getattr(ig_exc, "ClientThrottledError", ())
    ratelimit_type = getattr(ig_exc, "RateLimitError", ())

    new_items: list[tuple[str, str]] = []
    for user in followers:
        pk = str(getattr(user, "pk", "") or "")
        if not pk or pk == str(getattr(client, "user_id", "")):
            continue
        username = getattr(user, "username", "") or ""
        if not st.remembered(pk):
            new_items.append((pk, username))

    if not st.baseline_done:
        for pk, username in new_items:
            st.remember(pk, username)
        st.baseline_done = True
        st.save()
        logger.info(
            "ig-follow: baseline recorded (%s followers) — فقط از این به بعد فالو‌بک می‌شود",
            len(new_items),
        )
        return

    if not new_items:
        return
    logger.info("ig-follow: %s new follower(s) detected", len(new_items))

    followed_now = 0
    for pk, username in new_items:
        if not st.allow_by_caps():
            logger.info(
                "ig-follow: cap reached (hourly=%s daily=%s) → بقیه فردا/ساعت بعد",
                st.hourly.get("count", 0),
                st.daily.get("count", 0),
            )
            break
        # ۱) فالو‌بک (user_follow خودش اگر قبلاً فالو باشیم کاری نمی‌کند)
        #    نکته: pk فقط بعد از مشخص‌شدن نتیجه remember می‌شود تا اگر وسط دور
        #    محدودیت نرخ خوردیم، این فالوور دور بعد دوباره بررسی شود.
        try:
            ok = await bridge._run_ig(lambda: client.user_follow(int(pk)))
            st.remember(pk, username)  # از این به بعد «شناخته‌شده» است
            if ok:
                st.consume_follow()
                followed_now += 1
                logger.info("ig-follow: followed back @%s (%s)", username or "?", pk)
            else:
                logger.info("ig-follow: already following @%s (%s)", username or "?", pk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if throttled_type and isinstance(exc, throttled_type) or (
                ratelimit_type and isinstance(exc, ratelimit_type)
            ):
                logger.warning(
                    "ig-follow: rate limited → pause this round (این فالوور دور بعد بررسی می‌شود)"
                )
                break
            st.remember(pk, username)  # خطای غیر نرخی → دیگر تلاش مجدد نمی‌کنیم
            logger.error(
                "ig-follow: follow failed for @%s (%s): %s",
                username or "?",
                pk,
                type(exc).__name__,
            )
        if not st.is_welcomed(pk):
            if await _send_welcome(bridge, client, pk, username):
                st.mark_welcomed(pk)
            else:
                st.queue_dm(pk, username)
        await asyncio.sleep(FOLLOW_COOLDOWN + secrets.randbelow(20) / 10.0)

    if followed_now:
        logger.info("ig-follow: round done — %s follow-back(s)", followed_now)


async def _retry_pending_dms(bridge: Any, client: Any, st: FollowState) -> None:
    """تلاش مجدد پیام‌های خوش‌آمد ناموفق — هر دور اجرا می‌شود (حتی بدون فالوور جدید)."""
    if not st.pending_dm:
        return
    retry_dm = [dict(p) for p in st.pending_dm]
    for item in retry_dm:
        pk = str(item.get("pk", ""))
        username = str(item.get("u", ""))
        tries = int(item.get("tries", 0))
        if tries >= DM_MAX_TRIES:
            st.mark_dm_failed(pk)
            logger.info("ig-follow: DM for @%s (%s) gave up after %s tries", username or "?", pk, tries)
            continue
        if await _send_welcome(bridge, client, pk, username):
            st.mark_welcomed(pk)
        else:
            item["tries"] = tries + 1
            st.pending_dm = [
                dict(item) if str(p.get("pk")) == pk else p for p in st.pending_dm
            ]
        await asyncio.sleep(2.0)


async def _poll_once(bridge: Any, client: Any) -> None:
    st = _state()
    followers = await _fetch_newest_followers(bridge, client)
    await _process_new_followers(bridge, client, st, followers)
    await _retry_pending_dms(bridge, client, st)
    st.save()


async def _loop(bridge: Any) -> None:
    """حلقهٔ فالو‌بک — منتظر لاگین پل دایرکت می‌ماند و از همان کلاینت استفاده می‌کند."""
    await asyncio.sleep(10)  # اجازه بده پل دایرکت اول بالا بیاید
    st = _state()
    logger.info(
        "ig-follow: follow-back enabled (poll=%ss, sample=%s, caps=%s/h %s/d, cooldown=%ss)",
        FOLLOW_POLL_SECONDS,
        FOLLOWER_SAMPLE,
        HOURLY_CAP,
        DAILY_CAP,
        FOLLOW_COOLDOWN,
    )
    waiting_logged = False
    error_streak = 0
    while True:
        try:
            client = getattr(bridge, "_client", None)
            if client is None:
                if not waiting_logged:
                    logger.info("ig-follow: waiting for ig-dm login…")
                    waiting_logged = True
                await asyncio.sleep(15)
                continue
            waiting_logged = False
            await _poll_once(bridge, client)
            error_streak = 0
            await asyncio.sleep(FOLLOW_POLL_SECONDS + secrets.randbelow(5))
        except asyncio.CancelledError:
            logger.info("ig-follow: follow-back loop stopped")
            raise
        except Exception as exc:  # noqa: BLE001
            error_streak += 1
            name = type(exc).__name__
            delay = min(30 * (2 ** min(error_streak, 4)), 900)
            logger.error("ig-follow: poll error (%s): %s → retry in %ss", name, exc, delay)
            try:
                await bridge.notify_admin(
                    f"⚠️ فالو‌بک خودکار خطا داد: <code>{name}</code> — ادامه می‌دهم.",
                    f"follow_error:{name}",
                    cooldown=7200.0,
                )
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(delay)


_FOLLOW_TASK: asyncio.Task | None = None


def maybe_start_followback(bridge_getter: Callable[[], Any] | None = None) -> asyncio.Task | None:
    """اگر فعال باشد، حلقهٔ فالو‌بک را در پس‌زمینه اجرا می‌کند (no-op امن).

    bridge_getter: تابعی که پل دایرکت (ig_dm._BRIDGE) را برمی‌گرداند؛
    به‌صورت lazy در نخ حلقه خوانده می‌شود تا ترتیب راه‌اندازی مهم نباشد.
    """
    global _FOLLOW_TASK
    if not feature_enabled_followback():
        logger.info("ig-follow: disabled (IG_FOLLOWBACK_ENABLED=0)")
        return None
    if _FOLLOW_TASK is not None and not _FOLLOW_TASK.done():
        return _FOLLOW_TASK

    async def _run() -> None:
        getter = bridge_getter or _default_bridge_getter()
        bridge = None
        while bridge is None:
            bridge = getter()
            if bridge is None:
                await asyncio.sleep(10)
        await _loop(bridge)

    _FOLLOW_TASK = asyncio.create_task(_run(), name="ig-follow-back")
    return _FOLLOW_TASK


def _default_bridge_getter() -> Callable[[], Any]:
    def _get() -> Any:
        try:
            import ig_dm  # noqa: PLC0415

            return getattr(ig_dm, "_BRIDGE", None)
        except Exception:  # noqa: BLE001
            return None

    return _get


# ── دستور ادمین تلگرام ──


async def igfollowstate_command(update: Any, context: Any) -> None:
    """دستور /igfollowstate — خروجی base64 وضعیت فالو‌بک برای انتقال به Railway."""
    from telegram import Update  # noqa: PLC0415

    if not isinstance(update, Update) or update.effective_message is None:
        return
    st = _state()
    b64 = follow_state_b64()
    counts = (
        f"baseline: {'✅' if st.baseline_done else '❌ (اولین اجرا هنوز)'}\n"
        f"known: {len(st.known)}\n"
        f"pending_dm: {len(st.pending_dm)}\n"
        f"follows today: {st.daily.get('count', 0)}/{DAILY_CAP} | this hour: "
        f"{st.hourly.get('count', 0)}/{HOURLY_CAP}"
    )
    if not b64:
        await update.effective_message.reply_text(
            f"📊 وضعیت فالو‌بک:\n{counts}\n\n(فایل وضعیت هنوز خالی است)"
        )
        return
    chunks = [b64[i : i + 3800] for i in range(0, len(b64), 3800)]
    await update.effective_message.reply_text(
        f"📊 وضعیت فالو‌بک:\n{counts}\n\n"
        "برای انتقال به Railway، مقدار زیر را در متغیر IG_FOLLOW_STATE_B64 بگذار "
        f"({len(chunks)} بخش):"
    )
    for idx, chunk in enumerate(chunks, 1):
        await update.effective_message.reply_text(f"[{idx}/{len(chunks)}]\n{chunk}")
