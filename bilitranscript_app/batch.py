from __future__ import annotations

import re
import threading
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QStandardPaths, QThread, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from .bilibili import BilibiliClient, CancelledError
from .extractor import ExtractionError, ExtractionOptions, TranscriptExtractor
from .models import VideoInfo, safe_filename


_URL_RE = re.compile(
    r"(?i)(?<![\w.-])(?:https?://)?(?:www\.|m\.)?bilibili\.com/[^\s<>\"'，。；！？、]+"
    r"|(?<![\w.-])(?:https?://)?(?:www\.)?b23\.tv/[^\s<>\"'，。；！？、]+"
)
_BVID_RE = re.compile(r"(?i)\bBV[0-9A-Za-z]{10,}\b")
_AV_RE = re.compile(r"(?i)\bav\d+\b")
_TRAILING_PUNCTUATION = ".,;:!?)]}>\"'，。；：！？、）》」』】》"


def _clean_source(value: str) -> str:
    cleaned = value.strip().rstrip(_TRAILING_PUNCTUATION)
    if cleaned.lower().startswith(("www.", "bilibili.com/", "m.bilibili.com/", "b23.tv/", "www.b23.tv/")):
        return "https://" + cleaned
    return cleaned


def _canonical_source(value: str) -> str:
    cleaned = _clean_source(value)
    bvid = _BVID_RE.search(cleaned)
    if bvid:
        return "BV" + bvid.group(0)[2:]
    av = _AV_RE.search(cleaned)
    if av and (cleaned.lower().startswith("av") or "/av" in cleaned.lower()):
        return "av" + av.group(0)[2:]
    return cleaned


def extract_bilibili_sources(text: str) -> tuple[str, ...]:
    """Find Bilibili URLs and IDs in arbitrary pasted text, preserving order."""

    if not text:
        return ()
    found: list[str] = []
    seen: set[str] = set()
    matches: list[tuple[int, int, str]] = []
    accepted_spans: list[tuple[int, int]] = []

    def add(value: str) -> None:
        source = _canonical_source(value)
        if not source:
            return
        key = source.lower() if source.lower().startswith(("http://", "https://", "av")) else source
        if key not in seen:
            seen.add(key)
            found.append(source)

    for pattern in (_URL_RE, _BVID_RE, _AV_RE):
        matches.extend((match.start(), match.end(), match.group(0)) for match in pattern.finditer(text))
    for start, end, value in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start >= accepted_start and end <= accepted_end for accepted_start, accepted_end in accepted_spans):
            continue
        accepted_spans.append((start, end))
        add(value)
    return tuple(found)


def batch_output_filename(video: VideoInfo) -> str:
    title = safe_filename(video.title, "B站文稿")[:90].rstrip(" ._") or "B站文稿"
    return f"{title}__{video.bvid}.md"


@dataclass(frozen=True, slots=True)
class BatchItemResult:
    index: int
    source: str
    title: str = ""
    output_path: Path | None = None
    error: str = ""

    @property
    def succeeded(self) -> bool:
        return self.output_path is not None and not self.error


@dataclass(frozen=True, slots=True)
class BatchResult:
    items: tuple[BatchItemResult, ...]

    @property
    def success_count(self) -> int:
        return sum(item.succeeded for item in self.items)


