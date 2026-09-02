"""فالو‌بک خودکار + پیام خوش‌آمد دایرکت برای فالوورهای جدید پیج — نسخهٔ ۲.

چرایی نسخهٔ ۲ (رفع «ربات فالو‌بک نمی‌دهد»):
- v1 فقط «فالوورهای جدیدِ بعد از استارت» را می‌دید و هیچ‌گاه فالوورهای قدیمی
  (baseline) را فالو‌بک نمی‌کرد؛ حتی unfollow→refollow هم چون حافظهٔ known
  هرگز پاک نمی‌شد دیده نمی‌شد.
- v1 در اولین خطای غیرنرخیِ فالو، آن فالوور را برای همیشه remember می‌کرد
  (بدون تلاش مجدد) و پیام خوش‌آمد را هم با وجود فالو‌نشده می‌فرستاد.

رفع‌شده در v2:
- ✅ Backfill: فالوورهای فعلی که ما فالوشان نکرده‌ایم هم فالو‌بک می‌شوند
  (کند و ایمن: حداکثر IG_FOLLOWBACK_MAX_PER_ROUND در هر دور + همان سقف‌ها).
- ✅ تشخیص unfollow: فالووری که از فهرست محو شود با user_friendship_v1 بررسی
  می‌شود؛ اگر واقعاً unfollow کرده از حافظه حذف می‌شود تا refollow دوباره
  فالو‌بک + پیام بگیرد (دقیقاً سناریوی تست کاربر).
- ✅ صف retry فالو: خطای غیرنرخی → حداکثر ۳ بار در دورهای بعدی تلاش مجدد.
- ✅ پیام خوش‌آمد فقط بعد از فالوی موفق ارسال می‌شود (نه قبلش).
- ✅ تأیید (verify): بعد از فالو، وضعیت friendship چک و در لاگ تایید می‌شود.
- ✅ لاگ خلاصه در پایان هر دور + دستور تشخیصی /igfollowcheck برای ادمین.

طراحی و نکات مهم:
- از همان کلاینت و سشن «پل دایرکت» (ig_dm) استفاده می‌کند؛ هیچ لاگین دومی
  انجام نمی‌شود (بدون Conflict و بدون دستگاه/سشن جدا).
- هر IG_FOLLOW_POLL_SECONDS ثانیه، فالوورها با ترتیب date_followed_latest
  (جدیدترین اول) بررسی می‌شوند.
- قانون baseline همچنان برقرار است: در «اولین اجرا» فقط فهرست ثبت می‌شود تا
  شروعِ ناگهانیِ انبوه‌فالو رخ ندهد؛ ولی از همان دور بعد Backfill (اگر فعال
  باشد) شروع به فالو‌بکِ فالوورهای فعلی با همان سقف‌ها می‌کند.
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


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name, "") or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


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
# فالو‌بک فالوورهای فعلی (Backfill) — به‌صورت پیش‌فرض روشن ولی آرام و سقف‌دار
BACKFILL_ENABLED = _env_bool("IG_FOLLOWBACK_BACKFILL", True)
BACKFILL_MAX_PER_ROUND = _env_int("IG_FOLLOWBACK_MAX_PER_ROUND", 5, minimum=1)
# اعلان تلگرامی «✅ فالو‌بک شد» فقط برای فالوورهای جدید (نه Backfill)
NOTIFY_FOLLOWS = _env_bool("IG_FOLLOWBACK_NOTIFY", True)
# بعد از فالو، وضعیت friendship چک شود (کانفیرم)
VERIFY_FOLLOW = _env_bool("IG_FOLLOWBACK_VERIFY", True)
FOLLOW_MAX_TRIES = 3        # تلاش مجدد فالو بعد از خطای غیرنرخی
DM_MAX_TRIES = 3
FRIENDSHIP_BUDGET = 15      # حداکثر چک friendship در هر دور (بودجهٔ API)
MAX_KNOWN = 5000            # سقف حافظهٔ فالوورهای شناخته‌شده (حذف قدیمی‌ها)
MAX_WELCOMED = 3000
MAX_RECENT_SAMPLE = 1200
FOLLOWING_TTL = 600.0       # عمر کش «ما چه کسانی را فالو کرده‌ایم» (ثانیه)

DEFAULT_DM_TEXT = (
    "سلام 👋 فالو‌ات کردم! ✅\n\n"
    "این پیج ربات دانلوده — هر ریلز/پستی که همین‌جا Share کنی یا لینکش رو "
    "بفرستی، فایلش رو همین‌جا برات می‌فرستم 📥"
)


def _dm_text() -> str:
    return (os.getenv("IG_FOLLOWBACK_DM_TEXT", "") or "").strip() or DEFAULT_DM_TEXT


# ── وضعیت ماندگار ──


class FollowState:
    """فالوورهای شناخته‌شده + سقف‌ها + صف‌های تلاش مجدد (JSON محلی)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.known: dict[str, dict[str, Any]] = {}
        self.welcomed: list[str] = []
        self.dm_failed: list[str] = []
        self.pending_dm: list[dict[str, Any]] = []
        self.follow_pending: list[dict[str, Any]] = []
        self.recent_sample: list[str] = []
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
                self.follow_pending = data.get("follow_pending", []) or []
                self.recent_sample = data.get("recent_sample", []) or []
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
                        "follow_pending": self.follow_pending[-500:],
                        "recent_sample": self.recent_sample[-MAX_RECENT_SAMPLE:],
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

    def drop_known(self, pk: str) -> None:
        """حذف کامل از حافظه (unfollow واقعی) — refollow دوباره مثل فالوور جدید."""
        pk = str(pk)
        self.known.pop(pk, None)
        self.welcomed = [w for w in self.welcomed if w != pk]
        self.dm_failed = [w for w in self.dm_failed if w != pk]
        self.pending_dm = [p for p in self.pending_dm if str(p.get("pk")) != pk]
        self.follow_pending = [p for p in self.follow_pending if str(p.get("pk")) != pk]

    def username_of(self, pk: str) -> str:
        return str(self.known.get(str(pk), {}).get("u", "") or "")

    def set_recent_sample(self, pks: list[str]) -> None:
        self.recent_sample = [str(p) for p in pks[-MAX_RECENT_SAMPLE:]]

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

    def queue_follow(self, pk: str, username: str, tries: int, backfill: bool) -> None:
        for p in self.follow_pending:
            if str(p.get("pk")) == str(pk):
                p["tries"] = tries
                p["u"] = str(username or p.get("u", ""))
                return
        self.follow_pending.append(
            {"pk": str(pk), "u": str(username or ""), "tries": tries, "bf": bool(backfill)}
        )

    def pop_follow(self, pk: str) -> None:
        pk = str(pk)
        self.follow_pending = [p for p in self.follow_pending if str(p.get("pk")) != pk]

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


