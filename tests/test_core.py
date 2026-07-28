from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto

import bot
from config import PROJECT_DIR
from downloader import (
    AccountPool,
    AccountWorker,
    BotEventStream,
    CooldownRegistry,
    DownloadedMedia,
    DownloaderGateway,
    GatewayResult,
    MediaKind,
    StreamItem,
    WorkerLease,
    await_response_decision,
    cleanup_request_directory,
    create_attempt_directory,
    download_messages,
    expected_kind_for_url,
    extract_trusted_external_url,
    extract_quality_options,
    is_correlated_message,
    message_media_kind,
)


TEST_TEMP_ROOT = PROJECT_DIR / "downloads" / "test-temp"


class FakeButton:
    def __init__(self, text: str, data: bytes | None = b"callback", url: str = "") -> None:
        self.text = text
        self.data = data
        self.url = url


class FakeMessage:
    def __init__(
        self,
        message_id: int,
        *,
        kind: MediaKind = MediaKind.NONE,
        text: str = "",
        reply_to: int | None = None,
        grouped_id: int | None = None,
        buttons: list[list[FakeButton]] | None = None,
        name: str | None = None,
        payload: bytes = b"media-content",
        out: bool = False,
        width: int | None = None,
        height: int | None = None,
    ) -> None:
        self.id = message_id
        self.raw_text = text
        self.text = text
        self.message = text
        self.reply_to_msg_id = reply_to
        self.grouped_id = grouped_id
        self.buttons = buttons
        self.out = out
        self.edit_date = None
        self._payload = payload
        self.clicked: tuple[int, int] | None = None

        if kind == MediaKind.PHOTO:
            self.media = MessageMediaPhoto()
            self.document = None
            default_name = "photo.jpg"
            mime = "image/jpeg"
        elif kind in {MediaKind.VIDEO, MediaKind.AUDIO, MediaKind.DOCUMENT}:
            self.media = MessageMediaDocument()
            default_name = {
                MediaKind.VIDEO: "video.mp4",
                MediaKind.AUDIO: "audio.mp3",
                MediaKind.DOCUMENT: "file.bin",
            }[kind]
            mime = {
                MediaKind.VIDEO: "video/mp4",
                MediaKind.AUDIO: "audio/mpeg",
                MediaKind.DOCUMENT: "application/octet-stream",
            }[kind]
            self.document = SimpleNamespace(
                mime_type=mime,
                size=len(payload),
                attributes=(
                    [SimpleNamespace(w=width, h=height, duration=10)]
                    if width is not None or height is not None
                    else []
                ),
            )
        elif kind == MediaKind.REJECTED:
            self.media = MessageMediaDocument()
            default_name = "error.html"
            mime = "text/html"
            self.document = SimpleNamespace(
                mime_type=mime,
                size=len(payload),
                attributes=[],
            )
        else:
            self.media = None
            self.document = None
            default_name = ""
            mime = ""
        self.file = SimpleNamespace(name=name or default_name, size=len(payload), mime_type=mime)

    async def download_media(self, file: str, progress_callback=None) -> str:
        directory = Path(file)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (self.file.name or f"message-{self.id}.bin")
        path.write_bytes(self._payload)
        if progress_callback is not None:
            await progress_callback(len(self._payload), len(self._payload))
        return str(path)

    async def click(self, row: int, column: int) -> None:
        self.clicked = row, column


class FakeStream:
    def __init__(self, *messages: FakeMessage) -> None:
        self.items = [StreamItem(message, False, 0.0) for message in messages]

    async def get(self, timeout: float) -> StreamItem | None:
        if self.items:
            return self.items.pop(0)
        await asyncio.sleep(max(timeout, 0))
        return None


class GatewayFakeClient:
    def __init__(self, response: FakeMessage | None) -> None:
        self.response = response
        self.handlers: list[tuple[object, object]] = []
        self.send_count = 0

    def add_event_handler(self, callback: object, event: object) -> None:
        self.handlers.append((callback, event))

    def remove_event_handler(self, callback: object, event: object) -> None:
        with_context = (callback, event)
        if with_context in self.handlers:
            self.handlers.remove(with_context)

    async def get_messages(self, bot_username: str, limit: int = 1, ids: int | None = None):
        if ids is not None:
            return None
        return [SimpleNamespace(id=10)]

    async def send_message(self, bot_username: str, url: str) -> SimpleNamespace:
        self.send_count += 1
        sent = SimpleNamespace(id=11)
        if self.response is not None:
            self.response.reply_to_msg_id = 11
            for callback, event in list(self.handlers):
                if type(event).__name__ == "NewMessage":
                    await callback(SimpleNamespace(message=self.response))
        return sent