class BatchExtractionTask(QThread):
    item_started = Signal(int, str)
    item_progress = Signal(int, int, str)
    item_finished = Signal(int, bool, str)
    progress_changed = Signal(int, str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        sources: tuple[str, ...],
        options: ExtractionOptions,
        output_dir: Path,
        max_workers: int = 3,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.sources = sources
        self.options = options
        self.output_dir = output_dir
        self.max_workers = max(1, min(4, int(max_workers)))
        self._cancel_event = threading.Event()
        self._path_lock = threading.Lock()
        self._reserved_paths: set[str] = set()

    def cancel(self) -> None:
        self._cancel_event.set()

    def _output_path(self, video: VideoInfo) -> Path:
        base = self.output_dir / batch_output_filename(video)
        with self._path_lock:
            candidate = base
            counter = 2
            while candidate.name in self._reserved_paths or candidate.exists():
                candidate = base.with_name(f"{base.stem}_{counter}{base.suffix}")
                counter += 1
            self._reserved_paths.add(candidate.name)
            return candidate

    def _extract_one(self, index: int, source: str) -> BatchItemResult:
        if self._cancel_event.is_set():
            raise CancelledError("批量任务已取消")
        self.item_started.emit(index, source)
        video = BilibiliClient().fetch_video(source)
        if not video.parts:
            raise ExtractionError("视频没有可提取的分P")

        extractor = TranscriptExtractor()

        def report(value: int, message: str) -> None:
            self.item_progress.emit(index, max(0, min(100, int(value))), message)

        bundle = extractor.extract(
            video,
            list(video.parts),
            self.options,
            cancelled=self._cancel_event.is_set,
            progress=report,
            log=lambda message: self.item_progress.emit(index, -1, message),
        )
        if self._cancel_event.is_set():
            raise CancelledError("批量任务已取消")
        if not bundle.parts:
            raise ExtractionError("没有提取到有效文稿")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_path(video)
        output_path.write_text(bundle.to_markdown(), encoding="utf-8")
        return BatchItemResult(index=index, source=source, title=video.title, output_path=output_path)

    def run(self) -> None:
        if not self.sources:
            self.failed.emit("没有识别到 B站视频链接")
            return
        results: list[BatchItemResult | None] = [None] * len(self.sources)
        completed = 0
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="bili-batch") as executor:
                futures = {
                    executor.submit(self._extract_one, index, source): (index, source)
                    for index, source in enumerate(self.sources)
                }
                for future in as_completed(futures):
                    index, source = futures[future]
                    try:
                        result = future.result()
                    except FutureCancelledError:
                        result = BatchItemResult(index=index, source=source, error="已取消")
                    except CancelledError:
                        result = BatchItemResult(index=index, source=source, error="已取消")
                    except Exception as exc:
                        result = BatchItemResult(index=index, source=source, error=str(exc) or type(exc).__name__)
                    results[index] = result
                    completed += 1
                    if result.succeeded:
                        self.item_finished.emit(index, True, str(result.output_path))
                    else:
                        self.item_finished.emit(index, False, result.error)
                    self.progress_changed.emit(
                        int(completed / len(self.sources) * 100),
                        f"已完成 {completed}/{len(self.sources)} 个视频",
                    )
            if self._cancel_event.is_set():
                self.cancelled.emit()
                return
            complete_results = tuple(item for item in results if item is not None)
            self.succeeded.emit(BatchResult(complete_results))
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class BatchDialog(QDialog):
    def __init__(self, options: ExtractionOptions, initial_text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("批量提取文稿")
        self.setObjectName("appRoot")
        self.resize(800, 700)
        self.setMinimumSize(680, 560)
        self.options = options
        self.sources: tuple[str, ...] = ()
        self.task: BatchExtractionTask | None = None
        self._closing = False

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)

        title = QLabel("批量提取文稿")
        title.setObjectName("appTitle")
        root.addWidget(title)
        subtitle = QLabel("粘贴包含多个 B站链接、BV 号或混合文本，识别后并行提取，每个视频单独导出 Markdown。")
        subtitle.setObjectName("meta")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        input_panel = QFrame()
        input_panel.setObjectName("softPanel")
        input_layout = QVBoxLayout(input_panel)
        input_layout.setContentsMargins(12, 12, 12, 12)
        input_layout.setSpacing(8)
        self.input_edit = QPlainTextEdit()
        self.input_edit.setPlaceholderText("例如：\nhttps://www.bilibili.com/video/BV...\nBV...\nhttps://b23.tv/...")
        self.input_edit.setMinimumHeight(150)
        self.input_edit.textChanged.connect(self._refresh_sources)
        input_layout.addWidget(self.input_edit)
        input_actions = QHBoxLayout()
        paste_button = QPushButton("从剪贴板粘贴")
        paste_button.clicked.connect(self._paste_clipboard)
        input_actions.addWidget(paste_button)
        clear_button = QPushButton("清空")
        clear_button.clicked.connect(self.input_edit.clear)
        input_actions.addWidget(clear_button)
        input_actions.addStretch(1)
        self.detected_label = QLabel("未识别到 B站视频链接")
        self.detected_label.setObjectName("meta")
        input_actions.addWidget(self.detected_label)
        input_layout.addLayout(input_actions)
        root.addWidget(input_panel)

        output_row = QHBoxLayout()
        output_row.addWidget(QLabel("导出目录"))
        self.output_edit = QLineEdit(self._default_output_dir())
        self.output_edit.setReadOnly(True)
        output_row.addWidget(self.output_edit, 1)
        choose_button = QPushButton("选择…")
        choose_button.clicked.connect(self._choose_output_dir)
        output_row.addWidget(choose_button)
        root.addLayout(output_row)

        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("提取方式"))
        mode_text = {
            "auto": "智能提取",
            "public": "只用公开字幕",
            "anonymous": "只用匿名接口",
            "browser": "只用登录浏览器",
            "asr": "只用本地 ASR",
        }.get(options.mode, options.mode)
        mode_label = QLabel(mode_text)
        mode_label.setObjectName("metric")
        settings_row.addWidget(mode_label)
        settings_row.addSpacing(14)
        settings_row.addWidget(QLabel("并行数"))
        self.worker_spin = QSpinBox()
        self.worker_spin.setRange(1, 4)
        self.worker_spin.setValue(3)
        self.worker_spin.setToolTip("同时提取的视频数量；ASR 建议使用 1–2")
        settings_row.addWidget(self.worker_spin)
        settings_row.addStretch(1)
        root.addLayout(settings_row)

        self.result_list = QListWidget()
        self.result_list.setMinimumHeight(170)
        root.addWidget(self.result_list, 1)

        status_row = QHBoxLayout()
        self.status_label = QLabel("等待开始")
        self.status_label.setObjectName("meta")
        status_row.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        status_row.addWidget(self.progress, 1)
        root.addLayout(status_row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setVisible(False)
        self.cancel_button.clicked.connect(self._cancel)
        buttons.addWidget(self.cancel_button)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.close)
        buttons.addWidget(self.close_button)
        self.start_button = QPushButton("开始批量提取")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self._start)
        buttons.addWidget(self.start_button)
        root.addLayout(buttons)

        if initial_text:
            self.input_edit.setPlainText(initial_text)
        else:
            self._refresh_sources()

    @staticmethod
    def _default_output_dir() -> str:
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        return str(Path(documents) / "Bili文稿批量")

    def _paste_clipboard(self) -> None:
        from PySide6.QtWidgets import QApplication

        text = QApplication.clipboard().text()
        if text:
            self.input_edit.setPlainText(text)
            self.input_edit.setFocus()

    def _choose_output_dir(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择批量导出目录", self.output_edit.text())
        if selected:
            self.output_edit.setText(selected)

    def _refresh_sources(self) -> None:
        if self.task:
            return
        self.sources = extract_bilibili_sources(self.input_edit.toPlainText())
        count = len(self.sources)
        self.detected_label.setText(f"识别到 {count} 个视频（已自动去重）" if count else "未识别到 B站视频链接")
        self.start_button.setEnabled(bool(self.sources))
        self.result_list.clear()
        for index, source in enumerate(self.sources, 1):
            self.result_list.addItem(f"{index}. {source}\n等待提取")

    def _start(self) -> None:
        if self.task or not self.sources:
            return
        output_text = self.output_edit.text().strip()
        if not output_text:
            self.status_label.setText("请选择导出目录")
            return
        output_dir = Path(output_text).expanduser()
        self.start_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.cancel_button.setVisible(True)
        self.input_edit.setEnabled(False)
        self.status_label.setText(f"开始并行提取 {len(self.sources)} 个视频…")
        self.progress.setValue(0)
        self.task = BatchExtractionTask(self.sources, self.options, output_dir, self.worker_spin.value(), self)
        self.task.item_started.connect(self._item_started)
        self.task.item_progress.connect(self._item_progress)
        self.task.item_finished.connect(self._item_finished)
        self.task.progress_changed.connect(self._progress_changed)
        self.task.succeeded.connect(self._succeeded)
        self.task.failed.connect(self._failed)
        self.task.cancelled.connect(self._cancelled)
        self.task.finished.connect(self.task.deleteLater)
        self.task.start()

    def _item_started(self, index: int, source: str) -> None:
        self._set_item(index, f"{index + 1}. {source}\n读取视频信息…", "#F3BE62")

    def _item_progress(self, index: int, _value: int, message: str) -> None:
        item = self.result_list.item(index)
        if item and message:
            first_line = item.text().splitlines()[0] if item.text() else f"{index + 1}."
            item.setText(f"{first_line}\n{message}")

    def _item_finished(self, index: int, succeeded: bool, detail: str) -> None:
        item = self.result_list.item(index)
        if not item:
            return
        first_line = item.text().splitlines()[0] if item.text() else f"{index + 1}."
        if succeeded:
            item.setText(f"{first_line}\n已导出：{Path(detail).name}")
            item.setForeground(QBrush(QColor("#45D1A3")))
        else:
            item.setText(f"{first_line}\n失败：{detail}")
            item.setForeground(QBrush(QColor("#FF6E72")))

    def _progress_changed(self, value: int, message: str) -> None:
        self.progress.setValue(value)
        self.status_label.setText(message)

    def _set_item(self, index: int, text: str, color: str) -> None:
        item = self.result_list.item(index)
        if item:
            item.setText(text)
            item.setForeground(QBrush(QColor(color)))

    def _succeeded(self, result: BatchResult) -> None:
        self.task = None
        self.cancel_button.setVisible(False)
        self.close_button.setEnabled(True)
        self.input_edit.setEnabled(True)
        self.progress.setValue(100)
        self.status_label.setText(f"完成：成功导出 {result.success_count}/{len(result.items)} 个 Markdown")
        self.start_button.setEnabled(bool(self.sources))

    def _failed(self, message: str) -> None:
        self.task = None
        self.cancel_button.setVisible(False)
        self.close_button.setEnabled(True)
        self.input_edit.setEnabled(True)
        self.status_label.setText("批量任务失败")
        self.start_button.setEnabled(bool(self.sources))
        self._show_error(message)

    def _cancel(self) -> None:
        if self.task:
            self.status_label.setText("正在取消…")
            self.cancel_button.setEnabled(False)
            self.task.cancel()

    def _cancelled(self) -> None:
        self.task = None
        self.cancel_button.setVisible(False)
        self.close_button.setEnabled(True)
        self.input_edit.setEnabled(True)
        self.start_button.setEnabled(bool(self.sources))
        self.status_label.setText("批量任务已取消")

    def _show_error(self, message: str) -> None:
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.critical(self, "批量提取", message)

    def closeEvent(self, event) -> None:
        if self.task and self.task.isRunning():
            self.status_label.setText("请先取消正在运行的批量任务")
            event.ignore()
            return
        self._closing = True
        event.accept()
