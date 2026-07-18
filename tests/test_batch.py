from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest import mock

from bilitranscript_app.batch import BatchExtractionTask, batch_output_filename, extract_bilibili_sources
from bilitranscript_app.bilibili import BilibiliError
from bilitranscript_app.extractor import ExtractionOptions
from bilitranscript_app.models import PartTranscript, Segment, TranscriptBundle, VideoInfo, VideoPart


def batch_video(bvid: str, title: str) -> VideoInfo:
    part = VideoPart(1, 100, "正文", 5)
    return VideoInfo(bvid, 1, title, "UP", 5, "", 0, "", (part,))


class FakeBatchClient:
    videos = {
        "BV1abcdefghij": batch_video("BV1abcdefghij", "第一个视频"),
        "BV1zyxwvutsr": batch_video("BV1zyxwvutsr", "第二个视频"),
    }

    def fetch_video(self, source: str) -> VideoInfo:
        if source == "bad":
            raise BilibiliError("读取失败")
        return self.videos[source]


class FakeBatchExtractor:
    def extract(self, video: VideoInfo, parts: list[VideoPart], options: ExtractionOptions, **kwargs) -> TranscriptBundle:
        transcript = PartTranscript(parts[0], "测试字幕", "zh-CN", (Segment(0, 1, video.title),))
        return TranscriptBundle(video, [transcript])


class BatchSourceTests(unittest.TestCase):
    def test_extracts_mixed_sources_in_order_and_deduplicates(self) -> None:
        text = (
            "收藏： https://www.bilibili.com/video/BV1abcdefghij/?p=1。"
            "然后是 BV1abcdefghij 和 https://b23.tv/short-code，最后 av12345。"
        )
        self.assertEqual(
            extract_bilibili_sources(text),
            ("BV1abcdefghij", "https://b23.tv/short-code", "av12345"),
        )

    def test_handles_urls_without_scheme_and_trailing_punctuation(self) -> None:
        text = "www.bilibili.com/video/BV1abcdefghij/\nhttps://b23.tv/abc123)..."
        self.assertEqual(
            extract_bilibili_sources(text),
            ("BV1abcdefghij", "https://b23.tv/abc123"),
        )

    def test_does_not_extract_domains_embedded_in_other_words(self) -> None:
        text = "notbilibili.com/video/BV1abcdefghij and x-b23.tv/abc123"
        self.assertEqual(extract_bilibili_sources(text), ("BV1abcdefghij",))

    def test_preserves_standalone_id_order_relative_to_urls(self) -> None:
        text = "先 BV1zyxwvutsr，再看 https://b23.tv/abc123。"
        self.assertEqual(
            extract_bilibili_sources(text),
            ("BV1zyxwvutsr", "https://b23.tv/abc123"),
        )

    def test_batch_filename_contains_safe_title_and_bvid(self) -> None:
        video = VideoInfo(
            bvid="BV1abcdefghij",
            aid=1,
            title="标题 / 带冒号:测试",
            owner="UP",
            duration=10,
            cover_url="",
            published_at=0,
            description="",
            parts=(),
        )
        name = batch_output_filename(video)
        self.assertEqual(name, "标题 _ 带冒号_测试__BV1abcdefghij.md")

    def test_parallel_task_exports_each_success_and_keeps_failures_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            successes = []
            with mock.patch("bilitranscript_app.batch.BilibiliClient", return_value=FakeBatchClient()), mock.patch(
                "bilitranscript_app.batch.TranscriptExtractor", return_value=FakeBatchExtractor()
            ):
                task = BatchExtractionTask(
                    ("BV1abcdefghij", "bad", "BV1zyxwvutsr"),
                    ExtractionOptions(mode="public"),
                    Path(temporary),
                    max_workers=3,
                )
                task.succeeded.connect(successes.append)
                task.run()

            self.assertEqual(len(successes), 1)
            result = successes[0]
            self.assertEqual(result.success_count, 2)
            files = sorted(Path(temporary).glob("*.md"))
            self.assertEqual([path.name for path in files], [
                "第一个视频__BV1abcdefghij.md",
                "第二个视频__BV1zyxwvutsr.md",
            ])
            self.assertIn("# 第一个视频", files[0].read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
