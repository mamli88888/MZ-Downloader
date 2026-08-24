"""Broken-Apify-token alerting for the bot administrator.

Flow (operator spec):
  * The moment a token fails and the gateway switches to the next token, a
    private (PV) message is sent to the main admin containing: the failing
    token owner's e-mail, the requested platform, the exact error time and
    the error type (timeout / unauthorized / rate limit / billing / …).
  * If the admin does not read the message within the first 15 minutes, no
    additional action is taken (the first reminder only fires AT 15 minutes).
  * Unread alerts are re-sent every 15 minutes, at most 5 times (75 minutes
    total), then the cycle stops.
  * Reading/acknowledging any message stops the cycle.

Telegram's Bot API does not expose read ("seen") receipts for bot messages,
so "read" is detected through the strongest available signals: an explicit
✅ ack button on the alert, or any interaction the admin performs with the
bot (every update from the admin's user ID marks all open alerts as read).
Token status transitions (active / suspect / broken) are persisted in the
additive SQLite store and rendered by the admin /tokens dashboard.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import telegram
from telegram.constants import ParseMode

import store
from feature_flags import FLAGS

logger = logging.getLogger("MZDownloader.token_alerts")

TEHRAN = ZoneInfo("Asia/Tehran")
REMINDER_INTERVAL_SECONDS = 15 * 60
MAX_REMINDERS = 5
OWNER_CACHE_TTL = 24 * 3600

_ERROR_TYPE_FA = {
    "unauthorized": "عدم احراز هویت (Unauthorized)",
    "billing": "مشکل اعتبار/پرداخت (Billing)",
    "rate_limit": "محدودیت نرخ (Rate limit)",
    "timeout": "تایم‌اوت",
    "server": "خطای سرور Apify",
    "actor": "خطای اجرای Actor",
    "network": "خطای شبکه",
    "unknown": "نامشخص",
}

_state: dict[str, Any] = {
    "bot": None,
    "admin_chat_id": None,
    "tokens": (),           # tuple[str, ...] mirroring the gateway tokens
    "api_base": "https://api.apify.com/v2",
    "owner_cache": {},      # token_index -> (email, username, fetched_at)
    "loop_task": None,
}


def initialize(
    bot: telegram.Bot | None,
    admin_chat_id: int | None,
    tokens: tuple[str, ...] = (),
    api_base: str = "https://api.apify.com/v2",
) -> None:
    _state["bot"] = bot
    _state["admin_chat_id"] = admin_chat_id if admin_chat_id and admin_chat_id > 0 else None
    _state["tokens"] = tuple(tokens)
    _state["api_base"] = api_base.rstrip("/")
    if FLAGS.token_alerts and _state["admin_chat_id"] is None:
        logger.warning("TOKEN_ALERTS enabled but BOT_ADMIN_CHAT_ID is not set — PV alerts cannot be delivered")
    if FLAGS.token_alerts and _state["bot"] is not None and _state["admin_chat_id"] is not None:
        for index, token in enumerate(_state["tokens"]):
            asyncio.get_running_loop().create_task(
                store.upsert_token(token_hash(token), token_label=f"token-{index + 1}")
            )


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def classify_error(status_code: int | None, message: str) -> str:
    text = (message or "").lower()
    if status_code in {401, 403} or "unauthorized" in text or "invalid token" in text:
        return "unauthorized"
    if status_code == 402 or "billing" in text or "payment" in text or "credit" in text or "quota" in text:
        return "billing"
    if status_code == 429 or "rate limit" in text:
        return "rate_limit"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if status_code is not None and 500 <= status_code < 600:
        return "server"
    if "actor" in text or "succeed" in text:
        return "actor"
    if "network" in text or "connect" in text or "dns" in text:
        return "network"
    return "unknown"


def _fa_time(epoch: float | None = None) -> str:
    moment = datetime.fromtimestamp(epoch if epoch is not None else time.time(), tz=TEHRAN)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


async def _token_owner(token_index: int) -> tuple[str, str]:
    """Best-effort (email, username) of a token's owner via Apify /users/me."""
    cached = _state["owner_cache"].get(token_index)
    if cached and time.time() - cached[2] < OWNER_CACHE_TTL:
        return cached[0], cached[1]
    token = _state["tokens"][token_index] if 0 <= token_index < len(_state["tokens"]) else ""
    if not token:
        return "", ""
    try:
        from perf import pooled_client

        client = pooled_client("apify-admin")
        response = await asyncio.wait_for(
            client.get(
                f"{_state['api_base']}/users/me",
                headers={"Authorization": f"Bearer {token}"},
            ),
            timeout=12.0,
        )
        if response.status_code == 200:
            data = response.json().get("data") or {}
            email = str(data.get("email") or "").strip()
            username = str(data.get("username") or "").strip()
            _state["owner_cache"][token_index] = (email, username, time.time())
            return email, username
    except Exception as exc:  # noqa: BLE001 — best effort only
        logger.debug("Token owner lookup failed for #%d: %s", token_index + 1, exc)
    return "", ""


