from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from .asr_api import AsrApiSettings, OpenAICompatibleAsrRuntime
from .bilibili import BilibiliClient, CancelledError
from .browser_bridge import StandaloneBrowserBridge
from .extractor import ExtractionOptions, TranscriptExtractor
from .models import VideoInfo, VideoPart

if TYPE_CHECKING:
    from .api_server import ExtractionApiServer


class MetadataTask(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, source: str, parent=None) -> None:
        super().__init__(parent)
        self.source = source

    def run(self) -> None:
        try:
            self.succeeded.emit(BilibiliClient().fetch_video(self.source))
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class BrowserLaunchTask(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, destination: str | None, parent=None) -> None:
        super().__init__(parent)
        self.destination = destination

    def run(self) -> None:
        try:
            browser_name = StandaloneBrowserBridge().open_login_browser(self.destination)
            self.succeeded.emit(browser_name)
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class BrowserStatusTask(QThread):
    succeeded = Signal(bool, str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            logged_in, detail = StandaloneBrowserBridge().login_status()
            self.succeeded.emit(logged_in, detail)
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class AsrHealthTask(QThread):
    """Run the optional upstream health check without freezing the Qt UI."""

    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, settings: AsrApiSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings

    def run(self) -> None:
        try:
            self.succeeded.emit(OpenAICompatibleAsrRuntime().health(self.settings))
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class AvailabilityTask(QThread):
    progress_changed = Signal(int, str)
    log_message = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, video: VideoInfo, parts: list[VideoPart], parent=None, *, options: ExtractionOptions | None = None) -> None:
        super().__init__(parent)
        self.video = video
        self.parts = parts
        self.options = options
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            result = TranscriptExtractor().probe_availability(
                self.video,
                self.parts,
                options=self.options,
                cancelled=self._cancel_event.is_set,
                progress=lambda value, message: self.progress_changed.emit(value, message),
                log=lambda message: self.log_message.emit(message),
            )
            if self._cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)
        except CancelledError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class ExtractionTask(QThread):
    progress_changed = Signal(int, str)
    log_message = Signal(str)
    succeeded = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        video: VideoInfo,
        parts: list[VideoPart],
        options: ExtractionOptions,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.video = video
        self.parts = parts
        self.options = options
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        try:
            result = TranscriptExtractor().extract(
                self.video,
                self.parts,
                self.options,
                cancelled=self._cancel_event.is_set,
                progress=lambda value, message: self.progress_changed.emit(value, message),
                log=lambda message: self.log_message.emit(message),
            )
            if self._cancel_event.is_set():
                self.cancelled.emit()
            else:
                self.succeeded.emit(result)
        except CancelledError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


class ApiServerStopTask(QThread):
    failed = Signal(str)

    def __init__(self, server: "ExtractionApiServer", parent=None) -> None:
        super().__init__(parent)
        self.server = server

    def run(self) -> None:
        try:
            self.server.stop(cancel_jobs=True, wait_for_jobs=True)
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)
