from __future__ import annotations

import hmac
import json
import re
import socket
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from .asr import AsrCancelled
from .bilibili import BilibiliClient, CancelledError
from .extractor import ExtractionOptions, TranscriptExtractor
from .models import TranscriptBundle
from .sources import extract_bilibili_sources


DEFAULT_EXTRACTION_API_HOST = "0.0.0.0"
DEFAULT_EXTRACTION_API_PORT = 8766
MAX_REQUEST_BYTES = 16 * 1024
MAX_SOURCE_LENGTH = 2048
MAX_CONCURRENT_JOBS = 3
MAX_PENDING_JOBS = 32
MAX_RETAINED_JOBS = 100
JOB_RETENTION_SECONDS = 3600.0
RESULT_FORMATS = ("json", "markdown", "text", "srt")
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _public_options(options: ExtractionOptions) -> dict[str, Any]:
    return {
        "mode": options.mode,
        "browser_ai": options.browser_ai,
        "asr_backend": options.asr_backend,
        "asr_model": options.asr_model,
        "language": options.language,
    }


def _sanitize_message(message: str, options: ExtractionOptions) -> str:
    value = str(message or "").strip()
    replacements: list[str] = []
    api_key = str(options.asr_api_key or "").strip()
    if api_key:
        replacements.append(api_key)
    base_url = str(options.asr_api_base_url or "").strip().rstrip("/")
    if base_url:
        replacements.append(base_url)
        parsed = urlsplit(base_url if "://" in base_url else "http://" + base_url)
        if parsed.scheme and parsed.netloc:
            replacements.append(f"{parsed.scheme}://{parsed.netloc}")
    for secret in sorted(set(replacements), key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[已隐藏]")
    return value or "任务失败"


class ApiRequestError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message


@dataclass(slots=True)
class ExtractionJob:
    job_id: str
    source: str
    options: ExtractionOptions
    status: str = "queued"
    progress: int = 0
    message: str = "等待处理"
    error: str = ""
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    finished_monotonic: float | None = None
    result: TranscriptBundle | None = None
    cancel_requested: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    future: Future[None] | None = field(default=None, repr=False)
    completion_notified: bool = field(default=False, repr=False)


class ExtractionJobManager:
    def __init__(
        self,
        *,
        client_factory: Callable[[], BilibiliClient] = BilibiliClient,
        extractor_factory: Callable[[], TranscriptExtractor] = TranscriptExtractor,
        max_workers: int = MAX_CONCURRENT_JOBS,
        max_pending: int = MAX_PENDING_JOBS,
        max_retained: int = MAX_RETAINED_JOBS,
        retention_seconds: float = JOB_RETENTION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        on_finished: Callable[[ExtractionJob], None] | None = None,
        on_submitted: Callable[[ExtractionJob], None] | None = None,
        on_started: Callable[[ExtractionJob], None] | None = None,
    ) -> None:
        self.client_factory = client_factory
        self.extractor_factory = extractor_factory
        self.max_workers = max(1, int(max_workers))
        self.max_pending = max(1, int(max_pending))
        self.max_retained = max(self.max_pending, int(max_retained))
        self.retention_seconds = max(0.0, float(retention_seconds))
        self.clock = clock
        self.on_finished = on_finished
        self.on_submitted = on_submitted
        self.on_started = on_started
        self._lock = threading.RLock()
        self._jobs: dict[str, ExtractionJob] = {}
        self._callback_threads: set[threading.Thread] = set()
        self._closed = False
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="bili-api-job",
        )

    def _cleanup_locked(self) -> None:
        now = self.clock()
        expired = [
            job_id
            for job_id, job in self._jobs.items()
            if job.finished_monotonic is not None
            and now - job.finished_monotonic >= self.retention_seconds
        ]
        for job_id in expired:
            self._jobs.pop(job_id, None)
        if len(self._jobs) <= self.max_retained:
            return
        finished = sorted(
            (job for job in self._jobs.values() if job.finished_monotonic is not None),
            key=lambda item: item.finished_monotonic or 0.0,
        )
        for job in finished:
            if len(self._jobs) <= self.max_retained:
                break
            self._jobs.pop(job.job_id, None)

    def _finish_locked(self, job: ExtractionJob, status: str, message: str) -> None:
        if job.status in TERMINAL_STATUSES and job.completion_notified:
            return
        job.status = status
        job.message = message
        job.finished_at = _now_iso()
        job.finished_monotonic = self.clock()
        if status == "succeeded":
            job.progress = 100
        if status in TERMINAL_STATUSES and not job.completion_notified:
            job.completion_notified = True
            callback = self.on_finished
            if callback is not None:
                callback_thread = threading.Thread(
                    target=self._invoke_callback,
                    args=(callback, job),
                    name="bili-api-completion",
                    daemon=True,
                )
                self._callback_threads.add(callback_thread)
                callback_thread.start()

    def _invoke_callback(self, callback: Callable[[ExtractionJob], None], job: ExtractionJob) -> None:
        try:
            callback(job)
        except Exception:
            # Persistence/telemetry must never break an API task.
            pass
        finally:
            with self._lock:
                self._callback_threads.discard(threading.current_thread())

    def submit(self, source: str, options: ExtractionOptions) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            if self._closed:
                raise ApiRequestError(HTTPStatus.SERVICE_UNAVAILABLE, "service_stopping", "API 服务正在停止")
            active = sum(job.status not in TERMINAL_STATUSES for job in self._jobs.values())
            if active >= self.max_pending:
                raise ApiRequestError(HTTPStatus.TOO_MANY_REQUESTS, "queue_full", "提取任务队列已满，请稍后重试")
            job = ExtractionJob(job_id=uuid.uuid4().hex, source=source, options=options)
            self._jobs[job.job_id] = job
            callback = self.on_submitted
            if callback is not None:
                try:
                    callback(job)
                except Exception:
                    pass
            job.future = self._executor.submit(self._run_job, job.job_id)
            snapshot = self._snapshot_locked(job)
            return snapshot

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.cancel_event.is_set():
                self._finish_locked(job, "cancelled", "任务已取消")
                return
            job.status = "running"
            job.started_at = _now_iso()
            job.message = "正在读取视频信息"
            started_callback = self.on_started

        if started_callback is not None:
            try:
                started_callback(job)
            except Exception:
                pass

        try:
            video = self.client_factory().fetch_video(job.source)
            if job.cancel_event.is_set():
                raise CancelledError("操作已取消")
            if not video.parts:
                raise RuntimeError("视频没有可提取的分P")

            def report(value: int, message: str) -> None:
                with self._lock:
                    current = self._jobs.get(job_id)
                    if current is None or current.status in TERMINAL_STATUSES:
                        return
                    current.progress = max(0, min(100, int(value)))
                    if message:
                        current.message = str(message)

            bundle = self.extractor_factory().extract(
                video,
                list(video.parts),
                job.options,
                cancelled=job.cancel_event.is_set,
                progress=report,
                log=lambda _message: None,
            )
            with self._lock:
                current = self._jobs.get(job_id)
                if current is None:
                    return
                if current.cancel_event.is_set():
                    self._finish_locked(current, "cancelled", "任务已取消")
                else:
                    current.result = bundle
                    detail = f"已提取 {len(bundle.parts)} 个分P"
                    if bundle.issues:
                        detail += f"，{len(bundle.issues)} 个分P失败"
                    self._finish_locked(current, "succeeded", detail)
        except (CancelledError, AsrCancelled):
            with self._lock:
                current = self._jobs.get(job_id)
                if current is not None:
                    self._finish_locked(current, "cancelled", "任务已取消")
        except Exception as exc:
            with self._lock:
                current = self._jobs.get(job_id)
                if current is not None:
                    current.error = _sanitize_message(str(exc) or type(exc).__name__, current.options)
                    self._finish_locked(current, "failed", "提取失败")

    def _get_locked(self, job_id: str) -> ExtractionJob:
        self._cleanup_locked()
        job = self._jobs.get(job_id)
        if job is None:
            raise ApiRequestError(HTTPStatus.NOT_FOUND, "job_not_found", "没有找到该提取任务，任务可能已过期")
        return job

    @staticmethod
    def _snapshot_locked(job: ExtractionJob) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": job.job_id,
            "status": job.status,
            "source": job.source,
            "progress": job.progress,
            "message": job.message,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
            "cancel_requested": job.cancel_requested,
            "has_issues": bool(job.result and job.result.issues),
            "settings": _public_options(job.options),
            "status_url": f"/v1/extractions/{job.job_id}",
            "result_url": f"/v1/extractions/{job.job_id}/result",
        }
        if job.error:
            payload["error"] = job.error
        return payload

    def snapshot(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(self._get_locked(job_id))

    def result(self, job_id: str) -> TranscriptBundle:
        with self._lock:
            job = self._get_locked(job_id)
            if job.status != "succeeded" or job.result is None:
                detail = job.error or job.message
                raise ApiRequestError(
                    HTTPStatus.CONFLICT,
                    "result_not_ready",
                    f"任务当前状态为 {job.status}：{detail}",
                )
            return job.result

    def cancel(self, job_id: str) -> tuple[dict[str, Any], bool]:
        with self._lock:
            job = self._get_locked(job_id)
            if job.status in TERMINAL_STATUSES:
                return self._snapshot_locked(job), False
            job.cancel_requested = True
            job.cancel_event.set()
            job.message = "正在取消"
            if job.future is not None and job.future.cancel():
                self._finish_locked(job, "cancelled", "任务已取消")
            return self._snapshot_locked(job), True

    def counts(self) -> dict[str, int]:
        with self._lock:
            self._cleanup_locked()
            statuses = {name: 0 for name in ("queued", "running", "succeeded", "failed", "cancelled")}
            for job in self._jobs.values():
                statuses[job.status] = statuses.get(job.status, 0) + 1
            statuses["active"] = statuses.get("queued", 0) + statuses.get("running", 0)
            statuses["retained"] = len(self._jobs)
            return statuses

    def cleanup(self) -> None:
        with self._lock:
            self._cleanup_locked()

    def shutdown(self, *, cancel_jobs: bool = True, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            jobs = list(self._jobs.values())
            if cancel_jobs:
                for job in jobs:
                    if job.status not in TERMINAL_STATUSES:
                        job.cancel_requested = True
                        job.cancel_event.set()
                        job.message = "正在取消"
                        if job.future is not None and job.future.cancel():
                            self._finish_locked(job, "cancelled", "任务已取消")
        self._executor.shutdown(wait=wait, cancel_futures=cancel_jobs)
        if wait:
            with self._lock:
                callback_threads = list(self._callback_threads)
            for callback_thread in callback_threads:
                callback_thread.join(timeout=5.0)


def build_openapi_document(version: str) -> dict[str, Any]:
    error_schema = {
        "type": "object",
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string"},
                    "message": {"type": "string"},
                },
            }
        },
    }
    auth = [{"bearerAuth": []}]
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "BiliTranscript Extraction API",
            "version": version,
            "description": "提交单个 B站视频并异步提取全部分P文稿。提取设置由桌面软件提供。",
        },
        "paths": {
            "/health": {
                "get": {
                    "summary": "健康检查",
                    "responses": {"200": {"description": "服务可用"}},
                }
            },
            "/v1/capabilities": {
                "get": {
                    "summary": "读取能力与当前提取设置",
                    "security": auth,
                    "responses": {"200": {"description": "能力信息"}, "401": {"description": "未授权"}},
                }
            },
            "/v1/extractions": {
                "post": {
                    "summary": "提交提取任务",
                    "security": auth,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "required": ["source"],
                                    "properties": {"source": {"type": "string", "maxLength": MAX_SOURCE_LENGTH}},
                                }
                            }
                        },
                    },
                    "responses": {
                        "202": {"description": "任务已进入队列"},
                        "400": {"description": "请求无效", "content": {"application/json": {"schema": error_schema}}},
                        "401": {"description": "未授权"},
                        "429": {"description": "队列已满"},
                    },
                }
            },
            "/v1/extractions/{id}": {
                "get": {
                    "summary": "读取任务状态",
                    "security": auth,
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "任务状态"}, "404": {"description": "任务不存在"}},
                }
            },
            "/v1/extractions/{id}/result": {
                "get": {
                    "summary": "获取提取结果",
                    "security": auth,
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}},
                        {"name": "format", "in": "query", "schema": {"type": "string", "enum": list(RESULT_FORMATS), "default": "json"}},
                        {"name": "timestamps", "in": "query", "schema": {"type": "boolean", "default": False}},
                    ],
                    "responses": {"200": {"description": "文稿结果"}, "409": {"description": "任务尚未成功完成"}},
                }
            },
            "/v1/extractions/{id}/cancel": {
                "post": {
                    "summary": "取消提取任务",
                    "security": auth,
                    "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                    "responses": {"200": {"description": "任务已经结束"}, "202": {"description": "正在取消"}},
                }
            },
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "API Key"}
            }
        },
    }


