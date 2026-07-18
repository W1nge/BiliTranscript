from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .asr import AsrError
from .asr_api import (
    ASR_API_BACKEND,
    DEFAULT_ASR_API_BASE_URL,
    DEFAULT_ASR_API_KEY,
    DEFAULT_ASR_API_MODEL,
    DEFAULT_ASR_API_TIMEOUT,
    AsrApiSettings,
    OpenAICompatibleAsrRuntime,
)
from .batch import BatchDialog, extract_bilibili_sources
from .extractor import ExtractionOptions
from .models import (
    AvailabilityReport,
    PartAvailability,
    TranscriptBundle,
    VideoInfo,
    VideoPart,
    format_duration,
    safe_filename,
)
from .workers import AvailabilityTask, BrowserLaunchTask, BrowserStatusTask, ExtractionTask, MetadataTask


class SourceLineEdit(QLineEdit):
    multiple_pasted = Signal(str)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Paste):
            text = QApplication.clipboard().text()
            if len(extract_bilibili_sources(text)) > 1:
                self.multiple_pasted.emit(text)
                return
        super().keyPressEvent(event)


class AsrApiSettingsDialog(QDialog):
    def __init__(self, base_url: str, api_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("ASR API 设置")
        self.setMinimumWidth(500)

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 20)
        root.setSpacing(12)

        title = QLabel("OpenAI 兼容 ASR API")
        title.setObjectName("appTitle")
        root.addWidget(title)
        hint = QLabel(
            "适用于 CrisperWeaver / MiMo 等 OpenAI 兼容服务。\n"
            "服务端需要保持运行；默认地址为 http://127.0.0.1:8765。"
        )
        hint.setObjectName("meta")
        hint.setWordWrap(True)
        root.addWidget(hint)

        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        self.base_url_edit = QLineEdit(base_url or DEFAULT_ASR_API_BASE_URL)
        self.base_url_edit.setPlaceholderText(DEFAULT_ASR_API_BASE_URL)
        self.base_url_edit.setToolTip("可填写根地址或带 /v1 的地址，程序会自动规范化")
        form.addRow("Base URL", self.base_url_edit)
        self.api_key_edit = QLineEdit(api_key or DEFAULT_ASR_API_KEY)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText(DEFAULT_ASR_API_KEY)
        self.api_key_edit.setToolTip("本地 CrisperWeaver 通常填写 local；无鉴权服务可留空")
        form.addRow("API Key", self.api_key_edit)
        root.addLayout(form)

        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._test_connection)
        root.addWidget(self.test_button, 0, Qt.AlignmentFlag.AlignLeft)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_settings)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _settings(self) -> AsrApiSettings:
        return AsrApiSettings(
            base_url=self.base_url_edit.text().strip() or DEFAULT_ASR_API_BASE_URL,
            api_key=self.api_key_edit.text().strip(),
        )

    def _test_connection(self) -> None:
        self.test_button.setEnabled(False)
        detail = ""
        try:
            detail = OpenAICompatibleAsrRuntime().health(self._settings())
        except AsrError as exc:
            QMessageBox.critical(self, "ASR API 连接失败", str(exc))
        finally:
            self.test_button.setEnabled(True)
        if detail:
            QMessageBox.information(self, "ASR API 可用", detail)

    def _accept_settings(self) -> None:
        try:
            normalized = OpenAICompatibleAsrRuntime.normalize_base_url(self._settings().base_url)
        except AsrError as exc:
            QMessageBox.warning(self, "API 地址无效", str(exc))
            return
        self.base_url_edit.setText(normalized)
        self.accept()

    @property
    def base_url(self) -> str:
        return self._settings().base_url

    @property
    def api_key(self) -> str:
        return self._settings().api_key


