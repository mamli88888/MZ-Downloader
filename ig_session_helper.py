#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ig_session_helper.py — اتصال پل دایرکت بدون لاگینِ رمزی (روش sessionid).

چرا sessionid؟
    لاگین رمزی اینستاگرام از IP دیتاسنتر (Railway) و حتی از IP های VPN رد
    می‌شود (BadPassword/429). ولی مرورگر خودت که هر روز پیج را در آن باز
    می‌کنی، یک «سشن معتبر و قابل‌اعتماد» دارد. با کپی کردن کوکی sessionid
    از همان مرورگر، ربات از همان سشن استفاده می‌کند — بدون اینکه هیچ جا
    رمز عبور لاگین شود.

اجرا:
    pip install "instagrapi==2.18.18" pysocks
    python ig_session_helper.py

خروجی موفق:
    - فایل ig_session.json
    - مقدار IG_SESSIONID برای Railway (همان متن کپی‌شده از مرورگر)
    - (اختیاری) مقدار IG_SESSION_B64

نکتهٔ امنیتی: sessionid = کلید ورود پیج تو است؛ فقط در Railway خودت بگذارش
و با کسی به اشتراک نگذار. اگر روزی خواستی اتصال را قطع کنی، از اینستاگرام
«Log out» بزن تا سشن باطل شود.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import sys
from pathlib import Path

try:
    from instagrapi import Client  # type: ignore
    import instagrapi.exceptions as ig_exc  # type: ignore
except ImportError:
    print("❌ instagrapi نصب نیست. اول این را اجرا کن:")
    print('   pip install "instagrapi==2.18.18" pysocks')
    sys.exit(1)

SESSION_FILE = Path(os.getenv("IG_DM_SESSION_FILE", "ig_session.json"))
DIAG_FILE = Path("ig_login_diag.txt")

MASK_RE = re.compile(r"(\w{8})\w{10,}(\w{4})")


def mask(value: str) -> str:
    """ماسک کردن مقادیر حساس در فایل خطایابی."""
    return MASK_RE.sub(r"\1***\2", value)


def save_diag(stage: str, exc: BaseException, cl: Client | None = None) -> None:
    """ذخیرهٔ جزئیات خطا برای ارسال به دستیار."""
    lines = [
        f"stage: {stage}",
        f"exception: {type(exc).__name__}: {exc}",
    ]
    try:
        last_json = getattr(cl, "last_json", None) if cl else None
        if last_json:
            lines.append("last_json: " + mask(json.dumps(last_json, ensure_ascii=False, default=str)))
        error_type = ""
        if isinstance(last_json, dict):
            error_type = str(last_json.get("error_type", "") or last_json.get("message", ""))
        if error_type:
            lines.append(f"error_type: {mask(error_type)}")
    except Exception:  # noqa: BLE001
        pass
    DIAG_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📄 جزئیات فنی خطا در «{DIAG_FILE.resolve()}» ذخیره شد — اگر خواستی کمک بگیری، همین فایل را برایش بفرست.")


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def ask_proxy() -> str:
    print("\nاگر ترمینال/پایتون به اینستاگرام وصل نمی‌شود (مثلاً VPNت فقط مرورگر را رد می‌کند)،")
    print("پراکسی همان VPN را بده؛ مثل: socks5://127.0.0.1:10808 یا http://127.0.0.1:8888")
    proxy = ask("پراکسی (Enter = بدون پراکسی)", "")
    return proxy


def build_client(proxy: str) -> Client:
    cl = Client()
    cl.delay_range = [1, 3]
    if proxy:
        cl.set_proxy(proxy)
    return cl


def print_success(cl: Client, sessionid_hint: str) -> None:
    username = ""
    try:
        username = getattr(cl.account_info(), "username", "") or ""
    except Exception:  # noqa: BLE001
        username = str(getattr(cl, "username", "") or "")
    print("\n" + "=" * 62)
    print(f"✅ اتصال برقرار شد! پیج: @{username or '?'}")
    print("=" * 62)
    cl.dump_settings(str(SESSION_FILE))
    print(f"📁 سشن ذخیره شد: {SESSION_FILE.resolve()}")
    print("\nقدم بعدی — در Railway → Variables فقط این را بساز:")
    print("-" * 62)
    print(f"IG_SESSIONID = {sessionid_hint}")
    print("-" * 62)
    print("بعد Deploy بزن. در لاگ باید ببینی:")
    print("  ig-dm: enabled (page=@..., auth=sessionid, ...)")
    print("  ig-dm: login ok (session=sessionid, ...)")
    print("✅ تمام! دیگر هیچ لاگین رمزی لازم نیست.")
    print("\n(اختیاری) اگر خواستی سشن کامل را هم داشته باشی، IG_SESSION_B64:")
    raw = SESSION_FILE.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    for i in range(0, len(b64), 96):
        print(b64[i : i + 96])


