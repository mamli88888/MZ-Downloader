from __future__ import annotations

import unittest
from types import SimpleNamespace

import httpx

from instagram_caption import (
    InstagramCaptionError,
    canonical_instagram_url,
    extract_instagram_caption,
    fetch_instagram_caption,
)


class InstagramCaptionParserTests(unittest.TestCase):
    def test_extracts_caption_from_instaspeeder_textarea(self) -> None:
        page = '<div class="result"><textarea name="caption">Hello #tag\n@friend</textarea></div>'
        self.assertEqual(extract_instagram_caption(page), "Hello #tag\n@friend")

    def test_caption_field_wins_over_short_ui_text(self) -> None:
        page = '<span>Copy</span><textarea id="caption">Full caption with emoji 🎬</textarea>'
        self.assertEqual(extract_instagram_caption(page), "Full caption with emoji 🎬")

    def test_extracts_data_caption(self) -> None:
        page = '<div data-caption="A real caption"></div>'
        self.assertEqual(extract_instagram_caption(page), "A real caption")

    def test_og_boilerplate_does_not_become_a_caption(self) -> None:
        page = '<div class="alert">Post is private or not found.</div>'
        with self.assertRaises(InstagramCaptionError):
            extract_instagram_caption(page)


class InstagramCaptionSafetyTests(unittest.IsolatedAsyncioTestCase):
    def test_canonical_url_is_strict(self) -> None:
        self.assertEqual(
            canonical_instagram_url("https://instagram.com/p/ABC123/?igsh=ignored"),
            "https://www.instagram.com/p/ABC123/",
        )
        for unsafe in (
            "http://instagram.com/p/ABC123/",
            "https://instagram.com:444/p/ABC123/",
            "https://instagram.com/accounts/login/",
            "https://instagram.com.evil.example/p/ABC123/",
            "https://user:pass@instagram.com/p/ABC123/",
        ):
            with self.assertRaises(InstagramCaptionError):
                canonical_instagram_url(unsafe)

    async def test_fetches_html_without_following_external_redirect(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("getCaptionRapid.php"):
                return httpx.Response(302, headers={"location": "https://evil.example/caption"}, request=request)
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<html>form</html>',
                request=request,
            )

        transport = httpx.MockTransport(handler)
        with self.assertRaises(InstagramCaptionError):
            await fetch_instagram_caption("https://instagram.com/p/REDIRECT/", transport=transport)

    async def test_fetches_caption_through_mock_transport(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            text = '<textarea id="caption">Hello</textarea>' if request.method == "POST" else '<html>form</html>'
            return httpx.Response(
                200,
                headers={"content-type": "text/html; charset=utf-8"},
                text=text,
                request=request,
            )

        result = await fetch_instagram_caption(
            "https://instagram.com/p/MOCK123/",
            transport=httpx.MockTransport(handler),
        )
        self.assertEqual(result, "Hello")


class MembershipTests(unittest.TestCase):
    def test_member_status_helper_accepts_member_and_rejects_left(self) -> None:
        import bot

        self.assertTrue(bot.is_active_channel_member(SimpleNamespace(status="member")))
        self.assertTrue(bot.is_active_channel_member(SimpleNamespace(status="administrator")))
        self.assertTrue(
            bot.is_active_channel_member(SimpleNamespace(status="restricted", is_member=True))
        )
        self.assertFalse(bot.is_active_channel_member(SimpleNamespace(status="left")))
        self.assertFalse(bot.is_active_channel_member(SimpleNamespace(status="kicked")))


class MembershipGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import bot

        bot.MEMBERSHIP_CACHE.clear()

    async def test_non_member_gets_exactly_two_join_buttons(self) -> None:
        import bot

        class FakeBot:
            async def get_chat_member(self, chat_id: str, user_id: int):
                status = "left" if chat_id == "@MZBOTS_Monitor" else "member"
                return SimpleNamespace(status=status)

        class ReplyMessage:
            def __init__(self) -> None:
                self.calls = []

            async def reply_text(self, text: str, **kwargs):
                self.calls.append((text, kwargs))

        message = ReplyMessage()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_message=message,
            callback_query=None,
        )
        context = SimpleNamespace(bot=FakeBot())
        self.assertFalse(await bot.ensure_required_membership(update, context))
        self.assertEqual(len(message.calls), 1)
        markup = message.calls[0][1]["reply_markup"]
        self.assertEqual(len(markup.inline_keyboard), 2)
        self.assertIn("/start", message.calls[0][0])

    async def test_member_is_cached_after_both_channels_pass(self) -> None:
        import bot

        class FakeBot:
            def __init__(self) -> None:
                self.calls = 0

            async def get_chat_member(self, chat_id: str, user_id: int):
                self.calls += 1
                return SimpleNamespace(status="member")

        class ReplyMessage:
            async def reply_text(self, text: str, **kwargs):
                raise AssertionError("Member should not receive the join prompt")

        fake_bot = FakeBot()
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=456),
            effective_message=ReplyMessage(),
            callback_query=None,
        )
        context = SimpleNamespace(bot=fake_bot)
        self.assertTrue(await bot.ensure_required_membership(update, context))
        self.assertTrue(await bot.ensure_required_membership(update, context))
        self.assertEqual(fake_bot.calls, 2)