class PartRow(QFrame):
    selection_changed = Signal()

    def __init__(self, part: VideoPart, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.part = part
        self.setObjectName("softPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(11, 9, 11, 9)
        layout.setSpacing(9)

        self.checkbox = QCheckBox()
        self.checkbox.setChecked(True)
        self.checkbox.stateChanged.connect(self.selection_changed)
        layout.addWidget(self.checkbox, 0, Qt.AlignmentFlag.AlignTop)

        text_column = QVBoxLayout()
        text_column.setSpacing(3)
        title = QLabel(f"P{part.page} · {part.title}")
        title.setWordWrap(True)
        title.setToolTip(part.title)
        text_column.addWidget(title)
        self.detail = QLabel(format_duration(part.duration))
        self.detail.setObjectName("meta")
        text_column.addWidget(self.detail)
        layout.addLayout(text_column, 1)

        self.status = QLabel("待提取")
        self.status.setObjectName("meta")
        self.status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.status)

    @property
    def selected(self) -> bool:
        return self.checkbox.isChecked()

    def set_selected(self, selected: bool) -> None:
        self.checkbox.setChecked(selected)

    def set_status(self, text: str, kind: str = "muted") -> None:
        colors = {
            "muted": "#969EAC",
            "working": "#F3BE62",
            "success": "#45D1A3",
            "error": "#FF6E72",
        }
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {colors.get(kind, colors['muted'])};")

    def set_availability(self, availability: PartAvailability) -> None:
        compact_labels = {
            "public": "公开",
            "anonymous": "匿名",
            "browser": "登录",
            "asr": "ASR",
        }
        summary = " · ".join(
            f"{compact_labels.get(item.route, item.route)}{'✓' if item.available else '×'}"
            for item in availability.routes
        )
        self.detail.setText(f"{format_duration(self.part.duration)}\n{summary}")
        self.detail.setWordWrap(True)
        available_count = sum(1 for item in availability.routes if item.available)
        self.set_status(f"{available_count} 种可用", "success" if available_count else "error")
        lines = [
            f"{compact_labels.get(item.route, item.route)}：{item.detail}（尝试 {item.attempts or 1} 次）"
            for item in availability.routes
        ]
        self.setToolTip("\n".join(lines))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Bili 文稿")
        self.resize(1180, 840)
        self.setMinimumSize(960, 660)
        self.setAcceptDrops(True)

        self.settings = QSettings()
        self.video: VideoInfo | None = None
        self.bundle: TranscriptBundle | None = None
        self.availability_report: AvailabilityReport | None = None
        self.part_rows: dict[int, PartRow] = {}
        self.metadata_task: MetadataTask | None = None
        self.browser_task: BrowserLaunchTask | None = None
        self.browser_status_task: BrowserStatusTask | None = None
        self.availability_task: AvailabilityTask | None = None
        self.extraction_task: ExtractionTask | None = None
        self.batch_dialog: BatchDialog | None = None
        self._close_requested = False

        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(26, 20, 26, 20)
        root_layout.setSpacing(14)

        root_layout.addLayout(self._build_header())
        root_layout.addWidget(self._build_input_card())
        root_layout.addWidget(self._build_workspace(), 1)
        root_layout.addLayout(self._build_status_bar())

        self._update_model_choices()
        self._restore_settings()
        self._update_actions()
        QTimer.singleShot(1500, self._check_login_status)

    def _build_header(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setSpacing(12)
        logo = QLabel("稿")
        logo.setFixedSize(42, 42)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet(
            "background: #FB7299; color: white; border-radius: 12px; "
            "font-size: 20px; font-weight: 800;"
        )
        layout.addWidget(logo)

        text = QVBoxLayout()
        text.setSpacing(1)
        title = QLabel("Bili 文稿")
        title.setObjectName("appTitle")
        subtitle = QLabel("从 B站视频提取干净、可导出的完整文稿")
        subtitle.setObjectName("appSubtitle")
        text.addWidget(title)
        text.addWidget(subtitle)
        layout.addLayout(text)
        layout.addStretch(1)

        privacy = QLabel("独立运行 · 不复制 Cookie · ASR 本地或 API")
        privacy.setObjectName("privacyPill")
        privacy.setToolTip("登录状态保存在应用专用浏览器配置中；Python 进程不会读取或复制 Cookie")
        layout.addWidget(privacy)

        about = QPushButton("关于")
        about.setObjectName("ghostButton")
        about.clicked.connect(self._show_about)
        layout.addWidget(about)
        return layout

    def _build_input_card(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(14, 13, 14, 13)
        layout.setSpacing(9)
        self.url_input = SourceLineEdit()
        self.url_input.multiple_pasted.connect(self._open_batch_dialog)
        self.url_input.setPlaceholderText("粘贴 B站视频链接、b23.tv 短链接或 BV 号…")
        self.url_input.setClearButtonEnabled(True)
        self.url_input.returnPressed.connect(self._fetch_video)
        layout.addWidget(self.url_input, 1)
        paste = QPushButton("粘贴")
        paste.clicked.connect(self._paste_url)
        layout.addWidget(paste)
        self.fetch_button = QPushButton("读取视频")
        self.fetch_button.setObjectName("primaryButton")
        self.fetch_button.clicked.connect(self._fetch_video)
        layout.addWidget(self.fetch_button)
        self.batch_button = QPushButton("批量处理")
        self.batch_button.setToolTip("粘贴多条 B站链接，并行提取后分别导出 Markdown")
        self.batch_button.clicked.connect(self._open_batch_dialog)
        layout.addWidget(self.batch_button)
        return card

    def _build_workspace(self) -> QSplitter:
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_control_panel())
        splitter.addWidget(self._build_preview_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([390, 740])
        return splitter

    def _build_control_panel(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        card.setMinimumWidth(330)
        card.setMaximumWidth(460)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(17, 16, 17, 16)
        layout.setSpacing(13)

        self.video_title = QLabel("等待视频链接")
        self.video_title.setObjectName("videoTitle")
        self.video_title.setWordWrap(True)
        layout.addWidget(self.video_title)
        self.video_meta = QLabel("读取后可选择要提取的分P")
        self.video_meta.setObjectName("meta")
        self.video_meta.setWordWrap(True)
        layout.addWidget(self.video_meta)

        part_header = QHBoxLayout()
        part_title = QLabel("分P")
        part_title.setObjectName("sectionTitle")
        part_header.addWidget(part_title)
        part_header.addStretch(1)
        self.detect_button = QPushButton("检测方式")
        self.detect_button.setToolTip("对选中分P检测公开、匿名、登录浏览器和当前 ASR 后端")
        self.detect_button.clicked.connect(self._detect_availability)
        self.detect_button.setEnabled(False)
        part_header.addWidget(self.detect_button)
        self.select_all = QCheckBox("全选")
        self.select_all.setChecked(True)
        self.select_all.stateChanged.connect(self._toggle_all_parts)
        self.select_all.setEnabled(False)
        part_header.addWidget(self.select_all)
        layout.addLayout(part_header)

        self.part_scroll = QScrollArea()
        self.part_scroll.setWidgetResizable(True)
        self.part_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.part_scroll.viewport().setStyleSheet("background: transparent;")
        self.part_container = QWidget()
        self.part_container.setStyleSheet("background: transparent;")
        self.part_layout = QVBoxLayout(self.part_container)
        self.part_layout.setContentsMargins(0, 0, 0, 0)
        self.part_layout.setSpacing(7)
        self.parts_placeholder = QLabel("暂无分P")
        self.parts_placeholder.setObjectName("muted")
        self.parts_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.parts_placeholder.setMinimumHeight(88)
        self.part_layout.addWidget(self.parts_placeholder)
        self.part_layout.addStretch(1)
        self.part_scroll.setWidget(self.part_container)
        layout.addWidget(self.part_scroll, 1)

        options_title = QLabel("提取设置")
        options_title.setObjectName("sectionTitle")
        layout.addWidget(options_title)

        options = QFrame()
        options.setObjectName("softPanel")
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(11, 10, 11, 10)
        options_layout.setSpacing(8)

        route_row = QHBoxLayout()
        route_row.addWidget(QLabel("方式"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("智能提取", "auto")
        self.mode_combo.addItem("只用公开字幕", "public")
        self.mode_combo.addItem("只用匿名接口", "anonymous")
        self.mode_combo.addItem("只用登录浏览器", "browser")
        self.mode_combo.addItem("只用 ASR", "asr")
        self.mode_combo.setToolTip("智能提取会逐来源尝试两次，失败间隔 1 秒，再按固定顺序下降")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        route_row.addWidget(self.mode_combo, 1)
        options_layout.addLayout(route_row)

        backend_row = QHBoxLayout()
        backend_row.addWidget(QLabel("ASR"))
        self.backend_combo = QComboBox()
        self.backend_combo.addItem("自动检测", "auto")
        self.backend_combo.addItem("Faster-Whisper", "faster-whisper")
        self.backend_combo.addItem("FunASR / SenseVoice", "funasr")
        self.backend_combo.addItem("OpenAI Whisper", "openai-whisper")
        self.backend_combo.addItem("OpenAI 兼容 API（MiMo）", ASR_API_BACKEND)
        self.backend_combo.currentIndexChanged.connect(self._update_model_choices)
        backend_row.addWidget(self.backend_combo, 1)
        self.api_settings_button = QPushButton("API 设置…")
        self.api_settings_button.setToolTip("设置 API 地址、密钥并测试本地服务")
        self.api_settings_button.clicked.connect(self._open_api_settings)
        backend_row.addWidget(self.api_settings_button)
        options_layout.addLayout(backend_row)

        model_row = QHBoxLayout()
        model_row.addWidget(QLabel("模型"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        model_row.addWidget(self.model_combo, 1)
        options_layout.addLayout(model_row)

        login_row = QHBoxLayout()
        self.login_status_label = QLabel("登录：未检查")
        self.login_status_label.setObjectName("meta")
        login_row.addWidget(self.login_status_label, 1)
        self.check_login_button = QPushButton("检查登录")
        self.check_login_button.clicked.connect(self._check_login_status)
        login_row.addWidget(self.check_login_button)
        self.login_browser_button = QPushButton("登录浏览器")
        self.login_browser_button.setToolTip("首次使用请在打开的专用浏览器中登录 B站")
        self.login_browser_button.clicked.connect(self._open_login_browser)
        login_row.addWidget(self.login_browser_button)
        options_layout.addLayout(login_row)
        hint = QLabel("每个来源最多尝试 2 次，失败后等待 1 秒；API ASR 需要保持服务运行。")
        hint.setObjectName("meta")
        hint.setWordWrap(True)
        options_layout.addWidget(hint)
        layout.addWidget(options)

        self.extract_button = QPushButton("开始提取文稿")
        self.extract_button.setObjectName("primaryButton")
        self.extract_button.setMinimumHeight(42)
        self.extract_button.clicked.connect(self._start_extraction)
        layout.addWidget(self.extract_button)
        return card

    def _build_preview_panel(self) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(17, 15, 17, 16)
        layout.setSpacing(10)

        header = QHBoxLayout()
        preview_title = QLabel("文稿预览")
        preview_title.setObjectName("sectionTitle")
        header.addWidget(preview_title)
        self.result_metric = QLabel("尚未提取")
        self.result_metric.setObjectName("metric")
        header.addWidget(self.result_metric)
        header.addStretch(1)
        self.timestamps_check = QCheckBox("时间戳")
        self.timestamps_check.stateChanged.connect(self._update_preview)
        header.addWidget(self.timestamps_check)
        self.copy_button = QPushButton("复制")
        self.copy_button.clicked.connect(self._copy_transcript)
        header.addWidget(self.copy_button)
        self.save_button = QPushButton("导出…")
        self.save_button.setObjectName("primaryButton")
        self.save_button.clicked.connect(self._save_transcript)
        header.addWidget(self.save_button)
        layout.addLayout(header)

        self.editor = QPlainTextEdit()
        self.editor.setObjectName("transcriptEditor")
        self.editor.setReadOnly(True)
        self.editor.setPlaceholderText(
            "文稿会显示在这里。\n\n支持导出 Markdown、TXT、SRT 和 JSON；应用不会生成摘要、笔记或评论分析。"
        )
        layout.addWidget(self.editor, 1)
        return card

    def _build_status_bar(self) -> QHBoxLayout:
        layout = QHBoxLayout()
        layout.setSpacing(10)
        self.status_label = QLabel("就绪")
        self.status_label.setObjectName("meta")
        self.status_label.setMinimumWidth(220)
        layout.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress, 1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.clicked.connect(self._cancel_active_task)
        self.cancel_button.hide()
        layout.addWidget(self.cancel_button)
        return layout

    def _restore_settings(self) -> None:
        mode = str(self.settings.value("extract/mode", "auto"))
        backend = str(self.settings.value("extract/backend", "auto"))
        mode_index = self.mode_combo.findData(mode)
        backend_index = self.backend_combo.findData(backend)
        if mode_index >= 0:
            self.mode_combo.setCurrentIndex(mode_index)
        if backend_index >= 0:
            self.backend_combo.setCurrentIndex(backend_index)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于 Bili 文稿",
            f"<b>Bili 文稿 {__version__}</b><br><br>"
            "只做一件事：从 B站视频提取完整文稿。<br>"
            "固定顺序：公开字幕、匿名播放器接口、专用登录浏览器、ASR。<br>"
            "支持本地 ASR 或 OpenAI 兼容 API ASR，也支持批量识别链接。<br>"
            "每个来源最多两次，失败后间隔 1 秒再下降。<br><br>"
            "本应用是非官方工具，不隶属于哔哩哔哩。",
        )

    def _current_extraction_options(self) -> ExtractionOptions:
        model = self.model_combo.currentData()
        if model is None:
            model = self.model_combo.currentText().strip()
        base_url = self.settings.value("asr/api_base_url", DEFAULT_ASR_API_BASE_URL)
        api_key = self.settings.value("asr/api_key", DEFAULT_ASR_API_KEY)
        timeout = self.settings.value("asr/api_timeout", DEFAULT_ASR_API_TIMEOUT)
        try:
            api_timeout = float(timeout)
        except (TypeError, ValueError):
            api_timeout = DEFAULT_ASR_API_TIMEOUT
        return ExtractionOptions(
            mode=str(self.mode_combo.currentData()),
            browser_ai=True,
            asr_backend=str(self.backend_combo.currentData()),
            asr_model=str(model or ""),
            asr_api_base_url=str(base_url or DEFAULT_ASR_API_BASE_URL),
            asr_api_key=DEFAULT_ASR_API_KEY if api_key is None else str(api_key),
            asr_api_timeout=api_timeout,
        )

    def _open_api_settings(self) -> None:
        saved_base_url = self.settings.value("asr/api_base_url", DEFAULT_ASR_API_BASE_URL)
        saved_api_key = self.settings.value("asr/api_key", DEFAULT_ASR_API_KEY)
        dialog = AsrApiSettingsDialog(
            str(saved_base_url or DEFAULT_ASR_API_BASE_URL),
            DEFAULT_ASR_API_KEY if saved_api_key is None else str(saved_api_key),
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.settings.setValue("asr/api_base_url", dialog.base_url)
            self.settings.setValue("asr/api_key", dialog.api_key)
            self.status_label.setText(f"已保存 ASR API：{dialog.base_url}")

    def _open_batch_dialog(self, initial_text: str = "") -> None:
        if self.metadata_task or self.browser_task or self.browser_status_task or self.availability_task or self.extraction_task:
            return
        if self.batch_dialog and self.batch_dialog.isVisible():
            self.batch_dialog.raise_()
            self.batch_dialog.activateWindow()
            return
        if not initial_text:
            clipboard_text = QApplication.clipboard().text()
            if len(extract_bilibili_sources(clipboard_text)) > 1:
                initial_text = clipboard_text
        dialog = BatchDialog(self._current_extraction_options(), initial_text, self)
        self.batch_dialog = dialog
        try:
            dialog.exec()
        finally:
            self.batch_dialog = None

    def _open_login_browser(self) -> None:
        if self.batch_dialog or self.browser_task or self.browser_status_task or self.metadata_task or self.availability_task or self.extraction_task:
            return
        destination = self.video.url if self.video else None
        self.login_browser_button.setEnabled(False)
        self.login_browser_button.setText("正在打开…")
        self.status_label.setText("正在启动专用登录浏览器…")
        task = BrowserLaunchTask(destination, self)
        self.browser_task = task
        task.succeeded.connect(self._browser_opened)
        task.failed.connect(self._browser_open_failed)
        task.finished.connect(task.deleteLater)
        task.start()
        self._update_actions()

    def _browser_opened(self, browser_name: str) -> None:
        self.browser_task = None
        self.login_browser_button.setText("登录浏览器")
        self.login_browser_button.setEnabled(True)
        self._set_login_status("等待登录", "working")
        self.status_label.setText(f"已打开 {browser_name} 专用登录窗口")
        QMessageBox.information(
            self,
            "专用登录浏览器",
            f"已打开 {browser_name}。\n\n"
            "首次使用请在该窗口中登录 B站，并保持窗口打开。登录状态只保存在 Bili 文稿的专用浏览器配置中。\n\n"
            "完成登录后回到这里点击“检查登录”确认，再检测方式或开始智能提取。",
        )
        self._update_actions()

    def _browser_open_failed(self, message: str) -> None:
        self.browser_task = None
        self.login_browser_button.setText("登录浏览器")
        self.login_browser_button.setEnabled(True)
        self._set_login_status("启动失败", "error")
        self.status_label.setText("登录浏览器启动失败")
        self._show_error(message)
        self._update_actions()

    def _set_login_status(self, text: str, kind: str = "muted") -> None:
        colors = {
            "muted": "#969EAC",
            "working": "#F3BE62",
            "success": "#45D1A3",
            "error": "#FF6E72",
        }
        self.login_status_label.setText(f"登录：{text}")
        self.login_status_label.setStyleSheet(f"color: {colors.get(kind, colors['muted'])};")

    def _check_login_status(self) -> None:
        if self.batch_dialog or self.browser_status_task or self.browser_task or self.metadata_task or self.availability_task or self.extraction_task:
            return
        self.check_login_button.setEnabled(False)
        self._set_login_status("检查中…", "working")
        task = BrowserStatusTask(self)
        self.browser_status_task = task
        task.succeeded.connect(self._login_status_ready)
        task.failed.connect(self._login_status_failed)
        task.finished.connect(task.deleteLater)
        task.start()
        self._update_actions()

    def _login_status_ready(self, logged_in: bool, detail: str) -> None:
        self.browser_status_task = None
        self.check_login_button.setEnabled(True)
        if logged_in:
            self._set_login_status(detail or "已登录", "success")
            self.status_label.setText(f"B站已登录：{detail or '账号可用'}")
        else:
            if "未启动" in detail or "没有打开" in detail:
                self._set_login_status("浏览器未启动", "muted")
            else:
                self._set_login_status("未登录", "error")
            self.login_status_label.setToolTip(detail)
            self.status_label.setText(detail)
        self._update_actions()

    def _login_status_failed(self, message: str) -> None:
        self.browser_status_task = None
        self.check_login_button.setEnabled(True)
        self._set_login_status("检查失败", "error")
        self.login_status_label.setToolTip(message)
        self.status_label.setText(message)
        self._update_actions()

    def _paste_url(self) -> bool:
        text = QApplication.clipboard().text().strip()
        if text:
            if len(extract_bilibili_sources(text)) > 1:
                self._open_batch_dialog(text)
                return True
            self.url_input.setText(text)
            self.url_input.setFocus()
            return True
        return False

    def _fetch_video(self) -> None:
        source = self.url_input.text().strip()
        if not source:
            pasted = self._paste_url()
            source = self.url_input.text().strip()
            if pasted and not source:
                return
        if not source:
            self._show_error("请先粘贴 B站视频链接或 BV 号")
            return
        sources = extract_bilibili_sources(source)
        if len(sources) > 1:
            self._open_batch_dialog(source)
            return
        if len(sources) == 1:
            source = sources[0]
        if self.metadata_task or self.browser_task or self.browser_status_task or self.availability_task or self.extraction_task:
            return
        self._set_busy(True, "正在读取视频信息…", indeterminate=True)
        task = MetadataTask(source, self)
        self.metadata_task = task
        task.succeeded.connect(self._video_loaded)
        task.failed.connect(self._metadata_failed)
        task.finished.connect(task.deleteLater)
        task.start()

    def _video_loaded(self, video: VideoInfo) -> None:
        self.metadata_task = None
        self.video = video
        self.bundle = None
        self.availability_report = None
        self.editor.clear()
        self.video_title.setText(video.title)
        self.video_meta.setText(
            f"{video.owner}  ·  {format_duration(video.duration)}  ·  {len(video.parts)} 个分P  ·  {video.bvid}"
        )
        self._populate_parts(video.parts)
        self._set_busy(False, f"已读取 {video.bvid}")
        self._update_actions()

    def _metadata_failed(self, message: str) -> None:
        self.metadata_task = None
        self._set_busy(False, "读取失败")
        self._show_error(message)

    def _populate_parts(self, parts: tuple[VideoPart, ...]) -> None:
        while self.part_layout.count():
            item = self.part_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        self.part_rows.clear()
        for part in parts:
            row = PartRow(part)
            row.selection_changed.connect(self._sync_select_all)
            self.part_rows[part.page] = row
            self.part_layout.addWidget(row)
        self.part_layout.addStretch(1)
        self.select_all.blockSignals(True)
        self.select_all.setChecked(True)
        self.select_all.blockSignals(False)
        self.select_all.setEnabled(bool(parts))

    def _toggle_all_parts(self, state: int) -> None:
        selected = state == Qt.CheckState.Checked.value
        for row in self.part_rows.values():
            row.checkbox.blockSignals(True)
            row.set_selected(selected)
            row.checkbox.blockSignals(False)
        self._update_actions()

    def _sync_select_all(self) -> None:
        rows = list(self.part_rows.values())
        all_selected = bool(rows) and all(row.selected for row in rows)
        self.select_all.blockSignals(True)
        self.select_all.setChecked(all_selected)
        self.select_all.blockSignals(False)
        self._update_actions()

    def _selected_parts(self) -> list[VideoPart]:
        return [row.part for row in self.part_rows.values() if row.selected]

    def _detect_availability(self) -> None:
        if not self.video or self.metadata_task or self.browser_task or self.browser_status_task or self.availability_task or self.extraction_task:
            return
        parts = self._selected_parts()
        if not parts:
            self._show_error("请至少选择一个分P")
            return
        for row in self.part_rows.values():
            if row.selected:
                row.set_status("等待检测", "muted")
        self._set_busy(True, "准备检测可用方式…", cancellable=True)
        self.progress.setRange(0, 100)
        task = AvailabilityTask(self.video, parts, self, options=self._current_extraction_options())
        self.availability_task = task
        task.progress_changed.connect(self._availability_progress)
        task.log_message.connect(self._log_message)
        task.succeeded.connect(self._availability_succeeded)
        task.failed.connect(self._availability_failed)
        task.cancelled.connect(self._availability_cancelled)
        task.finished.connect(task.deleteLater)
        task.start()

    def _availability_progress(self, value: int, message: str) -> None:
        self.progress.setValue(value)
        self.status_label.setText(message)
        if message.startswith("P"):
            try:
                page = int(message.split("·", 1)[0].strip()[1:])
                row = self.part_rows.get(page)
                if row:
                    row.set_status("检测中", "working")
            except (ValueError, IndexError):
                pass

    def _availability_succeeded(self, report: AvailabilityReport) -> None:
        self.availability_task = None
        self.availability_report = report
        browser_available = False
        browser_details: list[str] = []
        for result in report.parts:
            row = self.part_rows.get(result.part.page)
            if row:
                row.set_availability(result)
            browser = result.get("browser")
            if browser:
                browser_available = browser_available or browser.available
                browser_details.append(browser.detail)
        if browser_available:
            self._set_login_status("已登录，字幕接口可用", "success")
        elif any("账号已登录" in detail for detail in browser_details):
            self._set_login_status("已登录，该视频无 AI 字幕", "success")
        elif any("未登录" in detail for detail in browser_details):
            self._set_login_status("未登录", "error")
        elif any("未启动" in detail or "没有打开" in detail for detail in browser_details):
            self._set_login_status("浏览器未启动", "muted")
        self._set_busy(False, f"检测完成：{len(report.parts)} 个分P")
        self.progress.setValue(100)
        self._update_actions()

    def _availability_failed(self, message: str) -> None:
        self.availability_task = None
        self._set_busy(False, "方式检测失败")
        self._update_actions()
        self._show_error(message)

    def _availability_cancelled(self) -> None:
        self.availability_task = None
        self._set_busy(False, "方式检测已取消")
        self._update_actions()

    def _mode_changed(self) -> None:
        mode = str(self.mode_combo.currentData())
        asr_enabled = mode in {"auto", "asr"}
        self.backend_combo.setEnabled(asr_enabled)
        self.model_combo.setEnabled(asr_enabled)
        self.api_settings_button.setEnabled(asr_enabled and self.backend_combo.currentData() == ASR_API_BACKEND)

    def _update_model_choices(self) -> None:
        backend = self.backend_combo.currentData() if hasattr(self, "backend_combo") else "auto"
        models = {
            "auto": [("按引擎默认", "")],
            "faster-whisper": [
                ("small（推荐）", "small"),
                ("base（更快）", "base"),
                ("medium（更准）", "medium"),
                ("large-v3-turbo", "large-v3-turbo"),
            ],
            "funasr": [("SenseVoiceSmall", "iic/SenseVoiceSmall")],
            "openai-whisper": [
                ("base", "base"),
                ("small", "small"),
                ("medium", "medium"),
                ("large", "large"),
            ],
            ASR_API_BACKEND: [("mimo-asr（CrisperWeaver）", DEFAULT_ASR_API_MODEL)],
        }
        if not hasattr(self, "model_combo"):
            return
        current = self.model_combo.currentData() or self.model_combo.currentText()
        self.model_combo.blockSignals(True)
        self.model_combo.clear()
        for label, value in models.get(str(backend), models["auto"]):
            self.model_combo.addItem(label, value)
        found = self.model_combo.findData(current)
        if found >= 0:
            self.model_combo.setCurrentIndex(found)
        self.model_combo.blockSignals(False)
        self._mode_changed()

    def _start_extraction(self) -> None:
        if (
            not self.video
            or self.metadata_task
            or self.browser_task
            or self.browser_status_task
            or self.availability_task
            or self.extraction_task
        ):
            return
        parts = self._selected_parts()
        if not parts:
            self._show_error("请至少选择一个分P")
            return
        options = self._current_extraction_options()
        self.settings.setValue("extract/mode", options.mode)
        self.settings.setValue("extract/backend", options.asr_backend)
        self.settings.setValue("asr/api_base_url", options.asr_api_base_url)
        self.settings.setValue("asr/api_key", options.asr_api_key)
        self.bundle = None
        self.editor.clear()
        for row in self.part_rows.values():
            row.set_status("排队中" if row.selected else "未选择", "muted")
        self._set_busy(True, "准备提取…", cancellable=True)
        self.progress.setRange(0, 100)
        task = ExtractionTask(self.video, parts, options, self)
        self.extraction_task = task
        task.progress_changed.connect(self._extraction_progress)
        task.log_message.connect(self._log_message)
        task.succeeded.connect(self._extraction_succeeded)
        task.failed.connect(self._extraction_failed)
        task.cancelled.connect(self._extraction_cancelled)
        task.finished.connect(task.deleteLater)
        task.start()

    def _extraction_progress(self, value: int, message: str) -> None:
        self.progress.setValue(value)
        self.status_label.setText(message)
        if message.startswith("P"):
            try:
                page = int(message.split("·", 1)[0].strip()[1:])
                row = self.part_rows.get(page)
                if row:
                    row.set_status("处理中", "working")
            except (ValueError, IndexError):
                pass

    def _log_message(self, message: str) -> None:
        if message:
            self.status_label.setText(message)
            self.status_label.setToolTip(message)

    def _extraction_succeeded(self, bundle: TranscriptBundle) -> None:
        self.extraction_task = None
        self.bundle = bundle
        successes = {part.part.page: part for part in bundle.parts}
        failures = {issue.page: issue for issue in bundle.issues}
        for page, row in self.part_rows.items():
            if page in successes:
                row.set_status(successes[page].source, "success")
            elif page in failures:
                row.set_status("失败", "error")
                row.setToolTip(failures[page].message)
        self._update_preview()
        self._set_busy(False, f"完成：{len(bundle.parts)} 个分P，{bundle.character_count} 字")
        self._update_actions()
        if bundle.issues:
            details = "\n".join(f"P{item.page} · {item.title}：{item.message}" for item in bundle.issues)
            QMessageBox.warning(self, "部分分P未提取", f"已保留成功结果。\n\n{details}")

    def _extraction_failed(self, message: str) -> None:
        self.extraction_task = None
        self._set_busy(False, "提取失败")
        self._update_actions()
        self._show_error(message)

    def _extraction_cancelled(self) -> None:
        self.extraction_task = None
        self._set_busy(False, "已取消")
        self._update_actions()

    def _cancel_active_task(self) -> None:
        self.status_label.setText("正在取消…")
        self.cancel_button.setEnabled(False)
        if self.availability_task:
            self.availability_task.cancel()
        elif self.extraction_task:
            self.extraction_task.cancel()

    def _set_busy(
        self,
        busy: bool,
        message: str,
        *,
        cancellable: bool = False,
        indeterminate: bool = False,
    ) -> None:
        self.status_label.setText(message)
        self.url_input.setEnabled(not busy)
        self.fetch_button.setEnabled(not busy)
        self.batch_button.setEnabled(not busy)
        self.extract_button.setEnabled(not busy and bool(self.video) and bool(self._selected_parts()))
        self.select_all.setEnabled(not busy and bool(self.part_rows))
        self.detect_button.setEnabled(not busy and bool(self.video) and bool(self._selected_parts()))
        for row in self.part_rows.values():
            row.checkbox.setEnabled(not busy)
        self.mode_combo.setEnabled(not busy)
        mode = str(self.mode_combo.currentData())
        self.backend_combo.setEnabled(not busy and mode in {"auto", "asr"})
        self.model_combo.setEnabled(not busy and mode in {"auto", "asr"})
        self.api_settings_button.setEnabled(
            not busy and mode in {"auto", "asr"} and self.backend_combo.currentData() == ASR_API_BACKEND
        )
        self.login_browser_button.setEnabled(not busy and not self.browser_task)
        self.check_login_button.setEnabled(not busy and not self.browser_status_task)
        self.cancel_button.setVisible(busy and cancellable)
        self.cancel_button.setEnabled(True)
        if busy and indeterminate:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            if not busy:
                self.progress.setValue(100 if self.bundle else 0)

    def _update_actions(self) -> None:
        busy = bool(
            self.metadata_task
            or self.browser_task
            or self.browser_status_task
            or self.availability_task
            or self.extraction_task
        )
        has_selection = bool(self._selected_parts())
        self.fetch_button.setEnabled(not busy)
        self.batch_button.setEnabled(not busy)
        self.extract_button.setEnabled(not busy and bool(self.video) and has_selection)
        self.detect_button.setEnabled(not busy and bool(self.video) and has_selection)
        self.login_browser_button.setEnabled(not busy)
        self.check_login_button.setEnabled(not busy)
        self.api_settings_button.setEnabled(
            not busy
            and str(self.mode_combo.currentData()) in {"auto", "asr"}
            and self.backend_combo.currentData() == ASR_API_BACKEND
        )
        self.copy_button.setEnabled(bool(self.bundle))
        self.save_button.setEnabled(bool(self.bundle))
        self.timestamps_check.setEnabled(bool(self.bundle))

    def _update_preview(self) -> None:
        if not self.bundle:
            return
        timestamps = self.timestamps_check.isChecked()
        self.editor.setPlainText(self.bundle.to_markdown(timestamps=timestamps))
        sources = " / ".join(dict.fromkeys(part.source for part in self.bundle.parts))
        self.result_metric.setText(
            f"{len(self.bundle.parts)} 个分P · {self.bundle.character_count} 字 · {sources}"
        )
        cursor = self.editor.textCursor()
        cursor.movePosition(cursor.MoveOperation.Start)
        self.editor.setTextCursor(cursor)

    def _copy_transcript(self) -> None:
        if not self.bundle:
            return
        QApplication.clipboard().setText(self.editor.toPlainText())
        self.status_label.setText("文稿已复制到剪贴板")

    def _save_transcript(self) -> None:
        if not self.bundle:
            return
        documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
        last_dir = str(self.settings.value("export/last_dir", documents))
        default_name = safe_filename(self.bundle.video.title) + ".md"
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "导出文稿",
            str(Path(last_dir) / default_name),
            "Markdown 文稿 (*.md);;纯文本 (*.txt);;SRT 字幕 (*.srt);;JSON 数据 (*.json)",
        )
        if not path:
            return
        output = Path(path)
        filter_suffix = {
            "Markdown 文稿 (*.md)": ".md",
            "纯文本 (*.txt)": ".txt",
            "SRT 字幕 (*.srt)": ".srt",
            "JSON 数据 (*.json)": ".json",
        }
        if not output.suffix:
            output = output.with_suffix(filter_suffix.get(selected_filter, ".md"))
        suffix = output.suffix.lower()
        if suffix == ".txt":
            content = self.bundle.to_text(timestamps=self.timestamps_check.isChecked())
        elif suffix == ".srt":
            content = self.bundle.to_srt()
        elif suffix == ".json":
            content = self.bundle.to_json()
        else:
            content = self.bundle.to_markdown(timestamps=self.timestamps_check.isChecked())
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(content, encoding="utf-8-sig" if suffix in {".txt", ".srt"} else "utf-8")
        except OSError as exc:
            self._show_error(f"导出失败：{exc}")
            return
        self.settings.setValue("export/last_dir", str(output.parent))
        self.status_label.setText(f"已导出：{output.name}")

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "Bili 文稿", message)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        mime = event.mimeData()
        text = mime.text() if mime else ""
        if text and ("bilibili.com" in text or "b23.tv" in text or "BV" in text):
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        text = event.mimeData().text().strip()
        if text:
            if len(extract_bilibili_sources(text)) > 1:
                self._open_batch_dialog(text)
            else:
                self.url_input.setText(text.splitlines()[0])
            event.acceptProposedAction()

    def closeEvent(self, event: QCloseEvent) -> None:
        active_extraction = self.extraction_task and self.extraction_task.isRunning()
        active_availability = self.availability_task and self.availability_task.isRunning()
        active_metadata = self.metadata_task and self.metadata_task.isRunning()
        active_browser = self.browser_task and self.browser_task.isRunning()
        active_browser_status = self.browser_status_task and self.browser_status_task.isRunning()
        if active_extraction or active_availability or active_metadata or active_browser or active_browser_status:
            if not self._close_requested:
                if active_extraction:
                    detail = "文稿仍在提取"
                elif active_availability:
                    detail = "正在检测可用方式"
                elif active_browser:
                    detail = "正在启动登录浏览器"
                elif active_browser_status:
                    detail = "正在检查登录状态"
                else:
                    detail = "正在读取视频信息"
                reply = QMessageBox.question(
                    self,
                    "退出 Bili 文稿",
                    f"{detail}。要结束后自动退出吗？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    event.ignore()
                    return
                self._close_requested = True
                if active_extraction and self.extraction_task:
                    self.extraction_task.cancel()
                    self.extraction_task.finished.connect(self.close)
                elif active_availability and self.availability_task:
                    self.availability_task.cancel()
                    self.availability_task.finished.connect(self.close)
                elif self.metadata_task:
                    self.metadata_task.finished.connect(self.close)
                elif self.browser_task:
                    self.browser_task.finished.connect(self.close)
                elif self.browser_status_task:
                    self.browser_status_task.finished.connect(self.close)
                self.hide()
            event.ignore()
            return
        event.accept()
