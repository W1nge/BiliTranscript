from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .models import Segment, combine_segments


class AsrError(RuntimeError):
    pass


class AsrCancelled(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AsrAvailability:
    python_executable: str | None
    backends: tuple[str, ...]

    @property
    def available(self) -> bool:
        return bool(self.python_executable and self.backends)


@dataclass(frozen=True, slots=True)
class AsrResult:
    backend: str
    model: str
    language: str
    segments: tuple[Segment, ...]


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / relative


class ExternalAsrRuntime:
    def __init__(self, python_executable: str | None = None) -> None:
        self.preferred_python = python_executable
        self._availability: AsrAvailability | None = None

    @property
    def worker_path(self) -> Path:
        packaged = resource_path("bilitranscript_app/asr_worker.py")
        if packaged.exists():
            return packaged
        return Path(__file__).with_name("asr_worker.py")

    def _python_candidates(self) -> list[str]:
        candidates: list[str] = []
        if self.preferred_python:
            candidates.append(self.preferred_python)
        if not getattr(sys, "frozen", False):
            candidates.append(sys.executable)
        for name in ("python", "python3"):
            found = shutil.which(name)
            if found:
                candidates.append(found)
        unique: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            key = os.path.normcase(os.path.abspath(item))
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    def detect(self, refresh: bool = False) -> AsrAvailability:
        if self._availability is not None and not refresh:
            return self._availability
        best = AsrAvailability(None, ())
        for executable in self._python_candidates():
            try:
                completed = subprocess.run(
                    [executable, str(self.worker_path), "--probe"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=12,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                if completed.returncode != 0:
                    continue
                payload = json.loads(completed.stdout.strip().splitlines()[-1])
                backends = tuple(str(item) for item in payload.get("backends") or [])
                if backends:
                    best = AsrAvailability(executable, backends)
                    break
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError, IndexError):
                continue
        self._availability = best
        return best

    def transcribe(
        self,
        audio_path: Path,
        output_path: Path,
        *,
        backend: str,
        model: str,
        language: str,
        duration: float,
        cancelled: Callable[[], bool],
        progress: Callable[[int, str], None],
        log: Callable[[str], None],
    ) -> AsrResult:
        availability = self.detect()
        if not availability.available or not availability.python_executable:
            raise AsrError("未找到本地 ASR 环境。请安装 faster-whisper 或 funasr 后重试。")
        resolved_backend = backend
        if resolved_backend == "auto":
            resolved_backend = next(
                (item for item in ("faster-whisper", "funasr", "openai-whisper") if item in availability.backends),
                "",
            )
        if resolved_backend not in availability.backends:
            labels = "、".join(availability.backends)
            raise AsrError(f"当前 Python 没有 {resolved_backend}；已检测到：{labels or '无'}")

        command = [
            availability.python_executable,
            str(self.worker_path),
            "--input",
            str(audio_path),
            "--output",
            str(output_path),
            "--backend",
            resolved_backend,
            "--language",
            language,
            "--duration",
            str(duration),
        ]
        if model:
            command.extend(["--model", model])
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lines: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line)
            lines.put(None)

        reader = threading.Thread(target=read_output, name="asr-output-reader", daemon=True)
        reader.start()
        last_error = ""
        stream_closed = False
        try:
            while process.poll() is None or not stream_closed or not lines.empty():
                if cancelled():
                    process.terminate()
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.kill()
                    raise AsrCancelled("操作已取消")
                try:
                    line = lines.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is None:
                    stream_closed = True
                    continue
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    event = json.loads(stripped)
                except json.JSONDecodeError:
                    log(stripped)
                    continue
                kind = event.get("event")
                message = str(event.get("message") or "")
                if kind == "progress":
                    progress(int(event.get("value") or 0), message)
                elif kind == "status":
                    progress(2, message)
                elif kind == "error":
                    last_error = message
                    log(message)
                elif message:
                    log(message)
            return_code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
        if return_code != 0:
            raise AsrError(last_error or f"本地 ASR 进程退出，代码 {return_code}")
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AsrError("ASR 已结束，但结果文件无法读取") from exc
        segments = combine_segments(payload.get("segments") or [])
        if not segments and str(payload.get("text") or "").strip():
            segments = (Segment(0, max(1.0, duration), str(payload["text"]).strip()),)
        if not segments:
            raise AsrError("ASR 没有识别出可用文字")
        return AsrResult(
            backend=str(payload.get("backend") or resolved_backend),
            model=str(payload.get("model") or model),
            language=str(payload.get("language") or language),
            segments=segments,
        )

