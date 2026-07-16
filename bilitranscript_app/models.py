from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable


def format_duration(seconds: float | int | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_clock(seconds: float | int | None) -> str:
    total = max(0, int(float(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_srt_clock(seconds: float | int | None) -> str:
    total_ms = max(0, int(round(float(seconds or 0) * 1000)))
    millis = total_ms % 1000
    total_seconds = total_ms // 1000
    secs = total_seconds % 60
    total_minutes = total_seconds // 60
    minutes = total_minutes % 60
    hours = total_minutes // 60
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def safe_filename(value: str, fallback: str = "B站文稿") -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value).strip(" ._")
    return (cleaned[:120] or fallback).rstrip(" .")


@dataclass(frozen=True, slots=True)
class VideoPart:
    page: int
    cid: int
    title: str
    duration: int = 0

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "VideoPart":
        return cls(
            page=int(payload.get("page") or 1),
            cid=int(payload.get("cid") or 0),
            title=str(payload.get("part") or f"P{payload.get('page') or 1}"),
            duration=int(payload.get("duration") or 0),
        )


@dataclass(frozen=True, slots=True)
class VideoInfo:
    bvid: str
    aid: int
    title: str
    owner: str
    duration: int
    cover_url: str
    published_at: int
    description: str
    parts: tuple[VideoPart, ...]

    @property
    def url(self) -> str:
        return f"https://www.bilibili.com/video/{self.bvid}/"

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> "VideoInfo":
        data = payload.get("data") or payload
        pages = tuple(VideoPart.from_api(item) for item in (data.get("pages") or []))
        if not pages and data.get("cid"):
            pages = (
                VideoPart(
                    page=1,
                    cid=int(data["cid"]),
                    title=str(data.get("title") or "P1"),
                    duration=int(data.get("duration") or 0),
                ),
            )
        return cls(
            bvid=str(data.get("bvid") or ""),
            aid=int(data.get("aid") or 0),
            title=str(data.get("title") or "未命名视频"),
            owner=str((data.get("owner") or {}).get("name") or "未知 UP 主"),
            duration=int(data.get("duration") or 0),
            cover_url=str(data.get("pic") or ""),
            published_at=int(data.get("pubdate") or 0),
            description=str(data.get("desc") or ""),
            parts=pages,
        )


@dataclass(frozen=True, slots=True)
class Segment:
    start: float
    end: float
    text: str

    def normalized(self) -> "Segment":
        start = max(0.0, float(self.start or 0))
        end = max(start + 0.05, float(self.end or start + 1))
        text = re.sub(r"\s+", " ", str(self.text or "")).strip()
        return Segment(start=start, end=end, text=text)


@dataclass(frozen=True, slots=True)
class PartTranscript:
    part: VideoPart
    source: str
    language: str
    segments: tuple[Segment, ...]

    @property
    def text(self) -> str:
        return "\n".join(segment.text for segment in self.segments if segment.text).strip()

    @property
    def character_count(self) -> int:
        return len(re.sub(r"\s+", "", self.text))

    @classmethod
    def from_subtitle_payload(
        cls,
        part: VideoPart,
        payload: dict[str, Any],
        *,
        source: str,
        language: str,
    ) -> "PartTranscript":
        segments: list[Segment] = []
        for item in payload.get("body") or []:
            text = str(item.get("content") or "").strip()
            if not text:
                continue
            segment = Segment(
                start=float(item.get("from") or 0),
                end=float(item.get("to") or (float(item.get("from") or 0) + 1)),
                text=text,
            ).normalized()
            segments.append(segment)
        return cls(part=part, source=source, language=language, segments=tuple(segments))


@dataclass(frozen=True, slots=True)
class ExtractionIssue:
    page: int
    title: str
    message: str


@dataclass(frozen=True, slots=True)
class RouteAvailability:
    route: str
    available: bool
    detail: str
    attempts: int = 0
    track_count: int = 0


@dataclass(frozen=True, slots=True)
class PartAvailability:
    part: VideoPart
    routes: tuple[RouteAvailability, ...]

    def get(self, route: str) -> RouteAvailability | None:
        return next((item for item in self.routes if item.route == route), None)


@dataclass(frozen=True, slots=True)
class AvailabilityReport:
    video: VideoInfo
    parts: tuple[PartAvailability, ...]

    def for_page(self, page: int) -> PartAvailability | None:
        return next((item for item in self.parts if item.part.page == page), None)


@dataclass(slots=True)
class TranscriptBundle:
    video: VideoInfo
    parts: list[PartTranscript] = field(default_factory=list)
    issues: list[ExtractionIssue] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))

    @property
    def character_count(self) -> int:
        return sum(part.character_count for part in self.parts)

    @property
    def segment_count(self) -> int:
        return sum(len(part.segments) for part in self.parts)

    def _part_lines(self, part: PartTranscript, timestamps: bool) -> list[str]:
        if timestamps:
            return [f"[{format_clock(segment.start)}] {segment.text}" for segment in part.segments if segment.text]
        return [segment.text for segment in part.segments if segment.text]

    def to_text(self, timestamps: bool = False) -> str:
        lines: list[str] = []
        show_headings = len(self.parts) > 1
        for index, transcript in enumerate(self.parts):
            if index:
                lines.append("")
            if show_headings:
                lines.extend([f"P{transcript.part.page} · {transcript.part.title}", ""])
            lines.extend(self._part_lines(transcript, timestamps))
        return "\n".join(lines).strip() + ("\n" if lines else "")

    def to_markdown(self, timestamps: bool = False) -> str:
        lines = [
            f"# {self.video.title}",
            "",
            f"- 来源：[{self.video.bvid}]({self.video.url})",
            f"- UP 主：{self.video.owner}",
            f"- 文稿来源：{', '.join(dict.fromkeys(part.source for part in self.parts)) or '无'}",
            f"- 提取时间：{self.created_at}",
            "",
        ]
        for transcript in self.parts:
            if len(self.video.parts) > 1 or len(self.parts) > 1:
                lines.extend(
                    [
                        f"## P{transcript.part.page} · {transcript.part.title}",
                        "",
                        f"> {transcript.source} · {format_duration(transcript.part.duration)}",
                        "",
                    ]
                )
            lines.extend(self._part_lines(transcript, timestamps))
            lines.append("")
        if self.issues:
            lines.extend(["## 未提取的分P", ""])
            for issue in self.issues:
                lines.append(f"- P{issue.page} · {issue.title}：{issue.message}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def to_srt(self) -> str:
        blocks: list[str] = []
        index = 1
        offset = 0.0
        for transcript in self.parts:
            last_end = 0.0
            for segment in transcript.segments:
                normalized = segment.normalized()
                if not normalized.text:
                    continue
                blocks.append(
                    "\n".join(
                        [
                            str(index),
                            f"{format_srt_clock(offset + normalized.start)} --> {format_srt_clock(offset + normalized.end)}",
                            normalized.text,
                        ]
                    )
                )
                index += 1
                last_end = max(last_end, normalized.end)
            offset += max(float(transcript.part.duration or 0), last_end)
        return "\n\n".join(blocks).rstrip() + ("\n" if blocks else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "created_at": self.created_at,
            "video": {
                "bvid": self.video.bvid,
                "aid": self.video.aid,
                "title": self.video.title,
                "owner": self.video.owner,
                "duration": self.video.duration,
                "url": self.video.url,
            },
            "parts": [
                {
                    "page": transcript.part.page,
                    "cid": transcript.part.cid,
                    "title": transcript.part.title,
                    "duration": transcript.part.duration,
                    "source": transcript.source,
                    "language": transcript.language,
                    "segments": [asdict(segment.normalized()) for segment in transcript.segments],
                    "text": transcript.text,
                }
                for transcript in self.parts
            ],
            "issues": [asdict(issue) for issue in self.issues],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"


def combine_segments(items: Iterable[dict[str, Any]]) -> tuple[Segment, ...]:
    segments: list[Segment] = []
    for item in items:
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        segments.append(
            Segment(
                start=float(item.get("start") or 0),
                end=float(item.get("end") or (float(item.get("start") or 0) + 1)),
                text=text,
            ).normalized()
        )
    return tuple(segments)