class _ApiHttpServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True
    request_queue_size = 32

    def server_bind(self) -> None:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def service_actions(self) -> None:
        super().service_actions()
        manager = getattr(self, "job_manager", None)
        if manager is not None:
            manager.cleanup()


class ExtractionApiServer:
    def __init__(
        self,
        *,
        host: str = DEFAULT_EXTRACTION_API_HOST,
        port: int = DEFAULT_EXTRACTION_API_PORT,
        api_key: str,
        options_provider: Callable[[], ExtractionOptions],
        version: str,
        job_manager: ExtractionJobManager | None = None,
        on_finished: Callable[[ExtractionJob], None] | None = None,
        on_submitted: Callable[[ExtractionJob], None] | None = None,
        on_started: Callable[[ExtractionJob], None] | None = None,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.options_provider = options_provider
        self.version = version
        self.job_manager = job_manager or ExtractionJobManager(
            on_finished=on_finished,
            on_submitted=on_submitted,
            on_started=on_started,
        )
        self._api_key = api_key
        self._key_lock = threading.Lock()
        self._httpd: _ApiHttpServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._httpd)

    @property
    def bound_port(self) -> int:
        if self._httpd:
            return int(self._httpd.server_address[1])
        return self.port

    def update_api_key(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("API Key 不能为空")
        with self._key_lock:
            self._api_key = api_key

    def _authorized(self, value: str) -> bool:
        with self._key_lock:
            expected = f"Bearer {self._api_key}"
        return hmac.compare_digest(str(value or ""), expected)

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        api = self
        job_pattern = re.compile(r"^/v1/extractions/([0-9a-f]{32})$")
        result_pattern = re.compile(r"^/v1/extractions/([0-9a-f]{32})/result$")
        cancel_pattern = re.compile(r"^/v1/extractions/([0-9a-f]{32})/cancel$")

        class Handler(BaseHTTPRequestHandler):
            server_version = "BiliTranscriptAPI"
            sys_version = ""
            protocol_version = "HTTP/1.1"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(15.0)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def send_error(
                self,
                code: int,
                message: str | None = None,
                explain: str | None = None,
            ) -> None:
                status = HTTPStatus.METHOD_NOT_ALLOWED if int(code) == HTTPStatus.NOT_IMPLEMENTED else int(code)
                self._error(
                    ApiRequestError(
                        status,
                        "method_not_allowed" if status == HTTPStatus.METHOD_NOT_ALLOWED else "http_error",
                        "不支持该请求方法" if status == HTTPStatus.METHOD_NOT_ALLOWED else str(message or explain or "HTTP 请求错误"),
                    )
                )

            def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
                self.send_response(int(status))
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                if status == HTTPStatus.UNAUTHORIZED:
                    self.send_header("WWW-Authenticate", 'Bearer realm="BiliTranscript"')
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def _send_json(self, status: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self._send_bytes(status, body, "application/json; charset=utf-8")

            def _error(self, error: ApiRequestError) -> None:
                self._send_json(error.status, {"error": {"code": error.code, "message": error.message}})

            def _require_auth(self) -> None:
                if not api._authorized(self.headers.get("Authorization", "")):
                    raise ApiRequestError(HTTPStatus.UNAUTHORIZED, "unauthorized", "需要有效的 Bearer API Key")

            def _read_json(self) -> dict[str, Any]:
                content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_content_type", "请求必须使用 application/json")
                raw_length = self.headers.get("Content-Length")
                if raw_length is None:
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "missing_content_length", "请求缺少 Content-Length")
                try:
                    length = int(raw_length)
                except ValueError as exc:
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Content-Length 无效") from exc
                if length < 0:
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Content-Length 无效")
                if length > MAX_REQUEST_BYTES:
                    raise ApiRequestError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "请求体不能超过 16 KiB")
                try:
                    payload = json.loads(self.rfile.read(length).decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_json", "请求体不是有效的 UTF-8 JSON") from exc
                if not isinstance(payload, dict):
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_request", "请求体必须是 JSON 对象")
                return payload

            @staticmethod
            def _query_value(query: dict[str, list[str]], name: str, default: str) -> str:
                values = query.get(name)
                if not values:
                    return default
                if len(values) != 1:
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_query", f"查询参数 {name} 只能出现一次")
                return values[0]

            def _result(self, job_id: str, query_string: str) -> None:
                query = parse_qs(query_string, keep_blank_values=True)
                unknown = set(query) - {"format", "timestamps"}
                if unknown:
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_query", f"不支持的查询参数：{sorted(unknown)[0]}")
                output_format = self._query_value(query, "format", "json").lower()
                if output_format not in RESULT_FORMATS:
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_format", "format 必须是 json、markdown、text 或 srt")
                raw_timestamps = self._query_value(query, "timestamps", "false").lower()
                if raw_timestamps not in {"true", "false", "1", "0"}:
                    raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_timestamps", "timestamps 必须是 true 或 false")
                timestamps = raw_timestamps in {"true", "1"}
                bundle = api.job_manager.result(job_id)
                if output_format == "json":
                    payload = bundle.to_dict()
                    payload["has_issues"] = bool(bundle.issues)
                    self._send_json(HTTPStatus.OK, payload)
                    return
                if output_format == "markdown":
                    content = bundle.to_markdown(timestamps=timestamps)
                    content_type = "text/markdown; charset=utf-8"
                elif output_format == "text":
                    content = bundle.to_text(timestamps=timestamps)
                    content_type = "text/plain; charset=utf-8"
                else:
                    content = bundle.to_srt()
                    content_type = "application/x-subrip; charset=utf-8"
                self._send_bytes(HTTPStatus.OK, content.encode("utf-8"), content_type)

            def do_GET(self) -> None:
                try:
                    parsed = urlsplit(self.path)
                    if parsed.path == "/health":
                        self._send_json(
                            HTTPStatus.OK,
                            {"status": "ok", "service": "BiliTranscript", "version": api.version},
                        )
                        return
                    if parsed.path == "/openapi.json":
                        self._send_json(HTTPStatus.OK, build_openapi_document(api.version))
                        return
                    self._require_auth()
                    if parsed.path == "/v1/capabilities":
                        options = api.options_provider()
                        self._send_json(
                            HTTPStatus.OK,
                            {
                                "service": "BiliTranscript",
                                "version": api.version,
                                "settings": _public_options(options),
                                "all_parts": True,
                                "result_formats": list(RESULT_FORMATS),
                                "max_concurrent_jobs": api.job_manager.max_workers,
                                "max_pending_jobs": api.job_manager.max_pending,
                            },
                        )
                        return
                    match = result_pattern.fullmatch(parsed.path)
                    if match:
                        self._result(match.group(1), parsed.query)
                        return
                    match = job_pattern.fullmatch(parsed.path)
                    if match:
                        self._send_json(HTTPStatus.OK, api.job_manager.snapshot(match.group(1)))
                        return
                    raise ApiRequestError(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
                except ApiRequestError as exc:
                    self._error(exc)
                except Exception:
                    self._error(ApiRequestError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "API 内部错误"))

            def do_POST(self) -> None:
                try:
                    parsed = urlsplit(self.path)
                    self._require_auth()
                    if parsed.path == "/v1/extractions":
                        if parsed.query:
                            raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_query", "提交任务不支持查询参数")
                        payload = self._read_json()
                        unknown = set(payload) - {"source"}
                        if unknown:
                            raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_request", f"不支持的字段：{sorted(unknown)[0]}")
                        source = payload.get("source")
                        if not isinstance(source, str) or not source.strip():
                            raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_source", "source 必须是非空字符串")
                        source = source.strip()
                        if len(source) > MAX_SOURCE_LENGTH:
                            raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_source", "source 不能超过 2048 个字符")
                        sources = extract_bilibili_sources(source)
                        if len(sources) != 1:
                            message = "source 中没有可识别的 B站视频" if not sources else "每个任务只能提交一个 B站视频"
                            raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_source", message)
                        snapshot = api.job_manager.submit(sources[0], api.options_provider())
                        self._send_json(HTTPStatus.ACCEPTED, snapshot)
                        return
                    match = cancel_pattern.fullmatch(parsed.path)
                    if match:
                        if parsed.query:
                            raise ApiRequestError(HTTPStatus.BAD_REQUEST, "invalid_query", "取消任务不支持查询参数")
                        snapshot, requested = api.job_manager.cancel(match.group(1))
                        self._send_json(HTTPStatus.ACCEPTED if requested else HTTPStatus.OK, snapshot)
                        return
                    raise ApiRequestError(HTTPStatus.NOT_FOUND, "not_found", "接口不存在")
                except ApiRequestError as exc:
                    self._error(exc)
                except Exception:
                    self._error(ApiRequestError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "API 内部错误"))

            def do_OPTIONS(self) -> None:
                self._error(ApiRequestError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "不支持该请求方法"))

            def do_PUT(self) -> None:
                self.do_OPTIONS()

            def do_PATCH(self) -> None:
                self.do_OPTIONS()

            def do_DELETE(self) -> None:
                self.do_OPTIONS()

        return Handler

    def start(self) -> None:
        if self.running:
            return
        if not self._api_key:
            raise ValueError("API Key 不能为空")
        httpd = _ApiHttpServer((self.host, self.port), self._handler_class())
        httpd.job_manager = self.job_manager
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="bili-extraction-api", daemon=True)
        self._thread.start()

    def stop(self, *, cancel_jobs: bool = True, wait_for_jobs: bool = False) -> None:
        httpd = self._httpd
        thread = self._thread
        self._httpd = None
        self._thread = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.job_manager.shutdown(cancel_jobs=cancel_jobs, wait=wait_for_jobs)

    def counts(self) -> dict[str, int]:
        return self.job_manager.counts()