def flow_sessionid() -> None:
    print(
        "\n── مراحل گرفتن sessionid از مرورگر (کامپیوتر) ──\n"
        "  1) در کروم/فایرفاکس برو به  instagram.com  و مطمئن شو پیج‌ات لوگین است\n"
        "  2) دکمه F12 را بزن (DevTools باز می‌شود)\n"
        "  3) تب  Application  (در فایرفاکس: Storage) را باز کن\n"
        "  4) از منوی چپ:  Cookies  →  https://www.instagram.com\n"
        "  5) از لیست، ردیف  sessionid  را پیدا کن و مقدار (Value) آن را کپی کن\n"
        "     (رشتهٔ خیلی طولانی که با چند رقم شروع می‌شود، وسطش %3A دارد)\n"
    )
    sessionid = ask("sessionid کپی‌شده را اینجا بچسبان").strip().strip('"').strip("'")
    if not sessionid:
        print("❌ sessionid خالی بود.")
        return save_diag("sessionid-empty", ValueError("empty sessionid"))
    if len(sessionid) < 30:
        print("❌ این مقدار خیلی کوتاه است — کل Value کوکی sessionid را کپی کن (چند صد کاراکتر).")
        return save_diag("sessionid-too-short", ValueError(f"len={len(sessionid)}"))
    proxy = ask_proxy()
    cl = build_client(proxy)
    try:
        cl.login_by_sessionid(sessionid)
        # اعتبارسنجی سخت: سشن باید واقعاً زنده باشد.
        # (در instagrapi 2.18 متد current_user حذف شده؛ account_info جای آن است)
        cl.account_info()
    except Exception as exc:  # noqa: BLE001
        print(f"\n❌ این sessionid پذیرفته نشد: {type(exc).__name__}: {str(exc)[:200]}")
        print("   چک کن: کل Value را کپی کرده باشی، مرورگر همان پیج را داشته باشد،")
        print("   و پیج داخل مرورگر لوگین/سالم باشد (صفحه را رفرش کن و مطمئن شو لاگین ماندی).")
        if proxy:
            print("   اگر پراکسی دادی، آدرس/پورتش درست باشد و VPN روشن باشد.")
        return save_diag("sessionid-login", exc, cl)
    print_success(cl, sessionid)


def flow_password() -> None:
    print("\n(این روش فقط وقتی sessionid کار نکرد؛ روی IP خانگی بدون VPN امتحان کن)")
    username = ask("یوزرنیم پیج", os.getenv("IG_USERNAME", "")).lstrip("@")
    password = os.getenv("IG_PASSWORD", "") or getpass.getpass("رمز پیج: ")
    totp_secret = (
        ask("سکرت 2FA (Enter = ندارم)", os.getenv("IG_TOTP_SECRET", ""))
        .replace(" ", "")
        .replace("-", "")
    )
    proxy = ask_proxy()
    cl = build_client(proxy)

    def totp_code(secret: str) -> str:
        import hashlib
        import hmac as hmac_mod
        import struct
        import time

        key = base64.b32decode(secret.upper() + "=" * ((8 - len(secret) % 8) % 8))
        counter = int(time.time()) // 30
        digest = hmac_mod.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
        offset = digest[19] & 0x0F
        value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
        return f"{value % 1_000_000:06d}"

    def try_login(client: Client, code: str = "") -> bool:
        try:
            client.login(username, password, verification_code=code)
            return True
        except ig_exc.TwoFactorRequired:
            if code:
                print("❌ کد 2FA پذیرفته نشد. دوباره امتحان کن.")
                return False
            print("🔐 این پیج 2FA دارد.")
            manual = input("کد 6 رقمی اپ Authenticator را وارد کن (Enter = تولید خودکار از سکرت): ").strip()
            return try_login(client, manual or (totp_code(totp_secret) if totp_secret else ""))
        except ig_exc.ChallengeRequired:
            print(
                "🚧 اینستاگرام چالش تأیید داده (کد پیامکی/ایمیلی یا «این تو بودی؟»)."
                "\n   1) با همان مرورگر وارد instagram.com شو — معمولاً خودش می‌پذیرد."
                "\n   2) بعد از تأیید، دیگر نیازی به این روش نداری: همان مراحل گزینه ۱ (sessionid) را برو."
            )
            return False
        except ig_exc.BadPassword:
            print("❌ رمز اشتباه است (یا اینستاگرام قبول نکرد).")
            print("   راه بهتر: از مرورگر که پیج در آن باز است، گزینهٔ ۱ (sessionid) را برو — بدون رمز!")
            return False

    if not try_login(cl):
        return save_diag("password-login", Exception("login failed"), cl)
    print_success(cl, "(همان sessionid مرورگر را در IG_SESSIONID بگذار — روش مطمئن‌تر)")


def main() -> None:
    print("── اتصال پل دایرکت اینستاگرام بدون لاگین رمزی ──\n")
    print("  1) ورود با sessionid مرورگر   ← پیشنهادی (بدون رمز، از هر IPای جواب می‌دهد)")
    print("  2) ورود با رمز عبور          ← فقط وقتی گزینهٔ ۱ کار نکرد")
    choice = ask("\nانتخاب", "1")
    if choice == "2":
        flow_password()
    else:
        flow_sessionid()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nلغو شد.")
        sys.exit(130)
