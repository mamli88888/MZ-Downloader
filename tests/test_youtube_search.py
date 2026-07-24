from __future__ import annotations

import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

import bot
from routing import Platform
from youtube_search import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    MAX_RESULTS,
    YouTubeSearchError,
    YouTubeSearchResult,
    YouTubeSearchService,
    normalize_search_query,
    parse_search_entries,
    render_collage,
)


def make_results(count: int = 30) -> tuple[YouTubeSearchResult, ...]:
    results = []
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    for index in range(count):
        video_id = (alphabet[index % len(alphabet)] * 10) + alphabet[(index + 1) % len(alphabet)]
        results.append(
            YouTubeSearchResult(
                video_id=video_id,
                title=f"Video {index + 1}",
                url=f"https://www.youtube.com/watch?v={video_id}",
                thumbnail_url=f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
            )
        )
    return tuple(results)


def jpeg_bytes(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (320, 180), color).save(output, "JPEG")
    return output.getvalue()


class YouTubeSearchParsingTests(unittest.TestCase):
    def test_query_normalization_and_limits(self) -> None:
        self.assertEqual(normalize_search_query("  موسیقی   بی کلام  "), "موسیقی بی کلام")
        with self.assertRaises(YouTubeSearchError):
            normalize_search_query("   ")
        with self.assertRaises(YouTubeSearchError):
            normalize_search_query("x" * 201)

    def test_entries_keep_order_deduplicate_and_validate_video_ids(self) -> None:
        entries = [
            {
                "id": "abcdefghijk",
                "title": " First ",
                "thumbnails": [{"url": "https://i.ytimg.com/one.jpg", "width": "unknown"}],
            },
            {"id": "invalid", "title": "Ignored"},
            {"id": "abcdefghijk", "title": "Duplicate"},
            {"url": "lmnopqrstuv", "title": "Second"},
            {"webpage_url": "https://www.youtube.com/watch?v=123456789_-", "title": "Third"},
        ]
        results = parse_search_entries(entries)
        self.assertEqual([result.video_id for result in results], ["abcdefghijk", "lmnopqrstuv", "123456789_-"])
        self.assertEqual(results[0].title, "First")
        self.assertEqual(results[1].url, "https://www.youtube.com/watch?v=lmnopqrstuv")

    def test_result_count_is_capped_at_thirty(self) -> None:
        entries = [
            {"id": result.video_id, "title": result.title}
            for result in make_results(MAX_RESULTS + 5)
        ]
        self.assertEqual(len(parse_search_entries(entries)), MAX_RESULTS)

    def test_live_music_and_non_video_results_are_filtered_but_long_and_short_remain(self) -> None:
        entries = [
            {"id": "AAAAAAAAAAB", "title": "Relevant long video", "duration": 1200, "live_status": "not_live"},
            {"id": "BBBBBBBBBBC", "title": "Live now: event", "live_status": "is_live"},
            {"id": "CCCCCCCCCCD", "title": "Recorded stream", "live_status": "was_live"},
            {"id": "DDDDDDDDDDE", "title": "Official Audio", "duration": 240},
            {"id": "EEEEEEEEEEF", "title": "A track", "artist": "Singer", "duration": 180},
            {"id": "FFFFFFFFFFG", "title": "Auto-generated track", "channel": "Singer - Topic"},
            {"id": "GGGGGGGGGGH", "title": "Playlist", "_type": "playlist"},
            {"id": "HHHHHHHHHHI", "title": "Relevant Short", "duration": 35, "live_status": "not_live"},
            {"id": "IIIIIIIIIIJ", "title": "How I live in Tokyo", "duration": 600, "live_status": "not_live"},
        ]
        results = parse_search_entries(entries)
        self.assertEqual(
            [result.video_id for result in results],
            ["AAAAAAAAAAB", "HHHHHHHHHHI", "IIIIIIIIIIJ"],
        )


class YouTubeCollageTests(unittest.IsolatedAsyncioTestCase):
    def test_collage_is_a_fixed_size_jpeg_and_accepts_missing_thumbnails(self) -> None:
        payload = render_collage(
            [jpeg_bytes("red"), None, jpeg_bytes("blue"), b"broken", jpeg_bytes("green"), None],
            7,
        )
        with Image.open(io.BytesIO(payload)) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertEqual(image.size, (CANVAS_WIDTH, CANVAS_HEIGHT))

    def test_collage_rejects_invalid_item_counts(self) -> None:
        with self.assertRaises(YouTubeSearchError):
            render_collage([], 1)
        with self.assertRaises(YouTubeSearchError):
            render_collage([None] * 7, 1)

    async def test_page_builder_fetches_only_the_requested_six_results(self) -> None:
        service = YouTubeSearchService()
        seen: list[str] = []

        async def fake_download(client, result):
            seen.append(result.video_id)
            return jpeg_bytes("purple")

        with patch.object(service, "_download_thumbnail", side_effect=fake_download):
            payload = await service.build_page_image(make_results(), 2)
        self.assertEqual(seen, [result.video_id for result in make_results()[12:18]])
        with Image.open(io.BytesIO(payload)) as image:
            self.assertEqual(image.size, (CANVAS_WIDTH, CANVAS_HEIGHT))


class YouTubeKeyboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = bot.YouTubeSearchSession(
            token="abc123",
            created_at=1.0,
            chat_id=10,
            user_id=20,
            reply_to=30,
            query="test",
            results=make_results(),
        )

    def test_first_middle_and_last_page_layouts(self) -> None:
        first = bot.youtube_search_keyboard(self.session, 0).inline_keyboard
        self.assertEqual([len(row) for row in first], [2, 2, 2, 1])
        self.assertEqual(first[0][0].callback_data, "ys:abc123:0")
        self.assertEqual(first[2][1].callback_data, "ys:abc123:5")
        self.assertEqual(first[3][0].callback_data, "yp:abc123:1")

        middle = bot.youtube_search_keyboard(self.session, 2).inline_keyboard
        self.assertEqual([len(row) for row in middle], [2, 2, 2, 2])
        self.assertEqual(middle[0][0].callback_data, "ys:abc123:12")
        self.assertEqual([button.callback_data for button in middle[-1]], ["yp:abc123:1", "yp:abc123:3"])

        last = bot.youtube_search_keyboard(self.session, 4).inline_keyboard
        self.assertEqual([len(row) for row in last], [2, 2, 2, 1])
        self.assertEqual(last[-1][0].callback_data, "yp:abc123:3")


class YouTubeCallbackTests(unittest.IsolatedAsyncioTestCase):
    def tearDown(self) -> None:
        bot.YOUTUBE_SEARCH_SESSIONS.clear()

    @staticmethod
    def make_update(data: str):
        callback = SimpleNamespace(
            data=data,
            answer=AsyncMock(),
            edit_message_reply_markup=AsyncMock(),
            edit_message_media=AsyncMock(),
        )
        update = SimpleNamespace(
            callback_query=callback,
            effective_chat=SimpleNamespace(id=10),
            effective_user=SimpleNamespace(id=20),
            effective_message=SimpleNamespace(message_id=99),
        )
        context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
        return update, context

    async def test_content_selection_enters_the_existing_download_flow(self) -> None:
        session = bot.YouTubeSearchSession(
            token="pickme",
            created_at=bot.time.monotonic(),
            chat_id=10,
            user_id=20,
            reply_to=30,
            query="test",
            results=make_results(),
        )
        bot.YOUTUBE_SEARCH_SESSIONS[session.token] = session
        update, context = self.make_update("ys:pickme:7")
        with (
            patch.object(bot, "ensure_required_membership", AsyncMock(return_value=True)),
            patch.object(bot, "process_urls", AsyncMock(return_value=True)) as process_urls,
        ):
            await bot.on_youtube_search_callback(update, context)
        process_urls.assert_awaited_once_with(
            update,
            context,
            (session.results[7].url,),
            30,
        )
        self.assertNotIn(session.token, bot.YOUTUBE_SEARCH_SESSIONS)
        self.assertEqual(bot.detect_platform(session.results[7].url), Platform.YOUTUBE)

    async def test_rejected_download_keeps_the_search_results_usable(self) -> None:
        session = bot.YouTubeSearchSession(
            token="tryagain",
            created_at=bot.time.monotonic(),
            chat_id=10,
            user_id=20,
            reply_to=30,
            query="test",
            results=make_results(),
        )
        bot.YOUTUBE_SEARCH_SESSIONS[session.token] = session
        update, context = self.make_update("ys:tryagain:0")
        with (
            patch.object(bot, "ensure_required_membership", AsyncMock(return_value=True)),
            patch.object(bot, "process_urls", AsyncMock(return_value=False)),
        ):
            await bot.on_youtube_search_callback(update, context)
        self.assertIn(session.token, bot.YOUTUBE_SEARCH_SESSIONS)
        self.assertFalse(session.selected)
        update.callback_query.edit_message_reply_markup.assert_not_awaited()

    async def test_navigation_replaces_the_same_photo_without_new_search(self) -> None:
        session = bot.YouTubeSearchSession(
            token="nextpg",
            created_at=bot.time.monotonic(),
            chat_id=10,
            user_id=20,
            reply_to=30,
            query="test",
            results=make_results(),
        )
        bot.YOUTUBE_SEARCH_SESSIONS[session.token] = session
        update, context = self.make_update("yp:nextpg:1")
        with (
            patch.object(bot, "ensure_required_membership", AsyncMock(return_value=True)),
            patch.object(bot.YOUTUBE_SEARCH, "build_page_image", AsyncMock(return_value=b"jpeg")) as build,
        ):
            await bot.on_youtube_search_callback(update, context)
        build.assert_awaited_once_with(session.results, 1)
        update.callback_query.edit_message_media.assert_awaited_once()
        self.assertEqual(session.current_page, 1)


if __name__ == "__main__":
    unittest.main()
