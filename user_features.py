"""User-facing upgrade features: bookmarks, auto-share, personal stats,
duplicate detection, scheduled downloads and the AI ask/summarize entry
points. Every capability is individually gated by a feature flag and all
persistency goes through the additive SQLite store (users.json untouched).

Command/callback handlers here follow the python-telegram-bot signature so
bot.py only has to register them.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import telegram
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import ai_service
import store
from feature_flags import FLAGS
from media_size import fmt_size_exact
from routing import detect_platform, platform_info

logger = logging.getLogger("MZDownloader.user_features")

BOOKMARKS_PER_PAGE = 5
SCHEDULER_MIN_INTERVAL_MIN = 60
SCHEDULER_MAX_JOBS_PER_USER = 20
SCHEDULER_TICK_SECONDS = 60
AI_TEXT_TTL = 15 * 60
_MAX_TEXT_LENGTH = 3600

# Compact, always-current bot context handed to the AI so /ask answers are
# grounded in what the bot ACTUALLY does (previously the model had zero
# context and invented answers).
BOT_HELP_CONTEXT = """پلتفرم‌ها: اینستاگرام (پست/ریلز/IGTV + کپشن + پروفایل/استوری با /profile)، یوتیوب (تا 4K + MP3 + زیرنویس فارسی/انگلیسی + جستجو با /search یا نوشتن متن در پیوی)، تیک‌تاک (بدون واترمارک)، توییتر/X (ویدیو/تصاویر/متن توییت)، فیسبوک (ویدیو/ریل عمومی + فقط صدا)، اسپاتیفای (تراک MP3 با متادیتا؛ آلبوم/پلی‌لیست به‌صورت ZIP)، ساوندکلاد (MP3)، پینترست (تصویر با کیفیت اصلی + ویدیوی پین)، VK، و لینک‌های عمومی دیگر.
دستورها: /start /help /dl /search /song (جست‌وجوی آهنگ با کاور) /caption (کپشن اینستا) /profile /stats /mystats (آمار شخصی) /bookmarks (ذخیره‌شده‌ها) /autoshare (ارسال خودکار به کانال/گروه) /schedule (دانلود زمان‌بندی‌شده مثل «7d») /ask (همین دستیار) /cancel /status /platforms
رفتارها: بعد از فرستادن لینک، منوی کیفیت/گزینه‌ها با حجم تقریبی نمایش داده می‌شود؛ زیر هر محتوای موفق دکمه «🔖 ذخیره» و زیر کپشن‌ها/متن توییت دکمه «🤖 خلاصه کن» می‌آید؛ محتوای تکراری بدون دانلود مجدد و آنی از کش سرور ارسال می‌شود؛ فایل بزرگ‌تر از حد تلگرام به‌صورت لینک موقت ابری ارسال می‌شود؛ در گروه‌ها با /dl یا ریپلای روی لینک کار می‌کند؛ عضویت در کانال‌های ربات برای استفاده الزامی است."""

# chat_id → (text, expires_at) for the 🤖 summarize button
AI_TEXTS: dict[int, tuple[str, float]] = {}
# token → bookmark-offer payload (url/platform/title/size/user_id, expires)
BOOKMARK_OFFERS: dict[str, tuple[str, str, str, int, int, float]] = {}
BOOKMARK_OFFER_TTL = 10 * 60


async def offer_bookmark(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    user_id: int,
    source_url: str,
    platform_value: str,
    media: tuple[Any, ...],
) -> None:
    """Show a one-tap 🔖 button under freshly delivered media."""
    if not (FLAGS.bookmarks and source_url):
        return
    title = ""
    if media:
        first = media[0]
        title = str(getattr(getattr(first, "path", None), "stem", "") or "")[:120]
    size = sum(int(getattr(item, "size", 0) or 0) for item in media)
    token = f"{user_id}-{int(time.time() * 1000) % 10**9:x}"
    BOOKMARK_OFFERS[token] = (source_url, platform_value, title, size, user_id, time.time() + BOOKMARK_OFFER_TTL)
    if len(BOOKMARK_OFFERS) > 200:
        now = time.time()
        for key in [k for k, v in BOOKMARK_OFFERS.items() if v[5] < now][:100]:
            BOOKMARK_OFFERS.pop(key, None)
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🔖 این محتوا را ذخیره کنی تا بعداً از /bookmarks ببینی؟",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔖 ذخیره", callback_data=f"bks:{token}")]]
            ),
        )
    except Exception:  # noqa: BLE001
        pass


async def bookmark_offer_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    token = (query.data or "").split(":", 1)[-1]
    payload = BOOKMARK_OFFERS.pop(token, None)
    if payload is None or payload[5] < time.time():
        await query.answer("مهلت این پیشنهاد تمام شد.", show_alert=True)
        return
    source_url, platform_value, title, size, user_id, _ = payload
    await store.add_bookmark(
        user_id=user_id,
        url=source_url,
        platform=platform_value,
        title=title,
        size_bytes=size,
    )
    await query.answer("ذخیره شد ✅")
    try:
        await query.edit_message_text("🔖 ذخیره شد — با /bookmarks قابل دسترسی است.")
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────── Helpers ───────────────────────────────

def _esc(value: Any) -> str:
    return html.escape(str(value or ""))


async def _quiet_edit(message: Any, text: str, reply_markup: Any = None) -> None:
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)
    except Exception:  # noqa: BLE001
        pass


def _bar(value: float, max_value: float, width: int = 10) -> str:
    if max_value <= 0:
        return "░" * width
    filled = round(max(0.0, min(1.0, value / max_value)) * width)
    return "█" * filled + "░" * (width - filled)


_DAILY_GLYPHS = "▁▂▃▄▅▆▇█"


def _sparkline(values: list[int]) -> str:
    peak = max(values or [0])
    if peak <= 0:
        return "▁" * max(1, len(values))
    return "".join(_DAILY_GLYPHS[min(7, int(v / peak * 7.99))] for v in values)


# ─────────────────────────────── Bookmarks ───────────────────────────────

async def _bookmarks_view(user_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    total = await store.count_bookmarks(user_id)
    pages = max(1, (total + BOOKMARKS_PER_PAGE - 1) // BOOKMARKS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    items = await store.list_bookmarks(user_id, limit=BOOKMARKS_PER_PAGE, offset=page * BOOKMARKS_PER_PAGE)
    rows: list[list[InlineKeyboardButton]] = []
    lines = [f"🔖 <b>ذخیره‌شده‌های من</b> — صفحه {page + 1} از {pages} (مجموع {total})", ""]
    if not items:
        lines.append("هنوز چیزی ذخیره نکرده‌ای.")
        lines.append("بعد از هر دانلود موفق، دکمه «🔖 ذخیره» را بزن.")
    for item in items:
        title = (item.get("title") or "").strip()
        url = item.get("url") or ""
        host = urlsplit(url).netloc or url
        size = int(item.get("size_bytes") or 0)
        label = title[:32] if title else host[:32]
        lines.append(f"• <b>{_esc(label)}</b>" + (f" · {fmt_size_exact(size)}" if size else ""))
        rows.append([
            InlineKeyboardButton("🔗 باز کردن", url=url),
            InlineKeyboardButton("🗑 حذف", callback_data=f"bm:d:{item['id']}:{page}"),
        ])
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"bm:p:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="bm:nop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"bm:p:{page + 1}"))
        rows.append(nav)
    rows.append([InlineKeyboardButton("✖️ بستن", callback_data="bm:x")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


async def bookmarks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not FLAGS.bookmarks:
        await update.effective_message.reply_text("این قابلیت فعلاً غیرفعال است.")
        return
    text, keyboard = await _bookmarks_view(update.effective_user.id, 0)
    await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def bookmarks_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    action = parts[1] if len(parts) > 1 else ""
    if action == "d" and len(parts) > 3:
        try:
            bookmark_id = int(parts[2])
            page = int(parts[3])
        except ValueError:
            return
        await store.delete_bookmark(bookmark_id, update.effective_user.id)
        text, keyboard = await _bookmarks_view(update.effective_user.id, page)
        await _quiet_edit(query.message, text, keyboard)
    elif action == "p" and len(parts) > 2:
        try:
            page = int(parts[2])
        except ValueError:
            return
        text, keyboard = await _bookmarks_view(update.effective_user.id, page)
        await _quiet_edit(query.message, text, keyboard)
    elif action == "x":
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass


# ──────────────────────────── Personal stats ────────────────────────────

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not FLAGS.user_stats:
        await update.effective_message.reply_text("این قابلیت فعلاً غیرفعال است.")
        return
    user_id = update.effective_user.id
    data = await store.user_stats(user_id, days=30)
    total_downloads = int(data.get("total_downloads") or 0)
    total_bytes = int(data.get("total_bytes") or 0)
    platforms = data.get("platforms") or []
    daily = data.get("daily") or []
    lines = [
        "📊 <b>آمار شخصی شما (۳۰ روز اخیر)</b>",
        "",
        f"📥 تعداد دانلود: <b>{total_downloads}</b>",
        f"💾 حجم کل: <b>{fmt_size_exact(total_bytes)}</b>",
    ]
    if platforms:
        lines += ["", "<b>پلتفرم‌های پراستفاده:</b>"]
        peak = max((int(p.get("downloads") or 0) for p in platforms), default=0)
        for entry in platforms:
            info = None
            platform_name = str(entry.get("platform") or "سایر")
            try:
                from routing import Platform

                info = platform_info(Platform(platform_name))
            except Exception:  # noqa: BLE001
                pass
            name = f"{info.icon} {info.label}" if info else platform_name
            count = int(entry.get("downloads") or 0)
            size = int(entry.get("bytes") or 0)
            lines.append(
                f"<code>{_bar(count, peak)}</code> {name}: <b>{count}</b> · {fmt_size_exact(size)}"
            )
    if daily:
        counts = [int(d.get("downloads") or 0) for d in daily]
        lines += ["", "<b>روند ۱۴ روز اخیر:</b>", f"<code>{_sparkline(counts)}</code>"]
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ───────────────────────────── Auto-share ─────────────────────────────

async def autoshare_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not FLAGS.autoshare:
        await update.effective_message.reply_text("این قابلیت فعلاً غیرفعال است.")
        return
    chat = update.effective_chat
    user_id = update.effective_user.id
    args = context.args or []
    if args and args[0].lower() in {"add", "افزودن"}:
        # PTB's ChatType is a str-Enum; unwrap .value so set membership works
        # regardless of how str() formats the enum on this Python version.
        raw_type = getattr(chat, "type", None)
        chat_type = getattr(raw_type, "value", raw_type) or ""
        if chat_type not in {"group", "supergroup", "channel"}:
            await update.effective_message.reply_text(
                "برای افزودن مقصد، این دستور را داخل همان کانال/گروه بفرستید:\n"
                "<code>/autoshare add</code>\n"
                "ربات باید ادمین آن چت با اجازه ارسال پیام باشد."
                ,
                parse_mode=ParseMode.HTML,
            )
            return
        member = None
        try:
            member = await context.bot.get_chat_member(chat.id, context.bot.id)
        except Exception:  # noqa: BLE001
            pass
        raw_status = getattr(member, "status", None)
        status = getattr(raw_status, "value", raw_status) or ""
        if chat_type == "channel" or status == "administrator":
            title = getattr(chat, "title", None) or f"chat {chat.id}"
            await store.add_autoshare_target(user_id, chat.id, str(title)[:120])
            await update.effective_message.reply_text(f"✅ «{_esc(title)}» به مقصدهای اشتراک‌گذاری شما اضافه شد.")
            return
        await update.effective_message.reply_text("ربات برای ارسال در این چت باید ادمین باشد.")
        return
    targets = await store.list_autoshare_targets(user_id)
    rows: list[list[InlineKeyboardButton]] = []
    lines = ["📤 <b>مقصدهای اشتراک‌گذاری خودکار</b>", ""]
    if not targets:
        lines.append("مقصدهایی ندارید. داخل کانال/گروه موردنظر (ربات ادمین) بفرستید:")
        lines.append("<code>/autoshare add</code>")
    for target in targets:
        title = target.get("title") or f"chat {target.get('chat_id')}"
        lines.append(f"• {_esc(title)}")
        rows.append([InlineKeyboardButton(f"🗑 حذف {_esc(title)[:24]}", callback_data=f"sh:r:{target['chat_id']}")])
    if rows:
        rows.append([InlineKeyboardButton("✖️ بستن", callback_data="sh:x")])
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows) if rows else None
    )


async def autoshare_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 2:
        return
    action = parts[1]
    if action == "r" and len(parts) > 2:
        try:
            chat_id = int(parts[2])
        except ValueError:
            return
        removed = await store.remove_autoshare_target(update.effective_user.id, chat_id)
        await _quiet_edit(query.message, "✅ مقصد حذف شد." if removed else "پیدا نشد.")
    elif action == "x":
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass


# ─────────────────────────── Scheduled downloads ───────────────────────────

_INTERVAL_RE_TABLE = {"m": 1, "h": 60, "d": 1440, "w": 10080}


def parse_interval(raw: str) -> int | None:
    """Parse '90m' / '12h' / '1d' / '7d' / '2w' into minutes."""
    value = raw.strip().lower()
    if value.isdigit():
        minutes = int(value)
        return minutes if minutes >= SCHEDULER_MIN_INTERVAL_MIN else None
    if len(value) >= 2 and value[-1] in _INTERVAL_RE_TABLE and value[:-1].isdigit():
        minutes = int(value[:-1]) * _INTERVAL_RE_TABLE[value[-1]]
        return minutes if minutes >= SCHEDULER_MIN_INTERVAL_MIN else None
    return None


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not FLAGS.scheduler:
        await update.effective_message.reply_text("این قابلیت فعلاً غیرفعال است.")
        return
    user_id = update.effective_user.id
    args = context.args or []
    usage = (
        "⏰ <b>دانلود زمان‌بندی‌شده</b>\n"
        "<code>/schedule &lt;لینک&gt; &lt;بازه&gt;</code>\n"
        "بازه: <code>90m</code> · <code>12h</code> · <code>1d</code> · <code>7d</code> (حداقل 60m)\n"
        "مثال: <code>/schedule https://pndsn.com/pin/123 7d</code>"
    )
    if not args:
        jobs = await store.list_jobs(user_id)
        lines = [usage, "", f"🗓 زمان‌بندی‌های شما: <b>{len(jobs)}</b>"]
        rows: list[list[InlineKeyboardButton]] = []
        for job in jobs:
            platform_name = job.get("platform") or "-"
            lines.append(
                f"• <code>#{job['id']}</code> · {platform_name} · هر {job.get('interval_minutes')} دقیقه"
                + (f" · آخرین وضعیت: {_esc(job.get('last_status'))}" if job.get("last_status") else "")
            )
            rows.append([InlineKeyboardButton(f"🗑 حذف #{job['id']}", callback_data=f"sc:r:{job['id']}")])
        if rows:
            rows.append([InlineKeyboardButton("✖️ بستن", callback_data="sc:x")])
        await update.effective_message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows) if rows else None
        )
        return
    if len(args) < 2:
        await update.effective_message.reply_text(usage, parse_mode=ParseMode.HTML)
        return
    url = args[0].strip()
    interval = parse_interval(args[1])
    if interval is None:
        await update.effective_message.reply_text("بازه نامعتبر است. مثال: <code>12h</code> یا <code>7d</code>", parse_mode=ParseMode.HTML)
        return
    platform = detect_platform(url)
    if platform is None:
        await update.effective_message.reply_text("این لینک قابل زمان‌بندی نیست.")
        return
    existing = await store.list_jobs(user_id)
    if len(existing) >= SCHEDULER_MAX_JOBS_PER_USER:
        await update.effective_message.reply_text(f"حداکثر {SCHEDULER_MAX_JOBS_PER_USER} زمان‌بندی برای هر کاربر مجاز است.")
        return
    if any(job.get("url") == url for job in existing):
        await update.effective_message.reply_text("این لینک قبلاً زمان‌بندی شده است.")
        return
    await store.add_scheduled_job(
        user_id=user_id,
        chat_id=update.effective_chat.id,
        url=url,
        platform=platform.value,
        interval_minutes=interval,
        next_run_at=time.time() + interval * 60,
    )
    await update.effective_message.reply_text(
        f"✅ ثبت شد. دفعه بعد حدود <b>{interval} دقیقه</b> دیگر به‌صورت خودکار دانلود و همین‌جا ارسال می‌شود.",
        parse_mode=ParseMode.HTML,
    )


async def schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) >= 3 and parts[1] == "r":
        try:
            job_id = int(parts[2])
        except ValueError:
            return
        removed = await store.delete_job(job_id, update.effective_user.id)
        await _quiet_edit(query.message, "✅ زمان‌بندی حذف شد." if removed else "پیدا نشد.")
    elif len(parts) >= 2 and parts[1] == "x":
        try:
            await query.message.delete()
        except Exception:  # noqa: BLE001
            pass


async def scheduler_tick(
    runner: Callable[[dict[str, Any]], Awaitable[str]],
) -> None:
    """Fetch due scheduled jobs and hand each one to the bot's runner.

    ``runner`` receives the job row and returns a short status string stored
    in ``last_status``.
    """
    try:
        due = await store.due_jobs(time.time())
        for job in due or []:
            try:
                status = await runner(job)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Scheduled job %s failed: %s", job.get("id"), exc)
                status = f"error: {exc}"[:120]
            await store.update_job_run(
                int(job["id"]),
                next_run_at=time.time() + int(job.get("interval_minutes") or 1440) * 60,
                last_status=status,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("scheduler_tick error: %s", exc)


# ─────────────────────────── Duplicate detection ───────────────────────────

async def try_send_deduped(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    source_url: str,
    quality: str,
    reply_to: int | None,
) -> bool:
    """If this exact content was served before, resend it by file_id instantly."""
    if not (FLAGS.dedupe and source_url):
        return False
    fingerprint = store.dedupe_fingerprint(source_url, quality)
    hit = await store.dedupe_lookup(fingerprint)
    if not hit or not hit.get("file_id"):
        return False
    stored = str(hit["file_id"])
    kind, _, file_id = stored.partition("|")
    if not file_id:
        file_id, kind = stored, "document"
    caption = "⚡ از کش سرور — بدون دانلود مجدد"
    try:
        if kind == "photo":
            await context.bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption, reply_to_message_id=reply_to)
        elif kind == "audio":
            await context.bot.send_audio(chat_id=chat_id, audio=file_id, caption=caption, reply_to_message_id=reply_to)
        elif kind == "video":
            await context.bot.send_video(chat_id=chat_id, video=file_id, caption=caption, reply_to_message_id=reply_to)
        else:
            await context.bot.send_document(chat_id=chat_id, document=file_id, caption=caption, reply_to_message_id=reply_to)
    except Exception as exc:  # noqa: BLE001 — stale file_id etc.
        logger.info("Dedupe resend failed (%s); falling back to a fresh download", exc)
        return False
    await store.dedupe_hit(fingerprint)
    return True


def remember_media(
    source_url: str,
    quality: str,
    file_id: str | None,
    mime_type: str,
    size_bytes: int,
    media_kind: str,
) -> None:
    if not (FLAGS.dedupe and source_url and file_id):
        return

    async def _save() -> None:
        fingerprint = store.dedupe_fingerprint(source_url, quality)
        # media_dedupe has no kind column; encode it alongside the file id
        # (Telegram file ids never contain "|").
        await store.dedupe_save(
            fingerprint=fingerprint,
            source_url=source_url,
            quality=quality,
            file_id=f"{media_kind}|{file_id}",
            mime_type=mime_type,
            size_bytes=size_bytes,
        )

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_save())
    except RuntimeError:
        pass


# ───────────────────── Post-success hooks (stats/autoshare) ─────────────────────

async def after_success(
    context: ContextTypes.DEFAULT_TYPE,
    user_id: int,
    chat_id: int,
    source_url: str,
    platform_value: str,
    media: tuple[Any, ...],
    quality: str | None,
    file_ids: list[tuple[str, str, int, str]] | None = None,
) -> None:
    """Record stats, remember dedupe entries and fan out to auto-share targets."""
    total_bytes = sum(int(getattr(item, "size", 0) or 0) for item in media)
    if FLAGS.user_stats:
        kind = str(getattr(media[0], "kind", "") or "") if media else ""
        await store.record_download_event(
            user_id=user_id,
            platform=platform_value,
            media_kind=kind,
            size_bytes=total_bytes,
            request_id="",
        )
    if file_ids:
        primary = file_ids[0]
        remember_media(source_url, quality or "", primary[0], primary[1], primary[2], primary[3])
    if FLAGS.autoshare:
        targets = await store.list_autoshare_targets(user_id)
        for target in targets:
            if int(target.get("chat_id") or 0) == chat_id:
                continue
            for file_id, mime_type, size, kind in (file_ids or [])[:1]:
                try:
                    if kind == "photo":
                        await context.bot.send_photo(chat_id=int(target["chat_id"]), photo=file_id, caption="📤 اشتراک‌گذاری خودکار")
                    elif kind == "audio":
                        await context.bot.send_audio(chat_id=int(target["chat_id"]), audio=file_id, caption="📤 اشتراک‌گذاری خودکار")
                    elif kind == "video":
                        await context.bot.send_video(chat_id=int(target["chat_id"]), video=file_id, caption="📤 اشتراک‌گذاری خودکار")
                    else:
                        await context.bot.send_document(chat_id=int(target["chat_id"]), document=file_id, caption="📤 اشتراک‌گذاری خودکار")
                except Exception as exc:  # noqa: BLE001
                    logger.info("Autoshare to %s failed: %s", target.get("chat_id"), exc)


# ─────────────────────────── AI summarize / ask ───────────────────────────

def remember_ai_text(chat_id: int, text: str) -> None:
    if not (FLAGS.ai_summary and ai_service.ai_available()):
        return
    if text and len(text.strip()) >= 120:
        AI_TEXTS[chat_id] = (text.strip()[:8000], time.time() + AI_TEXT_TTL)


async def maybe_send_summarize_button(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    reply_to: int | None,
    text: str,
) -> None:
    """Offer a 🤖 خلاصه فارسی button under freshly delivered text content."""
    if not (FLAGS.ai_summary and ai_service.ai_available()):
        return
    remember_ai_text(chat_id, text)
    if chat_id not in AI_TEXTS:
        return
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text="🤖 خلاصه فارسی این محتوا را می‌خواهی؟",
            reply_to_message_id=reply_to,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🤖 خلاصه کن", callback_data="ai:sum")]]
            ),
        )
    except Exception:  # noqa: BLE001
        pass


async def ai_summary_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("🤖 در حال خلاصه‌سازی…")
    chat_id = update.effective_chat.id
    entry = AI_TEXTS.get(chat_id)
    if not entry or entry[1] < time.time():
        await context.bot.send_message(chat_id=chat_id, text="⌛ مهلت خلاصه‌سازی تمام شده؛ دوباره محتوا را بفرست.")
        return
    text = entry[0]
    try:
        await query.edit_message_text("🤖 در حال خلاصه‌سازی… چند لحظه صبر کن.")
    except Exception:  # noqa: BLE001
        pass
    summary = await ai_service.summarize_persian(text)
    if not summary:
        await context.bot.send_message(chat_id=chat_id, text="⌛ سرویس هوش مصنوعی موقتاً در دسترس نیست؛ کمی بعد دوباره تلاش کن.")
        return
    AI_TEXTS.pop(chat_id, None)
    chunks = [summary[i:i + _MAX_TEXT_LENGTH] for i in range(0, len(summary), _MAX_TEXT_LENGTH)]
    for chunk in chunks:
        await context.bot.send_message(chat_id=chat_id, text=chunk)


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not FLAGS.ai_summary:
        await update.effective_message.reply_text("این قابلیت فعلاً غیرفعال است.")
        return
    question = " ".join(context.args or []).strip()
    if not question:
        await update.effective_message.reply_text("سوالت را بپرس:\n<code>/ask چطوری کیفیت انتخاب کنم؟</code>", parse_mode=ParseMode.HTML)
        return
    if not ai_service.ai_available():
        await update.effective_message.reply_text(
            "دستیار هوشمند فعال نیست.\n"
            "برای فعال‌سازی، در تنظیمات ربات (Variables) مقدار <code>AI_PROVIDER=auto</code> و "
            "<code>AI_API_KEY</code> را با کلید رایگان HuggingFace/Cohere/Mistral پر کنید و یک بار ربات را ری‌استارت کنید."
            ,
            parse_mode=ParseMode.HTML,
        )
        return
    answer = await ai_service.faq_answer(question, bot_help_text=BOT_HELP_CONTEXT)
    if not answer:
        await update.effective_message.reply_text("پاسخ قطعی پیدا نکردم؛ راهنمای کامل: /help")
        return
    await update.effective_message.reply_text(f"🤖 {answer}"[:4000])