def reset_state_for_tests() -> None:
    """فقط برای تست‌های آفلاین — وضعیت درون-حافظه‌ای را خالی می‌کند."""
    global _STATE, _FOLLOWING_CACHE
    _STATE = None
    _FOLLOWING_CACHE = {"ts": 0.0, "pks": set()}


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


_FOLLOWING_CACHE: dict[str, Any] = {"ts": 0.0, "pks": set()}


async def _get_following_set(bridge: Any, client: Any) -> set[str] | None:
    """مجموعهٔ pkهایی که ما فالو کرده‌ایم — با کش TTL دار (برای Backfill)."""
    now = time.monotonic()
    if now - float(_FOLLOWING_CACHE.get("ts", 0.0)) < FOLLOWING_TTL:
        return set(_FOLLOWING_CACHE.get("pks", set()))
    try:
        raw = await bridge._run_ig(
            lambda: client.user_following(
                str(client.user_id), use_cache=False, amount=2000
            )
        )
        pks = {str(pk) for pk in (raw or {}).keys()}
        _FOLLOWING_CACHE["ts"] = now
        _FOLLOWING_CACHE["pks"] = pks
        return set(pks)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ig-follow: could not fetch following set: %s", type(exc).__name__)
        return None


async def _friendship(bridge: Any, client: Any, pk: str) -> Any | None:
    """وضعیت friendship یک کاربر — هر خطا None (تماس‌گیرنده تصمیم می‌گیرد)."""
    try:
        return await bridge._run_ig(lambda: client.user_friendship_v1(str(pk)))
    except Exception:  # noqa: BLE001
        return None


