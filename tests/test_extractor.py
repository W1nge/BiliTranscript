from __future__ import annotations

import unittest
from collections import defaultdict
from pathlib import Path
from typing import Any

from bilitranscript_app.asr import AsrAvailability, AsrResult
from bilitranscript_app.bilibili import BilibiliClient, BilibiliError, SubtitleProbe, SubtitleTrack
from bilitranscript_app.extractor import (
    FIXED_EXTRACTION_RULES,
    ExtractionOptions,
    RetryPolicy,
    TranscriptExtractor,
)
from bilitranscript_app.models import Segment, VideoInfo, VideoPart


def sample_video() -> VideoInfo:
    part = VideoPart(1, 123, "正文", 30)
    return VideoInfo("BV1abcdefghij", 99, "测试", "UP", 30, "", 0, "", (part,))


def track(language: str, label: str, url: str, *, ai: bool = False) -> SubtitleTrack:
    return SubtitleTrack(language, label, url, ai)


class FakeBridge:
    def __init__(self, sequences: list[tuple[SubtitleTrack, ...]] | None = None) -> None:
        self.sequences = sequences or [()]
        self.calls = 0
        self.last_error = "登录浏览器不可用"

    def fetch_tracks(self, video: VideoInfo, part: VideoPart):
        index = min(self.calls, len(self.sequences) - 1)
        self.calls += 1
        value = self.sequences[index]
        if value:
            self.last_error = ""
        return value


class FakeAsr:
    def detect(self):
        return AsrAvailability("python", ("faster-whisper",))

    def transcribe(self, audio_path: Path, output_path: Path, **kwargs: Any):
        return AsrResult("faster-whisper", "small", "zh", (Segment(0, 2, "本地识别"),))


class FakeClient:
    def __init__(
        self,
        *,
        public: list[tuple[SubtitleTrack, ...] | Exception] | None = None,
        anonymous: list[tuple[SubtitleTrack, ...] | Exception] | None = None,
        payloads: dict[str, list[dict[str, Any] | Exception]] | None = None,
        audio_available: bool = True,
    ) -> None:
        self.sequences = {"public": public or [()], "anonymous": anonymous or [()]}
        self.calls: defaultdict[str, int] = defaultdict(int)
        self.payloads = payloads or {}
        self.payload_calls: defaultdict[str, int] = defaultdict(int)
        self.audio_available = audio_available
        self.audio_downloaded = False

    def _probe(self, route: str) -> SubtitleProbe:
        sequence = self.sequences[route]
        index = min(self.calls[route], len(sequence) - 1)
        self.calls[route] += 1
        value = sequence[index]
        if isinstance(value, Exception):
            raise value
        return SubtitleProbe(value, route=route)

    def probe_public_subtitles(self, video: VideoInfo, part: VideoPart) -> SubtitleProbe:
        return self._probe("public")

    def probe_anonymous_subtitles(self, video: VideoInfo, part: VideoPart) -> SubtitleProbe:
        return self._probe("anonymous")

    @staticmethod
    def rank_subtitles(tracks: tuple[SubtitleTrack, ...]):
        return BilibiliClient.rank_subtitles(tracks)

    @staticmethod
    def is_chinese_track(value: SubtitleTrack) -> bool:
        return BilibiliClient.is_chinese_track(value)

    def fetch_subtitle_payload(self, video: VideoInfo, value: SubtitleTrack):
        sequence = self.payloads.get(
            value.url,
            [{"body": [{"from": 0, "to": 1.2, "content": value.label}]}],
        )
        index = min(self.payload_calls[value.url], len(sequence) - 1)
        self.payload_calls[value.url] += 1
        result = sequence[index]
        if isinstance(result, Exception):
            raise result
        return result

    def audio_url(self, video: VideoInfo, part: VideoPart) -> str:
        self.calls["audio_url"] += 1
        if not self.audio_available:
            raise BilibiliError("没有音频")
        return "https://audio.bilivideo.com/test.m4s"

    def download_audio(self, video: VideoInfo, part: VideoPart, destination: Path, **kwargs: Any):
        self.calls["audio_download"] += 1
        if not self.audio_available:
            raise BilibiliError("没有音频")
        self.audio_downloaded = True
        destination.write_bytes(b"audio")


