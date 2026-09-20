from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import QSettings

from bilitranscript_app.history import HistoryStore
from bilitranscript_app.models import PartTranscript, Segment, TranscriptBundle, VideoInfo, VideoPart
from bilitranscript_app.settings_model import AppSettings


class SettingsAndHistoryTests(unittest.TestCase):
    def test_settings_persist_and_snapshot_is_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.ini"
            first = AppSettings(QSettings(str(path), QSettings.Format.IniFormat))
            first.set_theme("light")
            first.set_extraction(mode="asr", backend="openai-whisper", model="small")
            first.set_asr(base_url="http://secret-host:8765/v1", api_key="upstream-secret", timeout=30)
            snapshot = first.snapshot()
            self.assertEqual(snapshot.theme, "light")
            self.assertTrue(first.api_auto_start)
            self.assertEqual(snapshot.extraction.asr_model, "small")
            self.assertNotIn("secret", repr(snapshot.public_dict()))
            second = AppSettings(QSettings(str(path), QSettings.Format.IniFormat))
            self.assertEqual(second.theme, "light")
            self.assertEqual(second.extraction_options().asr_backend, "openai-whisper")

    def test_backend_models_are_persisted_independently(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            settings = AppSettings(QSettings(str(Path(temporary) / "settings.ini"), QSettings.Format.IniFormat))
            settings.set_extraction(backend="faster-whisper", model="large-v3-turbo")
            settings.set_extraction(backend="openai-whisper", model="medium")
            self.assertEqual(settings.extraction_options().asr_model, "medium")
            settings.set_extraction(backend="faster-whisper")
            self.assertEqual(settings.extraction_options().asr_model, "large-v3-turbo")

    def test_history_is_lazy_searchable_and_keeps_full_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HistoryStore(Path(temporary) / "history.sqlite3")
            part = VideoPart(1, 123, "正文", 5)
            video = VideoInfo("BV1abcdefghij", 1, "中文标题", "测试UP", 5, "", 0, "", (part,))
            bundle = TranscriptBundle(video, [PartTranscript(part, "B站 AI 字幕", "zh", (Segment(0, 1, "你好"),))])
            entry = store.save_result("BV1abcdefghij", bundle, settings={"mode": "auto", "asr_api_key": "do-not-save"})
            summary = store.list_entries(query="中文")[0]
            self.assertIsNone(summary.bundle)
            self.assertTrue(summary.has_issues is False)
            self.assertEqual(store.get(entry.id).bundle.parts[0].text, "你好")
            self.assertEqual(store.get(entry.id).settings, {"mode": "auto"})
            failure = store.save_terminal("BV1abcdefghij", "failed", error="http://secret-host:8765/v1 secret", secrets=("secret-host", "secret"))
            self.assertNotIn("secret-host", store.get(failure.id).error)
            self.assertTrue(store.delete(entry.id))
            self.assertIsNone(store.get(entry.id))

            pending = store.save_pending("BV1zyxwvutsr")
            self.assertTrue(store.mark_running(pending.id))
            self.assertEqual(store.get(pending.id).status, "running")

    def test_history_filters_pages_and_survives_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "history.sqlite3"
            store = HistoryStore(path)
            for index in range(7):
                store.save_terminal(f"BV1home{index:07d}", "failed", error=f"主页 {index}")
            for index in range(3):
                store.save_terminal(f"BV1api{index:08d}", "cancelled", source_kind="api", error="取消")
            self.assertEqual(store.count(source_kind="home"), 7)
            self.assertEqual(store.count(source_kind="api"), 3)
            first_page = store.list_entries(source_kind="home", limit=3)
            second_page = store.list_entries(source_kind="home", limit=3, offset=3)
            self.assertEqual(len(first_page), 3)
            self.assertEqual(len(second_page), 3)
            self.assertFalse({entry.id for entry in first_page} & {entry.id for entry in second_page})
            reopened = HistoryStore(path)
            self.assertEqual(reopened.count(source_kind="all"), 10)
            self.assertEqual(reopened.clear(), 10)
            self.assertEqual(reopened.count(source_kind="all"), 0)

    def test_history_marks_tasks_from_a_previous_process_as_interrupted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = HistoryStore(Path(temporary) / "history.sqlite3")
            queued = store.save_pending("BV1abcdefghij")
            running = store.save_terminal("BV1zyxwvutsr", "running", error="")
            finished = store.save_terminal("BV1finished00", "cancelled", error="已取消")
            self.assertEqual(store.mark_interrupted(), 2)
            self.assertEqual(store.get(queued.id).status, "failed")
            self.assertEqual(store.get(running.id).error, "程序上次运行时中断")
            self.assertEqual(store.get(finished.id).status, "cancelled")


if __name__ == "__main__":
    unittest.main()