async def on_token_failure(
    token_index: int,
    platform_label: str,
    error_message: str,
    status_code: int | None = None,
) -> None:
    """Gateway hook: a token failed and rotation moved to the next token."""
    error_type = classify_error(status_code, error_message)
    tokens: tuple[str, ...] = _state["tokens"]
    token = tokens[token_index] if 0 <= token_index < len(tokens) else ""
    digest = token_hash(token) if token else f"idx-{token_index}"
    email, username = await _token_owner(token_index) if token else ("", "")

    await store.mark_token_result(
        digest,
        ok=False,
        error_type=error_type,
        error_message=(error_message or "")[:500],
        owner_email=email,
        token_label=f"token-{token_index + 1}",
    )

    if not FLAGS.token_alerts:
        return
    bot = _state["bot"]
    admin_chat_id = _state["admin_chat_id"]
    if bot is None or admin_chat_id is None:
        return

    alert_id = await store.create_alert(
        token_hash=digest,
        owner_email=email,
        platform=platform_label,
        error_type=error_type,
        error_message=(error_message or "")[:500],
    )
    now = time.time()
    owner_line = html.escape(email) if email else (html.escape(username) or "نامشخص")
    body = (
        "🚨 <b>توکن Apify خطا داد و ربات به توکن بعدی سوئیچ کرد</b>\n\n"
        f"👤 مالک توکن: <b>{owner_line}</b>\n"
        f"🔢 توکن: <code>#{token_index + 1} · {digest[:8]}</code>\n"
        f"🌐 پلتفرم درخواستی: <b>{html.escape(platform_label)}</b>\n"
        f"🕒 زمان دقیق خطا: <code>{_fa_time(now)}</code>\n"
        f"❗ نوع خطا: <b>{_ERROR_TYPE_FA.get(error_type, error_type)}</b>"
        + (f" (HTTP {status_code})" if status_code else "")
        + "\n"
        f"📄 جزئیات: <i>{html.escape((error_message or '')[:180])}</i>\n\n"
        "✅ دانلود کاربر با توکن بعدی ادامه یافت."
    )
    keyboard = telegram.InlineKeyboardMarkup(
        [[telegram.InlineKeyboardButton("✅ دیدم / رسیدگی شد", callback_data=f"ack:{alert_id}")]]
    )
    try:
        await bot.send_message(
            chat_id=admin_chat_id,
            text=body,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        await store.set_alert_sent(alert_id, sent_at=now, next_reminder_at=now + REMINDER_INTERVAL_SECONDS)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not deliver token alert PV: %s", exc)
        await store.set_alert_sent(alert_id, sent_at=time.time(), next_reminder_at=time.time() + REMINDER_INTERVAL_SECONDS)


async def on_token_success(token_index: int) -> None:
    tokens: tuple[str, ...] = _state["tokens"]
    token = tokens[token_index] if 0 <= token_index < len(tokens) else ""
    if not token:
        return
    digest = token_hash(token)
    await store.mark_token_result(digest, ok=True)
    # A recovered token resolves its open alert cycle.
    open_alerts = await store.open_alerts()
    recovered_ids = [alert["id"] for alert in open_alerts if alert.get("token_hash") == digest]
    if recovered_ids:
        await store.ack_alerts(recovered_ids)
        bot = _state["bot"]
        admin_chat_id = _state["admin_chat_id"]
        if FLAGS.token_alerts and bot is not None and admin_chat_id is not None:
            try:
                await bot.send_message(
                    chat_id=admin_chat_id,
                    text=(
                        "🟢 <b>توکن Apify دوباره سالم شد</b>\n"
                        f"🔢 توکن: <code>#{token_index + 1} · {digest[:8]}</code>\n"
                        f"🕒 {_fa_time()}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except Exception:  # noqa: BLE001
                pass


async def handle_ack_callback(update: telegram.Update, context: Any) -> None:
    """Inline ✅ button on an alert message."""
    query = update.callback_query
    data = (query.data or "")
    try:
        alert_id = int(data.split(":", 1)[1])
    except (IndexError, ValueError):
        await query.answer("شناسه نامعتبر است.", show_alert=True)
        return
    await store.ack_alerts([alert_id])
    await query.answer("ثبت شد؛ چرخه یادآوری متوقف شد ✅")
    try:
        await query.edit_message_text(
            query.message.text_html + "\n\n✅ <b>به‌عنوان خوانده‌شده ثبت شد</b>",
            parse_mode=ParseMode.HTML,
        )
    except Exception:  # noqa: BLE001
        pass


async def mark_admin_seen(user_id: int) -> None:
    """Any interaction from the admin implies pending PVs were read."""
    admin_chat_id = _state["admin_chat_id"]
    if admin_chat_id is None or user_id != admin_chat_id:
        return
    count = await store.ack_alerts_for_admin()
    if count:
        logger.info("Admin activity auto-acked %d open token alert(s)", count)


async def alerts_loop() -> None:
    """Every 60s: re-send unread alerts whose reminder is due (≤5 times)."""
    while True:
        try:
            due = await store.due_reminders(time.time())
            for alert in due:
                bot = _state["bot"]
                admin_chat_id = _state["admin_chat_id"]
                if bot is None or admin_chat_id is None:
                    break
                reminder_no = int(alert.get("reminders_sent", 0)) + 1
                text = (
                    f"⏰ <b>یادآوری {reminder_no} از {MAX_REMINDERS} — توکن خراب Apify</b>\n"
                    f"👤 مالک: <b>{html.escape(alert.get('owner_email') or 'نامشخص')}</b>\n"
                    f"🌐 پلتفرم: <b>{html.escape(alert.get('platform') or '-')}</b>\n"
                    f"❗ نوع خطا: <b>{_ERROR_TYPE_FA.get(alert.get('error_type', ''), alert.get('error_type', '-'))}</b>\n"
                    f"🕒 زمان خطا: <code>{_fa_time(alert.get('first_seen_at'))}</code>"
                )
                keyboard = telegram.InlineKeyboardMarkup(
                    [[telegram.InlineKeyboardButton("✅ دیدم / رسیدگی شد", callback_data=f"ack:{alert['id']}")]]
                )
                try:
                    await bot.send_message(
                        chat_id=admin_chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        reply_markup=keyboard,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Reminder delivery failed for alert %s: %s", alert.get("id"), exc)
                await store.bump_alert_reminder(
                    int(alert["id"]), time.time() + REMINDER_INTERVAL_SECONDS
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("alerts_loop iteration error: %s", exc)
        await asyncio.sleep(60)


def start() -> None:
    if _state["loop_task"] is None or _state["loop_task"].done():
        _state["loop_task"] = asyncio.get_running_loop().create_task(alerts_loop(), name="token-alerts")


async def stop() -> None:
    task = _state["loop_task"]
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        _state["loop_task"] = None


_STATUS_FA = {"active": "🟢 فعال", "suspect": "🟡 مشکوک", "broken": "🔴 خراب"}


async def tokens_dashboard_text() -> str:
    """Admin dashboard: per-token status + open alerts summary (/tokens)."""
    rows = await store.token_statuses()
    lines = ["🧾 <b>داشبورد وضعیت توکن‌های Apify</b>", ""]
    if not rows:
        lines.append("توکنی ثبت نشده است.")
    for index, row in enumerate(rows, start=1):
        email = row.get("owner_email") or ""
        label = row.get("token_label") or f"token-{index}"
        status = _STATUS_FA.get(row.get("status", ""), row.get("status", "-"))
        lines.append(
            f"{status} <code>{html.escape(label)} · {str(row.get('token_hash', ''))[:8]}</code>"
            + (f"\n     👤 {html.escape(email)}" if email else "")
            + (
                f"\n     ❗ {html.escape((row.get('last_error_type') or ''))} · {html.escape((row.get('last_error') or '')[:60])}"
                if row.get("last_error")
                else ""
            )
            + (
                f"\n     🕒 آخرین خطا: {_fa_time(row.get('last_error_at'))} · تعداد: {row.get('fail_count', 0)}"
                if row.get("last_error_at")
                else ""
            )
        )
    open_alerts = await store.open_alerts()
    lines += ["", f"⏳ هشدارهای باز (خوانده‌نشده): <b>{len(open_alerts)}</b>"]
    for alert in open_alerts[:5]:
        lines.append(
            f"  • #{alert.get('id')} {html.escape(alert.get('platform') or '')} · "
            f"{_ERROR_TYPE_FA.get(alert.get('error_type', ''), alert.get('error_type', '-'))} · "
            f"یادآوری: {alert.get('reminders_sent', 0)}/{MAX_REMINDERS}"
        )
    return "\n".join(lines)
