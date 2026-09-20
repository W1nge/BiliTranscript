from __future__ import annotations

"""Thread-safe application settings shared by the desktop UI and API.

The old UI read values directly from widgets.  This module is deliberately small
and boring: QSettings remains the persistence layer, while a lock makes snapshots
safe to take from worker/API threads.  A snapshot is immutable and therefore can
be attached to a task without later UI changes leaking into it.
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSettings, QStandardPaths

from .asr_api import (
    ASR_API_BACKEND,
    DEFAULT_ASR_API_BASE_URL,
    DEFAULT_ASR_API_KEY,
    DEFAULT_ASR_API_MODEL,
    DEFAULT_ASR_API_TIMEOUT,
)
from .extractor import ExtractionOptions
from .security import generate_api_key


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "y", "是"}
    return bool(value)


def _int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def _float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(maximum, number))


def default_export_directory() -> Path:
    documents = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)
    return Path(documents or (Path.home() / "Documents")) / "Bili文稿"


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """Immutable settings captured at submission time."""

    extraction: ExtractionOptions
    theme: str = "system"
    timestamps: bool = False
    batch_concurrency: int = 3
    default_export_dir: str = ""

    @property
    def options(self) -> ExtractionOptions:
        return self.extraction

    @property
    def extraction_options(self) -> ExtractionOptions:
        return self.extraction

    def public_dict(self) -> dict[str, Any]:
        """Return a non-sensitive representation suitable for history/UI."""
        options = self.extraction
        return {
            "mode": options.mode,
            "browser_ai": options.browser_ai,
            "asr_backend": options.asr_backend,
            "asr_model": options.asr_model,
            "language": options.language,
            "theme": self.theme,
            "timestamps": self.timestamps,
            "batch_concurrency": self.batch_concurrency,
        }


class AppSettings:
    """Persistent, thread-safe settings facade.

    ``qsettings`` is optional so tests can inject an in-memory-like QSettings
    object.  Existing v0.7 keys are read to make upgrades seamless.
    """

    VALID_THEMES = {"system", "light", "dark"}
    VALID_MODES = {"auto", "public", "anonymous", "browser", "asr"}
    VALID_BACKENDS = {"auto", "faster-whisper", "funasr", "openai-whisper", ASR_API_BACKEND}

    def __init__(self, qsettings: QSettings | None = None) -> None:
        self.qsettings = qsettings or QSettings()
        self._lock = threading.RLock()

    def value(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.qsettings.value(key, default)

    def set_value(self, key: str, value: Any) -> None:
        with self._lock:
            self.qsettings.setValue(key, value)
            # Sync is intentionally best-effort; QSettings already queues writes.
            try:
                self.qsettings.sync()
            except Exception:
                pass

    @property
    def theme(self) -> str:
        value = str(self.value("ui/theme", "system") or "system").lower()
        return value if value in self.VALID_THEMES else "system"

    def set_theme(self, theme: str) -> str:
        normalized = str(theme or "system").lower()
        if normalized not in self.VALID_THEMES:
            raise ValueError("主题必须是 system、light 或 dark")
        self.set_value("ui/theme", normalized)
        return normalized

    @property
    def timestamps(self) -> bool:
        return _bool(self.value("extract/timestamps", False))

    def set_timestamps(self, enabled: bool) -> None:
        self.set_value("extract/timestamps", bool(enabled))

    @property
    def batch_concurrency(self) -> int:
        return _int(self.value("batch/concurrency", 3), 3, 1, 4)

    def set_batch_concurrency(self, value: int) -> int:
        number = _int(value, 3, 1, 4)
        self.set_value("batch/concurrency", number)
        return number

    @property
    def default_export_dir(self) -> Path:
        raw = str(self.value("export/default_dir", "") or "").strip()
        return Path(raw) if raw else default_export_directory()

    def set_default_export_dir(self, path: str | Path) -> Path:
        value = Path(path).expanduser()
        self.set_value("export/default_dir", str(value))
        # Keep the legacy key in sync for existing export code.
        self.set_value("export/last_dir", str(value))
        return value

    @property
    def startup_enabled(self) -> bool:
        return _bool(self.value("startup/enabled", False))

    def set_startup_enabled(self, enabled: bool) -> None:
        self.set_value("startup/enabled", bool(enabled))

    @property
    def api_auto_start(self) -> bool:
        return _bool(self.value("extraction_api/auto_start", True), True)

    def set_api_auto_start(self, enabled: bool) -> None:
        self.set_value("extraction_api/auto_start", bool(enabled))

    @property
    def api_port(self) -> int:
        return _int(self.value("extraction_api/port", 8766), 8766, 1024, 65535)

    def set_api_port(self, port: int) -> int:
        number = _int(port, 8766, 1024, 65535)
        self.set_value("extraction_api/port", number)
        return number

    @property
    def api_key(self) -> str:
        key = str(self.value("extraction_api/key", "") or "").strip()
        if len(key) < 16 or any(char.isspace() for char in key):
            key = generate_api_key()
            self.set_value("extraction_api/key", key)
        return key

    def regenerate_api_key(self) -> str:
        key = generate_api_key()
        self.set_value("extraction_api/key", key)
        return key

    def set_api_key(self, key: str) -> str:
        value = str(key or "").strip()
        if len(value) < 16 or any(char.isspace() for char in value):
            raise ValueError("API Key 至少需要 16 个不含空格的字符")
        self.set_value("extraction_api/key", value)
        return value

    @property
    def asr_base_url(self) -> str:
        return str(self.value("asr/api_base_url", DEFAULT_ASR_API_BASE_URL) or DEFAULT_ASR_API_BASE_URL)

    @property
    def asr_api_key(self) -> str:
        value = self.value("asr/api_key", DEFAULT_ASR_API_KEY)
        return DEFAULT_ASR_API_KEY if value is None else str(value)

    @property
    def asr_timeout(self) -> float:
        return _float(self.value("asr/api_timeout", DEFAULT_ASR_API_TIMEOUT), DEFAULT_ASR_API_TIMEOUT, 1.0, 3600.0)

    def set_asr(self, *, base_url: str | None = None, api_key: str | None = None, timeout: float | None = None) -> None:
        if base_url is not None:
            self.set_value("asr/api_base_url", str(base_url).strip() or DEFAULT_ASR_API_BASE_URL)
        if api_key is not None:
            self.set_value("asr/api_key", str(api_key))
        if timeout is not None:
            self.set_value("asr/api_timeout", _float(timeout, DEFAULT_ASR_API_TIMEOUT, 1.0, 3600.0))

    def _model_for_backend(self, backend: str) -> str:
        value = self.value(f"extract/model/{backend}", None)
        if value is not None:
            return str(value)
        if backend == ASR_API_BACKEND:
            return DEFAULT_ASR_API_MODEL
        return ""

    def extraction_options(self) -> ExtractionOptions:
        with self._lock:
            mode = str(self.value("extract/mode", "auto") or "auto").lower()
            backend = str(self.value("extract/backend", "auto") or "auto").lower()
            if mode not in self.VALID_MODES:
                mode = "auto"
            if backend not in self.VALID_BACKENDS:
                backend = "auto"
            model = self._model_for_backend(backend)
            return ExtractionOptions(
                mode=mode,
                browser_ai=True,
                asr_backend=backend,
                asr_model=model,
                language=str(self.value("extract/language", "zh") or "zh"),
                asr_api_base_url=self.asr_base_url,
                asr_api_key=self.asr_api_key,
                asr_api_timeout=self.asr_timeout,
            )

    def set_extraction(self, *, mode: str | None = None, backend: str | None = None, model: str | None = None) -> None:
        if mode is not None:
            value = str(mode).lower()
            if value not in self.VALID_MODES:
                raise ValueError(f"不支持的提取方式：{mode}")
            self.set_value("extract/mode", value)
        if backend is not None:
            value = str(backend).lower()
            if value not in self.VALID_BACKENDS:
                raise ValueError(f"不支持的 ASR 后端：{backend}")
            self.set_value("extract/backend", value)
            if model is not None:
                self.set_value(f"extract/model/{value}", str(model))
        elif model is not None:
            backend_value = str(self.value("extract/backend", "auto") or "auto")
            self.set_value(f"extract/model/{backend_value}", str(model))

    def snapshot(self) -> SettingsSnapshot:
        with self._lock:
            return SettingsSnapshot(
                extraction=self.extraction_options(),
                theme=self.theme,
                timestamps=self.timestamps,
                batch_concurrency=self.batch_concurrency,
                default_export_dir=str(self.default_export_dir),
            )

    def public_snapshot(self) -> dict[str, Any]:
        return self.snapshot().public_dict()
