from __future__ import annotations

import json
import unittest

from bilitranscript_app.models import (
    PartTranscript,
    Segment,
    TranscriptBundle,
    VideoInfo,
    VideoPart,
    format_clock,
    format_srt_clock,
    safe_filename,
)


class ModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parts = (
            VideoPart(1, 101, "开场", 10),
            VideoPart(2, 102, "正文", 20),
        )
        self.video = VideoInfo(
            bvid="BV1abcdefghij",
            aid=42,
            title="测试 / 视频",
            owner="UP",
            duration=30,
            cover_url="",
            published_at=0,
            description="",
            parts=self.parts,
        )

    def test_time_formatters(self) -> None:
        self.assertEqual(format_clock(65.2), "01:05")
        self.assertEqual(format_clock(3661), "01:01:01")
        self.assertEqual(format_srt_clock(1.234), "00:00:01,234")

    def test_filename_is_windows_safe(self) -> None:
        self.assertEqual(safe_filename('A/B:C*D?'), "A_B_C_D")

    def test_bundle_exports_text_markdown_srt_and_json(self) -> None:
        first = PartTranscript(
            self.parts[0],
            "B站公开字幕",
            "zh-CN",
            (Segment(0, 1, "第一句"), Segment(2, 3.5, "第二句")),
        )
        second = PartTranscript(
            self.parts[1],
            "Faster-Whisper 本地转写",
            "zh",
            (Segment(1, 2, "第三句"),),
        )
        bundle = TranscriptBundle(video=self.video, parts=[first, second], created_at="2026-01-01T00:00:00+08:00")

        text = bundle.to_text(timestamps=True)
        self.assertIn("P1 · 开场", text)
        self.assertIn("[00:02] 第二句", text)

        markdown = bundle.to_markdown()
        self.assertIn("# 测试 / 视频", markdown)
        self.assertIn("Faster-Whisper 本地转写", markdown)

        srt = bundle.to_srt()
        self.assertIn("00:00:00,000 --> 00:00:01,000", srt)
        self.assertIn("00:00:11,000 --> 00:00:12,000", srt)

        payload = json.loads(bundle.to_json())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["parts"][1]["text"], "第三句")
        self.assertEqual(bundle.character_count, 9)


if __name__ == "__main__":
    unittest.main()

