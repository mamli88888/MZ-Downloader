from __future__ import annotations

import unittest

from apify_gateway import (
    ApifyError,
    ApifyGateway,
    _actor_reference,
    _instagram_media_specs,
    _instagram_results_type,
)
from config import load_settings
from downloader import MediaKind
from routing import Platform


class ApifyGatewayHelpersTests(unittest.TestCase):
    def test_actor_reference_uses_named_actor_form(self) -> None:
        self.assertEqual(
            _actor_reference("streamers/youtube-video-downloader"),
            "streamers~youtube-video-downloader",
        )

    def test_instagram_results_type_matches_url_kind(self) -> None:
        self.assertEqual(
            _instagram_results_type("https://www.instagram.com/reel/ABC123/"),
            "reels",
        )
        self.assertEqual(
            _instagram_results_type("https://www.instagram.com/p/ABC123/"),
            "posts",
        )

    def test_instagram_media_extraction_handles_carousel_and_deduplicates(self) -> None:
        item = {
            "videoUrl": "https://cdn.example/video.mp4",
            "childPosts": [
                {"displayUrl": "https://cdn.example/image.jpg"},
                {"videoUrl": "https://cdn.example/child.mp4"},
                {"videoUrl": "https://cdn.example/video.mp4"},
            ],
        }
        self.assertEqual(
            _instagram_media_specs(item),
            [
                ("https://cdn.example/video.mp4", MediaKind.VIDEO),
                ("https://cdn.example/image.jpg", MediaKind.PHOTO),
                ("https://cdn.example/child.mp4", MediaKind.VIDEO),
            ],
        )

    def test_settings_read_multiple_apify_tokens_and_cooldown(self) -> None:
        settings = load_settings(
            {
                "APIFY_ENABLED": "true",
                "APIFY_TOKENS": "first-token, second-token, first-token",
                "APIFY_RUN_TIMEOUT_SECONDS": "420",
                "APIFY_POLL_INTERVAL_SECONDS": "2.5",
                "APIFY_TOKEN_COOLDOWN_SECONDS": "90",
            }
        )
        self.assertTrue(settings.apify_enabled)
        self.assertEqual(settings.apify_tokens, ("first-token", "second-token"))
        self.assertEqual(settings.apify_run_timeout, 420.0)
        self.assertEqual(settings.apify_poll_interval, 2.5)
        self.assertEqual(settings.apify_token_cooldown, 90.0)

    def test_legacy_single_token_is_still_accepted(self) -> None:
        settings = load_settings({"APIFY_TOKEN": "legacy-token"})
        self.assertEqual(settings.apify_tokens, ("legacy-token",))

    def test_youtube_menu_has_all_video_qualities_and_audio(self) -> None:
        options = ApifyGateway._youtube_options()
        self.assertEqual(len(options), 9)
        self.assertEqual(options[0].label, "144p")
        self.assertEqual(options[-1].expected_kind, MediaKind.AUDIO)
        self.assertEqual(options[-1].label, "فقط صدا (MP3)")

    def test_actor_request_uses_selected_youtube_quality_or_mp3(self) -> None:
        gateway = ApifyGateway(tokens=("token-a",))
        video = gateway._actor_request(
            "https://www.youtube.com/watch?v=abc",
            Platform.YOUTUBE,
            {"kind": "video", "quality": "1080p"},
        )
        self.assertEqual(video[1]["preferredQuality"], "1080p")
        self.assertEqual(video[1]["preferredFormat"], "mp4")
        self.assertEqual(video[2], MediaKind.VIDEO)

        audio = gateway._actor_request(
            "https://www.youtube.com/watch?v=abc",
            Platform.YOUTUBE,
            {"kind": "audio"},
        )
        self.assertEqual(audio[1]["preferredFormat"], "mp3")
        self.assertEqual(audio[2], MediaKind.AUDIO)

    def test_instagram_audio_selection_requests_local_mp3_conversion(self) -> None:
        gateway = ApifyGateway(tokens=("token-a",))
        request = gateway._actor_request(
            "https://www.instagram.com/reel/abc/",
            Platform.INSTAGRAM,
            {"kind": "audio"},
        )
        self.assertEqual(request[1]["resultsType"], "reels")
        self.assertTrue(request[3])


class ApifyGatewayTokenRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_token_is_skipped_during_cooldown(self) -> None:
        gateway = ApifyGateway(tokens=("first", "second"), token_cooldown=60)
        self.assertEqual(await gateway._token_candidates(), (0, 1))
        await gateway._mark_token_unavailable(0)
        self.assertEqual(await gateway._token_candidates(), (1,))
        await gateway._mark_token_success(0)
        self.assertEqual(await gateway._token_candidates(), (0, 1))

    async def test_actor_error_immediately_tries_the_next_token(self) -> None:
        gateway = ApifyGateway(tokens=("first", "second"), token_cooldown=60)
        used_tokens: list[str] = []

        async def fake_run_actor(client, actor_id, actor_input):
            del actor_id, actor_input
            used_tokens.append(client.headers["Authorization"])
            if len(used_tokens) == 1:
                raise ApifyError("temporary Actor failure", status_code=500)
            return {"defaultDatasetId": "dataset"}

        async def fake_dataset_items(client, run):
            del client, run
            return [{"downloadedFileUrl": "https://storage.example/file.mp4"}]

        gateway._run_actor = fake_run_actor  # type: ignore[method-assign]
        gateway._dataset_items = fake_dataset_items  # type: ignore[method-assign]
        result = await gateway._run_with_failover("owner/actor", {"url": "https://example.test"})
        self.assertEqual(used_tokens, ["Bearer first", "Bearer second"])
        self.assertEqual(len(result["items"]), 1)


if __name__ == "__main__":
    unittest.main()
