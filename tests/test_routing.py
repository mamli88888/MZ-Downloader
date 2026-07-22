from __future__ import annotations

import unittest
from types import SimpleNamespace

from downloader import MediaKind, expected_kind_for_url
from routing import Platform, all_providers, detect_platform, providers_for_platform, spotify_resource_type


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            primary_bot="download_it_bot",
            secondary_bot="allsavesbot",
            spotify_bot="spotifysavesbot",
            soundcloud_bot="scload_bot",
            instagram_youtube_bots=("allsaverbot", "instadowbot", "download_it_bot", "allsavesbot"),
            tiktok_bots=("download_it_bot", "allsavesbot"),
            fallback_bots=("allsaverbot", "instadowbot", "download_it_bot", "allsavesbot", "spotifysavesbot", "scload_bot"),
        )

    def test_detects_supported_domains_and_aliases(self) -> None:
        cases = {
            "https://m.instagram.com/p/abc": Platform.INSTAGRAM,
            "https://youtu.be/abc": Platform.YOUTUBE,
            "https://music.youtube.com/watch?v=abc": Platform.YOUTUBE,
            "https://vm.tiktok.com/abc": Platform.TIKTOK,
            "https://mobile.twitter.com/user/status/1": Platform.TWITTER,
            "https://x.com/user/status/1": Platform.TWITTER,
            "https://m.facebook.com/watch/1": Platform.FACEBOOK,
            "https://fb.watch/abc": Platform.FACEBOOK,
            "https://vkvideo.ru/video1": Platform.VK,
            "https://open.spotify.com/track/abc": Platform.SPOTIFY,
            "https://spotify.link/abc": Platform.SPOTIFY,
            "https://on.soundcloud.com/abc": Platform.SOUNDCLOUD,
            "https://snd.sc/abc": Platform.SOUNDCLOUD,
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), expected)

    def test_rejects_spoofed_or_unsupported_domains(self) -> None:
        for url in (
            "https://instagram.com.evil.example/p/1",
            "https://evilinstagram.com/p/1",
            "https://soundcloud.com.evil.example/file",
            "https://vimeo.com/1",
            "https://example.com/?url=https://youtube.com/watch?v=1",
            "https://l.instagram.com/?u=https://example.com",
        ):
            with self.subTest(url=url):
                self.assertIsNone(detect_platform(url))

    def test_provider_matrix_is_exact(self) -> None:
        expected_video = ("allsaverbot", "instadowbot", "download_it_bot", "allsavesbot")
        for platform in (Platform.INSTAGRAM, Platform.YOUTUBE):
            self.assertEqual(providers_for_platform(platform, self.settings), expected_video)
        self.assertEqual(providers_for_platform(Platform.TIKTOK, self.settings), ("download_it_bot", "allsavesbot"))
        for platform in (Platform.TWITTER, Platform.FACEBOOK, Platform.VK):
            self.assertEqual(providers_for_platform(platform, self.settings), ("download_it_bot",))
        self.assertEqual(providers_for_platform(Platform.SPOTIFY, self.settings), ("spotifysavesbot",))
        self.assertEqual(providers_for_platform(Platform.SOUNDCLOUD, self.settings), ("scload_bot",))
        self.assertEqual(all_providers(self.settings), self.settings.fallback_bots)
        self.assertEqual(spotify_resource_type("https://open.spotify.com/album/abc"), "album")
        self.assertEqual(spotify_resource_type("https://open.spotify.com/track/abc"), "track")

    def test_audio_platforms_reject_cover_art_as_final_output(self) -> None:
        self.assertEqual(expected_kind_for_url("https://open.spotify.com/track/abc"), MediaKind.AUDIO)
        self.assertEqual(expected_kind_for_url("https://soundcloud.com/user/track"), MediaKind.AUDIO)


if __name__ == "__main__":
    unittest.main()
