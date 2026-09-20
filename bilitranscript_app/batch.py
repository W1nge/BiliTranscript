from __future__ import annotations

import threading
from concurrent.futures import CancelledError as FutureCancelledError
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .bilibili import BilibiliClient, CancelledError
from .extractor import ExtractionError, ExtractionOptions, TranscriptExtractor
from .models import TranscriptBundle, VideoInfo, safe_filename
from .sources import extract_bilibili_sources


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
    bundle: TranscriptBundle | None = None

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
    cancelled = Signal(object)

    def __init__(
        self,
        sources: tuple[str, ...],
        options: ExtractionOptions,
        output_dir: Path,
        max_workers: int = 3,
        parent: QObject | None = None,
        *,
        timestamps: bool = False,
    ) -> None:
        super().__init__(parent)
        self.sources = sources
        self.options = options
        self.output_dir = output_dir
        self.max_workers = max(1, min(4, int(max_workers)))
        self.timestamps = bool(timestamps)
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
        output_path.write_text(bundle.to_markdown(timestamps=self.timestamps), encoding="utf-8")
        return BatchItemResult(index=index, source=source, title=video.title, output_path=output_path, bundle=bundle)

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
                complete_results = tuple(item for item in results if item is not None)
                self.cancelled.emit(BatchResult(complete_results))
                return
            complete_results = tuple(item for item in results if item is not None)
            self.succeeded.emit(BatchResult(complete_results))
        except Exception as exc:
            self.failed.emit(str(exc) or type(exc).__name__)


__all__ = [
    "BatchExtractionTask",
    "BatchItemResult",
    "BatchResult",
    "batch_output_filename",
    "extract_bilibili_sources",
]
