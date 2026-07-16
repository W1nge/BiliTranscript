from __future__ import annotations

import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Callable
from unittest import mock

from bilitranscript_app.bilibili import BilibiliClient, BilibiliError, SubtitleTrack, UrllibTransport


class FakeTransport:
    def __init__(self) -> None:
        self.resolved = "https://www.bilibili.com/video/BV1abcdefghij/"

    def get_json(self, url: str, *, referer: str, timeout: float = 30) -> dict[str, Any]:
        parsed = urllib.parse.urlparse(url)
        if parsed.path.endswith("/x/web-interface/view"):
            return {
                "code": 0,
                "data": {
                    "bvid": "BV1abcdefghij",
                    "aid": 99,
                    "title": "测试视频",
                    "owner": {"name": "测试UP"},
                    "duration": 12,
                    "pages": [{"page": 1, "cid": 123, "part": "测试视频", "duration": 12}],
                },
            }
        if parsed.path.endswith("/x/player/v2"):
            return {
                "code": 0,
                "data": {
                    "need_login_subtitle": False,
                    "subtitle": {
                        "subtitles": [
                            {
                                "lan": "zh-CN",
                                "lan_doc": "中文（简体）",
                                "subtitle_url": "//aisubtitle.hdslb.com/test.json",
                            }
                        ]
                    },
                },
            }
        if parsed.path.endswith("/x/player/wbi/v2"):
            return {
                "code": 0,
                "data": {
                    "subtitle": {
                        "subtitles": [
                            {
                                "lan": "ai-zh",
                                "lan_doc": "中文（自动生成）",
                                "ai_status": 1,
                                "subtitle_url": "//aisubtitle.hdslb.com/ai.json",
                            }
                        ]
                    }
                },
            }
        if parsed.hostname == "aisubtitle.hdslb.com":
            return {"body": [{"from": 0, "to": 1, "content": "你好"}]}
        if parsed.path.endswith("/x/player/playurl"):
            return {
                "code": 0,
                "data": {"dash": {"audio": [{"bandwidth": 99, "baseUrl": "https://audio.bilivideo.com/a.m4s"}]}},
            }
        raise AssertionError(f"unexpected URL {url}")

    def resolve_url(self, url: str, *, timeout: float = 15) -> str:
        return self.resolved

    def download(
        self,
        url: str,
        destination: Path,
        *,
        referer: str,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        timeout: float = 60,
    ) -> None:
        destination.write_bytes(b"audio")
        if progress:
            progress(5, 5)


class BilibiliClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = BilibiliClient(FakeTransport())

    def test_fetch_video_from_bvid_and_short_link(self) -> None:
        direct = self.client.fetch_video("BV1abcdefghij")
        short = self.client.fetch_video("https://b23.tv/abc123")
        self.assertEqual(direct.bvid, "BV1abcdefghij")
        self.assertEqual(short.parts[0].cid, 123)

    def test_rejects_untrusted_url(self) -> None:
        with self.assertRaises(BilibiliError):
            self.client.normalize_source("https://example.com/B-nope")

    def test_default_transport_does_not_add_hidden_retries(self) -> None:
        transport = UrllibTransport()
        with mock.patch(
            "bilitranscript_app.bilibili.urllib.request.urlopen",
            side_effect=urllib.error.URLError("offline"),
        ) as urlopen:
            with self.assertRaises(BilibiliError):
                transport.get_json("https://api.bilibili.com/test", referer="https://www.bilibili.com/")
        self.assertEqual(urlopen.call_count, 1)

    def test_probe_choose_and_fetch_subtitle(self) -> None:
        video = self.client.fetch_video("BV1abcdefghij")
        public = self.client.probe_public_subtitles(video, video.parts[0])
        anonymous = self.client.probe_anonymous_subtitles(video, video.parts[0])
        self.assertEqual(public.route, "public")
        self.assertEqual(anonymous.route, "anonymous")
        track = self.client.choose_subtitle(public.tracks + anonymous.tracks)
        self.assertIsNotNone(track)
        assert track is not None
        self.assertEqual(track.url, "https://aisubtitle.hdslb.com/test.json")
        payload = self.client.fetch_subtitle_payload(video, track)
        self.assertEqual(payload["body"][0]["content"], "你好")

    def test_subtitle_language_priority(self) -> None:
        tracks = (
            SubtitleTrack("en", "English", "https://aisubtitle.hdslb.com/en"),
            SubtitleTrack("ai-zh", "中文", "https://aisubtitle.hdslb.com/ai", True),
            SubtitleTrack("zh-CN", "中文", "https://aisubtitle.hdslb.com/zh"),
        )
        selected = self.client.choose_subtitle(tracks)
        self.assertEqual(selected.language if selected else None, "zh-CN")
        ranked = self.client.rank_subtitles(tracks)
        self.assertLess(ranked.index(tracks[1]), ranked.index(tracks[0]))

    def test_audio_download(self) -> None:
        video = self.client.fetch_video("BV1abcdefghij")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "audio.m4s"
            self.client.download_audio(video, video.parts[0], output)
            self.assertEqual(output.read_bytes(), b"audio")


if __name__ == "__main__":
    unittest.main()