class UrlTests(unittest.TestCase):
    def test_normalizes_common_link_forms(self) -> None:
        self.assertEqual(
            bot.normalize_url("www.instagram.com/p/example/"),
            "https://www.instagram.com/p/example/",
        )
        self.assertEqual(
            bot.normalize_url("https://youtu.be/abc?t=1)."),
            "https://youtu.be/abc?t=1",
        )
        self.assertEqual(
            bot.normalize_url("https://example.com/path(test)"),
            "https://example.com/path(test)",
        )

    def test_rejects_local_or_credentialed_urls(self) -> None:
        self.assertIsNone(bot.normalize_url("http://127.0.0.1/file"))
        self.assertIsNone(bot.normalize_url("http://localhost/file"))
        self.assertIsNone(bot.normalize_url("https://user:pass@example.com/file"))
        self.assertIsNone(bot.normalize_url("ftp://example.com/file"))

    def test_extracts_and_deduplicates_multiple_urls(self) -> None:
        urls = bot.extract_urls(
            "one https://example.com/a, duplicate https://example.com/a and www.example.org/b?x=1"
        )
        self.assertEqual(
            urls,
            ("https://example.com/a", "https://www.example.org/b?x=1"),
        )

    def test_user_facing_caption_contains_size_and_own_bot_link(self) -> None:
        item = DownloadedMedia(
            path=Path("video.mp4"),
            kind=MediaKind.VIDEO,
            source_message_id=1,
            mime_type="video/mp4",
            size=5 * 1024 * 1024,
        )
        caption = bot.media_caption(item, "1080p", 1, 1, "MZDownloaderBot")
        self.assertIn("✅", caption)
        self.assertIn("5.0 MB", caption)
        self.assertIn("https://t.me/MZDownloaderBot", caption)
        self.assertNotIn("source_bot", caption)

    def test_supported_link_feedback_classification(self) -> None:
        self.assertTrue(bot.links_are_supported(("https://www.instagram.com/p/example/",)))
        self.assertFalse(bot.links_are_supported(("https://example.com/file",)))
        self.assertFalse(bot.links_are_supported(()))


