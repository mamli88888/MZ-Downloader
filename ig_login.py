#!/usr/bin/env python3
"""One-time Instagram login helper for the IG bridge.

Run this script locally (NOT on the production server) to:
  1. Log in to your Instagram page account interactively.
  2. Handle any 2FA / challenge prompts.
  3. Save the session to `ig_session.json`.
  4. Copy `ig_session.json` to your server.

Usage:
    python3 ig_login.py

Environment variables (read from .env or shell):
    IG_BRIDGE_USERNAME   — your Instagram page username (required)
    IG_BRIDGE_PASSWORD   — your Instagram page password (required for first login)
    IG_BRIDGE_SESSION_FILE — path to save session (default: ig_session.json)
    IG_BRIDGE_PROXY      — optional proxy URL (STRONGLY RECOMMENDED in Iran)
    IG_BRIDGE_IMPERSONATE — curl_cffi impersonate target (default: chrome120)

If you hit `SSLError(SSLEOFError(...))` during login, it means Instagram
rejected your TLS fingerprint. Fix:
    pip install curl_cffi
and (if in Iran/China/Russia) set IG_BRIDGE_PROXY to a working proxy.
See ig_session.py:print_troubleshooting_hint() for full details.
"""

from __future__ import annotations

import getpass
import os
import sys
from pathlib import Path

# Load .env if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass


def main() -> int:
    try:
        from instagrapi import Client  # noqa: F401  (used by ig_session.build_client)
    except ImportError:
        print("ERROR: instagrapi is not installed. Run: pip install instagrapi", file=sys.stderr)
        return 1

    # Check for curl_cffi and warn if missing
    try:
        import curl_cffi  # noqa: F401
        have_curl_cffi = True
    except ImportError:
        have_curl_cffi = False
        print(
            "WARNING: curl_cffi is not installed. Instagram will probably reject\n"
            "your TLS handshake with SSLEOFError. Fix with:\n"
            "    pip install curl_cffi\n",
            file=sys.stderr,
        )

    username = os.environ.get("IG_BRIDGE_USERNAME", "").strip()
    password = os.environ.get("IG_BRIDGE_PASSWORD", "").strip()
    session_file = os.environ.get("IG_BRIDGE_SESSION_FILE", "ig_session.json")
    proxy = os.environ.get("IG_BRIDGE_PROXY", "").strip()
    impersonate = os.environ.get("IG_BRIDGE_IMPERSONATE", "chrome120").strip() or None

    if not username:
        username = input("Instagram username: ").strip()
    if not password:
        password = getpass.getpass("Instagram password: ").strip()
    if not username or not password:
        print("ERROR: username and password are required.", file=sys.stderr)
        return 1

    if not proxy:
        print(
            "NOTE: no proxy set. If you are in Iran/China/Russia, Instagram\n"
            "may kill the connection mid-handshake. Set IG_BRIDGE_PROXY to a\n"
            "working proxy/VPN URL if login fails.\n",
            file=sys.stderr,
        )

    # Resolve session file path relative to this script
    session_path = Path(session_file)
    if not session_path.is_absolute():
        session_path = Path(__file__).resolve().parent / session_path

    # Use the robust session builder (curl_cffi when available)
    import ig_session
    cl = ig_session.build_client(proxy=proxy or None, impersonate=impersonate)
    if proxy:
        print(f"Using proxy: {proxy}")
    if have_curl_cffi:
        print(f"Using curl_cffi impersonate={impersonate or 'chrome120'}")

    print(f"Logging in as @{username} ...")
    try:
        cl.login(username, password)
    except Exception as exc:
        # Print SSL troubleshooting hint if applicable
        ig_session.print_troubleshooting_hint(exc)

        # Check for 2FA challenge
        msg = str(exc).lower()
        if "two" in msg or "2fa" in msg or "verification" in msg:
            print("\n2FA verification required.")
            code = input("Enter the 2FA code sent to your device: ").strip()
            try:
                cl.two_factor_login(code)
            except Exception as e2:
                ig_session.print_troubleshooting_hint(e2)
                print(f"ERROR: 2FA login failed: {e2}", file=sys.stderr)
                return 1
        elif "challenge" in msg:
            print("\nInstagram is challenging this login (probably a new IP/device).")
            print("Check your email/phone for a verification code.")
            code = input("Enter the challenge code: ").strip()
            try:
                cl.challenge_code_send(username)
                cl.challenge_code_verify(code)
            except Exception as e2:
                ig_session.print_troubleshooting_hint(e2)
                print(f"ERROR: challenge failed: {e2}", file=sys.stderr)
                print("Tip: try logging in to instagram.com in a browser first, then re-run this script.")
                return 1
        else:
            print(f"ERROR: login failed: {exc}", file=sys.stderr)
            return 1

    # Verify login worked
    try:
        me = cl.user_info_by_username(username)
        print(f"\n✅ Login successful! Logged in as @{me.username} (user_id={cl.user_id}).")
    except Exception as exc:
        print(f"ERROR: login verification failed: {exc}", file=sys.stderr)
        return 1

    # Save session
    try:
        cl.dump_settings(str(session_path))
        print(f"\n✅ Session saved to: {session_path}")
        print("\nNext steps:")
        print("  1. Copy this file to your server (same directory as bot.py).")
        print("  2. Set IG_BRIDGE_ENABLED=true in your .env")
        print("  3. Set IG_BRIDGE_USERNAME to the same username you used here.")
        print("  4. Restart the bot.")
        print("\nNote: Do NOT commit ig_session.json to git. It contains your auth tokens.")
    except Exception as exc:
        print(f"ERROR: failed to save session: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
