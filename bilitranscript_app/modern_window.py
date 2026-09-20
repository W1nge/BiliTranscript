from __future__ import annotations

"""The compact Acrylic desktop shell used by BiliTranscript 1.0.

The extraction engine remains in ``extractor.py``.  This module is only a view
and orchestration layer: every task receives an immutable settings snapshot and
every completed task is sent to the same HistoryStore.
"""

import ctypes
import re
import socket
import sys
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCloseEvent, QDragEnterEvent, QDropEvent, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .api_server import (
    DEFAULT_EXTRACTION_API_HOST,
    ExtractionApiServer,
    ExtractionJob,
)
from .asr import AsrError
from .asr_api import (
    ASR_API_BACKEND,
    DEFAULT_ASR_API_BASE_URL,
    DEFAULT_ASR_API_MODEL,
    AsrApiSettings,
    OpenAICompatibleAsrRuntime,
)
from .batch import BatchExtractionTask, BatchResult
from .composition_backdrop import (
    CompositionBackdrop,
    WINDOWPOS,
    composition_backdrop_available,
    preload_composition_runtime,
)
from .history import HistoryEntry, HistoryStore, sanitize_error
from .login_controller import LoginController
from .models import AvailabilityReport, TranscriptBundle, VideoInfo, format_clock, format_duration, safe_filename
from .settings_model import AppSettings, SettingsSnapshot
from .sources import extract_bilibili_sources
from .startup import StartupError, is_startup_enabled, set_startup_enabled
from .windows_effects import AcrylicState, acrylic_supported, effective_theme
from .workers import (
    ApiServerStopTask,
    AsrHealthTask,
    AvailabilityTask,
    BrowserLaunchTask,
    BrowserStatusTask,
    ExtractionTask,
    MetadataTask,
)


# Settings layout tokens. Keep every category on one 8 px-based rhythm instead
# of letting individual controls inherit unrelated size hints.
SETTINGS_CONTENT_WIDTH = 760
SETTINGS_LABEL_WIDTH = 104
SETTINGS_COLUMN_GAP = 24
SETTINGS_ROW_GAP = 14
SETTINGS_SELECT_WIDTH = 360
SETTINGS_WIDE_SELECT_WIDTH = 420
SETTINGS_MODEL_WIDTH = 520
SETTINGS_LONG_FIELD_WIDTH = 560
SETTINGS_NUMBER_WIDTH = 140


def _lan_address(port: int) -> str:
    host = ""
    try:
        # No packet is sent; connect only asks Windows which adapter would be
        # used for a normal LAN/Internet route.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            host = str(probe.getsockname()[0])
    except OSError:
        pass
    if not host or host.startswith("127."):
        try:
            candidates = {
                item[4][0]
                for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                if item[4][0] and not item[4][0].startswith("127.")
            }
            host = sorted(candidates)[0] if candidates else "127.0.0.1"
        except OSError:
            host = "127.0.0.1"
    return f"http://{host}:{int(port)}  （本机 http://127.0.0.1:{int(port)}）"