class ExtractorTests(unittest.TestCase):
    def build_extractor(self, client: FakeClient, bridge: FakeBridge | None = None):
        self.sleeps: list[float] = []
        return TranscriptExtractor(
            client=client,
            browser_bridge=bridge or FakeBridge(),
            asr_runtime=FakeAsr(),
            retry_policy=RetryPolicy(attempts=2, interval_seconds=1, sleeper=self.sleeps.append),
        )

    @staticmethod
    def run_extract(extractor: TranscriptExtractor, options: ExtractionOptions):
        video = sample_video()
        return extractor.extract(
            video,
            [video.parts[0]],
            options,
            cancelled=lambda: False,
            progress=lambda value, message: None,
            log=lambda message: None,
        )

    def test_fixed_route_order(self) -> None:
        self.assertEqual(
            FIXED_EXTRACTION_RULES,
            (
                "public-player-v2",
                "anonymous-player-wbi-v2",
                "dedicated-browser-player-wbi-v2",
                "local-asr",
            ),
        )

    def test_auto_uses_public_chinese_without_lower_routes(self) -> None:
        public = track("zh-CN", "人工中文字幕", "https://aisubtitle.hdslb.com/public")
        client = FakeClient(public=[(public,)])
        bridge = FakeBridge()
        result = self.run_extract(self.build_extractor(client, bridge), ExtractionOptions(mode="auto"))
        self.assertEqual(result.parts[0].text, "人工中文字幕")
        self.assertEqual(client.calls["public"], 1)
        self.assertEqual(client.calls["anonymous"], 0)
        self.assertEqual(bridge.calls, 0)
        self.assertFalse(client.audio_downloaded)

    def test_auto_prefers_anonymous_chinese_ai_over_public_english(self) -> None:
        english = track("en", "English", "https://aisubtitle.hdslb.com/en")
        ai_chinese = track("ai-zh", "B站中文AI", "https://aisubtitle.hdslb.com/ai", ai=True)
        client = FakeClient(public=[(english,)], anonymous=[(ai_chinese,)])
        bridge = FakeBridge()
        result = self.run_extract(self.build_extractor(client, bridge), ExtractionOptions(mode="auto"))
        self.assertEqual(result.parts[0].text, "B站中文AI")
        self.assertIn("AI 字幕", result.parts[0].source)
        self.assertEqual(bridge.calls, 0)
        self.assertFalse(client.audio_downloaded)

    def test_each_empty_source_retries_twice_then_descends(self) -> None:
        browser_ai = track("ai-zh", "浏览器AI", "https://aisubtitle.hdslb.com/browser", ai=True)
        client = FakeClient(public=[(), ()], anonymous=[(), ()])
        bridge = FakeBridge([(), (browser_ai,)])
        result = self.run_extract(self.build_extractor(client, bridge), ExtractionOptions(mode="auto"))
        self.assertEqual(result.parts[0].text, "浏览器AI")
        self.assertEqual(client.calls["public"], 2)
        self.assertEqual(client.calls["anonymous"], 2)
        self.assertEqual(bridge.calls, 2)
        self.assertEqual(self.sleeps, [1, 1, 1])

    def test_empty_subtitle_body_retries_then_uses_same_track(self) -> None:
        chinese = track("zh-CN", "中文字幕", "https://aisubtitle.hdslb.com/retry")
        client = FakeClient(
            public=[(chinese,)],
            payloads={
                chinese.url: [
                    {"body": []},
                    {"body": [{"from": 0, "to": 1, "content": "第二次成功"}]},
                ]
            },
        )
        result = self.run_extract(
            self.build_extractor(client),
            ExtractionOptions(mode="public", browser_ai=False),
        )
        self.assertEqual(result.parts[0].text, "第二次成功")
        self.assertEqual(client.payload_calls[chinese.url], 2)
        self.assertEqual(self.sleeps, [1])

    def test_broken_public_track_does_not_abort_anonymous_ai_fallback(self) -> None:
        public = track("zh-CN", "损坏字幕", "https://aisubtitle.hdslb.com/broken")
        anonymous = track("ai-zh", "匿名AI成功", "https://aisubtitle.hdslb.com/anonymous", ai=True)
        client = FakeClient(
            public=[(public,)],
            anonymous=[(anonymous,)],
            payloads={public.url: [BilibiliError("CDN失败"), BilibiliError("CDN仍失败")]},
        )
        result = self.run_extract(self.build_extractor(client), ExtractionOptions(mode="auto"))
        self.assertEqual(result.parts[0].text, "匿名AI成功")
        self.assertEqual(client.payload_calls[public.url], 2)
        self.assertEqual(client.calls["anonymous"], 1)

    def test_second_source_attempt_reprobes_and_can_receive_fresh_url(self) -> None:
        expired = track("zh-CN", "过期字幕", "https://aisubtitle.hdslb.com/expired")
        refreshed = track("zh-CN", "刷新后字幕", "https://aisubtitle.hdslb.com/refreshed")
        client = FakeClient(
            public=[(expired,), (refreshed,)],
            payloads={expired.url: [BilibiliError("URL已过期")]},
        )
        result = self.run_extract(
            self.build_extractor(client),
            ExtractionOptions(mode="public"),
        )
        self.assertEqual(result.parts[0].text, "刷新后字幕")
        self.assertEqual(client.calls["public"], 2)
        self.assertEqual(self.sleeps, [1])

    def test_auto_falls_back_to_local_asr_after_two_attempts_per_subtitle_source(self) -> None:
        client = FakeClient(public=[(), ()], anonymous=[(), ()])
        bridge = FakeBridge([(), ()])
        result = self.run_extract(
            self.build_extractor(client, bridge),
            ExtractionOptions(mode="auto", asr_backend="faster-whisper", asr_model="small"),
        )
        self.assertEqual(result.parts[0].text, "本地识别")
        self.assertEqual(client.calls["public"], 2)
        self.assertEqual(client.calls["anonymous"], 2)
        self.assertEqual(bridge.calls, 2)
        self.assertTrue(client.audio_downloaded)

    def test_manual_anonymous_mode_does_not_touch_other_sources(self) -> None:
        anonymous = track("ai-zh", "指定匿名字幕", "https://aisubtitle.hdslb.com/manual", ai=True)
        client = FakeClient(anonymous=[(anonymous,)])
        bridge = FakeBridge()
        result = self.run_extract(
            self.build_extractor(client, bridge),
            ExtractionOptions(mode="anonymous"),
        )
        self.assertEqual(result.parts[0].text, "指定匿名字幕")
        self.assertEqual(client.calls["public"], 0)
        self.assertEqual(client.calls["anonymous"], 1)
        self.assertEqual(bridge.calls, 0)
        self.assertFalse(client.audio_downloaded)

    def test_availability_report_exposes_all_four_routes(self) -> None:
        public = track("zh-CN", "公开字幕", "https://aisubtitle.hdslb.com/public")
        client = FakeClient(public=[(public,)], anonymous=[(), ()])
        bridge = FakeBridge([(), ()])
        extractor = self.build_extractor(client, bridge)
        video = sample_video()
        report = extractor.probe_availability(
            video,
            [video.parts[0]],
            cancelled=lambda: False,
            progress=lambda value, message: None,
            log=lambda message: None,
        )
        routes = {item.route: item for item in report.parts[0].routes}
        self.assertEqual(set(routes), {"public", "anonymous", "browser", "asr"})
        self.assertTrue(routes["public"].available)
        self.assertFalse(routes["anonymous"].available)
        self.assertFalse(routes["browser"].available)
        self.assertTrue(routes["asr"].available)
        self.assertEqual(routes["anonymous"].attempts, 2)


if __name__ == "__main__":
    unittest.main()