class ClassificationTests(unittest.TestCase):
    def test_classifies_media_and_rejects_html(self) -> None:
        self.assertEqual(message_media_kind(FakeMessage(1, kind=MediaKind.PHOTO)), MediaKind.PHOTO)
        self.assertEqual(message_media_kind(FakeMessage(2, kind=MediaKind.VIDEO)), MediaKind.VIDEO)
        self.assertEqual(message_media_kind(FakeMessage(3, kind=MediaKind.AUDIO)), MediaKind.AUDIO)
        self.assertEqual(message_media_kind(FakeMessage(4, kind=MediaKind.REJECTED)), MediaKind.REJECTED)

    def test_quality_parser_handles_modern_labels_and_duplicate_text(self) -> None:
        message = FakeMessage(
            1,
            buttons=[
                [FakeButton("1080p60", b"a"), FakeButton("4K", b"b")],
                [FakeButton("MP3 320kbps", b"c"), FakeButton("MP3 320kbps", b"d")],
                [FakeButton("Back", b"e"), FakeButton("720p", b"f", url="https://example.com")],
            ],
        )
        options = extract_quality_options(message)
        self.assertEqual([item.label for item in options], ["1080p60", "4K", "MP3 320kbps", "MP3 320kbps"])
        self.assertEqual(options[0].expected_kind, MediaKind.VIDEO)
        self.assertEqual(options[0].expected_height, 1080)
        self.assertEqual(options[1].expected_height, 2160)
        self.assertEqual(options[2].expected_kind, MediaKind.AUDIO)
        self.assertEqual(options[2].expected_bitrate_kbps, 320)
        self.assertNotEqual(options[2].fingerprint, options[3].fingerprint)

    def test_caption_button_is_exposed_but_post_processing_buttons_are_not(self) -> None:
        message = FakeMessage(
            1,
            buttons=[
                [FakeButton("Download caption", b"caption")],
                [FakeButton("Extract audio", b"extract"), FakeButton("Edit video", b"edit")],
            ],
        )
        options = extract_quality_options(message)
        self.assertEqual(len(options), 1)
        self.assertEqual(options[0].action, "caption")

    def test_infers_video_only_sources_to_block_thumbnails(self) -> None:
        self.assertEqual(expected_kind_for_url("https://youtu.be/abc"), MediaKind.VIDEO)
        self.assertEqual(
            expected_kind_for_url("https://www.instagram.com/reel/example/"),
            MediaKind.VIDEO,
        )
        self.assertEqual(
            expected_kind_for_url("https://www.tiktok.com/@user/video/123"),
            MediaKind.VIDEO,
        )
        self.assertIsNone(expected_kind_for_url("https://www.instagram.com/p/example/"))

    def test_only_known_external_media_hosts_are_trusted(self) -> None:
        trusted = "https://instance-5.pictube.app/tunnel?id=123"
        self.assertEqual(extract_trusted_external_url(f"Download: {trusted}"), trusted)
        self.assertIsNone(extract_trusted_external_url("https://pictube.app.evil.example/file"))

    def test_correlation_rejects_old_outgoing_and_wrong_reply(self) -> None:
        self.assertFalse(
            is_correlated_message(FakeMessage(99, kind=MediaKind.VIDEO), after_id=100, reply_targets={100})
        )
        self.assertFalse(
            is_correlated_message(
                FakeMessage(101, kind=MediaKind.VIDEO, reply_to=50),
                after_id=100,
                reply_targets={100},
            )
        )
        self.assertFalse(
            is_correlated_message(
                FakeMessage(101, kind=MediaKind.VIDEO, out=True),
                after_id=100,
                reply_targets={100},
            )
        )
        self.assertTrue(
            is_correlated_message(
                FakeMessage(102, kind=MediaKind.VIDEO, reply_to=100),
                after_id=100,
                reply_targets={100},
            )
        )


class DecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_nextsaver_cover_waits_for_delayed_numbered_menu(self) -> None:
        cover = FakeMessage(101, kind=MediaKind.PHOTO)
        numbered_cover = FakeMessage(
            101,
            kind=MediaKind.PHOTO,
            buttons=[[FakeButton("1", b"song-1"), FakeButton("2", b"song-2")]],
        )
        stream = FakeStream(cover)
        stream.client = SimpleNamespace(
            get_messages=lambda _bot, ids: asyncio.sleep(0, result=numbered_cover)
        )
        stream.bot_username = "NextSaverBot"

        decision = await await_response_decision(
            stream,
            after_id=100,
            reply_targets={100},
            timeout=2,
            preview_grace=0.01,
            album_window=0.01,
            expected_option=extract_quality_options(
                FakeMessage(90, buttons=[[FakeButton("Audio", b"identify")]])
            )[0],
            accept_any_button=True,
            wait_for_followup_menu=True,
        )

        self.assertEqual(decision.status, "menu")
        self.assertEqual(decision.menu_message_id, 101)
        self.assertEqual([option.label for option in decision.options], ["1", "2"])

    async def test_ad_gate_is_rejected_immediately(self) -> None:
        decision = await await_response_decision(
            FakeStream(FakeMessage(101, text="Watch ad to continue", reply_to=100)),
            after_id=100,
            reply_targets={100},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
        )
        self.assertEqual(decision.status, "error")
        self.assertEqual(decision.reason, "ad_required")

    async def test_quality_menu_wins_over_thumbnail(self) -> None:
        preview = FakeMessage(101, kind=MediaKind.PHOTO, reply_to=100)
        menu = FakeMessage(
            102,
            kind=MediaKind.PHOTO,
            reply_to=100,
            buttons=[[FakeButton("720p", b"quality")]],
        )
        decision = await await_response_decision(
            FakeStream(preview, menu),
            after_id=100,
            reply_targets={100},
            timeout=0.1,
            preview_grace=0.02,
            album_window=0.01,
        )
        self.assertEqual(decision.status, "menu")
        self.assertEqual(decision.menu_message_id, 102)

    async def test_selected_video_never_accepts_photo_preview(self) -> None:
        preview = FakeMessage(101, kind=MediaKind.PHOTO, reply_to=100)
        decision = await await_response_decision(
            FakeStream(preview),
            after_id=100,
            reply_targets={100},
            timeout=0.02,
            preview_grace=0.005,
            album_window=0.005,
            expected_kind=MediaKind.VIDEO,
        )
        self.assertEqual(decision.status, "timeout")
        self.assertFalse(decision.messages)

    async def test_wrong_reply_and_promotion_are_skipped(self) -> None:
        unrelated = FakeMessage(101, kind=MediaKind.VIDEO, reply_to=55)
        promotion = FakeMessage(102, kind=MediaKind.VIDEO, reply_to=100, text="Sponsored advertisement")
        valid = FakeMessage(103, kind=MediaKind.VIDEO, reply_to=100)
        decision = await await_response_decision(
            FakeStream(unrelated, promotion, valid),
            after_id=100,
            reply_targets={100},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
        )
        self.assertEqual(decision.status, "media")
        self.assertEqual(decision.messages[0].id, 103)
        self.assertEqual(decision.correlation, "reply")

    async def test_post_processing_buttons_do_not_hide_the_downloaded_file(self) -> None:
        file_message = FakeMessage(
            103,
            kind=MediaKind.VIDEO,
            reply_to=100,
            width=320,
            height=240,
            buttons=[[FakeButton("Extract audio", b"extract"), FakeButton("Edit video", b"edit")]],
        )
        decision = await await_response_decision(
            FakeStream(file_message),
            after_id=100,
            reply_targets={100},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
            expected_kind=MediaKind.VIDEO,
        )
        self.assertEqual(decision.status, "media")
        self.assertEqual(decision.messages[0].id, 103)

    async def test_media_response_preserves_instagram_caption_text(self) -> None:
        media = FakeMessage(
            103,
            kind=MediaKind.VIDEO,
            reply_to=100,
            text="Original Instagram caption #travel",
        )
        decision = await await_response_decision(
            FakeStream(media),
            after_id=100,
            reply_targets={100},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
        )
        self.assertEqual(decision.status, "media")
        self.assertIn("#travel", decision.text)

    async def test_intermediary_reply_chain_is_followed(self) -> None:
        progress = FakeMessage(101, text="Processing, please wait", reply_to=100)
        file_message = FakeMessage(102, kind=MediaKind.VIDEO, reply_to=101, width=320, height=240)
        decision = await await_response_decision(
            FakeStream(progress, file_message),
            after_id=100,
            reply_targets={100},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
            expected_kind=MediaKind.VIDEO,
        )
        self.assertEqual(decision.status, "media")
        self.assertEqual(decision.messages[0].id, 102)

    async def test_caption_button_returns_text(self) -> None:
        menu = FakeMessage(90, buttons=[[FakeButton("Download caption", b"caption")]])
        option = extract_quality_options(menu)[0]
        caption = FakeMessage(101, text="A real caption with #hashtags", reply_to=90, buttons=[[FakeButton("Back", b"back")]])
        decision = await await_response_decision(
            FakeStream(caption),
            after_id=90,
            reply_targets={90},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
            expected_option=option,
        )
        self.assertEqual(decision.status, "text")
        self.assertIn("#hashtags", decision.text)

    async def test_trusted_fallback_link_is_returned_after_quality_click(self) -> None:
        menu = FakeMessage(90, buttons=[[FakeButton("144p", b"144")]])
        option = extract_quality_options(menu)[0]
        fallback = FakeMessage(
            101,
            text="Failed to send video. https://instance-5.pictube.app/tunnel?id=123",
        )
        decision = await await_response_decision(
            FakeStream(fallback),
            after_id=90,
            reply_targets={90},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
            expected_option=option,
        )
        self.assertEqual(decision.status, "external_url")

    async def test_selected_resolution_must_match_video_dimensions(self) -> None:
        menu = FakeMessage(90, buttons=[[FakeButton("720p", b"720")]])
        option = extract_quality_options(menu)[0]
        wrong = FakeMessage(101, kind=MediaKind.VIDEO, reply_to=100, width=1920, height=1080)
        correct = FakeMessage(102, kind=MediaKind.VIDEO, reply_to=100, width=1280, height=720)
        decision = await await_response_decision(
            FakeStream(wrong, correct),
            after_id=100,
            reply_targets={100},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
            expected_kind=MediaKind.VIDEO,
            expected_option=option,
        )
        self.assertEqual(decision.status, "media")
        self.assertEqual(decision.messages[0].id, 102)

    async def test_album_is_grouped_sorted_and_does_not_absorb_other_group(self) -> None:
        second = FakeMessage(102, kind=MediaKind.PHOTO, reply_to=100, grouped_id=7)
        other = FakeMessage(103, kind=MediaKind.PHOTO, reply_to=100, grouped_id=8)
        first = FakeMessage(101, kind=MediaKind.PHOTO, reply_to=100, grouped_id=7)
        decision = await await_response_decision(
            FakeStream(second, other, first),
            after_id=100,
            reply_targets={100},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
        )
        self.assertEqual(decision.status, "media")
        self.assertEqual([message.id for message in decision.messages], [101, 102])

    async def test_ten_separate_carousel_photos_are_all_collected(self) -> None:
        photos = [
            FakeMessage(message_id, kind=MediaKind.PHOTO, reply_to=100)
            for message_id in range(101, 111)
        ]
        decision = await await_response_decision(
            FakeStream(*photos),
            after_id=100,
            reply_targets={100},
            timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
        )
        self.assertEqual(decision.status, "media")
        self.assertEqual([message.id for message in decision.messages], list(range(101, 111)))


class PoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_lease_owner_is_required_for_release(self) -> None:
        pool = AccountPool()
        worker = AccountWorker("one", "+1", object())
        pool.add_worker(worker)
        first = await pool.acquire()
        waiter = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        self.assertEqual(pool.queue_length, 1)
        stale = WorkerLease(worker, "wrong")
        self.assertFalse(pool.release(stale))
        self.assertFalse(waiter.done())
        self.assertTrue(pool.release(first))
        second = await asyncio.wait_for(waiter, timeout=0.1)
        self.assertNotEqual(first.lease_id, second.lease_id)
        self.assertTrue(pool.release(second))

    async def test_cancelled_waiter_does_not_poison_queue(self) -> None:
        pool = AccountPool()
        pool.add_worker(AccountWorker("one", "+1", object()))
        lease = await pool.acquire()
        waiter = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(pool.queue_length, 0)
        pool.release(lease)
        next_lease = await asyncio.wait_for(pool.acquire(), timeout=0.1)
        pool.release(next_lease)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()
        if TEST_TEMP_ROOT.exists() and not any(TEST_TEMP_ROOT.iterdir()):
            TEST_TEMP_ROOT.rmdir()

    async def test_listener_is_installed_before_fast_response(self) -> None:
        client = GatewayFakeClient(FakeMessage(12, kind=MediaKind.VIDEO, name="same.mp4"))
        cooldowns = CooldownRegistry(1.0)
        gateway = DownloaderGateway(
            wait_timeout=0.1,
            preview_grace=0.01,
            album_window=0.01,
            max_download_size=1024,
            cooldowns=cooldowns,
        )
        attempt = self.root / "attempt"
        attempt.mkdir()
        result = await gateway.request(
            client=client,
            worker_name="one",
            bot_username="source_bot",
            url="https://example.com/video",
            attempt_directory=attempt,
        )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.media[0].source_message_id, 12)
        self.assertEqual(result.correlation, "reply")
        self.assertFalse(client.handlers)

    async def test_zero_download_limit_accepts_source_larger_than_old_cap(self) -> None:
        attempt = self.root / "unlimited"
        attempt.mkdir()
        payload = b"x" * 4096
        progress = []

        async def on_progress(current: int, total: int) -> None:
            progress.append((current, total))

        media = await download_messages(
            [FakeMessage(12, kind=MediaKind.VIDEO, payload=payload)],
            attempt,
            max_download_size=0,
            progress_callback=on_progress,
        )
        self.assertEqual(media[0].size, len(payload))
        self.assertEqual(progress[-1], (len(payload), len(payload)))

    async def test_timeout_enters_cooldown_and_prevents_second_send(self) -> None:
        client = GatewayFakeClient(None)
        cooldowns = CooldownRegistry(1.0)
        gateway = DownloaderGateway(
            wait_timeout=0.01,
            preview_grace=0.005,
            album_window=0.005,
            max_download_size=1024,
            cooldowns=cooldowns,
        )
        first_dir = self.root / "first"
        second_dir = self.root / "second"
        first_dir.mkdir()
        second_dir.mkdir()
        first = await gateway.request(
            client=client,
            worker_name="one",
            bot_username="source_bot",
            url="https://example.com/a",
            attempt_directory=first_dir,
        )
        second = await gateway.request(
            client=client,
            worker_name="one",
            bot_username="source_bot",
            url="https://example.com/b",
            attempt_directory=second_dir,
        )
        self.assertEqual(first.reason, "timeout")
        self.assertEqual(second.reason, "cooldown")
        self.assertEqual(client.send_count, 1)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()
        if TEST_TEMP_ROOT.exists() and not any(TEST_TEMP_ROOT.iterdir()):
            TEST_TEMP_ROOT.rmdir()

    async def test_eleven_photos_are_sent_as_pack_of_ten_plus_one(self) -> None:
        media = []
        for index in range(11):
            path = self.root / f"photo-{index}.jpg"
            path.write_bytes(f"photo-{index}".encode())
            media.append(
                DownloadedMedia(
                    path=path,
                    kind=MediaKind.PHOTO,
                    source_message_id=100 + index,
                    mime_type="image/jpeg",
                    size=path.stat().st_size,
                )
            )

        class FakeDeliveryBot:
            username = "MZDownloaderBot"

            def __init__(self) -> None:
                self.groups: list[tuple[int, list[str]]] = []
                self.singles: list[str] = []

            async def send_media_group(self, *, media, **kwargs):
                self.groups.append((len(media), [item.caption for item in media]))

            async def send_photo(self, *, caption, **kwargs):
                self.singles.append(caption)

            async def send_document(self, **kwargs):
                self.fail("photo fallback was not expected")

        class FakeStatus:
            async def edit_text(self, *args, **kwargs):
                return None

        fake_bot = FakeDeliveryBot()
        await bot.send_result_to_user(
            SimpleNamespace(effective_chat=SimpleNamespace(id=1)),
            SimpleNamespace(bot=fake_bot),
            FakeStatus(),
            GatewayResult(status="ready", bot_username="hidden_source", media=tuple(media)),
            reply_to=50,
            request_id="pack-test",
        )
        self.assertEqual([count for count, _ in fake_bot.groups], [10])
        self.assertEqual(len(fake_bot.singles), 1)
        captions = fake_bot.groups[0][1] + fake_bot.singles
        self.assertTrue(all("https://t.me/MZDownloaderBot" in caption for caption in captions))
        self.assertTrue(all("hidden_source" not in caption for caption in captions))


class FileIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=TEST_TEMP_ROOT)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()
        if TEST_TEMP_ROOT.exists() and not any(TEST_TEMP_ROOT.iterdir()):
            TEST_TEMP_ROOT.rmdir()

    def test_attempt_directories_isolate_identical_names(self) -> None:
        first = create_attempt_directory(self.root, "request-a", "bot")
        second = create_attempt_directory(self.root, "request-b", "bot")
        (first / "video.mp4").write_bytes(b"first")
        (second / "video.mp4").write_bytes(b"second")
        self.assertEqual((first / "video.mp4").read_bytes(), b"first")
        self.assertEqual((second / "video.mp4").read_bytes(), b"second")
        cleanup_request_directory(first, self.root)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_split_file_is_lossless_and_uses_part_suffixes(self) -> None:
        source = self.root / "clip.mp4"
        source.write_bytes(b"0123456789")
        parts = bot.split_file(source, 4)
        self.assertEqual([part.name for part in parts], ["clip.mp4.part001", "clip.mp4.part002", "clip.mp4.part003"])
        self.assertEqual(b"".join(part.read_bytes() for part in parts), source.read_bytes())


if __name__ == "__main__":
    unittest.main()
