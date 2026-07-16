from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .models import VideoInfo, VideoPart


API_BASE = "https://api.bilibili.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
)
ALLOWED_SOURCE_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com", "b23.tv"}
ALLOWED_MEDIA_SUFFIXES = (
    ".bilibili.com",
    ".hdslb.com",
    ".bilivideo.com",
    ".bilivideo.cn",
    ".biliapi.net",
    ".akamaized.net",
)


class BilibiliError(RuntimeError):
    """A user-facing Bilibili extraction error."""


class CancelledError(RuntimeError):
    pass


class Transport(Protocol):
    def get_json(self, url: str, *, referer: str, timeout: float = 30) -> dict[str, Any]: ...

    def resolve_url(self, url: str, *, timeout: float = 15) -> str: ...

    def download(
        self,
        url: str,
        destination: Path,
        *,
        referer: str,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        timeout: float = 60,
    ) -> None: ...


def _headers(referer: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Referer": referer,
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
    }


class UrllibTransport:
    def __init__(self, retries: int = 0) -> None:
        # TranscriptExtractor owns the fixed two-attempt, one-second retry policy.
        self.retries = max(0, retries)

    def _open(self, request: urllib.request.Request, timeout: float):
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                return urllib.request.urlopen(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in {408, 429, 500, 502, 503, 504} or attempt >= self.retries:
                    raise
            except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
                last_error = exc
                if attempt >= self.retries:
                    raise
            time.sleep(0.45 * (attempt + 1))
        raise last_error or BilibiliError("网络请求失败")

    def get_json(self, url: str, *, referer: str, timeout: float = 30) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=_headers(referer))
        try:
            with self._open(request, timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 412:
                raise BilibiliError("B站拒绝了当前请求（HTTP 412），请稍后重试或切换网络") from exc
            raise BilibiliError(f"B站请求失败（HTTP {exc.code}）") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            reason = getattr(exc, "reason", exc)
            raise BilibiliError(f"无法连接 B站：{reason}") from exc
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BilibiliError("B站返回了无法解析的数据") from exc

    def resolve_url(self, url: str, *, timeout: float = 15) -> str:
        request = urllib.request.Request(url, headers=_headers("https://www.bilibili.com/"))
        try:
            with self._open(request, timeout) as response:
                return str(response.geturl())
        except Exception as exc:
            if isinstance(exc, BilibiliError):
                raise
            raise BilibiliError("短链接解析失败") from exc

    def download(
        self,
        url: str,
        destination: Path,
        *,
        referer: str,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        timeout: float = 60,
    ) -> None:
        request = urllib.request.Request(url, headers=_headers(referer))
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with self._open(request, timeout) as response, temporary.open("wb") as output:
                total = int(response.headers.get("Content-Length") or 0)
                received = 0
                while True:
                    if cancelled and cancelled():
                        raise CancelledError("操作已取消")
                    chunk = response.read(1024 * 512)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
                    if progress:
                        progress(received, total)
            temporary.replace(destination)
        except urllib.error.HTTPError as exc:
            raise BilibiliError(f"音频下载失败（HTTP {exc.code}）") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise BilibiliError(f"音频下载失败：{getattr(exc, 'reason', exc)}") from exc
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    language: str
    label: str
    url: str
    is_ai: bool = False
    raw: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SubtitleProbe:
    tracks: tuple[SubtitleTrack, ...]
    need_login: bool = False
    ai_without_url: bool = False
    route: str = ""


class BilibiliClient:
    def __init__(self, transport: Transport | None = None) -> None:
        self.transport = transport or UrllibTransport()

    @staticmethod
    def _referer(bvid: str | None = None) -> str:
        if bvid:
            return f"https://www.bilibili.com/video/{bvid}/"
        return "https://www.bilibili.com/"

    def _api_get(self, path: str, params: dict[str, Any], bvid: str | None = None) -> dict[str, Any]:
        query = urllib.parse.urlencode(params)
        payload = self.transport.get_json(
            f"{API_BASE}{path}?{query}",
            referer=self._referer(bvid),
        )
        if int(payload.get("code") or 0) != 0:
            code = payload.get("code")
            message = payload.get("message") or payload.get("msg") or "未知错误"
            if code == -404:
                raise BilibiliError("没有找到这个视频，可能已删除或不可见")
            if code in {-101, -400, -403}:
                raise BilibiliError(f"B站暂不允许访问该内容（{message}）")
            raise BilibiliError(f"B站接口错误 {code}：{message}")
        return payload

    def normalize_source(self, source: str) -> tuple[str, str]:
        value = source.strip()
        if not value:
            raise BilibiliError("请粘贴 B站视频链接或 BV 号")

        bvid = re.search(r"(?i)(BV[0-9A-Za-z]{10,})", value)
        if bvid:
            normalized = "BV" + bvid.group(1)[2:]
            return "bvid", normalized

        aid = re.search(r"(?i)(?:^|/|\b)av(\d+)(?:\b|/|\?|$)", value)
        if aid:
            return "aid", aid.group(1)

        parsed = urllib.parse.urlparse(value if "://" in value else f"https://{value}")
        host = (parsed.hostname or "").lower()
        if host not in ALLOWED_SOURCE_HOSTS:
            raise BilibiliError("只支持 bilibili.com 或 b23.tv 的视频链接")
        if host == "b23.tv":
            resolved = self.transport.resolve_url(parsed.geturl())
            return self.normalize_source(resolved)
        raise BilibiliError("链接中没有可识别的 BV / av 号")

    def fetch_video(self, source: str) -> VideoInfo:
        kind, value = self.normalize_source(source)
        params: dict[str, Any] = {kind: value}
        payload = self._api_get("/x/web-interface/view", params, value if kind == "bvid" else None)
        video = VideoInfo.from_api(payload)
        if not video.bvid or not video.parts:
            raise BilibiliError("视频信息不完整，无法读取分P")
        return video

    @staticmethod
    def _tracks_from_payload(payload: dict[str, Any]) -> tuple[SubtitleTrack, ...]:
        data = payload.get("data") or {}
        raw_tracks = (data.get("subtitle") or {}).get("subtitles") or []
        tracks: list[SubtitleTrack] = []
        seen: set[tuple[str, str]] = set()
        for raw in raw_tracks:
            language = str(raw.get("lan") or raw.get("id_str") or "unknown")
            label = str(raw.get("lan_doc") or language)
            url = str(raw.get("subtitle_url") or raw.get("subtitle_url_v2") or "")
            if url.startswith("//"):
                url = "https:" + url
            is_ai = bool(raw.get("ai_status")) or language.lower().startswith("ai-") or int(raw.get("type") or 0) == 1
            key = (language, url)
            if key in seen:
                continue
            seen.add(key)
            tracks.append(SubtitleTrack(language=language, label=label, url=url, is_ai=is_ai, raw=dict(raw)))
        return tuple(tracks)

    def probe_public_subtitles(self, video: VideoInfo, part: VideoPart) -> SubtitleProbe:
        payload = self._api_get(
            "/x/player/v2",
            {"bvid": video.bvid, "cid": part.cid},
            video.bvid,
        )
        tracks = self._tracks_from_payload(payload)
        data = payload.get("data") or {}
        return SubtitleProbe(
            tracks=tracks,
            need_login=bool(data.get("need_login_subtitle")),
            ai_without_url=any(track.is_ai and not track.url for track in tracks),
            route="public",
        )

    def probe_anonymous_subtitles(self, video: VideoInfo, part: VideoPart) -> SubtitleProbe:
        payload = self._api_get(
            "/x/player/wbi/v2",
            {"bvid": video.bvid, "cid": part.cid, "aid": video.aid},
            video.bvid,
        )
        tracks = self._tracks_from_payload(payload)
        data = payload.get("data") or {}
        return SubtitleProbe(
            tracks=tracks,
            need_login=bool(data.get("need_login_subtitle")),
            ai_without_url=any(track.is_ai and not track.url for track in tracks),
            route="anonymous",
        )

    def probe_subtitles(self, video: VideoInfo, part: VideoPart) -> SubtitleProbe:
        """Compatibility helper that merges public and anonymous probes."""
        public = self.probe_public_subtitles(video, part)
        tracks = list(public.tracks)
        try:
            anonymous = self.probe_anonymous_subtitles(video, part)
        except BilibiliError:
            anonymous = SubtitleProbe((), route="anonymous")
        for track in anonymous.tracks:
            if not any((existing.language, existing.url) == (track.language, track.url) for existing in tracks):
                tracks.append(track)
        return SubtitleProbe(
            tracks=tuple(tracks),
            need_login=public.need_login or anonymous.need_login,
            ai_without_url=any(track.is_ai and not track.url for track in tracks),
            route="merged",
        )

    @staticmethod
    def is_chinese_track(track: SubtitleTrack) -> bool:
        language = track.language.lower().replace("_", "-")
        return language.startswith("zh") or language in {"ai-zh", "zho", "chi"}

    @classmethod
    def rank_subtitles(cls, tracks: tuple[SubtitleTrack, ...]) -> list[SubtitleTrack]:
        def score(track: SubtitleTrack) -> tuple[int, int, str]:
            language = track.language.lower().replace("_", "-")
            priorities = {
                "zh-cn": 0,
                "zh-hans": 1,
                "zh": 2,
                "ai-zh": 3,
                "zh-hant": 4,
                "zh-tw": 5,
                "en": 0,
            }
            if cls.is_chinese_track(track):
                quality_group = 1 if track.is_ai else 0
            else:
                quality_group = 3 if track.is_ai else 2
            return quality_group, priorities.get(language, 20), track.label

        return sorted((track for track in tracks if track.url), key=score)

    @classmethod
    def choose_subtitle(cls, tracks: tuple[SubtitleTrack, ...]) -> SubtitleTrack | None:
        ranked = cls.rank_subtitles(tracks)
        return ranked[0] if ranked else None

    @staticmethod
    def _validate_media_url(url: str) -> str:
        if url.startswith("//"):
            url = "https:" + url
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not any(host.endswith(suffix) for suffix in ALLOWED_MEDIA_SUFFIXES):
            raise BilibiliError("字幕或音频地址不属于可信的 B站域名")
        return url

    def fetch_subtitle_payload(self, video: VideoInfo, track: SubtitleTrack) -> dict[str, Any]:
        url = self._validate_media_url(track.url)
        payload = self.transport.get_json(url, referer=self._referer(video.bvid))
        if not isinstance(payload.get("body"), list):
            raise BilibiliError("字幕数据为空或格式不受支持")
        return payload

    def audio_url(self, video: VideoInfo, part: VideoPart) -> str:
        payload = self._api_get(
            "/x/player/playurl",
            {"bvid": video.bvid, "cid": part.cid, "qn": 16, "fnval": 16, "fourk": 1},
            video.bvid,
        )
        data = payload.get("data") or {}
        audio_tracks = (data.get("dash") or {}).get("audio") or []
        if audio_tracks:
            selected = max(audio_tracks, key=lambda item: int(item.get("bandwidth") or 0))
            url = str(selected.get("baseUrl") or selected.get("base_url") or "")
            if url:
                return self._validate_media_url(url)
        legacy = data.get("durl") or []
        if legacy and legacy[0].get("url"):
            return self._validate_media_url(str(legacy[0]["url"]))
        raise BilibiliError("B站没有返回可用于转写的公开音频")

    def download_audio(
        self,
        video: VideoInfo,
        part: VideoPart,
        destination: Path,
        *,
        progress: Callable[[int, int], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        url = self.audio_url(video, part)
        self.transport.download(
            url,
            destination,
            referer=self._referer(video.bvid),
            progress=progress,
            cancelled=cancelled,
        )
