from __future__ import annotations

import unittest

from bilitranscript_app.browser_bridge import StandaloneBrowserBridge
from bilitranscript_app.models import VideoInfo, VideoPart


class FakeBridge(StandaloneBrowserBridge):
    def is_running(self) -> bool:
        return True

    def _http_json(self, path: str, *, method: str = "GET", timeout: float | None = None):
        if path == "/json/list":
            return [
                {
                    "id": "target-1",
                    "type": "page",
                    "url": "https://www.bilibili.com/video/BV1abcdefghij/",
                    "title": "测试视频",
                    "webSocketDebuggerUrl": "ws://127.0.0.1:39271/devtools/page/target-1",
                }
            ]
        raise AssertionError(f"unexpected path {path}")

    def _evaluate_target(self, target, javascript: str, timeout: float = 45):
        return {
            "isLogin": True,
            "uname": "tester",
            "code": 0,
            "subtitles": [
                {
                    "lan": "ai-zh",
                    "lan_doc": "中文（自动生成）",
                    "ai_status": 1,
                    "subtitle_url": "//aisubtitle.hdslb.com/ai.json",
                }
            ],
        }


class BrowserBridgeTests(unittest.TestCase):
    def test_reads_ai_track_from_dedicated_browser_without_cookie_copy(self) -> None:
        part = VideoPart(1, 123, "正文", 30)
        video = VideoInfo("BV1abcdefghij", 99, "测试", "UP", 30, "", 0, "", (part,))
        bridge = FakeBridge()
        target = bridge.find_video_target(video.bvid)
        self.assertEqual(target.target_id if target else None, "target-1")
        tracks = bridge.fetch_tracks(video, part)
        self.assertEqual(len(tracks), 1)
        self.assertTrue(tracks[0].is_ai)
        self.assertEqual(tracks[0].url, "https://aisubtitle.hdslb.com/ai.json")


if __name__ == "__main__":
    unittest.main()
