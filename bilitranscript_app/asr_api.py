from __future__ import annotations

import http.client
import json
import mimetypes
import socket
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from queue import Queue
from typing import Callable
from urllib.parse import urlsplit, urlunsplit

from .asr import AsrCancelled, AsrError, AsrResult
from .models import Segment, combine_segments


ASR_API_BACKEND = "openai-compatible"
DEFAULT_ASR_API_BASE_URL = "http://127.0.0.1:8765/v1"
DEFAULT_ASR_API_KEY = "local"
DEFAULT_ASR_API_MODEL = "mimo-asr"
DEFAULT_ASR_API_TIMEOUT = 3600.0


@dataclass(frozen=True, slots=True)
class AsrApiSettings:
    base_url: str = DEFAULT_ASR_API_BASE_URL
    api_key: str = DEFAULT_ASR_API_KEY
    timeout_seconds: float = DEFAULT_ASR_API_TIMEOUT


_API_REQUEST_LOCK = threading.Lock()


class OpenAICompatibleAsrRuntime:
    """Client for local or remote OpenAI-compatible audio transcription servers."""

    @staticmethod
    def normalize_base_url(value: str) -> str:
        raw = (value or DEFAULT_ASR_API_BASE_URL).strip()
        if "://" not in raw:
            raw = "http://" + raw
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
            raise AsrError("ASR API 地址无效，请填写 http:// 或 https:// 地址")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise AsrError("ASR API 地址中的端口无效") from exc
        if parsed.username or parsed.password:
            raise AsrError("请在 API Key 字段填写凭据，不要把用户名密码写进 URL")
        if parsed.query or parsed.fragment:
            raise AsrError("ASR API 地址不能包含查询参数或片段")
        path = parsed.path.rstrip("/")
        if not path:
            path = "/v1"
        elif not path.lower().endswith("/v1"):
            path += "/v1"
        return urlunsplit((parsed.scheme.lower(), parsed.netloc, path, "", ""))

    @classmethod
    def _endpoint(cls, base_url: str, suffix: str, *, root: bool = False) -> str:
        normalized = urlsplit(cls.normalize_base_url(base_url))
        path = normalized.path.rstrip("/")
        if root and path.lower().endswith("/v1"):
            path = path[:-3].rstrip("/")
        if not path:
            path = ""
        path += "/" + suffix.lstrip("/")
        return urlunsplit((normalized.scheme, normalized.netloc, path, "", ""))

    @staticmethod
    def _connection(parsed, timeout: float | None) -> http.client.HTTPConnection:
        connection_type = http.client.HTTPSConnection if parsed.scheme.lower() == "https" else http.client.HTTPConnection
        return connection_type(parsed.hostname, parsed.port, timeout=timeout)

    @staticmethod
    def _request_path(parsed) -> str:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        return path

    @staticmethod
    def _error_detail(raw: bytes, status: int) -> str:
        detail = raw.decode("utf-8", errors="replace").strip()
        if detail:
            try:
                payload = json.loads(detail)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    detail = str(error.get("message") or error.get("detail") or detail)
                else:
                    detail = str(payload.get("message") or payload.get("detail") or detail)
        return f"ASR API 请求失败（HTTP {status}）：{detail or '服务器未返回具体原因'}"

    @staticmethod
    def _connection_failure(endpoint: str, exc: BaseException) -> AsrError:
        winerror = getattr(exc, "winerror", None)
        refused = isinstance(exc, ConnectionRefusedError) or winerror == 10061 or 10061 in getattr(exc, "args", ())
        if refused:
            health_url = endpoint
            return AsrError(
                f"无法连接 ASR API（{endpoint}）：端口没有服务监听。\n"
                "请确认 CrisperWeaver 已启动、已加载 MiMo，并在“本地 HTTP 服务器（OpenAI 兼容）”中打开“运行服务器”。\n"
                f"检查命令：curl.exe {health_url}"
            )
        return AsrError(f"无法连接 ASR API：{exc}")

    def health(self, settings: AsrApiSettings) -> str:
        endpoint = self._endpoint(settings.base_url, "health", root=True)
        parsed = urlsplit(endpoint)
        connection = self._connection(parsed, timeout=8.0)
        try:
            headers = {"Accept": "application/json,text/plain,*/*", "Connection": "close"}
            token = (settings.api_key or "").strip()
            if any(char in token for char in "\r\n"):
                raise AsrError("ASR API Key 不能包含换行符")
            if token:
                headers["Authorization"] = f"Bearer {token}"
            connection.request("GET", self._request_path(parsed), headers=headers)
            response = connection.getresponse()
            raw = response.read()
            if not 200 <= response.status < 300:
                raise AsrError(self._error_detail(raw, response.status))
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                return str(payload.get("status") or payload.get("message") or "ASR API 服务可用")
            return raw.decode("utf-8", errors="replace").strip() or "ASR API 服务可用"
        except AsrError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise self._connection_failure(endpoint, exc) from exc
        finally:
            connection.close()

    @staticmethod
    def _multipart_field(boundary: str, name: str, value: str) -> bytes:
        if any(char in value for char in "\r\n"):
            raise AsrError("ASR API 表单参数不能包含换行符")
        return (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n"
        ).encode("utf-8")

    @staticmethod
    def _multipart_file_header(boundary: str, filename: str) -> bytes:
        safe_name = "audio.wav" if not filename else Path(filename).name.replace('"', "_")
        content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
        return (
            f"--{boundary}\r\n"
            f"Content-Disposition: form-data; name=\"file\"; filename=\"{safe_name}\"\r\n"
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")

    @staticmethod
    def _parse_transcription(raw: bytes, *, model: str, language: str, duration: float) -> AsrResult:
        text = raw.decode("utf-8", errors="replace").strip()
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"text": text}
        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
            error = payload["error"]
            raise AsrError(str(error.get("message") or error.get("detail") or "ASR API 返回错误"))
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            payload = {"text": str(payload or "")}
        response_text = str(payload.get("text") or "").strip()
        segments = combine_segments(payload.get("segments") or [])
        if not segments and response_text:
            segments = (Segment(0, max(1.0, float(duration or 0)), response_text),)
        if not segments:
            raise AsrError("ASR API 没有返回可用文字")
        return AsrResult(
            backend=ASR_API_BACKEND,
            model=str(payload.get("model") or model),
            language=str(payload.get("language") or language),
            segments=segments,
        )

    def _transcribe_unlocked(
        self,
        audio_path: Path,
        settings: AsrApiSettings,
        *,
        model: str,
        language: str,
        duration: float,
        cancelled: Callable[[], bool],
        progress: Callable[[int, str], None],
        log: Callable[[str], None],
    ) -> AsrResult:
        if not audio_path.exists() or audio_path.stat().st_size <= 0:
            raise AsrError("待上传的音频不存在或为空")
        endpoint = self._endpoint(settings.base_url, "audio/transcriptions")
        parsed = urlsplit(endpoint)
        boundary = "----BiliTranscript" + uuid.uuid4().hex
        prefix = b"".join(
            [
                self._multipart_field(boundary, "model", model or DEFAULT_ASR_API_MODEL),
                self._multipart_field(boundary, "language", language or "zh"),
                self._multipart_field(boundary, "response_format", "verbose_json"),
                self._multipart_file_header(boundary, audio_path.name),
            ]
        )
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")
        content_length = len(prefix) + audio_path.stat().st_size + len(suffix)
        token = (settings.api_key or "").strip()
        if any(char in token for char in "\r\n"):
            raise AsrError("ASR API Key 不能包含换行符")
        headers = {
            "Accept": "application/json",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Content-Length": str(content_length),
            "Connection": "close",
            "User-Agent": "BiliTranscript/ASR-API",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        timeout = max(30.0, float(settings.timeout_seconds or DEFAULT_ASR_API_TIMEOUT))
        connection = self._connection(parsed, timeout=15.0)
        try:
            connection.connect()
            if connection.sock is not None:
                # The API may not send response headers until the model finishes.
                # The caller thread enforces the overall timeout and can close this socket on cancel.
                connection.sock.settimeout(None)
            connection.putrequest("POST", self._request_path(parsed))
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()
            connection.send(prefix)
            sent = 0
            total = audio_path.stat().st_size
            with audio_path.open("rb") as audio:
                while True:
                    if cancelled():
                        connection.close()
                        raise AsrCancelled("操作已取消")
                    chunk = audio.read(256 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
                    sent += len(chunk)
                    progress(min(45, 8 + int(sent / max(1, total) * 37)), "正在上传音频到 ASR API")
            connection.send(suffix)
            progress(50, "ASR API 正在转录")

            result_queue: Queue[tuple[int, bytes, BaseException | None]] = Queue(maxsize=1)

            def receive() -> None:
                try:
                    response = connection.getresponse()
                    result_queue.put((response.status, response.read(), None))
                except BaseException as exc:  # propagate network errors to the caller thread
                    result_queue.put((0, b"", exc))

            reader = threading.Thread(target=receive, name="asr-api-reader", daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            while reader.is_alive():
                if cancelled():
                    connection.close()
                    reader.join(timeout=1)
                    raise AsrCancelled("操作已取消")
                if time.monotonic() >= deadline:
                    connection.close()
                    reader.join(timeout=1)
                    raise AsrError("ASR API 请求超时")
                reader.join(timeout=0.2)
            status, raw, error = result_queue.get()
            if error:
                if isinstance(error, AsrCancelled):
                    raise error
                raise AsrError(f"ASR API 请求失败：{error}") from error
            if not 200 <= status < 300:
                raise AsrError(self._error_detail(raw, status))
            progress(100, "ASR API 转录完成")
            return self._parse_transcription(raw, model=model, language=language, duration=duration)
        except AsrCancelled:
            raise
        except AsrError:
            raise
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            health_endpoint = self._endpoint(settings.base_url, "health", root=True)
            raise self._connection_failure(health_endpoint, exc) from exc
        finally:
            connection.close()

    def transcribe(
        self,
        audio_path: Path,
        settings: AsrApiSettings,
        *,
        model: str,
        language: str,
        duration: float,
        cancelled: Callable[[], bool],
        progress: Callable[[int, str], None],
        log: Callable[[str], None],
    ) -> AsrResult:
        """Submit one audio file. Requests are serialized for local ASR servers."""

        while not _API_REQUEST_LOCK.acquire(timeout=0.2):
            if cancelled():
                raise AsrCancelled("操作已取消")
            progress(2, "等待 ASR API 空闲")
        try:
            return self._transcribe_unlocked(
                audio_path,
                settings,
                model=model or DEFAULT_ASR_API_MODEL,
                language=language or "zh",
                duration=duration,
                cancelled=cancelled,
                progress=progress,
                log=log,
            )
        finally:
            _API_REQUEST_LOCK.release()
