from __future__ import annotations

import argparse
import asyncio
import sys
import time

import socks
from telethon import TelegramClient

from config import SETTINGS
from downloader import (
    CooldownRegistry,
    DownloaderGateway,
    MediaKind,
    cleanup_request_directory,
    create_attempt_directory,
)


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def proxy_config():
    if not SETTINGS.use_proxy:
        return None
    kind = socks.SOCKS5 if SETTINGS.proxy_type == "socks5" else socks.HTTP
    return kind, SETTINGS.proxy_host, SETTINGS.proxy_port


def choose_small_video(options):
    videos = [
        option
        for option in options
        if option.action == "media" and option.expected_kind == MediaKind.VIDEO
    ]
    return min(videos, key=lambda item: item.expected_height or 99999) if videos else None


async def request_menu(gateway, client, account_name, bot_username, url, request_id, suffix):
    directory = create_attempt_directory(
        SETTINGS.download_root,
        request_id,
        f"{suffix}-{bot_username}",
    )
    result = await gateway.request(
        client=client,
        worker_name=account_name,
        bot_username=bot_username,
        url=url,
        attempt_directory=directory,
    )
    return directory, result


async def test_bot(gateway, client, account_name, bot_username, url, request_id):
    print(f"[{bot_username}] requesting menu")
    directory, menu = await request_menu(
        gateway, client, account_name, bot_username, url, request_id, "video"
    )
    try:
        print(
            f"[{bot_username}] status={menu.status} preview={bool(menu.preview)} "
            f"options={[item.label for item in menu.options]}"
        )
        if menu.status != "needs_selection" or menu.preview is None:
            return False
        video_option = choose_small_video(menu.options)
        if video_option is None:
            return False
        selected = await gateway.select(
            client=client,
            worker_name=account_name,
            bot_username=bot_username,
            request_message_id=int(menu.request_message_id or 0),
            menu_message_id=int(menu.menu_message_id or 0),
            option=video_option,
            attempt_directory=directory,
        )
        print(
            f"[{bot_username}] selected={video_option.label} status={selected.status} "
            f"media={[(item.kind.value, item.size, item.width, item.height) for item in selected.media]}"
        )
        if selected.status != "ready" or not selected.media:
            return False
    finally:
        cleanup_request_directory(directory, SETTINGS.download_root)
    return True


async def main(url: str) -> int:
    account = SETTINGS.accounts[0]
    client = TelegramClient(
        str(account.session_path),
        account.api_id,
        account.api_hash,
        proxy=proxy_config(),
    )
    gateway = DownloaderGateway(
        wait_timeout=min(SETTINGS.wait_timeout, 75),
        preview_grace=SETTINGS.preview_grace,
        album_window=SETTINGS.album_collect_window,
        max_download_size=SETTINGS.max_download_size,
        cooldowns=CooldownRegistry(5),
        http_proxy_url=(
            f"{SETTINGS.proxy_type}://{SETTINGS.proxy_host}:{SETTINGS.proxy_port}"
            if SETTINGS.use_proxy
            else None
        ),
    )
    await client.connect()
    request_id = f"live-{int(time.time())}"
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Account session is not authorized")
        results = []
        for bot_username in SETTINGS.downloader_bots:
            results.append(
                await test_bot(
                    gateway,
                    client,
                    account.name,
                    bot_username,
                    url,
                    request_id,
                )
            )
        # Production also succeeds when the first intermediary is unavailable
        # and a later one returns a valid result.
        return 0 if any(results) else 1
    finally:
        await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Live downloader contract test")
    parser.add_argument(
        "--url",
        default="https://youtube.com/shorts/Yt_zAYCDJAM",
    )
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(main(arguments.url)))