def _history_time(value: str | None) -> str:
    """Render stored ISO timestamps as compact local UI text."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone()
        now = datetime.now().astimezone()
        return f"今天 {moment:%H:%M}" if moment.date() == now.date() else f"{moment:%Y-%m-%d %H:%M}"
    except (TypeError, ValueError):
        return str(value).replace("T", " ")[:16]


class SourceInput(QLineEdit):
    """Single visual line that still accepts a multiline clipboard payload."""

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Paste):
            text = QApplication.clipboard().text()
            if text:
                self.insert(re.sub(r"\s+", " ", text).strip())
                return
        super().keyPressEvent(event)


class GlyphButton(QPushButton):
    """Paint small interface glyphs geometrically so font baselines cannot shift them."""

    def __init__(self, kind: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self.setText("")

    def paintEvent(self, _event) -> None:
        if self.kind == "arrow":
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            color = QColor("#E95788" if self.isDown() else "#FF79A5" if self.underMouse() else "#FF6699")
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(self.rect().adjusted(0, 0, -1, -1))
        else:
            painter = QStylePainter(self)
            option = QStyleOptionButton()
            self.initStyleOption(option)
            painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        center = self.rect().center()
        width = 2.6 if self.kind == "arrow" else 1.5
        glyph_color = QColor("#FFFFFF") if self.kind == "arrow" else self.palette().buttonText().color()
        painter.setPen(QPen(glyph_color, width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        if self.kind == "arrow":
            start = QPointF(center.x() - 8, center.y())
            end = QPointF(center.x() + 7, center.y())
            painter.drawLine(start, end)
            painter.drawLine(end, QPointF(center.x() + 2, center.y() - 5))
            painter.drawLine(end, QPointF(center.x() + 2, center.y() + 5))
        elif self.kind == "minimize":
            painter.drawLine(QPointF(center.x() - 6, center.y()), QPointF(center.x() + 6, center.y()))
        elif self.kind == "maximize":
            if self.window().isMaximized():
                painter.drawRect(center.x() - 4, center.y() - 5, 9, 9)
                painter.drawRect(center.x() - 6, center.y() - 3, 9, 9)
            else:
                painter.drawRect(center.x() - 5, center.y() - 5, 10, 10)
        elif self.kind == "close":
            painter.drawLine(QPointF(center.x() - 5, center.y() - 5), QPointF(center.x() + 5, center.y() + 5))
            painter.drawLine(QPointF(center.x() + 5, center.y() - 5), QPointF(center.x() - 5, center.y() + 5))
        elif self.kind == "search":
            painter.drawEllipse(QPointF(center.x() - 2, center.y() - 2), 5, 5)
            painter.drawLine(QPointF(center.x() + 2, center.y() + 2), QPointF(center.x() + 7, center.y() + 7))
        elif self.kind == "menu":
            painter.setPen(QPen(self.palette().buttonText().color(), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            for offset in (-6, 0, 6):
                painter.drawPoint(QPointF(center.x() + offset, center.y()))


class SettingsSwitch(QCheckBox):
    """A compact, non-animated switch for binary settings."""

    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(32)

    def sizeHint(self) -> QSize:
        width = 48 + self.fontMetrics().horizontalAdvance(self.text())
        return QSize(width, max(32, self.fontMetrics().height() + 10))

    def hitButton(self, point) -> bool:
        return self.rect().contains(point)

    def paintEvent(self, _event) -> None:
        if self.width() <= 0 or self.height() <= 0:
            return
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setOpacity(1.0 if self.isEnabled() else 0.45)
        text_color = self.palette().color(self.foregroundRole())
        track = QRectF(0.5, (self.height() - 20) / 2, 36, 20)
        if self.isChecked():
            painter.setPen(QPen(QColor("#FB7299"), 1))
            painter.setBrush(QColor("#FF8EAE") if self.underMouse() else QColor("#FB7299"))
        else:
            muted = QColor(text_color)
            muted.setAlpha(72 if not self.underMouse() else 105)
            border = QColor(text_color)
            border.setAlpha(110)
            painter.setPen(QPen(border, 1))
            painter.setBrush(muted)
        painter.drawRoundedRect(track, 10, 10)
        knob_x = 18.5 if self.isChecked() else 2.5
        knob = QRectF(knob_x, (self.height() - 16) / 2, 16, 16)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FFFFFF") if self.isChecked() else text_color)
        painter.drawEllipse(knob)
        painter.setPen(text_color)
        painter.drawText(
            QRectF(48, 0, max(0, self.width() - 48), self.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.text(),
        )


class TitleDragArea(QWidget):
    """Transparent drag target used by the frameless Acrylic window."""

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle is not None:
                handle.startSystemMove()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            window = self.window()
            window.showNormal() if window.isMaximized() else window.showMaximized()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    start: float
    end: float
    text_start: int
    text_end: int


class TranscriptTimeline(QWidget):
    """Vertical transcript seek bar with one stable point-in-time marker."""

    entry_selected = Signal(int)
    hover_changed = Signal(float, object)
    hover_left = Signal()

    def __init__(self, editor: QPlainTextEdit, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self.entries: tuple[TimelineEntry, ...] = ()
        self.duration = 0.0
        self.current_time = 0.0
        self._dragging = False
        self.setFixedWidth(20)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("拖动以跳转到对应文稿")

    def set_entries(self, entries: list[TimelineEntry], duration: float) -> None:
        self.entries = tuple(entries)
        self.duration = max(float(duration or 0), max((entry.end for entry in entries), default=0.0), 0.05)
        self.current_time = entries[0].start if entries else 0.0
        self.setVisible(bool(entries))
        self.update()

    @property
    def dragging(self) -> bool:
        return self._dragging

    def set_current_time(self, value: float) -> None:
        normalized = min(self.duration, max(0.0, float(value)))
        if abs(normalized - self.current_time) > 0.001:
            self.current_time = normalized
            self.update()

    def _track_bounds(self) -> tuple[float, float]:
        line_height = max(1.0, float(self.editor.fontMetrics().height()))
        return line_height / 2, max(line_height / 2 + 1.0, float(self.height()) - line_height / 2)

    def _time_at_y(self, y: float) -> float:
        top, bottom = self._track_bounds()
        ratio = min(1.0, max(0.0, (float(y) - top) / max(1.0, bottom - top)))
        return ratio * self.duration

    def _y_at_time(self, value: float) -> float:
        top, bottom = self._track_bounds()
        return top + min(1.0, max(0.0, float(value) / self.duration)) * (bottom - top)

    def _entry_index_at_time(self, value: float) -> int:
        if not self.entries:
            return -1
        selected = 0
        for index, entry in enumerate(self.entries):
            if entry.start > value:
                break
            selected = index
        return selected

    def seek_time(self, value: float) -> None:
        value = min(self.duration, max(0.0, float(value)))
        self.set_current_time(value)
        index = self._entry_index_at_time(value)
        if index >= 0:
            self.entry_selected.emit(index)

    def _seek_y(self, y: float) -> None:
        self.seek_time(self._time_at_y(y))

    def paintEvent(self, _event) -> None:
        if not self.entries or self.width() <= 0 or self.height() <= 0:
            return
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        top, bottom = self._track_bounds()
        center_x = self.width() / 2
        track_color = self.palette().color(self.foregroundRole())
        track_color.setAlpha(120)
        painter.setPen(QPen(track_color, 3.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(QPointF(center_x, top), QPointF(center_x, bottom))

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#FB7299"))
        painter.drawEllipse(QPointF(center_x, self._y_at_time(self.current_time)), 6.0, 6.0)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.entries:
            self._dragging = True
            self._seek_y(event.position().y())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if not self.entries:
            return
        current = self._time_at_y(event.position().y())
        self.hover_changed.emit(current, event.globalPosition().toPoint())
        if self._dragging:
            self._seek_y(event.position().y())

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event) -> None:
        self._dragging = False
        self.hover_left.emit()
        super().leaveEvent(event)


class HomePage(QWidget):
    submit_requested = Signal(str)
    cancel_requested = Signal()
    copy_requested = Signal()
    export_requested = Signal()
    next_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("homePage")
        self.stack = QStackedWidget(self)
        self.idle_page = self._build_idle()
        self.working_page = self._build_working()
        self.result_page = self._build_result()
        self.error_page = self._build_error()
        for page in (self.idle_page, self.working_page, self.result_page, self.error_page):
            self.stack.addWidget(page)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.addWidget(self.stack)
        self.bundle: TranscriptBundle | None = None
        self._show_idle_focus = True
        self._working_phase = 0
        self._working_animation = QTimer(self)
        self._working_animation.setInterval(360)
        self._working_animation.timeout.connect(self._advance_working_animation)

    def _switch(self, page: QWidget) -> None:
        """Switch state without an off-screen opacity render."""
        self.stack.setCurrentWidget(page)

    def _advance_working_animation(self) -> None:
        frames = ("正在提取", "正在提取 ·", "正在提取 ··", "正在提取 ···")
        self._working_phase = (self._working_phase + 1) % len(frames)
        self.working_title.setText(frames[self._working_phase])

    def _start_working_animation(self) -> None:
        self._working_phase = 0
        self.working_title.setText("正在提取")
        if not self._working_animation.isActive():
            self._working_animation.start()

    def _stop_working_animation(self) -> None:
        self._working_animation.stop()
        self._working_phase = 0

    def _build_idle(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(80, 80, 80, 80)
        layout.addStretch(1)
        row = QHBoxLayout()
        row.setSpacing(0)
        row.addStretch(1)
        self.input_frame = QFrame()
        self.input_frame.setObjectName("heroInputFrame")
        self.input_frame.setFixedHeight(68)
        self.input_frame.setMinimumWidth(560)
        self.input_frame.setMaximumWidth(680)
        self.input_frame.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        input_layout = QHBoxLayout(self.input_frame)
        input_layout.setContentsMargins(20, 8, 8, 8)
        input_layout.setSpacing(8)
        self.url_input = SourceInput()
        self.url_input.setObjectName("heroInput")
        self.url_input.setPlaceholderText("粘贴 B站链接、BV / av 号或包含多个链接的文本")
        self.url_input.setClearButtonEnabled(False)
        self.url_input.setAcceptDrops(True)
        self.url_input.returnPressed.connect(self._submit)
        input_layout.addWidget(self.url_input, 1)
        self.submit_button = GlyphButton("arrow")
        self.submit_button.setObjectName("heroSubmit")
        self.submit_button.setFixedSize(52, 52)
        self.submit_button.setToolTip("开始提取（Enter）")
        self.submit_button.clicked.connect(self._submit)
        input_layout.addWidget(self.submit_button)
        row.addWidget(self.input_frame, 0)
        row.addStretch(1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _build_working(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(100, 100, 100, 100)
        layout.addStretch(1)
        self.working_title = QLabel("正在准备…")
        self.working_title.setObjectName("pageTitle")
        self.working_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.working_title)
        self.working_detail = QLabel("")
        self.working_detail.setObjectName("statusText")
        self.working_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.working_detail.setWordWrap(True)
        layout.addWidget(self.working_detail)
        self.working_progress = QProgressBar()
        self.working_progress.setTextVisible(False)
        self.working_progress.setRange(0, 100)
        layout.addWidget(self.working_progress)
        self.working_cancel = QPushButton("取消")
        self.working_cancel.setObjectName("dangerButton")
        self.working_cancel.clicked.connect(self.cancel_requested)
        layout.addWidget(self.working_cancel, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return page

    def _build_result(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(48, 52, 48, 34)
        root.setSpacing(12)
        heading = QVBoxLayout()
        self.result_heading = heading
        heading.setContentsMargins(0, 0, 0, 0)
        heading.setSpacing(24)
        self.result_title = QLabel("")
        self.result_title.setObjectName("resultContentTitle")
        self.result_title.setWordWrap(True)
        heading.addWidget(self.result_title)
        meta_row = QHBoxLayout()
        meta_row.setSpacing(10)
        self.result_meta = QLabel("")
        self.result_meta.setObjectName("meta")
        self.result_meta.setWordWrap(True)
        meta_row.addWidget(self.result_meta, 1)
        self.result_copy = QPushButton("复制")
        self.result_copy.clicked.connect(self.copy_requested)
        meta_row.addWidget(self.result_copy)
        self.result_export = QPushButton("导出")
        self.result_export.setObjectName("primaryButton")
        self.result_export.clicked.connect(self.export_requested)
        meta_row.addWidget(self.result_export)
        heading.addLayout(meta_row)
        root.addLayout(heading)
        self.result_issue = QLabel("")
        self.result_issue.setObjectName("meta")
        self.result_issue.setWordWrap(True)
        self.result_issue.hide()
        root.addWidget(self.result_issue)
        self.result_issue_toggle = QPushButton("查看问题")
        self.result_issue_toggle.setObjectName("ghostButton")
        self.result_issue_toggle.setCheckable(True)
        self.result_issue_toggle.hide()
        root.addWidget(self.result_issue_toggle, 0, Qt.AlignmentFlag.AlignLeft)
        self.result_issue_details = QLabel("")
        self.result_issue_details.setObjectName("meta")
        self.result_issue_details.setWordWrap(True)
        self.result_issue_details.hide()
        root.addWidget(self.result_issue_details)
        self.result_issue_toggle.toggled.connect(self.result_issue_details.setVisible)
        editor_area = QGridLayout()
        editor_area.setContentsMargins(0, 0, 0, 0)
        self.transcript_surface = QFrame()
        self.transcript_surface.setObjectName("transcriptSurface")
        transcript_layout = QHBoxLayout(self.transcript_surface)
        transcript_layout.setContentsMargins(16, 14, 12, 14)
        transcript_layout.setSpacing(12)
        self.result_editor = QPlainTextEdit()
        self.result_editor.setObjectName("transcriptEditor")
        self.result_editor.setReadOnly(True)
        self.result_editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.result_editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.result_editor.document().setDocumentMargin(0)
        self.result_timeline = TranscriptTimeline(self.result_editor)
        self.result_timeline.entry_selected.connect(self._timeline_entry_selected)
        self.result_timeline.hover_changed.connect(self._show_timeline_tooltip)
        self.result_timeline.hover_left.connect(self._hide_timeline_tooltip)
        transcript_layout.addWidget(self.result_timeline)
        transcript_layout.addWidget(self.result_editor, 1)
        self.timeline_tooltip = QLabel(self.transcript_surface)
        self.timeline_tooltip.setObjectName("timelineTooltip")
        self.timeline_tooltip.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.timeline_tooltip.hide()
        self._timeline_sync_suspended = False
        self._timeline_sync_timer = QTimer(self)
        self._timeline_sync_timer.setSingleShot(True)
        self._timeline_sync_timer.setInterval(120)
        self._timeline_sync_timer.timeout.connect(self._resume_timeline_sync)
        self.result_editor.verticalScrollBar().valueChanged.connect(self._sync_timeline_from_editor)
        editor_area.addWidget(self.transcript_surface, 0, 0)
        self.result_next = QPushButton("提取下一个")
        self.result_next.setObjectName("editorCornerButton")
        self.result_next.clicked.connect(self.next_requested)
        corner = QWidget()
        corner.setFixedSize(112, 50)
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 12, 12)
        corner_layout.addWidget(self.result_next)
        editor_area.addWidget(
            corner,
            0,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
        )
        root.addLayout(editor_area, 1)
        self._result_is_batch = False
        return page

    def _set_result_timeline_enabled(self, enabled: bool) -> None:
        self.result_timeline.setVisible(enabled)
        if not enabled:
            self._hide_timeline_tooltip()

    @staticmethod
    def _timeline_data(bundle: TranscriptBundle, text: str, *, timestamps: bool) -> tuple[list[TimelineEntry], float]:
        entries: list[TimelineEntry] = []
        marker = f"- 提取时间：{bundle.created_at}"
        search_from = max(0, text.find(marker) + len(marker))
        part_offset = 0.0
        for transcript in bundle.parts:
            part_end = 0.0
            for raw_segment in transcript.segments:
                segment = raw_segment.normalized()
                if not segment.text:
                    continue
                line = f"[{format_clock(segment.start)}] {raw_segment.text}" if timestamps else str(raw_segment.text)
                token = f"\n{line}\n"
                token_position = text.find(token, search_from)
                if token_position >= 0:
                    text_start = token_position + 1 + len(line) - len(str(raw_segment.text))
                else:
                    text_start = text.find(str(raw_segment.text), search_from)
                if text_start < 0:
                    continue
                text_end = text_start + len(str(raw_segment.text))
                search_from = text_end
                entries.append(
                    TimelineEntry(
                        start=part_offset + segment.start,
                        end=part_offset + segment.end,
                        text_start=text_start,
                        text_end=text_end,
                    )
                )
                part_end = max(part_end, segment.end)
            part_offset += max(float(transcript.part.duration or 0), part_end)
        duration = max(part_offset, float(bundle.video.duration or 0), max((entry.end for entry in entries), default=0.0))
        return entries, duration

    def _timeline_entry_selected(self, index: int) -> None:
        if not (0 <= index < len(self.result_timeline.entries)):
            return
        cursor = self.result_editor.textCursor()
        cursor.clearSelection()
        cursor.setPosition(self.result_timeline.entries[index].text_start)
        self._timeline_sync_suspended = True
        self.result_editor.setTextCursor(cursor)
        self.result_editor.centerCursor()
        self._timeline_sync_timer.start()

    def _resume_timeline_sync(self) -> None:
        self._timeline_sync_suspended = False

    def _sync_timeline_from_editor(self) -> None:
        entries = self.result_timeline.entries
        if not entries or self._timeline_sync_suspended or self.result_timeline.dragging:
            return
        position = self.result_editor.cursorForPosition(QPoint(1, 1)).position()
        index = len(entries) - 1
        for candidate, entry in enumerate(entries):
            if position <= entry.text_end:
                index = candidate
                break
        self.result_timeline.set_current_time(entries[index].start)

    def _show_timeline_tooltip(self, current: float, global_position: QPoint) -> None:
        self.timeline_tooltip.setText(format_clock(current))
        self.timeline_tooltip.adjustSize()
        local = self.transcript_surface.mapFromGlobal(global_position)
        x = 44
        y = max(8, min(self.transcript_surface.height() - self.timeline_tooltip.height() - 8, local.y() - self.timeline_tooltip.height() // 2))
        self.timeline_tooltip.move(x, y)
        self.timeline_tooltip.show()
        self.timeline_tooltip.raise_()

    def _hide_timeline_tooltip(self) -> None:
        self.timeline_tooltip.hide()

    def _build_error(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(100, 100, 100, 100)
        layout.addStretch(1)
        self.error_title = QLabel("提取失败")
        self.error_title.setObjectName("pageTitle")
        self.error_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.error_title)
        self.error_detail = QLabel("")
        self.error_detail.setObjectName("errorText")
        self.error_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.error_detail.setWordWrap(True)
        layout.addWidget(self.error_detail)
        retry = QPushButton("提取下一个")
        retry.setObjectName("primaryButton")
        retry.clicked.connect(self.next_requested)
        layout.addWidget(retry, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return page

    def _submit(self) -> None:
        if self.url_input.isEnabled():
            self.submit_requested.emit(self.url_input.text().strip())

    def show_idle(self, *, clear: bool = False) -> None:
        self._stop_working_animation()
        self._hide_timeline_tooltip()
        self._switch(self.idle_page)
        if clear:
            self.url_input.clear()
        self.url_input.setEnabled(True)
        self.submit_button.setEnabled(True)
        self.url_input.setFocus()

    def show_working(self, message: str = "正在提取…", progress: int = 0, *, indeterminate: bool = False) -> None:
        self._switch(self.working_page)
        self._start_working_animation()
        self.url_input.setEnabled(False)
        self.submit_button.setEnabled(False)
        self.working_detail.setText(message)
        self.working_cancel.show()
        self.working_cancel.setEnabled(True)
        self.working_progress.setRange(0, 0 if indeterminate else 100)
        if not indeterminate:
            self.working_progress.setValue(max(0, min(100, int(progress))))

    def update_progress(self, progress: int, message: str) -> None:
        self.working_detail.setText(message)
        if self.working_progress.maximum() != 0:
            self.working_progress.setValue(max(0, min(100, int(progress))))

    def show_result(self, bundle: TranscriptBundle, *, timestamps: bool = False) -> None:
        self._stop_working_animation()
        self.bundle = bundle
        self._result_is_batch = False
        self.result_copy.show()
        self.result_export.show()
        self._switch(self.result_page)
        self.result_title.setText(bundle.video.title)
        sources = " / ".join(dict.fromkeys(part.source for part in bundle.parts)) or "无"
        self.result_meta.setText(
            f"{bundle.video.owner}  ·  {format_duration(bundle.video.duration)}  ·  "
            f"{len(bundle.video.parts) or len(bundle.parts)} 个分P  ·  {sources}  ·  {bundle.character_count} 字"
        )
        if bundle.issues:
            details = "；".join(f"P{i.page}：{i.message}" for i in bundle.issues)
            self.result_issue.setText(f"部分分P未完成（{len(bundle.issues)}）")
            self.result_issue_details.setText(details)
            self.result_issue.show()
            self.result_issue_toggle.show()
            self.result_issue_toggle.setChecked(False)
        else:
            self.result_issue.clear()
            self.result_issue.hide()
            self.result_issue_details.clear()
            self.result_issue_details.hide()
            self.result_issue_toggle.hide()
        text = bundle.to_markdown(timestamps=timestamps)
        self.result_editor.setPlainText(text)
        timeline_entries, duration = self._timeline_data(bundle, text, timestamps=timestamps)
        self.result_timeline.set_entries(timeline_entries, duration)
        self._set_result_timeline_enabled(bool(timeline_entries))
        self.result_editor.moveCursor(self.result_editor.textCursor().MoveOperation.Start)
        self._sync_timeline_from_editor()

    def show_batch_result(self, result: BatchResult, *, cancelled: bool = False) -> None:
        self._stop_working_animation()
        self.bundle = None
        self._result_is_batch = True
        self.result_copy.show()
        self.result_export.hide()
        self._switch(self.result_page)
        ok = result.success_count
        prefix = "批量任务已取消" if cancelled else "批量提取完成"
        self.result_title.setText(f"{prefix}  {ok}/{len(result.items)}")
        self.result_meta.setText("已完成的文稿已分别导出到设置的默认目录，并已写入历史。")
        lines = []
        for item in result.items:
            if item.succeeded:
                lines.append(f"✓ {item.title or item.source}\n  {item.output_path}")
            else:
                lines.append(f"× {item.source}\n  {item.error}")
        self.result_issue.setText("；".join(item.error for item in result.items if item.error))
        self.result_issue.setVisible(any(item.error for item in result.items))
        self.result_issue_toggle.hide()
        self.result_issue_details.hide()
        self.result_editor.setPlainText("\n\n".join(lines))
        self.result_timeline.set_entries([], 0)
        self._set_result_timeline_enabled(False)

    def show_error(self, message: str, *, title: str = "提取失败") -> None:
        self._stop_working_animation()
        self._hide_timeline_tooltip()
        self._switch(self.error_page)
        self.error_title.setText(title)
        self.error_detail.setText(message)

    @property
    def editor(self) -> QPlainTextEdit:
        return self.result_editor


class HistoryPage(QWidget):
    entry_selected = Signal(object)
    copy_requested = Signal(object)
    export_requested = Signal(object)
    reextract_requested = Signal(object)
    delete_requested = Signal(object)

    def __init__(self, store: HistoryStore, qsettings: QSettings | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.qsettings = qsettings
        remembered = str(qsettings.value("history/last_kind", "home")) if qsettings is not None else "home"
        self.kind = "api" if remembered == "api" else "home"
        self.offset = 0
        self._has_more = False
        self._loading = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self._apply_search)
        root = QVBoxLayout(self)
        # Match the effective 44/52 title origin used by every Settings page.
        root.setContentsMargins(44, 52, 52, 28)
        root.setSpacing(12)
        header = QHBoxLayout()
        title = QLabel("历史")
        title.setObjectName("contentPageTitle")
        header.addWidget(title)
        header.addStretch(1)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索标题、BVID 或来源")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(320)
        self.search.hide()
        self.search.textChanged.connect(self._search_changed)
        self.search.editingFinished.connect(self._collapse_search_if_empty)
        self.search_button = GlyphButton("search")
        self.search_button.setObjectName("iconButton")
        self.search_button.setFixedSize(38, 38)
        self.search_button.setToolTip("搜索历史")
        self.search_button.clicked.connect(self._show_search)
        root.addLayout(header)
        root.addSpacing(12)
        controls = QHBoxLayout()
        self.home_tab = QPushButton("主页调用")
        self.api_tab = QPushButton("API 调用")
        for button in (self.home_tab, self.api_tab):
            button.setCheckable(True)
            button.setObjectName("navButton")
            controls.addWidget(button)
        self.home_tab.clicked.connect(lambda: self.set_kind("home"))
        self.api_tab.clicked.connect(lambda: self.set_kind("api"))
        self.home_tab.setChecked(True)
        self.api_tab.setChecked(self.kind == "api")
        self.home_tab.setChecked(self.kind == "home")
        controls.addStretch(1)
        controls.addWidget(self.search)
        controls.addWidget(self.search_button)
        root.addLayout(controls)
        self.list = QListWidget()
        self.list.setObjectName("historyList")
        self.list.setSpacing(16)
        self.list.itemClicked.connect(self._item_clicked)
        self.list.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        root.addWidget(self.list, 1)
        self.empty_label = QLabel("还没有记录")
        self.empty_label.setObjectName("meta")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.hide()
        root.addWidget(self.empty_label, 1)

    def _show_search(self) -> None:
        self.search_button.hide()
        self.search.show()
        self.search.setFocus()

    def _collapse_search_if_empty(self) -> None:
        if not self.search.text().strip():
            self.search.hide()
            self.search_button.show()

    def set_kind(self, kind: str) -> None:
        self.kind = "api" if kind == "api" else "home"
        if self.qsettings is not None:
            self.qsettings.setValue("history/last_kind", self.kind)
        self.home_tab.setChecked(self.kind == "home")
        self.api_tab.setChecked(self.kind == "api")
        self.offset = 0
        self.refresh()

    def _search_changed(self, _text: str) -> None:
        self._search_timer.start()

    def _apply_search(self) -> None:
        self.offset = 0
        self.refresh()

    def refresh(self) -> None:
        if self._loading:
            return
        self._loading = True
        try:
            # Rebuild every loaded page from the beginning.  Appending the
            # current offset again duplicates rows when an API job completes.
            entries = self.store.list_entries(
                source_kind=self.kind,
                query=self.search.text(),
                limit=self.offset + 50,
                offset=0,
            )
            self.list.clear()
            for entry in entries:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, entry.id)
                self.list.addItem(item)
                row = HistoryRowWidget(entry)
                row.open_requested.connect(lambda _entry_id=entry.id: self._open_by_id(_entry_id))
                row.copy_requested.connect(lambda _entry_id=entry.id: self.copy_requested.emit(self.store.get(_entry_id, include_bundle=True)))
                row.export_requested.connect(lambda _entry_id=entry.id: self.export_requested.emit(self.store.get(_entry_id, include_bundle=True)))
                row.reextract_requested.connect(lambda _entry_id=entry.id: self.reextract_requested.emit(self.store.get(_entry_id, include_bundle=True)))
                row.delete_requested.connect(lambda _entry_id=entry.id: self.delete_requested.emit(self.store.get(_entry_id, include_bundle=False)))
                self.list.setItemWidget(item, row)
                item.setSizeHint(row.sizeHint())
            total = self.store.count(source_kind=self.kind, query=self.search.text())
            self.list.setVisible(total > 0)
            self.empty_label.setVisible(total == 0)
            self._has_more = len(entries) < total
        finally:
            self._loading = False

    def _load_more(self) -> None:
        if self._loading or not self._has_more:
            return
        self.offset += 50
        self.refresh()

    def _maybe_load_more(self, value: int) -> None:
        scrollbar = self.list.verticalScrollBar()
        if value >= scrollbar.maximum() and self._has_more:
            self._load_more()

    def _item_clicked(self, item: QListWidgetItem) -> None:
        entry = self.store.get(str(item.data(Qt.ItemDataRole.UserRole)), include_bundle=True)
        if entry:
            self.entry_selected.emit(entry)

    def _open_by_id(self, entry_id: str) -> None:
        entry = self.store.get(entry_id, include_bundle=True)
        if entry:
            self.entry_selected.emit(entry)


class HistoryRowWidget(QFrame):
    """Compact history row;正文 is loaded only after the user opens it."""

    open_requested = Signal(str)
    copy_requested = Signal(str)
    export_requested = Signal(str)
    reextract_requested = Signal(str)
    delete_requested = Signal(str)

    def __init__(self, entry: HistoryEntry, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("historyRow")
        self.setMinimumHeight(72)
        self.entry_id = entry.id
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 9, 10, 9)
        layout.setSpacing(10)
        info = QVBoxLayout()
        info.setSpacing(3)
        title = QLabel(entry.title or entry.source)
        title.setObjectName("sectionTitle")
        title.setWordWrap(False)
        info.addWidget(title)
        status = {"succeeded": "已完成", "failed": "失败", "cancelled": "已取消", "queued": "排队中", "running": "进行中"}.get(entry.status, entry.status)
        detail = QLabel(" · ".join(item for item in (status, entry.bvid, entry.owner, _history_time(entry.finished_at or entry.created_at)) if item))
        detail.setObjectName("meta")
        info.addWidget(detail)
        layout.addLayout(info, 1)
        self.menu_button = GlyphButton("menu")
        self.menu_button.setObjectName("iconButton")
        self.menu_button.setFixedSize(38, 38)
        self.menu_button.setToolTip("更多操作")
        self.menu_button.clicked.connect(lambda: self._show_menu(entry.status == "succeeded"))
        layout.addWidget(self.menu_button)
        self.setToolTip(entry.source)

    def _show_menu(self, has_result: bool) -> None:
        menu = QMenu(self)
        copy_action = menu.addAction("复制文稿")
        export_action = menu.addAction("重新导出")
        copy_action.setEnabled(has_result)
        export_action.setEnabled(has_result)
        copy_action.triggered.connect(lambda: self.copy_requested.emit(self.entry_id))
        export_action.triggered.connect(lambda: self.export_requested.emit(self.entry_id))
        menu.addSeparator()
        menu.addAction("再次提取").triggered.connect(lambda: self.reextract_requested.emit(self.entry_id))
        menu.addAction("删除记录").triggered.connect(lambda: self.delete_requested.emit(self.entry_id))
        menu.exec(self.menu_button.mapToGlobal(self.menu_button.rect().bottomRight()))


class SettingsPage(QWidget):
    theme_changed = Signal(str)
    startup_changed = Signal(bool)
    extraction_changed = Signal(str, str, str)
    asr_test_requested = Signal()
    api_apply_requested = Signal()
    api_run_requested = Signal(bool)
    api_auto_start_changed = Signal(bool)
    api_regenerate_requested = Signal()
    history_clear_requested = Signal()
    diagnostic_requested = Signal(str)
    diagnostic_cancel_requested = Signal()

    def __init__(self, app_settings: AppSettings, history_store: HistoryStore | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.app_settings = app_settings
        self.history_store = history_store
        self._updating_extraction = False
        root = QVBoxLayout(self)
        root.setContentsMargins(34, 48, 34, 28)
        self.categories = ["常规", "提取", "B站账号", "ASR", "提取 API", "关于"]
        self.stack = QStackedWidget()
        self.stack.addWidget(self._scroll(self._general_page()))
        self.stack.addWidget(self._scroll(self._extract_page()))
        self.stack.addWidget(self._scroll(self._account_page()))
        self.stack.addWidget(self._scroll(self._asr_page()))
        self.stack.addWidget(self._scroll(self._api_page()))
        self.stack.addWidget(self._scroll(self._about_page()))
        root.addWidget(self.stack, 1)
        self.stack.setCurrentIndex(0)

    @staticmethod
    def _heading(title: str, subtitle: str = "") -> tuple[QVBoxLayout, QWidget]:
        page = QWidget()
        page.setObjectName("settingsPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 4, 18, 20)
        layout.setSpacing(0)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        title_label = QLabel(title)
        title_label.setObjectName("contentPageTitle")
        layout.addWidget(title_label)
        if subtitle:
            layout.addSpacing(20)
            hint = QLabel(subtitle)
            hint.setObjectName("meta")
            hint.setWordWrap(True)
            hint.setMaximumWidth(SETTINGS_CONTENT_WIDTH)
            layout.addWidget(hint)
        layout.addSpacing(28)
        return layout, page

    @staticmethod
    def _scroll(page: QWidget) -> QScrollArea:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        scroll.setWidget(page)
        return scroll

    @staticmethod
    def _form() -> tuple[QFormLayout, QWidget]:
        """Create one consistently sized settings form on an 8 px rhythm."""
        container = QWidget()
        container.setObjectName("settingsForm")
        container.setMaximumWidth(SETTINGS_CONTENT_WIDTH)
        container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        form = QFormLayout(container)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(SETTINGS_COLUMN_GAP)
        form.setVerticalSpacing(SETTINGS_ROW_GAP)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        return form, container

    @staticmethod
    def _add_form_row(form: QFormLayout, label: str, field: Any) -> None:
        label_widget = QLabel(label)
        label_widget.setObjectName("settingsFieldLabel")
        label_widget.setMinimumWidth(SETTINGS_LABEL_WIDTH)
        form.addRow(label_widget, field)

    @classmethod
    def _add_action_row(cls, form: QFormLayout, actions: Any) -> None:
        cls._add_form_row(form, "", actions)

    @staticmethod
    def _prepare_number_input(control: QSpinBox) -> None:
        control.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        control.setKeyboardTracking(False)
        control.setAlignment(Qt.AlignmentFlag.AlignLeft)
        control.setMaximumWidth(SETTINGS_NUMBER_WIDTH)

    def _general_page(self) -> QWidget:
        layout, page = self._heading("常规", "外观、启动和本地历史")
        form, form_widget = self._form()
        self.theme_combo = QComboBox()
        self.theme_combo.addItem("跟随系统", "system")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.setMaximumWidth(SETTINGS_SELECT_WIDTH)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(self.app_settings.theme)))
        self.theme_combo.currentIndexChanged.connect(lambda: self.theme_changed.emit(str(self.theme_combo.currentData())))
        self.startup_check = SettingsSwitch("登录 Windows 时自动打开")
        self.startup_check.setChecked(self.app_settings.startup_enabled)
        self.startup_check.toggled.connect(self.startup_changed)
        export_row = QHBoxLayout()
        self.export_dir = QLineEdit(str(self.app_settings.default_export_dir))
        self.export_dir.editingFinished.connect(self._save_export_dir)
        export_row.addWidget(self.export_dir, 1)
        browse = QPushButton("选择…")
        browse.setMinimumWidth(76)
        browse.clicked.connect(self._choose_export_dir)
        export_row.addWidget(browse)
        # Visual order deliberately grows from the shortest control to the longest.
        self._add_form_row(form, "开机自启", self.startup_check)
        self._add_form_row(form, "主题", self.theme_combo)
        self._add_form_row(form, "默认导出目录", export_row)
        layout.addWidget(form_widget)
        self.general_feedback = QLabel("")
        self.general_feedback.setObjectName("meta")
        self.general_feedback.setMaximumWidth(SETTINGS_CONTENT_WIDTH)
        layout.addWidget(self.general_feedback)
        layout.addSpacing(28)
        history_title = QLabel("历史记录")
        history_title.setObjectName("settingsSectionTitle")
        layout.addWidget(history_title)
        layout.addSpacing(8)
        self.history_usage = QLabel("")
        self.history_usage.setObjectName("meta")
        layout.addWidget(self.history_usage)
        layout.addSpacing(10)
        clear_history = QPushButton("清理全部历史")
        clear_history.setObjectName("dangerButton")
        clear_history.clicked.connect(self.history_clear_requested)
        layout.addWidget(clear_history, 0, Qt.AlignmentFlag.AlignLeft)
        self.refresh_history_usage()
        return page

    def refresh_history_usage(self) -> None:
        if self.history_store is None:
            self.history_usage.setText("历史永久保留")
            return
        size = self.history_store.storage_bytes()
        if size < 1024:
            display = f"{size} B"
        elif size < 1024 * 1024:
            display = f"{size / 1024:.1f} KiB"
        else:
            display = f"{size / (1024 * 1024):.1f} MiB"
        self.history_usage.setText(f"历史永久保留 · 数据库占用 {display}")

    def _extract_page(self) -> QWidget:
        layout, page = self._heading("提取", "提交时会快照这些设置；运行中的任务不会被后续修改影响。")
        form, form_widget = self._form()
        self.mode_combo = QComboBox()
        self.mode_combo.setMaximumWidth(SETTINGS_SELECT_WIDTH)
        for label, value in (("智能提取", "auto"), ("只用公开字幕", "public"), ("只用匿名接口", "anonymous"), ("只用登录浏览器", "browser"), ("只用 ASR", "asr")):
            self.mode_combo.addItem(label, value)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(str(self.app_settings.value("extract/mode", "auto")))))
        self.backend_combo = QComboBox()
        self.backend_combo.setMaximumWidth(SETTINGS_WIDE_SELECT_WIDTH)
        for label, value in (("自动检测", "auto"), ("Faster-Whisper", "faster-whisper"), ("FunASR / SenseVoice", "funasr"), ("OpenAI Whisper", "openai-whisper"), ("OpenAI 兼容 API（MiMo）", ASR_API_BACKEND)):
            self.backend_combo.addItem(label, value)
        backend = str(self.app_settings.value("extract/backend", "auto"))
        self.backend_combo.setCurrentIndex(max(0, self.backend_combo.findData(backend)))
        self.model_edit = QLineEdit(self.app_settings.extraction_options().asr_model)
        self.model_edit.setMaximumWidth(SETTINGS_MODEL_WIDTH)
        self.model_edit.setPlaceholderText(DEFAULT_ASR_API_MODEL)
        self.timestamps_check = SettingsSwitch("在文稿中显示时间戳")
        self.timestamps_check.setChecked(self.app_settings.timestamps)
        self.timestamps_check.toggled.connect(self._timestamps_toggled)
        self.batch_spin = QSpinBox()
        self._prepare_number_input(self.batch_spin)
        self.batch_spin.setRange(1, 4)
        self.batch_spin.setValue(self.app_settings.batch_concurrency)
        self.batch_spin.valueChanged.connect(self.app_settings.set_batch_concurrency)
        self._add_form_row(form, "批量并行数", self.batch_spin)
        self._add_form_row(form, "默认输出", self.timestamps_check)
        self._add_form_row(form, "方式", self.mode_combo)
        self._add_form_row(form, "ASR 后端", self.backend_combo)
        self._add_form_row(form, "模型", self.model_edit)
        layout.addWidget(form_widget)
        layout.addSpacing(28)
        self.diagnostic_button = QPushButton("来源诊断")
        self.diagnostic_button.setObjectName("ghostButton")
        self.diagnostic_button.setCheckable(True)
        self.diagnostic_button.setToolTip("实际任务会按固定规则逐来源探测，每个来源最多两次，间隔 1 秒")
        layout.addWidget(self.diagnostic_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.diagnostic_panel = QFrame()
        self.diagnostic_panel.setObjectName("softPanel")
        self.diagnostic_panel.setMaximumWidth(SETTINGS_CONTENT_WIDTH)
        diagnostic_layout = QVBoxLayout(self.diagnostic_panel)
        diagnostic_layout.setContentsMargins(14, 12, 14, 12)
        diagnostic_hint = QLabel("公开字幕 → 匿名播放器接口 → 登录浏览器 AI 字幕 → ASR\n每个来源最多尝试 2 次，失败间隔 1 秒；检测会覆盖视频的全部分P。")
        diagnostic_hint.setObjectName("meta")
        diagnostic_hint.setWordWrap(True)
        diagnostic_layout.addWidget(diagnostic_hint)
        diagnostic_input_row = QHBoxLayout()
        self.diagnostic_source = QLineEdit()
        self.diagnostic_source.setPlaceholderText("输入一个 BV / av 号或 B站视频链接")
        self.diagnostic_source.returnPressed.connect(self._request_diagnostic)
        diagnostic_input_row.addWidget(self.diagnostic_source, 1)
        self.diagnostic_run = QPushButton("开始检测")
        self.diagnostic_run.setObjectName("primaryButton")
        self.diagnostic_run.clicked.connect(self._request_diagnostic)
        diagnostic_input_row.addWidget(self.diagnostic_run)
        self.diagnostic_cancel = QPushButton("取消")
        self.diagnostic_cancel.setObjectName("dangerButton")
        self.diagnostic_cancel.hide()
        self.diagnostic_cancel.clicked.connect(self.diagnostic_cancel_requested)
        diagnostic_input_row.addWidget(self.diagnostic_cancel)
        diagnostic_layout.addLayout(diagnostic_input_row)
        self.diagnostic_progress = QProgressBar()
        self.diagnostic_progress.setRange(0, 100)
        self.diagnostic_progress.setValue(0)
        self.diagnostic_progress.setTextVisible(False)
        self.diagnostic_progress.hide()
        diagnostic_layout.addWidget(self.diagnostic_progress)
        self.diagnostic_status = QLabel("等待检测")
        self.diagnostic_status.setObjectName("meta")
        self.diagnostic_status.setWordWrap(True)
        diagnostic_layout.addWidget(self.diagnostic_status)
        self.diagnostic_results = QPlainTextEdit()
        self.diagnostic_results.setReadOnly(True)
        self.diagnostic_results.setMaximumHeight(230)
        self.diagnostic_results.hide()
        diagnostic_layout.addWidget(self.diagnostic_results)
        self.diagnostic_panel.hide()
        layout.addWidget(self.diagnostic_panel)
        self.diagnostic_button.toggled.connect(self.diagnostic_panel.setVisible)
        self.mode_combo.currentIndexChanged.connect(self._mode_selection_changed)
        self.backend_combo.currentIndexChanged.connect(self._backend_selection_changed)
        self.model_edit.editingFinished.connect(self._extraction_changed)
        return page

    def _account_page(self) -> QWidget:
        layout, page = self._heading("B站账号", "应用使用专用浏览器配置，不读取或复制你的 Cookie。")
        form, form_widget = self._form()
        self.login_status_label = QLabel("未检查")
        self.login_status_label.setObjectName("meta")
        self._add_form_row(form, "登录状态", self.login_status_label)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.check_login_button = QPushButton("立即检查")
        self.open_login_button = QPushButton("打开登录浏览器")
        row.addWidget(self.open_login_button)
        row.addWidget(self.check_login_button)
        row.addStretch(1)
        self._add_action_row(form, row)
        layout.addWidget(form_widget)
        return page

    def _asr_page(self) -> QWidget:
        layout, page = self._heading("ASR", "配置 OpenAI 兼容接口和请求参数。")
        form, form_widget = self._form()
        self.asr_backend_label = QLabel(self.app_settings.extraction_options().asr_backend)
        self.asr_backend_label.setObjectName("meta")
        self.asr_base_url = QLineEdit(self.app_settings.asr_base_url)
        self.asr_base_url.setMaximumWidth(SETTINGS_LONG_FIELD_WIDTH)
        self.asr_key = QLineEdit(self.app_settings.asr_api_key)
        self.asr_key.setMaximumWidth(SETTINGS_MODEL_WIDTH)
        self.asr_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.asr_timeout = QSpinBox()
        self._prepare_number_input(self.asr_timeout)
        self.asr_timeout.setRange(1, 3600)
        self.asr_timeout.setValue(int(self.app_settings.asr_timeout))
        self._add_form_row(form, "当前后端", self.asr_backend_label)
        self._add_form_row(form, "超时（秒）", self.asr_timeout)
        self._add_form_row(form, "密钥", self.asr_key)
        self._add_form_row(form, "兼容地址", self.asr_base_url)
        asr_actions = QHBoxLayout()
        asr_actions.setSpacing(8)
        self.asr_apply = QPushButton("应用")
        self.asr_test = QPushButton("测试连接")
        self.asr_test.clicked.connect(self.asr_test_requested)
        asr_actions.addWidget(self.asr_apply)
        asr_actions.addWidget(self.asr_test)
        asr_actions.addStretch(1)
        self._add_action_row(form, asr_actions)
        layout.addWidget(form_widget)
        self.asr_feedback = QLabel("")
        self.asr_feedback.setObjectName("meta")
        self.asr_feedback.setWordWrap(True)
        self.asr_feedback.setMaximumWidth(SETTINGS_CONTENT_WIDTH)
        layout.addWidget(self.asr_feedback)
        return page

    def _api_page(self) -> QWidget:
        layout, page = self._heading("提取 API", "仅监听可信局域网；网页前端请由后端转发，不要把密钥放进浏览器。")
        form, form_widget = self._form()
        self.api_run = SettingsSwitch("运行 API 服务")
        self.api_auto = SettingsSwitch("随软件启动")
        self.api_auto.setChecked(self.app_settings.api_auto_start)
        self.api_auto.toggled.connect(self.api_auto_start_changed)
        self.api_port = QSpinBox()
        self._prepare_number_input(self.api_port)
        self.api_port.setRange(1024, 65535)
        self.api_port.setValue(self.app_settings.api_port)
        self.api_key = QLineEdit(self.app_settings.api_key)
        self.api_key.setMaximumWidth(SETTINGS_LONG_FIELD_WIDTH)
        self.api_key.setReadOnly(True)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_address = QLabel(_lan_address(self.app_settings.api_port))
        self.api_active = QLabel("活跃任务：0")
        self.api_active.setObjectName("meta")
        self._add_form_row(form, "状态", self.api_active)
        self._add_form_row(form, "启动", self.api_auto)
        self._add_form_row(form, "端口", self.api_port)
        self._add_form_row(form, "运行", self.api_run)
        self._add_form_row(form, "调用地址", self.api_address)
        self._add_form_row(form, "API Key", self.api_key)
        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.api_apply = QPushButton("应用")
        self.api_apply.clicked.connect(self.api_apply_requested)
        self.api_copy = QPushButton("复制密钥")
        self.api_copy.clicked.connect(lambda: QApplication.clipboard().setText(self.api_key.text()))
        self.api_regenerate = QPushButton("重新生成")
        self.api_regenerate.clicked.connect(self.api_regenerate_requested)
        actions.addWidget(self.api_apply)
        actions.addWidget(self.api_copy)
        actions.addWidget(self.api_regenerate)
        actions.addStretch(1)
        self._add_action_row(form, actions)
        layout.addWidget(form_widget)
        self.api_feedback = QLabel("")
        self.api_feedback.setObjectName("meta")
        self.api_feedback.setWordWrap(True)
        self.api_feedback.setMaximumWidth(SETTINGS_CONTENT_WIDTH)
        layout.addWidget(self.api_feedback)
        self.api_run.toggled.connect(self.api_run_requested)
        return page

    def _about_page(self) -> QWidget:
        layout, page = self._heading("关于", "应用信息与开源许可")
        form, form_widget = self._form()
        name = QLabel("BiliTranscript")
        version = QLabel(__version__)
        project = QLabel('<a style="color:#FB7299;text-decoration:none" href="https://github.com/W1nge/BiliTranscript">github.com/W1nge/BiliTranscript</a>')
        project.setOpenExternalLinks(True)
        license_name = QLabel("MIT License")
        for value in (name, version, project, license_name):
            value.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._add_form_row(form, "版本", version)
        self._add_form_row(form, "开源许可", license_name)
        self._add_form_row(form, "应用", name)
        self._add_form_row(form, "项目主页", project)
        layout.addWidget(form_widget)
        return page

    def _switch(self, index: int) -> None:
        if index >= 0:
            self.stack.setCurrentIndex(index)

    def set_category(self, index: int) -> None:
        self._switch(max(0, min(len(self.categories) - 1, int(index))))

    def _choose_export_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择默认导出目录", self.export_dir.text())
        if path:
            self.export_dir.setText(path)
            self.app_settings.set_default_export_dir(path)
            self.general_feedback.setText("默认导出目录已保存")

    def _save_export_dir(self) -> None:
        path = self.export_dir.text().strip()
        if not path:
            self.export_dir.setText(str(self.app_settings.default_export_dir))
            self.general_feedback.setText("导出目录不能为空")
            return
        saved = self.app_settings.set_default_export_dir(path)
        self.export_dir.setText(str(saved))
        self.general_feedback.setText("默认导出目录已保存")

    def _request_diagnostic(self) -> None:
        self.diagnostic_requested.emit(self.diagnostic_source.text().strip())

    def set_diagnostic_running(self, running: bool) -> None:
        self.diagnostic_source.setEnabled(not running)
        self.diagnostic_run.setVisible(not running)
        self.diagnostic_cancel.setVisible(running)
        self.diagnostic_cancel.setEnabled(running)
        self.diagnostic_progress.setVisible(running)

    def _timestamps_toggled(self, enabled: bool) -> None:
        self.app_settings.set_timestamps(enabled)

    def _extraction_changed(self) -> None:
        if self._updating_extraction:
            return
        self.extraction_changed.emit(
            str(self.mode_combo.currentData()), str(self.backend_combo.currentData()), self.model_edit.text().strip()
        )

    def _mode_selection_changed(self) -> None:
        self._extraction_changed()

    def _backend_selection_changed(self) -> None:
        backend = str(self.backend_combo.currentData())
        defaults = {
            "auto": "",
            "faster-whisper": "small",
            "funasr": "iic/SenseVoiceSmall",
            "openai-whisper": "small",
            ASR_API_BACKEND: DEFAULT_ASR_API_MODEL,
        }
        saved = str(self.app_settings.value(f"extract/model/{backend}", defaults.get(backend, "")) or "")
        self._updating_extraction = True
        self.model_edit.setText(saved)
        self._updating_extraction = False
        self._extraction_changed()


class ModernMainWindow(LoginController, QMainWindow):
    """Compact Acrylic desktop shell and service coordinator."""

    api_state_changed = Signal()
    composition_runtime_ready = Signal(str)

    def __init__(
        self,
        *,
        autostart_services: bool = True,
        qsettings: QSettings | None = None,
        history_store: HistoryStore | None = None,
    ) -> None:
        QMainWindow.__init__(self)
        self.setWindowTitle("Bili 文稿")
        self.setWindowFlags(
            Qt.WindowType.Window
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowSystemMenuHint
            | Qt.WindowType.WindowMinMaxButtonsHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.resize(1180, 760)
        self.setMinimumSize(960, 660)
        self.setAcceptDrops(True)
        self.settings = qsettings if qsettings is not None else QSettings()
        self.app_settings = AppSettings(self.settings)
        if autostart_services:
            self._migrate_automatic_services()
        self.history_store = history_store if history_store is not None else HistoryStore()
        self.history_store.mark_interrupted()
        self._restoring_settings = False
        self._api_options_lock = threading.RLock()
        self._api_options = self.app_settings.extraction_options()
        self.extraction_api_server: ExtractionApiServer | None = None
        self.api_stop_task: ApiServerStopTask | None = None
        self._pending_api_start: tuple[int, str] | None = None
        self.video: VideoInfo | None = None
        self.bundle: TranscriptBundle | None = None
        self.metadata_task: MetadataTask | None = None
        self.extraction_task: ExtractionTask | None = None
        self.batch_task: BatchExtractionTask | None = None
        self.availability_task = None
        self.diagnostic_metadata_task: MetadataTask | None = None
        self._diagnostic_cancel_requested = False
        self._diagnostic_snapshot: SettingsSnapshot | None = None
        self.asr_health_task: AsrHealthTask | None = None
        self.browser_task: BrowserLaunchTask | None = None
        self.browser_status_task: BrowserStatusTask | None = None
        self.availability_report = None
        self._active_history_id: str | None = None
        self._active_source = ""
        self._active_snapshot: SettingsSnapshot | None = None
        self._active_batch_sources: tuple[str, ...] = ()
        self._active_batch_history_ids: dict[str, str] = {}
        self._submission_active = False
        self._cancel_requested = False
        self._close_requested = False
        self._applying_theme = False
        self._composition_backdrop: CompositionBackdrop | None = None
        self._composition_error = ""
        self._composition_preload_started = False
        self._resolved_theme = effective_theme(self.app_settings.theme)
        self._browser_launch_automatic = False
        self._startup_browser_retries_remaining = 1
        self._login_watch_active = False
        self._login_watch_checks_remaining = 0
        self._login_check_timer = QTimer(self)
        self._login_check_timer.setSingleShot(True)
        self._login_check_timer.timeout.connect(self._check_login_status)

        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        shell.addWidget(self._build_sidebar())
        self.pages = QStackedWidget()
        self.pages.setObjectName("contentArea")
        self.home_page = HomePage()
        self.history_page = HistoryPage(self.history_store, self.settings)
        self.settings_page = SettingsPage(self.app_settings, self.history_store)
        self.pages.addWidget(self.home_page)
        self.pages.addWidget(self.history_page)
        self.pages.addWidget(self.settings_page)
        shell.addWidget(self.pages, 1)
        self.stacked_widget = self.pages
        self.title_drag_area = TitleDragArea(root)
        self.title_drag_area.setObjectName("titleDragArea")
        self.window_chrome = self._build_window_chrome(root)
        self.window_chrome.move(self.width() - self.window_chrome.width(), 0)
        self.title_drag_area.setGeometry(218, 0, self.width() - 218 - self.window_chrome.width(), 42)

        # Short aliases used by the window coordinator and login controller.
        self.url_input = self.home_page.url_input
        self.fetch_button = self.home_page.submit_button
        self.editor = self.home_page.editor
        self.status_label = self.home_page.working_detail
        self.login_status_label = self.settings_page.login_status_label
        self.login_browser_button = self.settings_page.open_login_button
        self.check_login_button = self.settings_page.check_login_button
        self.startup_button = self.settings_page.startup_check

        self.home_page.submit_requested.connect(self._submit_source)
        self.home_page.cancel_requested.connect(self._cancel_active_task)
        self.home_page.copy_requested.connect(self._copy_transcript)
        self.home_page.export_requested.connect(self._save_transcript)
        self.home_page.next_requested.connect(self._next_extraction)
        self.settings_page.theme_changed.connect(self._theme_changed)
        self.settings_page.startup_changed.connect(self._toggle_startup)
        self.settings_page.extraction_changed.connect(self._settings_extraction_changed)
        self.settings_page.timestamps_check.toggled.connect(lambda _enabled: self._update_preview())
        self.settings_page.asr_apply.clicked.connect(self._apply_asr_settings)
        self.settings_page.asr_test_requested.connect(self._test_asr_connection)
        self.settings_page.api_apply_requested.connect(self._apply_api_settings)
        self.settings_page.api_run_requested.connect(self._api_run_toggled)
        self.settings_page.api_auto_start_changed.connect(self.app_settings.set_api_auto_start)
        self.settings_page.api_regenerate_requested.connect(self._regenerate_api_key)
        self.api_state_changed.connect(self._update_api_page)
        self.composition_runtime_ready.connect(self._composition_runtime_loaded)
        self.settings_page.open_login_button.clicked.connect(self._open_login_browser)
        self.settings_page.check_login_button.clicked.connect(self._check_login_status)
        self.history_page.entry_selected.connect(self._history_entry_selected)
        self.settings_page.history_clear_requested.connect(self._clear_history)
        self.settings_page.diagnostic_requested.connect(self._start_diagnostic)
        self.settings_page.diagnostic_cancel_requested.connect(self._cancel_diagnostic)
        self.history_page.copy_requested.connect(self._history_copy)
        self.history_page.export_requested.connect(self._history_export)
        self.history_page.reextract_requested.connect(self._history_reextract)
        self.history_page.delete_requested.connect(self._history_delete)
        self.nav_home.clicked.connect(lambda: self._navigate(0))
        self.nav_history.clicked.connect(lambda: self._navigate(1))
        self.nav_settings.clicked.connect(self._settings_nav_clicked)
        for index, button in enumerate(self.settings_drawer_buttons):
            button.clicked.connect(lambda _checked=False, i=index: self._select_settings_category(i))
        self.nav_home.setChecked(True)

        self._apply_theme(self.app_settings.theme)
        self._refresh_login_status_text()
        self._refresh_startup_button()
        self._update_api_page()
        self.url_input.setFocus()
        self._update_actions()
        if autostart_services:
            # Preserve the existing automatic browser/API behavior and its testable timing.
            QTimer.singleShot(350, self._auto_start_extraction_api)
            QTimer.singleShot(650, self._auto_open_login_browser)
        else:
            self.home_page.show_idle()

    def _migrate_automatic_services(self) -> None:
        """Adopt the requested API-on-start default once, then preserve choices."""
        marker = "migration/extraction_api_default_on"
        if self.settings.contains(marker):
            return
        self.settings.setValue("extraction_api/auto_start", True)
        self.settings.setValue(marker, True)

    def _build_sidebar(self) -> QFrame:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setFixedWidth(218)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(18, 58, 18, 18)
        layout.setSpacing(8)
        brand_row = QHBoxLayout()
        logo = QLabel()
        icon = QApplication.windowIcon()
        if icon.isNull():
            logo.setText("稿")
            logo.setObjectName("brandMark")
            logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            logo.setPixmap(icon.pixmap(38, 38))
            logo.setObjectName("brandIcon")
        logo.setFixedSize(38, 38)
        brand_row.addWidget(logo)
        name = QLabel("BiliTranscript")
        name.setObjectName("appTitle")
        brand_row.addWidget(name)
        brand_row.addStretch(1)
        layout.addLayout(brand_row)
        layout.addSpacing(24)
        self.nav_home = QPushButton("首页")
        self.nav_history = QPushButton("历史")
        self.nav_settings = QPushButton("设置")
        for button in (self.nav_home, self.nav_history, self.nav_settings):
            button.setObjectName("navButton")
            button.setCheckable(True)
            layout.addWidget(button)
        self.settings_drawer = QFrame()
        self.settings_drawer.setObjectName("settingsDrawer")
        drawer_layout = QVBoxLayout(self.settings_drawer)
        drawer_layout.setContentsMargins(12, 2, 0, 6)
        drawer_layout.setSpacing(2)
        self.settings_drawer_buttons: list[QPushButton] = []
        for label in ("常规", "提取", "B站账号", "ASR", "提取 API", "关于"):
            button = QPushButton(label)
            button.setObjectName("drawerButton")
            button.setCheckable(True)
            drawer_layout.addWidget(button)
            self.settings_drawer_buttons.append(button)
        self.settings_drawer.hide()
        layout.addWidget(self.settings_drawer)
        layout.addStretch(1)
        return sidebar

    def _build_window_chrome(self, parent: QWidget) -> QFrame:
        chrome = QFrame(parent)
        chrome.setObjectName("windowChrome")
        chrome.setFixedSize(132, 42)
        layout = QHBoxLayout(chrome)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.minimize_button = GlyphButton("minimize")
        self.maximize_button = GlyphButton("maximize")
        self.close_button = GlyphButton("close")
        for button in (self.minimize_button, self.maximize_button, self.close_button):
            button.setObjectName("chromeButton")
            button.setFixedSize(44, 42)
        self.minimize_button.setToolTip("最小化")
        self.maximize_button.setToolTip("最大化 / 还原")
        self.close_button.setToolTip("关闭")
        self.close_button.setObjectName("chromeCloseButton")
        self.minimize_button.clicked.connect(self.showMinimized)
        self.maximize_button.clicked.connect(self._toggle_maximized)
        self.close_button.clicked.connect(self.close)
        layout.addWidget(self.minimize_button)
        layout.addWidget(self.maximize_button)
        layout.addWidget(self.close_button)
        return chrome

    def _toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self.maximize_button.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        chrome_width = self.window_chrome.width() if hasattr(self, "window_chrome") else 132
        if hasattr(self, "window_chrome"):
            self.window_chrome.move(max(0, self.width() - chrome_width), 0)
            self.window_chrome.raise_()
        if hasattr(self, "title_drag_area"):
            self.title_drag_area.setGeometry(218, 0, max(0, self.width() - 218 - chrome_width), 42)
            self.title_drag_area.raise_()

    def nativeEvent(self, event_type, message):
        """Synchronize the compositor layer and retain native edge resizing."""
        if sys.platform == "win32":
            try:
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                backdrop = self._composition_backdrop
                if backdrop is not None and msg.message in (0x0046, 0x0047):  # WM_WINDOWPOSCHANGING / CHANGED
                    position = ctypes.cast(msg.lParam, ctypes.POINTER(WINDOWPOS)).contents
                    current = backdrop.window_rect()
                    if current is not None:
                        x, y, width, height = current
                        if not position.flags & 0x0002:  # SWP_NOMOVE
                            x, y = position.x, position.y
                        if not position.flags & 0x0001:  # SWP_NOSIZE
                            width, height = position.cx, position.cy
                        if position.flags & 0x0080:  # SWP_HIDEWINDOW
                            backdrop.set_visible(False)
                        else:
                            backdrop.sync_rect(x, y, width, height, sync_z_order=False)
                elif backdrop is not None and msg.message == 0x0005:  # WM_SIZE
                    if int(msg.wParam) == 1:  # SIZE_MINIMIZED
                        backdrop.set_visible(False)
                    else:
                        backdrop.sync_to_window(sync_z_order=False)
                elif backdrop is not None and msg.message == 0x0006 and (int(msg.wParam) & 0xFFFF):  # WM_ACTIVATE
                    QTimer.singleShot(0, self._sync_composition_z_order)

                if msg.message == 0x0084 and not self.isMaximized():  # WM_NCHITTEST
                    x = ctypes.c_short(msg.lParam & 0xFFFF).value
                    y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
                    frame = self.frameGeometry()
                    left = x - frame.left()
                    top = y - frame.top()
                    right = frame.right() - x
                    bottom = frame.bottom() - y
                    border = 7
                    if left < border and top < border:
                        return True, 13  # HTTOPLEFT
                    if right < border and top < border:
                        return True, 14  # HTTOPRIGHT
                    if left < border and bottom < border:
                        return True, 16  # HTBOTTOMLEFT
                    if right < border and bottom < border:
                        return True, 17  # HTBOTTOMRIGHT
                    if left < border:
                        return True, 10  # HTLEFT
                    if right < border:
                        return True, 11  # HTRIGHT
                    if top < border:
                        return True, 12  # HTTOP
                    if bottom < border:
                        return True, 15  # HTBOTTOM
            except Exception:
                pass
        return super().nativeEvent(event_type, message)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._sync_composition_z_order)
        QTimer.singleShot(80, self._start_composition_preload)

    def hideEvent(self, event) -> None:
        if self._composition_backdrop is not None:
            self._composition_backdrop.set_visible(False)
        super().hideEvent(event)

    def _sync_composition_z_order(self) -> None:
        if self._composition_backdrop is not None and self.isVisible() and not self.isMinimized():
            self._composition_backdrop.sync_to_window(sync_z_order=True)

    def _dispose_composition_backdrop(self) -> None:
        backdrop = self._composition_backdrop
        self._composition_backdrop = None
        if backdrop is not None:
            backdrop.close()

    def _start_composition_preload(self) -> None:
        if (
            self._composition_preload_started
            or self._composition_backdrop is not None
            or self._close_requested
            or not acrylic_supported()
            or not composition_backdrop_available()
        ):
            return
        self._composition_preload_started = True

        def load_runtime() -> None:
            error = ""
            try:
                preload_composition_runtime()
            except Exception as exc:
                error = str(exc) or type(exc).__name__
            self.composition_runtime_ready.emit(error)

        threading.Thread(target=load_runtime, name="AcrylicRuntimeLoader", daemon=True).start()

    def _composition_runtime_loaded(self, error: str) -> None:
        if self._close_requested:
            return
        self._composition_error = error
        if not error:
            try:
                self._composition_backdrop = CompositionBackdrop(self, theme=self._resolved_theme)
                self._acrylic_state = AcrylicState(True, True, self._resolved_theme, "Windows Composition")
            except Exception as exc:
                self._dispose_composition_backdrop()
                self._composition_error = str(exc) or type(exc).__name__
        if self._composition_backdrop is None:
            self._acrylic_state = AcrylicState(True, False, self._resolved_theme, self._composition_error)

        from .styles import stylesheet

        app = QApplication.instance()
        if app and self._composition_backdrop is None:
            app.setStyleSheet(stylesheet(self._resolved_theme, acrylic=self._composition_backdrop is not None))
        self._sync_composition_z_order()

    def _navigate(self, index: int) -> None:
        self.pages.setCurrentIndex(index)
        self.nav_home.setChecked(index == 0)
        self.nav_history.setChecked(index == 1)
        self.nav_settings.setChecked(index == 2)
        if index != 2:
            self.settings_drawer.hide()
        else:
            self.settings_drawer.show()
            current_category = self.settings_page.stack.currentIndex()
            for button_index, button in enumerate(self.settings_drawer_buttons):
                button.setChecked(button_index == current_category)
        if index == 1:
            self.history_page.refresh()
        if index == 0 and not self._submission_active and self.bundle is None:
            self.home_page.show_idle()

    def _settings_nav_clicked(self) -> None:
        if self.pages.currentWidget() is self.settings_page:
            self.nav_settings.setChecked(True)
            self.settings_drawer.setVisible(not self.settings_drawer.isVisible())
            return
        self._navigate(2)

    def _select_settings_category(self, index: int) -> None:
        self.pages.setCurrentWidget(self.settings_page)
        self.settings_page.set_category(index)
        self.nav_home.setChecked(False)
        self.nav_history.setChecked(False)
        self.nav_settings.setChecked(True)
        self.settings_drawer.show()
        for button_index, button in enumerate(self.settings_drawer_buttons):
            button.setChecked(button_index == index)

    def _apply_theme(self, requested: str) -> None:
        if self._applying_theme:
            return
        self._applying_theme = True
        resolved = effective_theme(requested)
        self._resolved_theme = resolved
        try:
            state = AcrylicState(True, False, resolved)
            if self._composition_backdrop is not None:
                self._composition_backdrop.update_theme(resolved)
                state = AcrylicState(True, True, resolved, "Windows Composition")
                if self.isVisible():
                    QTimer.singleShot(0, self._sync_composition_z_order)
            elif acrylic_supported() and composition_backdrop_available():
                state = AcrylicState(True, False, resolved, "Windows Composition loading")
                if self.isVisible():
                    QTimer.singleShot(0, self._start_composition_preload)
            else:
                self._dispose_composition_backdrop()
                state = AcrylicState(True, False, resolved, "compositor unavailable or transparency disabled")
            from .styles import stylesheet

            app = QApplication.instance()
            if app:
                use_acrylic_style = state.applied or state.reason == "Windows Composition loading"
                app.setStyleSheet(stylesheet(resolved, acrylic=use_acrylic_style))
            self._acrylic_state = state
        finally:
            self._applying_theme = False

    def _theme_changed(self, requested: str) -> None:
        self.app_settings.set_theme(requested)
        self._apply_theme(requested)

    def changeEvent(self, event: QEvent) -> None:
        theme_events = {QEvent.Type.ApplicationPaletteChange, QEvent.Type.PaletteChange}
        native_theme_event = getattr(QEvent.Type, "ThemeChange", None)
        if native_theme_event is not None:
            theme_events.add(native_theme_event)
        if event.type() in theme_events:
            if self.app_settings.theme == "system" and not self._applying_theme:
                self._apply_theme("system")
        super().changeEvent(event)

    def _refresh_history_views(self) -> None:
        if self.pages.currentWidget() is self.history_page:
            self.history_page.refresh()
        self.settings_page.refresh_history_usage()

    def _current_extraction_options(self):
        return self.app_settings.extraction_options()

    def _api_options_snapshot(self):
        with self._api_options_lock:
            return self._api_options

    def _refresh_api_options_cache(self) -> None:
        options = self.app_settings.extraction_options()
        with self._api_options_lock:
            self._api_options = options

    def _settings_extraction_changed(self, mode: str, backend: str, model: str) -> None:
        try:
            self.app_settings.set_extraction(mode=mode, backend=backend, model=model)
        except ValueError:
            return
        self.settings_page.asr_backend_label.setText(backend)
        self._refresh_api_options_cache()

    def _refresh_login_status_text(self) -> None:
        if not self.login_status_label.text():
            self.login_status_label.setText("未检查")

    def _show_error(self, message: str) -> None:
        """Keep ordinary extraction errors inline instead of blocking dialogs."""
        self.home_page.show_error(str(message or "发生未知错误"))

    def _safe_task_error(self, message: str) -> str:
        options = self._active_snapshot.extraction if self._active_snapshot else self.app_settings.extraction_options()
        return sanitize_error(message, secrets=(options.asr_api_key, options.asr_api_base_url))

    def _update_actions(self) -> None:
        busy = bool(
            self.metadata_task
            or self.extraction_task
            or self.batch_task
            or self.availability_task
            or self.diagnostic_metadata_task
            or self.browser_task
            or self.browser_status_task
        )
        self.url_input.setEnabled(not busy)
        self.fetch_button.setEnabled(not busy)
        self.home_page.working_cancel.setEnabled(busy)

    def _set_busy(self, busy: bool, message: str, *, cancellable: bool = False, indeterminate: bool = False) -> None:
        if busy:
            self.home_page.show_working(message, 0, indeterminate=indeterminate)
        else:
            self.status_label.setText(message)
            self.home_page.working_cancel.setVisible(False)
            self._update_actions()

    def _submit_source(self, text: str) -> None:
        if self._submission_active or self.availability_task or self.diagnostic_metadata_task:
            return
        sources = extract_bilibili_sources(text)
        if not sources:
            self.home_page.show_error("请输入 B站视频链接、BV / av 号或包含可识别链接的文本", title="无法识别")
            return
        self._submission_active = True
        self._active_batch_sources = sources
        if len(sources) > 1:
            self._start_batch(sources)
        else:
            self._start_single(sources[0])

    def _start_single(self, source: str) -> None:
        self._active_source = source
        self._cancel_requested = False
        snapshot = self.app_settings.snapshot()
        self._active_snapshot = snapshot
        pending = self.history_store.save_pending(source, settings=snapshot.public_dict())
        self._active_history_id = pending.id
        self.video = None
        self.bundle = None
        self.home_page.show_working("正在读取视频信息…", indeterminate=True)
        task = MetadataTask(source, self)
        self.metadata_task = task
        task.succeeded.connect(self._video_loaded)
        task.failed.connect(self._metadata_failed)
        task.finished.connect(task.deleteLater)
        task.start()

    def _video_loaded(self, video: VideoInfo) -> None:
        self.metadata_task = None
        if self._cancel_requested:
            self._extraction_cancelled()
            return
        self.video = video
        if not video.parts:
            self._metadata_failed("视频没有可提取的分P")
            return
        # All parts are always selected; there is no intermediate confirmation view.
        options = self._active_snapshot.extraction if self._active_snapshot else self.app_settings.extraction_options()
        self.home_page.show_working(f"已读取 {video.title}，准备提取 {len(video.parts)} 个分P…", 0)
        task = ExtractionTask(video, list(video.parts), options, self)
        self.extraction_task = task
        task.progress_changed.connect(self._extraction_progress)
        task.log_message.connect(self._extraction_log)
        task.succeeded.connect(self._extraction_succeeded)
        task.failed.connect(self._extraction_failed)
        task.cancelled.connect(self._extraction_cancelled)
        task.finished.connect(task.deleteLater)
        task.start()

    def _metadata_failed(self, message: str) -> None:
        self.metadata_task = None
        if self._cancel_requested:
            self.history_store.save_terminal(self._active_source, "cancelled", error="已取消", entry_id=self._active_history_id, settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()))
            self._submission_active = False
            self.home_page.show_error("已取消", title="任务已取消")
            self._refresh_history_views()
            self._update_actions()
            return
        self.history_store.save_terminal(self._active_source, "failed", error=self._safe_task_error(message), entry_id=self._active_history_id, settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()))
        self._submission_active = False
        self.home_page.show_error(message)
        self._refresh_history_views()
        self._update_actions()

    def _extraction_progress(self, value: int, message: str) -> None:
        self.home_page.update_progress(value, message)

    def _extraction_log(self, message: str) -> None:
        if message:
            self.home_page.working_detail.setText(message)

    def _extraction_succeeded(self, bundle: TranscriptBundle) -> None:
        self.extraction_task = None
        self.bundle = bundle
        self.history_store.save_result(
            self._active_source,
            bundle,
            entry_id=self._active_history_id,
            settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()),
        )
        self._submission_active = False
        self.home_page.show_result(bundle, timestamps=self.app_settings.timestamps)
        self._refresh_history_views()
        self._update_actions()

    def _extraction_failed(self, message: str) -> None:
        self.extraction_task = None
        self.history_store.save_terminal(self._active_source, "failed", error=self._safe_task_error(message), entry_id=self._active_history_id, settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()))
        self._submission_active = False
        self.home_page.show_error(message)
        self._refresh_history_views()
        self._update_actions()

    def _extraction_cancelled(self) -> None:
        self.extraction_task = None
        self.history_store.save_terminal(self._active_source, "cancelled", error="已取消", entry_id=self._active_history_id, settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()))
        self._submission_active = False
        self.home_page.show_error("已取消", title="任务已取消")
        self._refresh_history_views()
        self._update_actions()

    def _update_preview(self) -> None:
        if self.bundle:
            self.home_page.show_result(self.bundle, timestamps=self.app_settings.timestamps)

    def _start_batch(self, sources: tuple[str, ...]) -> None:
        self._cancel_requested = False
        snapshot = self.app_settings.snapshot()
        self._active_snapshot = snapshot
        self._active_batch_history_ids = {}
        for source in sources:
            pending = self.history_store.save_pending(source, settings=snapshot.public_dict())
            self._active_batch_history_ids[source] = pending.id
        self.home_page.show_working(f"准备批量提取 {len(sources)} 个视频…", 0)
        task = BatchExtractionTask(
            sources,
            snapshot.extraction,
            Path(snapshot.default_export_dir),
            max_workers=snapshot.batch_concurrency,
            timestamps=snapshot.timestamps,
            parent=self,
        )
        self.batch_task = task
        task.item_progress.connect(
            lambda _index, _value, message: self.home_page.working_detail.setText(message) if message else None
        )
        task.progress_changed.connect(lambda value, message: self.home_page.update_progress(value, message))
        task.succeeded.connect(self._batch_succeeded)
        task.failed.connect(self._batch_failed)
        task.cancelled.connect(self._batch_cancelled)
        task.finished.connect(task.deleteLater)
        task.start()

    def _batch_succeeded(self, result: BatchResult) -> None:
        self.batch_task = None
        # Save each item independently; a failed video never hides successful ones.
        for item in result.items:
            if item.succeeded and item.bundle:
                self.history_store.save_result(item.source, item.bundle, entry_id=self._active_batch_history_ids.get(item.source), settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()), export_paths=[item.output_path] if item.output_path else ())
            else:
                self.history_store.save_terminal(item.source, "failed", entry_id=self._active_batch_history_ids.get(item.source), error=self._safe_task_error(item.error), settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()))
        self._submission_active = False
        self.home_page.show_batch_result(result)
        self._refresh_history_views()
        self._update_actions()

    def _batch_failed(self, message: str) -> None:
        self.batch_task = None
        for source in self._active_batch_sources:
            self.history_store.save_terminal(source, "failed", entry_id=self._active_batch_history_ids.get(source), error=self._safe_task_error(message), settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()))
        self._submission_active = False
        self.home_page.show_error(message)
        self._refresh_history_views()
        self._update_actions()

    def _batch_cancelled(self, result: BatchResult | None = None) -> None:
        self.batch_task = None
        completed_sources: set[str] = set()
        if result is not None:
            for item in result.items:
                completed_sources.add(item.source)
                if item.succeeded and item.bundle:
                    self.history_store.save_result(
                        item.source,
                        item.bundle,
                        entry_id=self._active_batch_history_ids.get(item.source),
                        settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()),
                        export_paths=[item.output_path] if item.output_path else (),
                    )
                else:
                    self.history_store.save_terminal(item.source, "cancelled", entry_id=self._active_batch_history_ids.get(item.source), error="已取消", settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()))
        for source in self._active_batch_sources:
            if source not in completed_sources:
                self.history_store.save_terminal(source, "cancelled", entry_id=self._active_batch_history_ids.get(source), error="已取消", settings=(self._active_snapshot.public_dict() if self._active_snapshot else self.app_settings.public_snapshot()))
        self._submission_active = False
        if result is not None:
            self.home_page.show_batch_result(result, cancelled=True)
        else:
            self.home_page.show_error("批量任务已取消", title="任务已取消")
        self._refresh_history_views()
        self._update_actions()

    def _cancel_active_task(self) -> None:
        self._cancel_requested = True
        self.home_page.working_detail.setText("正在取消…")
        if self.metadata_task:
            # MetadataTask has no cooperative cancellation; stopping submission prevents follow-up work.
            self.metadata_task.requestInterruption()
        if self.extraction_task:
            self.extraction_task.cancel()
        if self.batch_task:
            self.batch_task.cancel()

    def _next_extraction(self) -> None:
        if self._submission_active:
            return
        self.video = None
        self.bundle = None
        self._active_history_id = None
        self._active_source = ""
        self._cancel_requested = False
        self._navigate(0)
        self.home_page.show_idle(clear=True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        text = event.mimeData().text() if event.mimeData() else ""
        if text and extract_bilibili_sources(text):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        text = event.mimeData().text().strip() if event.mimeData() else ""
        if text:
            # Dropping only fills the field; submission still requires Enter/the arrow.
            self.url_input.setText(text)
            self.url_input.setFocus()
            event.acceptProposedAction()

    def _start_diagnostic(self, source: str) -> None:
        if self.diagnostic_metadata_task or self.availability_task:
            return
        sources = extract_bilibili_sources(source)
        if len(sources) != 1:
            self.settings_page.diagnostic_status.setText("请输入且只输入一个 B站视频")
            return
        if self._submission_active or self.browser_task or self.browser_status_task:
            self.settings_page.diagnostic_status.setText("请等待当前任务结束后再检测")
            return
        self._diagnostic_cancel_requested = False
        self._diagnostic_snapshot = self.app_settings.snapshot()
        self.settings_page.set_diagnostic_running(True)
        self.settings_page.diagnostic_results.clear()
        self.settings_page.diagnostic_results.hide()
        self.settings_page.diagnostic_progress.setRange(0, 0)
        self.settings_page.diagnostic_status.setText("正在读取视频信息…")
        task = MetadataTask(sources[0], self)
        self.diagnostic_metadata_task = task
        task.succeeded.connect(self._diagnostic_video_loaded)
        task.failed.connect(self._diagnostic_failed)
        task.finished.connect(task.deleteLater)
        task.start()
        self._update_actions()

    def _diagnostic_video_loaded(self, video: VideoInfo) -> None:
        self.diagnostic_metadata_task = None
        if self._diagnostic_cancel_requested:
            self._diagnostic_cancelled()
            return
        if not video.parts:
            self._diagnostic_failed("视频没有可检测的分P")
            return
        self.settings_page.diagnostic_progress.setRange(0, 100)
        self.settings_page.diagnostic_status.setText(f"正在检测 {len(video.parts)} 个分P…")
        options = self._diagnostic_snapshot.extraction if self._diagnostic_snapshot else self.app_settings.extraction_options()
        task = AvailabilityTask(video, list(video.parts), self, options=options)
        self.availability_task = task
        task.progress_changed.connect(self._diagnostic_progress)
        task.log_message.connect(lambda message: self.settings_page.diagnostic_status.setText(message) if message else None)
        task.succeeded.connect(self._diagnostic_succeeded)
        task.failed.connect(self._diagnostic_failed)
        task.cancelled.connect(self._diagnostic_cancelled)
        task.finished.connect(task.deleteLater)
        task.start()
        self._update_actions()

    def _diagnostic_progress(self, value: int, message: str) -> None:
        self.settings_page.diagnostic_progress.setValue(max(0, min(100, int(value))))
        if message:
            self.settings_page.diagnostic_status.setText(message)

    def _diagnostic_succeeded(self, report: AvailabilityReport) -> None:
        self.availability_task = None
        self.availability_report = report
        labels = {"public": "公开", "anonymous": "匿名", "browser": "登录", "asr": "ASR"}
        lines = [f"{report.video.title} · {report.video.bvid}"]
        for item in report.parts:
            lines.append("")
            lines.append(f"P{item.part.page} · {item.part.title}")
            for route in item.routes:
                mark = "✓" if route.available else "×"
                count = f"，{route.track_count} 条字幕" if route.track_count else ""
                lines.append(
                    f"  {labels.get(route.route, route.route)} {mark} · {route.attempts} 次{count} · {route.detail}"
                )
        self.settings_page.diagnostic_results.setPlainText("\n".join(lines))
        self.settings_page.diagnostic_results.show()
        self.settings_page.diagnostic_progress.setValue(100)
        self.settings_page.diagnostic_status.setText(f"检测完成：{len(report.parts)} 个分P")
        self.settings_page.set_diagnostic_running(False)
        self._update_actions()

    def _diagnostic_failed(self, message: str) -> None:
        self.diagnostic_metadata_task = None
        self.availability_task = None
        options = self._diagnostic_snapshot.extraction if self._diagnostic_snapshot else self.app_settings.extraction_options()
        safe_message = sanitize_error(message, secrets=(options.asr_api_key, options.asr_api_base_url))
        self.settings_page.diagnostic_status.setText(f"检测失败：{safe_message}")
        self.settings_page.set_diagnostic_running(False)
        self._update_actions()

    def _cancel_diagnostic(self) -> None:
        self._diagnostic_cancel_requested = True
        self.settings_page.diagnostic_cancel.setEnabled(False)
        self.settings_page.diagnostic_status.setText("正在取消检测…")
        if self.diagnostic_metadata_task:
            self.diagnostic_metadata_task.requestInterruption()
        if self.availability_task:
            self.availability_task.cancel()

    def _diagnostic_cancelled(self) -> None:
        self.diagnostic_metadata_task = None
        self.availability_task = None
        self.settings_page.diagnostic_status.setText("检测已取消")
        self.settings_page.set_diagnostic_running(False)
        self._update_actions()

    def _copy_transcript(self) -> None:
        if self.bundle:
            QApplication.clipboard().setText(self.editor.toPlainText())
            self.status_label.setText("文稿已复制")
        elif self.home_page._result_is_batch:
            QApplication.clipboard().setText(self.editor.toPlainText())

    def _save_transcript(self) -> None:
        if not self.bundle:
            return
        default_dir = self.app_settings.default_export_dir
        default_name = safe_filename(self.bundle.video.title) + ".md"
        path, selected = QFileDialog.getSaveFileName(self, "导出文稿", str(default_dir / default_name), "Markdown (*.md);;纯文本 (*.txt);;SRT (*.srt);;JSON (*.json)")
        if not path:
            return
        output = Path(path)
        suffix_map = {"纯文本 (*.txt)": ".txt", "SRT (*.srt)": ".srt", "JSON (*.json)": ".json", "Markdown (*.md)": ".md"}
        if not output.suffix:
            output = output.with_suffix(suffix_map.get(selected, ".md"))
        suffix = output.suffix.lower()
        content = self.bundle.to_json() if suffix == ".json" else self.bundle.to_srt() if suffix == ".srt" else self.bundle.to_text(timestamps=self.app_settings.timestamps) if suffix == ".txt" else self.bundle.to_markdown(timestamps=self.app_settings.timestamps)
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8-sig" if suffix in {".txt", ".srt"} else "utf-8")
            self.app_settings.set_default_export_dir(output.parent)
            self.status_label.setText(f"已导出：{output.name}")
        except OSError as exc:
            self.home_page.show_error(f"导出失败：{exc}", title="导出失败")

    def _history_entry_selected(self, entry: HistoryEntry) -> None:
        self._navigate(0)
        if entry.bundle:
            self.bundle = entry.bundle
            self.home_page.show_result(entry.bundle, timestamps=self.app_settings.timestamps)
        elif entry.error:
            self.home_page.show_error(entry.error, title="历史任务未完成")

    def _history_copy(self, entry: HistoryEntry | None) -> None:
        if entry and entry.bundle:
            QApplication.clipboard().setText(entry.bundle.to_markdown(timestamps=self.app_settings.timestamps))

    def _history_export(self, entry: HistoryEntry | None) -> None:
        if entry and entry.bundle:
            self.bundle = entry.bundle
            self._save_transcript()

    def _history_reextract(self, entry: HistoryEntry | None) -> None:
        if entry:
            self._navigate(0)
            self.home_page.show_idle(clear=True)
            self.url_input.setText(entry.source)
            self._submit_source(entry.source)

    def _history_delete(self, entry: HistoryEntry | None) -> None:
        if entry:
            self.history_store.delete(entry.id)
            self.history_page.offset = 0
            self.history_page.refresh()
            self.settings_page.refresh_history_usage()

    def _clear_history(self) -> None:
        reply = QMessageBox.question(self, "清空历史", "清空所有历史记录？已导出的文件不会删除。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            self.history_store.clear()
            self.history_page.refresh()
            self.settings_page.refresh_history_usage()

    def _toggle_startup(self, enabled: bool) -> None:
        try:
            set_startup_enabled(bool(enabled))
            self.app_settings.set_startup_enabled(bool(enabled))
            self.settings_page.general_feedback.setText("开机自启已开启" if enabled else "开机自启已关闭")
            self._refresh_startup_button()
        except StartupError as exc:
            self.app_settings.set_startup_enabled(False)
            self.startup_button.blockSignals(True)
            self.startup_button.setChecked(False)
            self.startup_button.blockSignals(False)
            self.settings_page.general_feedback.setText(str(exc))

    def _refresh_startup_button(self) -> None:
        try:
            enabled = bool(is_startup_enabled())
        except StartupError:
            enabled = False
        self.startup_button.blockSignals(True)
        self.startup_button.setChecked(enabled)
        self.startup_button.setText("登录 Windows 时自动打开")
        self.startup_button.blockSignals(False)
        self.app_settings.set_startup_enabled(enabled)

    def _show_about(self) -> None:
        self._navigate(2)
        self._select_settings_category(5)

    def _open_api_settings(self) -> None:
        self._navigate(2)
        self._select_settings_category(3)

    def _open_extraction_api_settings(self) -> None:
        self._navigate(2)
        self._select_settings_category(4)

    def _apply_asr_settings(self) -> bool:
        try:
            normalized = OpenAICompatibleAsrRuntime.normalize_base_url(self.settings_page.asr_base_url.text().strip() or DEFAULT_ASR_API_BASE_URL)
        except AsrError as exc:
            self.settings_page.asr_base_url.setStyleSheet("border: 1px solid #C64151;")
            self.settings_page.asr_feedback.setText(str(exc))
            return False
        self.settings_page.asr_base_url.setStyleSheet("")
        self.app_settings.set_asr(base_url=normalized, api_key=self.settings_page.asr_key.text(), timeout=self.settings_page.asr_timeout.value())
        self._refresh_api_options_cache()
        self.settings_page.asr_base_url.setText(normalized)
        self.settings_page.asr_feedback.setText("ASR 设置已应用")
        return True

    def _test_asr_connection(self) -> None:
        if self.asr_health_task or not self._apply_asr_settings():
            return
        self.settings_page.asr_test.setEnabled(False)
        self.settings_page.asr_test.setText("测试中…")
        self.settings_page.asr_feedback.setText("正在连接 ASR API…")
        # A health check should stay short even when transcription uses a long timeout.
        settings = AsrApiSettings(
            self.app_settings.asr_base_url,
            self.app_settings.asr_api_key,
            timeout_seconds=min(15.0, self.app_settings.asr_timeout),
        )
        task = AsrHealthTask(settings, self)
        self.asr_health_task = task
        task.succeeded.connect(self._asr_health_succeeded)
        task.failed.connect(self._asr_health_failed)
        task.finished.connect(task.deleteLater)
        task.start()

    def _asr_health_succeeded(self, detail: str) -> None:
        self.asr_health_task = None
        self.settings_page.asr_test.setEnabled(True)
        self.settings_page.asr_test.setText("测试连接")
        self.settings_page.asr_feedback.setText(f"连接成功：{detail}")

    def _asr_health_failed(self, message: str) -> None:
        self.asr_health_task = None
        self.settings_page.asr_test.setEnabled(True)
        self.settings_page.asr_test.setText("测试连接")
        self.settings_page.asr_feedback.setText(f"连接失败：{sanitize_error(message, secrets=(self.app_settings.asr_api_key, self.app_settings.asr_base_url))}")

    def _update_api_page(self) -> None:
        server = self.extraction_api_server
        self.settings_page.api_run.blockSignals(True)
        self.settings_page.api_run.setChecked(bool(server and server.running))
        self.settings_page.api_run.blockSignals(False)
        self.settings_page.api_port.setValue(server.bound_port if server else self.app_settings.api_port)
        self.settings_page.api_key.setText(self.app_settings.api_key)
        port = server.bound_port if server else self.app_settings.api_port
        self.settings_page.api_address.setText(_lan_address(port))
        active = int(server.counts().get("active", 0)) if server else 0
        self.settings_page.api_active.setText(f"活跃任务：{active}")
        self._refresh_history_views()

    def _api_run_toggled(self, enabled: bool) -> None:
        if enabled:
            self._start_extraction_api(self.settings_page.api_port.value(), self.app_settings.api_key)
        else:
            self._stop_extraction_api()
            self.settings_page.api_feedback.setText("API 服务正在停止" if self.api_stop_task else "API 服务已停止")
        self._update_api_page()

    def _apply_api_settings(self) -> None:
        port = self.settings_page.api_port.value()
        previous_port = self.app_settings.api_port
        self.app_settings.set_api_port(port)
        if self.extraction_api_server and self.extraction_api_server.bound_port != port:
            active = int(self.extraction_api_server.counts().get("active", 0))
            if active:
                reply = QMessageBox.question(self, "更换 API 端口", f"API 仍有 {active} 个任务。更换端口会取消这些任务，继续吗？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if reply != QMessageBox.StandardButton.Yes:
                    self.app_settings.set_api_port(previous_port)
                    self._update_api_page()
                    return
            self._stop_extraction_api(pending_start=(port, self.app_settings.api_key))
        elif self.extraction_api_server:
            self.extraction_api_server.update_api_key(self.app_settings.api_key)
            self.settings_page.api_feedback.setText("API 设置已应用，密钥已立即更新")
        else:
            self.settings_page.api_feedback.setText("API 设置已保存")
        self._update_api_page()

    def _regenerate_api_key(self) -> None:
        key = self.app_settings.regenerate_api_key()
        self.settings_page.api_key.setText(key)
        if self.extraction_api_server:
            self.extraction_api_server.update_api_key(key)
        self.settings_page.api_feedback.setText("已生成新密钥；旧密钥已立即失效")

    def _auto_start_extraction_api(self) -> None:
        if self._close_requested or not self.app_settings.api_auto_start:
            return
        self._start_extraction_api(self.app_settings.api_port, self.app_settings.api_key)

    def _start_extraction_api(self, port: int, api_key: str) -> bool:
        if self.extraction_api_server:
            self.extraction_api_server.update_api_key(api_key)
            self._update_api_page()
            return True
        if self.api_stop_task:
            self._pending_api_start = (int(port), api_key)
            return False
        server = ExtractionApiServer(
            host=DEFAULT_EXTRACTION_API_HOST,
            port=int(port),
            api_key=api_key,
            options_provider=self._api_options_snapshot,
            version=__version__,
            on_finished=self._api_job_finished,
            on_submitted=self._api_job_submitted,
            on_started=self._api_job_started,
        )
        try:
            server.start()
        except Exception as exc:
            server.stop()
            self.settings_page.api_feedback.setText(f"启动失败：{sanitize_error(str(exc))}")
            return False
        self.extraction_api_server = server
        self.settings_page.api_run.blockSignals(True)
        self.settings_page.api_run.setChecked(True)
        self.settings_page.api_run.blockSignals(False)
        self.settings_page.api_address.setText(_lan_address(server.bound_port))
        self.settings_page.api_feedback.setText(f"API 已运行，端口 {server.bound_port}")
        return True

    def _api_job_finished(self, job: ExtractionJob) -> None:
        # Called from the API worker thread; HistoryStore opens its own connection.
        settings = {
            "mode": job.options.mode,
            "browser_ai": job.options.browser_ai,
            "asr_backend": job.options.asr_backend,
            "asr_model": job.options.asr_model,
            "language": job.options.language,
        }
        if job.status == "succeeded" and job.result:
            self.history_store.save_result(job.source, job.result, source_kind="api", entry_id=job.job_id, settings=settings, created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at)
        elif job.status in {"failed", "cancelled"}:
            self.history_store.save_terminal(job.source, job.status, source_kind="api", entry_id=job.job_id, error=job.error or job.message, settings=settings, created_at=job.created_at, started_at=job.started_at, finished_at=job.finished_at)
        self.api_state_changed.emit()

    def _api_job_submitted(self, job: ExtractionJob) -> None:
        self.history_store.save_pending(
            job.source,
            source_kind="api",
            entry_id=job.job_id,
            settings={
                "mode": job.options.mode,
                "browser_ai": job.options.browser_ai,
                "asr_backend": job.options.asr_backend,
                "asr_model": job.options.asr_model,
                "language": job.options.language,
            },
        )
        self.api_state_changed.emit()

    def _api_job_started(self, job: ExtractionJob) -> None:
        self.history_store.mark_running(job.job_id, started_at=job.started_at)
        self.api_state_changed.emit()

    def _stop_extraction_api(self, *, pending_start: tuple[int, str] | None = None) -> None:
        server = self.extraction_api_server
        self._pending_api_start = pending_start
        if server is None:
            return
        self.extraction_api_server = None
        self.settings_page.api_run.blockSignals(True)
        self.settings_page.api_run.setChecked(False)
        self.settings_page.api_run.blockSignals(False)
        task = ApiServerStopTask(server, self)
        self.api_stop_task = task
        task.failed.connect(lambda message: self.status_label.setText(f"提取 API 停止失败：{message}"))
        task.finished.connect(self._api_stop_finished)
        task.finished.connect(task.deleteLater)
        task.start()

    def _api_stop_finished(self) -> None:
        self.api_stop_task = None
        pending = self._pending_api_start
        self._pending_api_start = None
        if pending and not self._close_requested:
            self._start_extraction_api(*pending)
        elif not self._close_requested:
            self.settings_page.api_feedback.setText("API 服务已停止")
        self._update_api_page()
        if self._close_requested:
            self._continue_close()

    def _api_runtime_state(self) -> dict[str, Any]:
        server = self.extraction_api_server
        return {"running": bool(server and server.running), "stopping": bool(self.api_stop_task), "port": server.bound_port if server else None, "counts": server.counts() if server else {"active": 0}}

    # The inherited automatic login methods use these aliases and remain unchanged.
    def _close_active_tasks(self) -> None:
        self._cancel_requested = True
        self._login_watch_active = False
        self._login_check_timer.stop()
        self.home_page._stop_working_animation()
        if self.metadata_task:
            self.metadata_task.requestInterruption()
        if self.extraction_task:
            self.extraction_task.cancel()
        if self.batch_task:
            self.batch_task.cancel()
        if self.availability_task:
            self.availability_task.cancel()
        if self.diagnostic_metadata_task:
            self._diagnostic_cancel_requested = True
            self.diagnostic_metadata_task.requestInterruption()
        if self.extraction_api_server:
            self._stop_extraction_api()

    def _wait_for_close_signals(self) -> None:
        for task in (
            self.metadata_task,
            self.extraction_task,
            self.batch_task,
            self.availability_task,
            self.diagnostic_metadata_task,
            self.asr_health_task,
            self.browser_task,
            self.browser_status_task,
            self.api_stop_task,
        ):
            if task is not None and task.isRunning():
                try:
                    task.finished.connect(self._continue_close)
                except (RuntimeError, TypeError):
                    pass

    def closeEvent(self, event: QCloseEvent) -> None:
        active = any(task and task.isRunning() for task in (self.metadata_task, self.extraction_task, self.batch_task, self.availability_task, self.diagnostic_metadata_task, self.asr_health_task, self.browser_task, self.browser_status_task))
        api_active = int(self._api_runtime_state().get("counts", {}).get("active", 0))
        if (active or api_active or self.api_stop_task) and not self._close_requested:
            reply = QMessageBox.question(self, "退出 Bili 文稿", "仍有任务运行，继续会取消任务并退出，是否继续？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        if active or self.extraction_api_server or self.api_stop_task:
            self._close_requested = True
            self._close_active_tasks()
            self._wait_for_close_signals()
            self.hide()
            event.ignore()
            QTimer.singleShot(0, self._continue_close)
            return
        self._dispose_composition_backdrop()
        event.accept()

    def _continue_close(self) -> None:
        if any(task and task.isRunning() for task in (self.metadata_task, self.extraction_task, self.batch_task, self.availability_task, self.diagnostic_metadata_task, self.asr_health_task, self.browser_task, self.browser_status_task)) or self.api_stop_task:
            return
        self.close()


__all__ = ["ModernMainWindow", "HomePage", "HistoryPage", "SettingsPage"]
