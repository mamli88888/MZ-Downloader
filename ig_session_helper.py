#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ig_session_helper.py — ساخت سشن اینستاگرام روی سیستم خودت (IP خانگی).

چرا این اسکریپت؟
    لاگین اینستاگرام از IP دیتاسنتر Railway معمولاً رد می‌شود (BadPassword/429)
    چون اینستاگرام IP کلود را باور نمی‌کند. راه‌حل: یک‌بار روی کامپیوتر خودت
    (با IP خانگی) لاگین کن، سشن را ذخیره کن و به‌صورت base64 در متغیر
    IG_SESSION_B64 به Railway بده. از این به بعد ربات «بدون لاگینِ مجدد»
    با همین سشن کار می‌کند.

اجرا:
    pip install "instagrapi==2.18.18" pysocks
    python ig_session_helper.py

خروجی:
    - فایل ig_session.json در همین پوشه
    - متن base64 که باید در Railway به متغیر IG_SESSION_B64 داده شود

نکتهٔ امنیتی: این فایل حاوی کوکی‌های ورود پیج است؛ فقط در Railway خودت
بگذارش و با کسی به اشتراک نگذار. برای خروج، از اینستاگرام روی همان پیج
«Log out of all sessions» بزن.
"""

from __future__ import annotations

import base64
import getpass
import os
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


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def save_and_print_b64(cl: Client) -> None:
    cl.dump_settings(str(SESSION_FILE))
    raw = SESSION_FILE.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    print("\n" + "=" * 62)
    print(f"✅ سشن ذخیره شد: {SESSION_FILE.resolve()}")
    print("=" * 62)
    print("این متن را کامل کپی کن و در Railway به متغیر زیر بده:")
    print("   IG_SESSION_B64 = (این متن)")
    print("-" * 62)
    for i in range(0, len(b64), 96):
        print(b64[i : i + 96])
    print("=" * 62)
    print("قدم بعدی در Railway (Variables):")
    print("  1) IG_SESSION_B64 را با متن بالا بساز")
    print("  2) Deploy دوباره بزن")
    print("  3) در لاگ باید ببینی: «ig-dm: session seeded from IG_SESSION_B64»")
    print("     و بعد: «ig-dm: login ok (session=file ...)»")
    print("✅ با این کار دیگر روی Railway لاگینِ پسوردی زده نمی‌شود.")


def main() -> None:
    print("── ساخت سشن اینستاگرام برای پل دایرکت MZ-Downloader ──\n")
    username = ask("یوزرنیم پیج", os.getenv("IG_USERNAME", "")).lstrip("@")
    password = os.getenv("IG_PASSWORD", "") or getpass.getpass("رمز پیج: ")
    totp_secret = (
        ask("سکرت 2FA (اگر داری؛ Enter یعنی ندارم)", os.getenv("IG_TOTP_SECRET", ""))
        .replace(" ", "")
        .replace("-", "")
    )
    proxy = ask("پراکسی (معمولاً لازم نیست روی IP خانگی؛ Enter = بدون پراکسی)", "")

    def build() -> Client:
        cl = Client()
        cl.delay_range = [1, 3]
        if proxy:
            cl.set_proxy(proxy)
        return cl

    # ۱) اول با سشن قبلی (اگر هست) امتحان کن تا دستگاه ثابت بماند
    cl = build()
    if SESSION_FILE.exists():
        try:
            cl.load_settings(str(SESSION_FILE))
            print("📁 سشن قبلی بارگذاری شد؛ اعتبارسنجی می‌کنم…")
        except Exception as exc:  # noqa: BLE001
            print(f"⚠️ سشن قبلی نامعتبر بود ({exc})؛ لاگین تازه می‌زنم.")

    def try_login(client: Client, code: str = "") -> bool:
        try:
            client.login(username, password, verification_code=code)
            return True
        except ig_exc.TwoFactorRequired:
            if code:
                print("❌ کد 2FA پذیرفته نشد. دوباره امتحان کن.")
                return False
            print("🔐 این پیج 2FA دارد.")
            if not totp_secret:
                manual = input("کد 6 رقمی اپ Authenticator را وارد کن: ").strip()
                return try_login(client, manual)
            import hashlib
            import hmac as hmac_mod
            import struct
            import time

            key = base64.b32decode(totp_secret.upper() + "=" * ((8 - len(totp_secret) % 8) % 8))
            counter = int(time.time()) // 30
            digest = hmac_mod.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
            offset = digest[19] & 0x0F
            value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
            generated = f"{value % 1_000_000:06d}"
            print(f"🔑 کد TOTP تولید شد: {generated}")
            return try_login(client, generated)
        except ig_exc.ChallengeRequired:
            print(
                "🚧 اینستاگرام چالش تأیید داده (مثلاً کد پیامکی/ایمیلی).\n"
                "   یک‌بار با اپ یا مرورگر خودت وارد پیج شو، تأیید را کامل کن،\n"
                "   بعد دوباره این اسکریپت را اجرا کن."
            )
            return False
        except ig_exc.BadPassword:
            print("❌ رمز اشتباه است (یا اینستاگرام قبول نکرد). دوباره چک کن.")
            return False

    if not try_login(cl):
        sys.exit(1)
    try:
        info = cl.account_info()
        print(f"✅ لاگین موفق: @{info.username} (pk={info.pk})")
    except Exception as exc:  # noqa: BLE001
        print(f"⚠️ لاگین انجام شد ولی گرفتن مشخصات اکانت خطا داد: {exc}")

    save_and_print_b64(cl)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nلغو شد.")
        sys.exit(130)
