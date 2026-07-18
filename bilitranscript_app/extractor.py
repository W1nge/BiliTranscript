from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from .asr import AsrCancelled, AsrError, ExternalAsrRuntime
from .asr_api import (
    ASR_API_BACKEND,
    DEFAULT_ASR_API_BASE_URL,
    DEFAULT_ASR_API_KEY,
    DEFAULT_ASR_API_MODEL,
    DEFAULT_ASR_API_TIMEOUT,
    AsrApiSettings,
    OpenAICompatibleAsrRuntime,
)
from .bilibili import BilibiliClient, BilibiliError, CancelledError, SubtitleProbe, SubtitleTrack
from .browser_bridge import StandaloneBrowserBridge
from .models import (
    AvailabilityReport,
    ExtractionIssue,
    PartAvailability,
    PartTranscript,
    RouteAvailability,
    TranscriptBundle,
    VideoInfo,
    VideoPart,
)


class ExtractionError(RuntimeError):
    pass


FIXED_EXTRACTION_RULES = (
    "public-player-v2",
    "anonymous-player-wbi-v2",
    "dedicated-browser-player-wbi-v2",
    "local-asr",
)

SUBTITLE_ROUTES = ("public", "anonymous", "browser")
ROUTE_LABELS = {
    "public": "公开字幕",
    "anonymous": "匿名接口",
    "browser": "登录浏览器",
    "asr": "ASR",
}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 2
    interval_seconds: float = 1.0
    sleeper: Callable[[float], None] = time.sleep


@dataclass(frozen=True, slots=True)
class ExtractionOptions:
    mode: str = "auto"  # auto, public, anonymous, browser, subtitles, asr
    browser_ai: bool = True
    asr_backend: str = "auto"  # auto, faster-whisper, funasr, openai-whisper, openai-compatible
    asr_model: str = ""
    language: str = "zh"
    asr_api_base_url: str = DEFAULT_ASR_API_BASE_URL
    asr_api_key: str = DEFAULT_ASR_API_KEY
    asr_api_timeout: float = DEFAULT_ASR_API_TIMEOUT


@dataclass(frozen=True, slots=True)
class _ProbeResult:
    route: str
    tracks: tuple[SubtitleTrack, ...]
    attempts: int
    detail: str


T = TypeVar("T")


