from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import socks
from telethon import TelegramClient

from config import SETTINGS


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def proxy_config():
    if not SETTINGS.use_proxy:
        return None
    kind = socks.SOCKS5 if SETTINGS.proxy_type == "socks5" else socks.HTTP
    return kind, SETTINGS.proxy_host, SETTINGS.proxy_port


def describe(message):
    document = getattr(message, "document", None)
    attributes = getattr(document, "attributes", None) or ()
    dimensions = [
        (getattr(item, "w", None), getattr(item, "h", None), getattr(item, "duration", None))
        for item in attributes
        if getattr(item, "w", None) or getattr(item, "h", None) or getattr(item, "duration", None)
    ]
    buttons = [
        str(getattr(button, "text", "") or "")
        for row in (getattr(message, "buttons", None) or ())
        for button in row
    ]
    text = str(getattr(message, "raw_text", "") or "").replace("\n", " ")[:100]
    file_obj = getattr(message, "file", None)
    return {
        "id": message.id,
        "out": bool(getattr(message, "out", False)),
        "reply_to": getattr(message, "reply_to_msg_id", None),
        "media": type(getattr(message, "media", None)).__name__,
        "mime": getattr(file_obj, "mime_type", None),
        "name": Path(str(getattr(file_obj, "name", "") or "")).name,
        "size": getattr(file_obj, "size", None),
        "dimensions": dimensions,
        "group": getattr(message, "grouped_id", None),
        "buttons": buttons,
        "text": text,
    }


async def main():
    account = SETTINGS.accounts[0]
    client = TelegramClient(
        str(account.session_path),
        account.api_id,
        account.api_hash,
        proxy=proxy_config(),
    )
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Account session is not authorized")
        for bot_username in SETTINGS.downloader_bots:
            print(f"\n@{bot_username}")
            messages = await client.get_messages(bot_username, limit=30)
            for message in reversed(messages):
                print(describe(message))
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