async def _send_welcome(bridge: Any, client: Any, pk: str, username: str) -> bool:
    """پیام خوش‌آمد دایرکت — بعد از فالو‌بک موفق."""
    try:
        await bridge._run_ig(
            lambda: client.direct_send(_dm_text(), user_ids=[int(pk)])
        )
        logger.info("ig-follow: welcome DM sent to @%s (%s)", username or "?", pk)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "ig-follow: welcome DM failed for @%s (%s): %s %s",
            username or "?",
            pk,
            type(exc).__name__,
            str(exc)[:200],
        )
        return False


async def _notify_followed(bridge: Any, username: str, pk: str, backfill: bool) -> None:
    """اعلان تلگرامی «فالو‌بک شد» — فقط برای فالوورهای جدید (نه Backfill)."""
    if backfill or not NOTIFY_FOLLOWS:
        return
    try:
        await bridge.notify_admin(
            f"✅ فالو‌بک شد: @{username or pk}",
            f"followed:{pk}",
            cooldown=0.0,
        )
    except Exception:  # noqa: BLE001
        pass


async def _follow_one(
    bridge: Any,
    client: Any,
    st: FollowState,
    pk: str,
    username: str,
    *,
    backfill: bool,
    tries: int = 0,
) -> str:
    """فالو‌بک یک نفر + verify + DM. خروجی: ok / already / throttled / error / giveup.

    - throttled → دور فعلی متوقف می‌شود و این فالوور دور بعد بررسی می‌شود.
    - error     → در صف follow_pending می‌رود (حداکثر FOLLOW_MAX_TRIES بار).
    - giveup    → بعد از اتمام تلاش‌ها remember می‌شود (دیگر retry نمی‌شود).
    پیام خوش‌آمد فقط بعد از فالوی موفق (ok) یا «قبلاً فالو بودیم» برای
    فالوور جدید ارسال می‌شود.
    """
    ig_exc = getattr(bridge, "_ig_exc", None)
    throttled_type = getattr(ig_exc, "ClientThrottledError", ())
    ratelimit_type = getattr(ig_exc, "RateLimitError", ())

    try:
        ok = await bridge._run_ig(lambda: client.user_follow(int(pk)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        if (throttled_type and isinstance(exc, throttled_type)) or (
            ratelimit_type and isinstance(exc, ratelimit_type)
        ):
            logger.warning(
                "ig-follow: rate limited → pause this round (@%s دور بعد بررسی می‌شود)",
                username or pk,
            )
            return "throttled"
        logger.error(
            "ig-follow: follow failed for @%s (%s): %s %s",
            username or "?",
            pk,
            type(exc).__name__,
            str(exc)[:200],
        )
        if tries + 1 >= FOLLOW_MAX_TRIES:
            st.remember(pk, username)
            st.pop_follow(pk)
            logger.info(
                "ig-follow: give up follow @%s (%s) after %s tries",
                username or "?",
                pk,
                tries + 1,
            )
            return "giveup"
        st.queue_follow(pk, username, tries + 1, backfill)
        return "error"

    # فالو انجام شد (یا از قبل بوده)
    st.remember(pk, username)
    st.pop_follow(pk)
    # در کش following هم ثبت کن تا Backfill در پنجرهٔ کش دوباره انتخابش نکند
    try:
        _FOLLOWING_CACHE.setdefault("pks", set()).add(str(pk))
    except Exception:  # noqa: BLE001
        pass
    if ok:
        st.consume_follow()
        logger.info(
            "ig-follow: followed back @%s (%s)%s",
            username or "?",
            pk,
            " [backfill]" if backfill else "",
        )
    else:
        logger.info("ig-follow: already following @%s (%s)", username or "?", pk)

    # کانفیرم: وضعیت friendship را چک می‌کنیم (best-effort)
    if VERIFY_FOLLOW and ok:
        rel = await _friendship(bridge, client, pk)
        if rel is not None:
            confirmed = bool(getattr(rel, "following", False)) or bool(
                getattr(rel, "outgoing_request", False)
            )
            if confirmed:
                logger.info(
                    "ig-follow: confirmed ✅ @%s (%s) — following/outgoing_request",
                    username or "?",
                    pk,
                )
            else:
                logger.warning(
                    "ig-follow: follow NOT reflected for @%s (%s) — may be private/rejected",
                    username or "?",
                    pk,
                )

    if ok:
        await _notify_followed(bridge, username, pk, backfill)

    # پیام خوش‌آمد — فقط وقتی فالو نتیجه داد
    if ok or (not backfill):
        if not st.is_welcomed(pk):
            if await _send_welcome(bridge, client, pk, username):
                st.mark_welcomed(pk)
            else:
                st.queue_dm(pk, username)
    return "ok" if ok else "already"


async def _detect_unfollows(
    bridge: Any, client: Any, st: FollowState, current_pks: list[str]
) -> int:
    """فالوورهایی که از نمونهٔ قبلی محو شده‌اند → چک friendship → حذف از حافظه.

    خروجی: تعداد unfollow واقعیِ شناسایی‌شده. فقط pkهایی که در نمونهٔ قبلی
    بودند بررسی می‌شوند (نه کل known) تا تعداد فراخوانی API کنترل‌شده بماند.
    """
    prev = list(st.recent_sample)
    if not prev or not st.baseline_done:
        st.set_recent_sample(current_pks)
        return 0
    current = set(current_pks)
    missing = [pk for pk in prev if pk not in current]
    st.set_recent_sample(current_pks)
    if not missing:
        return 0

    removed = 0
    budget = FRIENDSHIP_BUDGET
    for pk in missing:
        if budget <= 0:
            break
        budget -= 1
        rel = await _friendship(bridge, client, pk)
        followed_by = bool(getattr(rel, "followed_by", False)) if rel is not None else True
        if rel is None:
            # نتوانستیم چک کنیم → محافظه‌کارانه نگه می‌داریم
            continue
        if not followed_by:
            st.drop_known(pk)
            removed += 1
            logger.info(
                "ig-follow: @%s (%s) unfollowed → از حافظه حذف شد (refollow دوباره فالو‌بک می‌گیرد)",
                st.username_of(pk) or "?",
                pk,
            )
    return removed


async def _process_new_followers(
    bridge: Any, client: Any, st: FollowState, followers: list[Any]
) -> dict[str, int]:
    """تفاوت فالوورها → فالو‌بک + پیام خوش‌آمد با سقف و فاصلهٔ امن (+Backfill)."""
    stats = {"new": 0, "unfollowed": 0, "followed": 0, "backfill": 0}
    sample_pks: list[str] = []
    new_items: list[tuple[str, str]] = []

    self_pk = str(getattr(client, "user_id", "") or "")
    for user in followers:
        pk = str(getattr(user, "pk", "") or "")
        if not pk or pk == self_pk:
            continue
        sample_pks.append(pk)
        username = getattr(user, "username", "") or ""
        if not st.remembered(pk):
            new_items.append((pk, username))

    if not st.baseline_done:
        for pk, username in new_items:
            st.remember(pk, username)
        st.baseline_done = True
        st.set_recent_sample(sample_pks)
        st.save()
        logger.info(
            "ig-follow: baseline recorded (%s followers) — از دور بعد فالو‌بک/Backfill شروع می‌شود",
            len(new_items),
        )
        return stats

    # unfollow های واقعی را از حافظه پاک کن (تا refollow دیده شود)
    stats["unfollowed"] = await _detect_unfollows(bridge, client, st, sample_pks)

    stats["new"] = len(new_items)
    if new_items:
        logger.info("ig-follow: %s new follower(s) detected", len(new_items))
    for pk, username in new_items:
        if not st.allow_by_caps():
            logger.info(
                "ig-follow: cap reached (hourly=%s daily=%s) → بقیه دور بعد",
                st.hourly.get("count", 0),
                st.daily.get("count", 0),
            )
            break
        outcome = await _follow_one(bridge, client, st, pk, username, backfill=False)
        if outcome == "throttled":
            break
        if outcome == "ok":
            stats["followed"] += 1
        await asyncio.sleep(FOLLOW_COOLDOWN + secrets.randbelow(20) / 10.0)

    # Backfill: فالوورهای فعلی که هنوز فالوشان نکرده‌ایم — آرام و سقف‌دار
    if BACKFILL_ENABLED and st.allow_by_caps():
        following = await _get_following_set(bridge, client)
        if following is not None:
            candidates = [
                pk
                for pk in sample_pks
                if pk in st.known and pk not in following
            ]
            if candidates:
                logger.info(
                    "ig-follow: backfill — %s candidate(s) in sample, following up to %s this round",
                    len(candidates),
                    BACKFILL_MAX_PER_ROUND,
                )
            for pk in candidates[:BACKFILL_MAX_PER_ROUND]:
                if not st.allow_by_caps():
                    break
                username = st.username_of(pk)
                outcome = await _follow_one(
                    bridge, client, st, pk, username, backfill=True
                )
                if outcome == "throttled":
                    break
                if outcome == "ok":
                    stats["backfill"] += 1
                    stats["followed"] += 1
                await asyncio.sleep(FOLLOW_COOLDOWN + secrets.randbelow(20) / 10.0)
        else:
            logger.info("ig-follow: backfill skipped this round (following set unavailable)")

    return stats


async def _retry_follows(bridge: Any, client: Any, st: FollowState) -> None:
    """تلاش مجدد فالوهای خطاخورده — هر دور اجرا می‌شود."""
    if not st.follow_pending:
        return
    for item in [dict(p) for p in st.follow_pending]:
        pk = str(item.get("pk", ""))
        username = str(item.get("u", ""))
        tries = int(item.get("tries", 0))
        backfill = bool(item.get("bf", False))
        if not st.allow_by_caps():
            break
        outcome = await _follow_one(
            bridge,
            client,
            st,
            pk,
            username,
            backfill=backfill,
            tries=tries,
        )
        if outcome == "throttled":
            break
        # ok/already/giveup: صف داخل _follow_one مدیریت شد
        await asyncio.sleep(FOLLOW_COOLDOWN + secrets.randbelow(20) / 10.0)


async def _retry_pending_dms(bridge: Any, client: Any, st: FollowState) -> None:
    """تلاش مجدد پیام‌های خوش‌آمد ناموفق — هر دور اجرا می‌شود."""
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
    stats = await _process_new_followers(bridge, client, st, followers)
    await _retry_follows(bridge, client, st)
    await _retry_pending_dms(bridge, client, st)
    st.save()
    logger.info(
        "ig-follow: poll — sample=%s new=%s unfollowed=%s followed=%s (backfill=%s) "
        "known=%s caps h=%s/%s d=%s/%s pending_follow=%s pending_dm=%s",
        len(followers),
        stats.get("new", 0),
        stats.get("unfollowed", 0),
        stats.get("followed", 0),
        stats.get("backfill", 0),
        len(st.known),
        st.hourly.get("count", 0),
        HOURLY_CAP,
        st.daily.get("count", 0),
        DAILY_CAP,
        len(st.follow_pending),
        len(st.pending_dm),
    )


async def _loop(bridge: Any) -> None:
    """حلقهٔ فالو‌بک — منتظر لاگین پل دایرکت می‌ماند و از همان کلاینت استفاده می‌کند."""
    await asyncio.sleep(10)  # اجازه بده پل دایرکت اول بالا بیاید
    st = _state()
    logger.info(
        "ig-follow: follow-back enabled (poll=%ss, sample=%s, caps=%s/h %s/d, "
        "cooldown=%ss, backfill=%s max=%s/round, verify=%s)",
        FOLLOW_POLL_SECONDS,
        FOLLOWER_SAMPLE,
        HOURLY_CAP,
        DAILY_CAP,
        FOLLOW_COOLDOWN,
        "on" if BACKFILL_ENABLED else "off",
        BACKFILL_MAX_PER_ROUND,
        "on" if VERIFY_FOLLOW else "off",
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


# ── دستورهای ادمین تلگرام ──


def _fmt_counts(st: FollowState) -> str:
    return (
        f"baseline: {'✅' if st.baseline_done else '❌ (اولین اجرا هنوز)'}\n"
        f"known: {len(st.known)}\n"
        f"pending_follow: {len(st.follow_pending)}\n"
        f"pending_dm: {len(st.pending_dm)}\n"
        f"follows today: {st.daily.get('count', 0)}/{DAILY_CAP} | this hour: "
        f"{st.hourly.get('count', 0)}/{HOURLY_CAP}"
    )


async def igfollowstate_command(update: Any, context: Any) -> None:
    """دستور /igfollowstate — خروجی base64 وضعیت فالو‌بک برای انتقال به Railway."""
    from telegram import Update  # noqa: PLC0415

    if not isinstance(update, Update) or update.effective_message is None:
        return
    st = _state()
    b64 = follow_state_b64()
    counts = _fmt_counts(st)
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


async def igfollowcheck_command(update: Any, context: Any) -> None:
    """دستور /igfollowcheck — تشخیص زنده و فقط-خواندنی (هیچ اقدامی انجام نمی‌دهد)."""
    from telegram import Update  # noqa: PLC0415

    if not isinstance(update, Update) or update.effective_message is None:
        return
    msg = update.effective_message
    lines: list[str] = ["🔍 تشخیص زندهٔ فالو‌بک:"]

    try:
        bridge = _default_bridge_getter()()
    except Exception:  # noqa: BLE001
        bridge = None
    if bridge is None:
        lines.append("❌ پل دایرکت (ig-dm) هنوز بالا نیامده — فالو‌بک منتظر لاگین است.")
        lines.append("   اول وضعیت لاگین پل را با لاگ Railway چک کن (ig-dm: logged in as …).")
        await msg.reply_text("\n".join(lines))
        return

    st = _state()
    lines.append(_fmt_counts(st))
    client = getattr(bridge, "_client", None)
    if client is None:
        lines.append("⏳ کلاینت اینستاگرام هنوز لاگین نکرده — چند دقیقه بعد دوباره امتحان کن.")
        await msg.reply_text("\n".join(lines))
        return

    lines.append("✅ کلاینت آماده است. در حال گرفتن فالوورها…")
    try:
        followers = await _fetch_newest_followers(bridge, client)
    except Exception as exc:  # noqa: BLE001
        lines.append(f"❌ گرفتن فالوورها شکست خورد: {type(exc).__name__}: {str(exc)[:200]}")
        await msg.reply_text("\n".join(lines))
        return

    self_pk = str(getattr(client, "user_id", "") or "")
    sample_pks: list[str] = []
    new_items: list[tuple[str, str]] = []
    for user in followers:
        pk = str(getattr(user, "pk", "") or "")
        if not pk or pk == self_pk:
            continue
        sample_pks.append(pk)
        if not st.remembered(pk):
            new_items.append((pk, str(getattr(user, "username", "") or "")))

    lines.append(f"👥 نمونهٔ فالوور: {len(sample_pks)} نفر (سقف نمونه: {FOLLOWER_SAMPLE})")
    newest = [
        f"@{getattr(u, 'username', '') or getattr(u, 'pk', '?')}"
        for u in list(followers)[:5]
        if str(getattr(u, "pk", "") or "") != self_pk
    ]
    if newest:
        lines.append("🆕 ۵ فالوور آخر: " + "، ".join(newest))
    lines.append(f"✨ جدید (فالو‌بک‌نشده در حافظه): {len(new_items)} نفر")
    if new_items:
        names = [f"@{u or p}" for p, u in new_items[:5]]
        lines.append("   " + "، ".join(names))

    if BACKFILL_ENABLED:
        following = await _get_following_set(bridge, client)
        if following is None:
            lines.append("↩️ Backfill: فعال — ولی گرفتن «فالو‌شده‌ها» شکست خورد (دور بعد دوباره).")
        else:
            cands = [pk for pk in sample_pks if pk in st.known and pk not in following]
            lines.append(
                f"↩️ Backfill: فعال — {len(cands)} نفر از نمونه هنوز فالو‌بک نشده‌اند "
                f"(ما {len(following)} نفر را فالو کرده‌ایم؛ تا {BACKFILL_MAX_PER_ROUND} نفر در هر دور)"
            )
    else:
        lines.append("↩️ Backfill: خاموش (IG_FOLLOWBACK_BACKFILL=0)")

    if not st.baseline_done:
        lines.append("ℹ️ دور اول فقط baseline را ثبت می‌کند؛ از دور بعد فالو‌بک شروع می‌شود.")
    lines.append("ℹ️ این دستور فقط گزارش می‌دهد؛ فالو‌بک توسط حلقهٔ پس‌زمینه انجام می‌شود.")
    await msg.reply_text("\n".join(lines))