class TranscriptExtractor:
    def __init__(
        self,
        client: BilibiliClient | None = None,
        browser_bridge: StandaloneBrowserBridge | None = None,
        asr_runtime: ExternalAsrRuntime | None = None,
        api_runtime: OpenAICompatibleAsrRuntime | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.client = client or BilibiliClient()
        self.browser_bridge = browser_bridge or StandaloneBrowserBridge()
        self.asr_runtime = asr_runtime or ExternalAsrRuntime()
        self.api_runtime = api_runtime or OpenAICompatibleAsrRuntime()
        self.retry_policy = retry_policy or RetryPolicy()

    @staticmethod
    def _source_label(track: SubtitleTrack, route: str) -> str:
        if route == "browser":
            return "专用登录浏览器 AI 字幕" if track.is_ai else "专用登录浏览器字幕"
        if route == "anonymous":
            return "B站匿名接口 AI 字幕" if track.is_ai else "B站匿名接口字幕"
        return "B站 AI 字幕" if track.is_ai else "B站公开字幕"

    def _wait_before_retry(self, cancelled: Callable[[], bool]) -> None:
        if cancelled():
            raise CancelledError("操作已取消")
        delay = max(0.0, float(self.retry_policy.interval_seconds))
        if delay:
            self.retry_policy.sleeper(delay)
        if cancelled():
            raise CancelledError("操作已取消")

    def _retry_value(
        self,
        operation: Callable[[], T],
        *,
        success: Callable[[T], bool],
        description: str,
        cancelled: Callable[[], bool],
        log: Callable[[str], None],
        retry_exceptions: tuple[type[BaseException], ...] = (BilibiliError, OSError),
    ) -> tuple[T | None, int, str]:
        attempts = max(1, int(self.retry_policy.attempts))
        last_value: T | None = None
        last_detail = ""
        for attempt in range(1, attempts + 1):
            if cancelled():
                raise CancelledError("操作已取消")
            try:
                last_value = operation()
                if success(last_value):
                    log(f"{description}：第 {attempt}/{attempts} 次成功")
                    return last_value, attempt, ""
                last_detail = "未返回可用结果"
            except retry_exceptions as exc:
                last_detail = str(exc) or type(exc).__name__
            log(f"{description}：第 {attempt}/{attempts} 次失败（{last_detail}）")
            if attempt < attempts:
                self._wait_before_retry(cancelled)
        return last_value, attempts, last_detail or "两次尝试均不可用"

    def _route_operation(
        self,
        route: str,
        video: VideoInfo,
        part: VideoPart,
    ) -> Callable[[], SubtitleProbe | tuple[SubtitleTrack, ...]]:
        if route == "public":
            return lambda: self.client.probe_public_subtitles(video, part)
        if route == "anonymous":
            return lambda: self.client.probe_anonymous_subtitles(video, part)
        if route == "browser":
            return lambda: self.browser_bridge.fetch_tracks(video, part)
        raise ValueError(f"Unsupported subtitle route: {route}")

    @staticmethod
    def _tracks_from_probe(value: SubtitleProbe | tuple[SubtitleTrack, ...] | None) -> tuple[SubtitleTrack, ...]:
        if isinstance(value, SubtitleProbe):
            return value.tracks
        return tuple(value or ())

    def _probe_route_once(self, route: str, video: VideoInfo, part: VideoPart) -> tuple[tuple[SubtitleTrack, ...], str]:
        try:
            value = self._route_operation(route, video, part)()
            tracks = self._tracks_from_probe(value)
            if any(track.url for track in tracks):
                return tracks, ""
            if route == "browser" and self.browser_bridge.last_error:
                return tracks, self.browser_bridge.last_error
            if any(track.is_ai and not track.url for track in tracks):
                return tracks, "检测到 AI 字幕元数据，但没有下载地址"
            return tracks, "没有可下载字幕"
        except (BilibiliError, OSError, ValueError, TypeError) as exc:
            return (), str(exc) or type(exc).__name__

    def _probe_route(
        self,
        route: str,
        video: VideoInfo,
        part: VideoPart,
        *,
        cancelled: Callable[[], bool],
        log: Callable[[str], None],
    ) -> _ProbeResult:
        operation = self._route_operation(route, video, part)

        value, attempts, detail = self._retry_value(
            operation,
            success=lambda item: any(track.url for track in self._tracks_from_probe(item)),
            description=f"P{part.page} {ROUTE_LABELS[route]}探测",
            cancelled=cancelled,
            log=log,
        )
        tracks = self._tracks_from_probe(value)
        if not any(track.url for track in tracks):
            if route == "browser" and self.browser_bridge.last_error:
                detail = self.browser_bridge.last_error
            elif any(track.is_ai and not track.url for track in tracks):
                detail = "检测到 AI 字幕元数据，但没有下载地址"
            else:
                detail = detail or "没有可下载字幕"
        return _ProbeResult(route=route, tracks=tracks, attempts=attempts, detail=detail)

    def _fetch_track_once(
        self,
        video: VideoInfo,
        part: VideoPart,
        track: SubtitleTrack,
        route: str,
        *,
        cancelled: Callable[[], bool],
        log: Callable[[str], None],
    ) -> tuple[PartTranscript | None, str]:
        source = self._source_label(track, route)
        try:
            payload = self.client.fetch_subtitle_payload(video, track)
            transcript = PartTranscript.from_subtitle_payload(
                part,
                payload,
                source=source,
                language=track.language,
            )
            if transcript.segments:
                return transcript, ""
            return None, "字幕正文为空"
        except (BilibiliError, OSError, ValueError, TypeError) as exc:
            return None, str(exc) or type(exc).__name__

    def _try_subtitle_route(
        self,
        route: str,
        video: VideoInfo,
        part: VideoPart,
        *,
        cancelled: Callable[[], bool],
        log: Callable[[str], None],
    ) -> tuple[PartTranscript, SubtitleTrack] | None:
        attempts = max(1, int(self.retry_policy.attempts))
        for attempt in range(1, attempts + 1):
            if cancelled():
                raise CancelledError("操作已取消")
            tracks, probe_detail = self._probe_route_once(route, video, part)
            ranked = self.client.rank_subtitles(tracks)
            last_detail = probe_detail
            for track in ranked:
                transcript, detail = self._fetch_track_once(
                    video,
                    part,
                    track,
                    route,
                    cancelled=cancelled,
                    log=log,
                )
                if transcript:
                    log(f"P{part.page} {ROUTE_LABELS[route]}：第 {attempt}/{attempts} 次成功")
                    return transcript, track
                last_detail = detail
            log(
                f"P{part.page} {ROUTE_LABELS[route]}：第 {attempt}/{attempts} 次失败"
                f"（{last_detail or '没有有效字幕正文'}）"
            )
            if attempt < attempts:
                self._wait_before_retry(cancelled)
        return None

    def _smart_subtitle_transcript(
        self,
        video: VideoInfo,
        part: VideoPart,
        options: ExtractionOptions,
        *,
        cancelled: Callable[[], bool],
        log: Callable[[str], None],
    ) -> PartTranscript | None:
        fallback: PartTranscript | None = None
        routes = ["public", "anonymous"]
        if options.browser_ai:
            routes.append("browser")
        for route in routes:
            log(f"P{part.page}：尝试 {ROUTE_LABELS[route]}")
            result = self._try_subtitle_route(
                route,
                video,
                part,
                cancelled=cancelled,
                log=log,
            )
            if not result:
                continue
            transcript, track = result
            if self.client.is_chinese_track(track):
                return transcript
            if fallback is None:
                fallback = transcript
                log(f"P{part.page}：暂存非中文字幕，继续寻找中文或 AI 中文字幕")
        return fallback

    def _manual_subtitle_transcript(
        self,
        route: str,
        video: VideoInfo,
        part: VideoPart,
        *,
        cancelled: Callable[[], bool],
        log: Callable[[str], None],
    ) -> PartTranscript | None:
        result = self._try_subtitle_route(
            route,
            video,
            part,
            cancelled=cancelled,
            log=log,
        )
        return result[0] if result else None

    @staticmethod
    def _convert_to_wav(audio: Path, output: Path, cancelled: Callable[[], bool]) -> Path:
        ffmpeg = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if not ffmpeg:
            raise AsrError("FunASR、OpenAI Whisper 和 API ASR 需要 ffmpeg，请先安装 ffmpeg 或改用 Faster-Whisper")
        process = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(audio), "-ar", "16000", "-ac", "1", str(output)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        while process.poll() is None:
            if cancelled():
                process.terminate()
                raise AsrCancelled("操作已取消")
            threading.Event().wait(0.15)
        if process.returncode != 0 or not output.exists():
            detail = (process.stderr.read() if process.stderr else b"").decode("utf-8", errors="replace").strip()
            raise AsrError(f"音频转换失败：{detail or 'ffmpeg 返回错误'}")
        return output

    def _asr_transcript(
        self,
        video: VideoInfo,
        part: VideoPart,
        options: ExtractionOptions,
        work_dir: Path,
        cancelled: Callable[[], bool],
        progress: Callable[[int, str], None],
        log: Callable[[str], None],
    ) -> PartTranscript:
        backend = options.asr_backend
        if backend != ASR_API_BACKEND:
            availability = self.asr_runtime.detect()
            if not availability.available:
                if backend == "auto":
                    try:
                        self.api_runtime.health(
                            AsrApiSettings(
                                base_url=options.asr_api_base_url,
                                api_key=options.asr_api_key,
                                timeout_seconds=options.asr_api_timeout,
                            )
                        )
                    except AsrError as exc:
                        raise AsrError(
                            "没有可用的本地 ASR，且 OpenAI 兼容 API 也不可用。请安装本地引擎或启动 API 服务。"
                        ) from exc
                    backend = ASR_API_BACKEND
                    log(f"P{part.page}：本地 ASR 不可用，改用 OpenAI 兼容 ASR API")
                else:
                    raise AsrError(
                        "没有可用的本地 ASR。可安装 faster-whisper 或 FunASR，或切换到 OpenAI 兼容 API。"
                    )
            if backend == "auto":
                backend = next(
                    (item for item in ("faster-whisper", "funasr", "openai-whisper") if item in availability.backends),
                    "",
                )
        if backend == ASR_API_BACKEND:
            log(f"P{part.page}：使用 OpenAI 兼容 ASR API（{options.asr_api_base_url}）")
        else:
            log(f"P{part.page}：字幕来源全部不可用，改用 {backend} 本地转写")
        audio_path = work_dir / f"p{part.page:02d}_{part.cid}.m4s"

        def download_progress(received: int, total: int) -> None:
            value = int(received / total * 100) if total else 0
            progress(min(35, int(value * 0.35)), "正在下载音频")

        def download_audio() -> bool:
            if audio_path.exists():
                audio_path.unlink()
            self.client.download_audio(
                video,
                part,
                audio_path,
                progress=download_progress,
                cancelled=cancelled,
            )
            return audio_path.exists() and audio_path.stat().st_size > 0

        downloaded, _, detail = self._retry_value(
            download_audio,
            success=bool,
            description=f"P{part.page} ASR 音频",
            cancelled=cancelled,
            log=log,
        )
        if not downloaded:
            raise AsrError(f"音频连续两次不可用：{detail}")

        if backend == ASR_API_BACKEND:
            api_input = audio_path
            if audio_path.suffix.lower() not in {".wav", ".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".webm", ".ogg", ".flac"}:
                progress(38, "正在转换 API 音频格式")
                api_input = self._convert_to_wav(audio_path, work_dir / f"p{part.page:02d}_{part.cid}.wav", cancelled)
            result = self.api_runtime.transcribe(
                api_input,
                AsrApiSettings(
                    base_url=options.asr_api_base_url,
                    api_key=options.asr_api_key,
                    timeout_seconds=options.asr_api_timeout,
                ),
                model=options.asr_model or DEFAULT_ASR_API_MODEL,
                language=options.language,
                duration=float(part.duration),
                cancelled=cancelled,
                progress=progress,
                log=log,
            )
            return PartTranscript(
                part=part,
                source="OpenAI 兼容 API ASR",
                language=result.language,
                segments=result.segments,
            )

        asr_input = audio_path
        if backend in {"funasr", "openai-whisper"}:
            progress(38, "正在转换音频")
            asr_input = self._convert_to_wav(audio_path, work_dir / f"p{part.page:02d}_{part.cid}.wav", cancelled)
        output_path = work_dir / f"p{part.page:02d}_{part.cid}.asr.json"

        def asr_progress(value: int, message: str) -> None:
            progress(40 + int(max(0, min(100, value)) * 0.58), message)

        result = self.asr_runtime.transcribe(
            asr_input,
            output_path,
            backend=backend,
            model=options.asr_model,
            language=options.language,
            duration=float(part.duration),
            cancelled=cancelled,
            progress=asr_progress,
            log=log,
        )
        labels = {
            "faster-whisper": "Faster-Whisper 本地转写",
            "funasr": "FunASR 本地转写",
            "openai-whisper": "OpenAI Whisper 本地转写",
        }
        return PartTranscript(
            part=part,
            source=labels.get(result.backend, f"{result.backend} 本地转写"),
            language=result.language,
            segments=result.segments,
        )

    def _probe_asr(
        self,
        video: VideoInfo,
        part: VideoPart,
        *,
        options: ExtractionOptions | None = None,
        cancelled: Callable[[], bool],
        log: Callable[[str], None],
    ) -> RouteAvailability:
        if options and options.asr_backend in {"auto", ASR_API_BACKEND}:
            settings = AsrApiSettings(
                base_url=options.asr_api_base_url,
                api_key=options.asr_api_key,
                timeout_seconds=options.asr_api_timeout,
            )
            local_available = self.asr_runtime.detect().available
            if options.asr_backend == ASR_API_BACKEND or not local_available:
                if not shutil.which("ffmpeg") and not shutil.which("ffmpeg.exe"):
                    return RouteAvailability("asr", False, "API ASR 需要 ffmpeg 将 B站音频转换为 WAV")
                _, attempts, detail = self._retry_value(
                    lambda: self.api_runtime.health(settings),
                    success=lambda value: bool(value),
                    description=f"P{part.page} ASR API 服务探测",
                    cancelled=cancelled,
                    log=log,
                    retry_exceptions=(AsrError, OSError),
                )
                if detail and attempts:
                    if options.asr_backend == ASR_API_BACKEND:
                        return RouteAvailability("asr", False, detail, attempts=attempts)
                    if not local_available:
                        return RouteAvailability("asr", False, detail, attempts=attempts)
                if options.asr_backend == ASR_API_BACKEND:
                    return RouteAvailability("asr", True, "ASR API 服务可用", attempts=attempts)
                if not local_available:
                    return RouteAvailability("asr", True, "ASR API 服务可用（本地引擎不可用）", attempts=attempts)
        availability = self.asr_runtime.detect()
        if not availability.available:
            return RouteAvailability("asr", False, "未安装 Faster-Whisper、FunASR 或 OpenAI Whisper")
        audio_url, attempts, detail = self._retry_value(
            lambda: self.client.audio_url(video, part),
            success=lambda value: bool(value),
            description=f"P{part.page} ASR 音频源探测",
            cancelled=cancelled,
            log=log,
        )
        if not audio_url:
            return RouteAvailability("asr", False, detail or "没有公开音频", attempts=attempts)
        backends = "、".join(availability.backends)
        return RouteAvailability("asr", True, f"音频可用；{backends}", attempts=attempts)

    def probe_availability(
        self,
        video: VideoInfo,
        parts: list[VideoPart],
        *,
        options: ExtractionOptions | None = None,
        cancelled: Callable[[], bool],
        progress: Callable[[int, str], None],
        log: Callable[[str], None],
    ) -> AvailabilityReport:
        if not parts:
            raise ExtractionError("请至少选择一个分P")
        results: list[PartAvailability] = []
        total_steps = max(1, len(parts) * 4)
        completed_steps = 0
        for part in parts:
            routes: list[RouteAvailability] = []
            for route in SUBTITLE_ROUTES:
                if cancelled():
                    raise CancelledError("操作已取消")
                progress(int(completed_steps / total_steps * 100), f"P{part.page} · 检测{ROUTE_LABELS[route]}")
                probe = self._probe_route(route, video, part, cancelled=cancelled, log=log)
                ranked = self.client.rank_subtitles(probe.tracks)
                if ranked:
                    first = ranked[0]
                    kind = "AI 字幕" if first.is_ai else "字幕"
                    detail = f"{first.label or first.language} {kind}；{len(ranked)} 条可下载"
                    routes.append(RouteAvailability(route, True, detail, probe.attempts, len(ranked)))
                else:
                    routes.append(RouteAvailability(route, False, probe.detail or "不可用", probe.attempts, 0))
                completed_steps += 1
            progress(int(completed_steps / total_steps * 100), f"P{part.page} · 检测 ASR")
            routes.append(self._probe_asr(video, part, options=options, cancelled=cancelled, log=log))
            completed_steps += 1
            results.append(PartAvailability(part=part, routes=tuple(routes)))
        progress(100, f"检测完成：{len(results)} 个分P")
        return AvailabilityReport(video=video, parts=tuple(results))

    def _extract_part(
        self,
        video: VideoInfo,
        part: VideoPart,
        options: ExtractionOptions,
        work_dir: Path,
        *,
        cancelled: Callable[[], bool],
        progress: Callable[[int, str], None],
        log: Callable[[str], None],
    ) -> PartTranscript | None:
        mode = options.mode
        if mode in {"auto", "subtitles"}:
            transcript = self._smart_subtitle_transcript(
                video,
                part,
                options,
                cancelled=cancelled,
                log=log,
            )
            if transcript or mode == "subtitles":
                return transcript
            return self._asr_transcript(video, part, options, work_dir, cancelled, progress, log)
        if mode in SUBTITLE_ROUTES:
            return self._manual_subtitle_transcript(
                mode,
                video,
                part,
                cancelled=cancelled,
                log=log,
            )
        if mode == "asr":
            return self._asr_transcript(video, part, options, work_dir, cancelled, progress, log)
        raise ExtractionError(f"不支持的提取方式：{mode}")

    def extract(
        self,
        video: VideoInfo,
        parts: list[VideoPart],
        options: ExtractionOptions,
        *,
        cancelled: Callable[[], bool],
        progress: Callable[[int, str], None],
        log: Callable[[str], None],
    ) -> TranscriptBundle:
        if not parts:
            raise ExtractionError("请至少选择一个分P")
        bundle = TranscriptBundle(video=video)
        with tempfile.TemporaryDirectory(prefix=f"bilitranscript-{video.bvid}-") as temporary:
            work_dir = Path(temporary)
            for index, part in enumerate(parts):
                if cancelled():
                    raise CancelledError("操作已取消")
                part_base = int(index / len(parts) * 100)
                part_span = max(1, int(100 / len(parts)))

                def part_progress(value: int, message: str) -> None:
                    overall = min(99, part_base + int(max(0, min(100, value)) / 100 * part_span))
                    progress(overall, f"P{part.page} · {message}")

                part_progress(2, "正在检查字幕来源")
                try:
                    transcript = self._extract_part(
                        video,
                        part,
                        options,
                        work_dir,
                        cancelled=cancelled,
                        progress=part_progress,
                        log=log,
                    )
                    if transcript is None:
                        route = ROUTE_LABELS.get(options.mode, "字幕来源")
                        raise ExtractionError(f"{route}连续两次尝试后仍没有可用文稿")
                    bundle.parts.append(transcript)
                    part_progress(100, f"已完成 · {transcript.source}")
                except (BilibiliError, AsrError, ExtractionError) as exc:
                    message = str(exc)
                    bundle.issues.append(ExtractionIssue(page=part.page, title=part.title, message=message))
                    log(f"P{part.page} 失败：{message}")
                except (CancelledError, AsrCancelled):
                    raise CancelledError("操作已取消")
        if not bundle.parts:
            details = "；".join(f"P{item.page}：{item.message}" for item in bundle.issues)
            raise ExtractionError(details or "没有提取到文稿")
        progress(100, f"完成：{len(bundle.parts)} 个分P")
        return bundle
