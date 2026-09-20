from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QLabel, QLineEdit

from bilitranscript_app.batch import BatchItemResult, BatchResult
from bilitranscript_app.history import HistoryStore
from bilitranscript_app.main_window import MainWindow
from bilitranscript_app.models import (
    AvailabilityReport,
    ExtractionIssue,
    PartAvailability,
    PartTranscript,
    RouteAvailability,
    Segment,
    TranscriptBundle,
    VideoInfo,
    VideoPart,
)


class UiShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def make_window(self) -> MainWindow:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        return MainWindow(
            autostart_services=False,
            qsettings=QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat),
            history_store=HistoryStore(root / "history.sqlite3"),
        )

    def test_idle_home_contains_only_one_input_and_submit_control(self) -> None:
        window = self.make_window()
        try:
            idle = window.home_page.idle_page
            self.assertEqual(len(idle.findChildren(QLineEdit)), 1)
            self.assertIs(window.home_page.stack.currentWidget(), idle)
            self.assertEqual(window.nav_home.text().strip().endswith("首页"), True)
        finally:
            window.close()

    def test_duplicate_submission_is_ignored_while_active(self) -> None:
        window = self.make_window()
        try:
            with patch.object(window, "_start_single") as start:
                window._submit_source("BV1abcdefghij")
                window._submit_source("BV1abcdefghij")
            start.assert_called_once_with("BV1abcdefghij")
        finally:
            window.close()

    def test_enter_and_arrow_share_the_same_deduplicated_submission(self) -> None:
        window = self.make_window()
        try:
            window.url_input.setText("BV1abcdefghij")
            with patch.object(window, "_start_single") as start:
                window.home_page._submit()
                window.home_page.submit_button.click()
            start.assert_called_once_with("BV1abcdefghij")
        finally:
            window.close()

    def test_multiple_sources_go_directly_to_batch(self) -> None:
        window = self.make_window()
        try:
            with patch.object(window, "_start_batch") as start:
                window._submit_source("BV1abcdefghij 以及 https://www.bilibili.com/video/BV1zyxwvutsr")
            start.assert_called_once_with(("BV1abcdefghij", "BV1zyxwvutsr"))
        finally:
            window.close()

    def test_loaded_video_extracts_every_part_with_submission_snapshot(self) -> None:
        window = self.make_window()
        parts = (VideoPart(1, 1, "一"), VideoPart(2, 2, "二"))
        video = VideoInfo("BV1abcdefghij", 1, "标题", "UP", 10, "", 0, "", parts)
        try:
            window._submission_active = True
            window._active_snapshot = window.app_settings.snapshot()
            with patch("bilitranscript_app.modern_window.ExtractionTask") as task_type:
                task = task_type.return_value
                window._video_loaded(video)
            self.assertEqual(task_type.call_args.args[1], list(parts))
            self.assertIs(task_type.call_args.args[2], window._active_snapshot.extraction)
            task.start.assert_called_once_with()
        finally:
            window.extraction_task = None
            window._submission_active = False
            window.close()

    def test_working_and_batch_result_controls_are_actionable(self) -> None:
        window = self.make_window()
        try:
            window.home_page.working_cancel.hide()
            window.home_page.show_working("处理中")
            self.assertTrue(window.home_page.working_cancel.isVisibleTo(window.home_page.working_page))
            result = BatchResult((BatchItemResult(0, "BV1abcdefghij", error="失败"),))
            window.home_page.show_batch_result(result)
            self.assertTrue(window.home_page.result_copy.isVisibleTo(window.home_page.result_page))
            self.assertFalse(window.home_page.result_export.isVisibleTo(window.home_page.result_page))
            window._copy_transcript()
            self.assertIn("BV1abcdefghij", QApplication.clipboard().text())
        finally:
            window.close()

    def test_cancel_dispatches_to_running_desktop_tasks(self) -> None:
        window = self.make_window()
        try:
            extraction = MagicMock()
            batch = MagicMock()
            window.extraction_task = extraction
            window.batch_task = batch
            window._cancel_active_task()
            extraction.cancel.assert_called_once_with()
            batch.cancel.assert_called_once_with()
            self.assertTrue(window._cancel_requested)
        finally:
            window.extraction_task = None
            window.batch_task = None
            window.close()

    def test_partial_result_and_history_open_reuse_home_reader(self) -> None:
        window = self.make_window()
        part = VideoPart(1, 1, "正文", 8)
        video = VideoInfo("BV1abcdefghij", 1, "部分成功", "UP", 8, "", 0, "", (part,))
        bundle = TranscriptBundle(
            video,
            [PartTranscript(part, "B站 AI 字幕", "zh", (Segment(0, 1, "内容"),))],
            [ExtractionIssue(2, "缺失", "没有字幕")],
        )
        try:
            entry = window.history_store.save_result("BV1abcdefghij", bundle)
            window._history_entry_selected(window.history_store.get(entry.id))
            self.assertIs(window.pages.currentWidget(), window.home_page)
            self.assertIs(window.home_page.stack.currentWidget(), window.home_page.result_page)
            self.assertTrue(window.home_page.result_issue.isVisibleTo(window.home_page.result_page))
            self.assertIn("B站 AI 字幕", window.home_page.result_meta.text())
        finally:
            window.close()

    def test_transcript_timeline_uses_one_stable_point_and_seeks_text(self) -> None:
        window = self.make_window()
        first_part = VideoPart(1, 1, "第一段", 10)
        second_part = VideoPart(2, 2, "第二段", 20)
        video = VideoInfo("BV1abcdefghij", 1, "时间轴", "UP", 30, "", 0, "", (first_part, second_part))
        bundle = TranscriptBundle(
            video,
            [
                PartTranscript(first_part, "AI 字幕", "zh", (Segment(2, 5, "第一句"),)),
                PartTranscript(second_part, "AI 字幕", "zh", (Segment(1, 4, "第二句"),)),
            ],
        )
        try:
            page = window.home_page
            page.show_result(bundle)
            entries = page.result_timeline.entries
            self.assertEqual(page.result_editor.verticalScrollBarPolicy(), Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.assertEqual(len(entries), 2)
            self.assertEqual((entries[0].start, entries[0].end), (2.0, 5.0))
            self.assertEqual((entries[1].start, entries[1].end), (11.0, 14.0))
            page.result_timeline.seek_time(12)
            self.assertEqual(page.result_editor.textCursor().position(), entries[1].text_start)
            self.assertEqual(page.result_timeline.current_time, 12)
            surface_layout = page.transcript_surface.layout()
            body_indent = surface_layout.contentsMargins().left() + page.result_timeline.width() + surface_layout.spacing()
            self.assertEqual(page.result_heading.contentsMargins().left(), 0)
            self.assertGreater(body_indent, page.result_heading.contentsMargins().left())
            page._show_timeline_tooltip(12, QPoint(0, 0))
            self.assertEqual(page.timeline_tooltip.text(), "00:12")
            self.assertNotIn("\n", page.timeline_tooltip.text())
        finally:
            window.close()

    def test_diagnostic_renders_each_real_route_and_attempt_count(self) -> None:
        window = self.make_window()
        part = VideoPart(1, 1, "正文", 8)
        video = VideoInfo("BV1abcdefghij", 1, "诊断", "UP", 8, "", 0, "", (part,))
        routes = (
            RouteAvailability("public", True, "中文字幕", attempts=1, track_count=1),
            RouteAvailability("anonymous", False, "不可用", attempts=2),
            RouteAvailability("browser", True, "AI 字幕", attempts=2, track_count=1),
            RouteAvailability("asr", True, "服务可用", attempts=1),
        )
        try:
            window._diagnostic_succeeded(AvailabilityReport(video, (PartAvailability(part, routes),)))
            output = window.settings_page.diagnostic_results.toPlainText()
            self.assertIn("公开 ✓ · 1 次", output)
            self.assertIn("匿名 × · 2 次", output)
            self.assertIn("登录 ✓ · 2 次", output)
            self.assertIn("ASR ✓ · 1 次", output)
        finally:
            window.close()

    def test_settings_contain_real_collapsed_diagnostic_and_persist_typed_export_dir(self) -> None:
        window = self.make_window()
        try:
            page = window.settings_page
            self.assertFalse(page.diagnostic_panel.isVisible())
            page.diagnostic_button.setChecked(True)
            self.assertTrue(page.diagnostic_results.isReadOnly())
            target = Path(window.history_store.path).parent / "exports"
            page.export_dir.setText(str(target))
            page._save_export_dir()
            self.assertEqual(window.app_settings.default_export_dir, target)
        finally:
            window.close()

    def test_settings_use_visual_width_order_and_hide_spin_buttons(self) -> None:
        window = self.make_window()
        try:
            page = window.settings_page
            self.assertTrue(page.stack.widget(1).isAncestorOf(page.backend_combo))
            self.assertFalse(page.stack.widget(3).isAncestorOf(page.backend_combo))
            self.assertLess(page.batch_spin.maximumWidth(), page.mode_combo.maximumWidth())
            self.assertLess(page.mode_combo.maximumWidth(), page.backend_combo.maximumWidth())
            self.assertLess(page.backend_combo.maximumWidth(), page.model_edit.maximumWidth())
            for control in (page.batch_spin, page.asr_timeout, page.api_port):
                self.assertEqual(control.buttonSymbols(), QAbstractSpinBox.ButtonSymbols.NoButtons)
            about = " ".join(label.text() for label in page.stack.widget(5).findChildren(QLabel))
            self.assertNotIn("固定规则", about)
            self.assertIn("MIT License", about)
        finally:
            window.close()

    def test_next_extraction_clears_result_and_focuses_home(self) -> None:
        window = self.make_window()
        try:
            window.bundle = None
            window._next_extraction()
            self.assertEqual(window.pages.currentWidget(), window.home_page)
            self.assertEqual(window.home_page.url_input.text(), "")
        finally:
            window.close()

    def test_main_window_modules_import_without_a_cycle(self) -> None:
        from bilitranscript_app import main_window, modern_window

        self.assertIs(main_window.MainWindow, modern_window.ModernMainWindow)

    def test_history_refresh_does_not_duplicate_loaded_pages(self) -> None:
        window = self.make_window()
        try:
            for index in range(60):
                window.history_store.save_terminal(f"BV1page{index:06d}", "failed", error="测试")
            page = window.history_page
            page.refresh()
            page._load_more()
            self.assertEqual(page.list.count(), 60)
            window.history_store.save_terminal("BV1newrecord0", "failed", error="新增")
            page.refresh()
            self.assertEqual(page.list.count(), 61)
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
