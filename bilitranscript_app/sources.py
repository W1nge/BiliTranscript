from __future__ import annotations

"""Recognize Bilibili video sources without importing the desktop UI."""

import re


_URL_RE = re.compile(
    r"(?i)(?<![\w.-])(?:https?://)?(?:www\.|m\.)?bilibili\.com/[^\s<>\"'，。；！？、]+"
    r"|(?<![\w.-])(?:https?://)?(?:www\.)?b23\.tv/[^\s<>\"'，。；！？、]+"
)
_BVID_RE = re.compile(r"(?i)\bBV[0-9A-Za-z]{10,}\b")
_AV_RE = re.compile(r"(?i)\bav\d+\b")
_TRAILING_PUNCTUATION = ".,;:!?)]}>\"'，。；：！？、）》」』】》"


def _clean_source(value: str) -> str:
    cleaned = value.strip().rstrip(_TRAILING_PUNCTUATION)
    if cleaned.lower().startswith(("www.", "bilibili.com/", "m.bilibili.com/", "b23.tv/", "www.b23.tv/")):
        return "https://" + cleaned
    return cleaned


def _canonical_source(value: str) -> str:
    cleaned = _clean_source(value)
    bvid = _BVID_RE.search(cleaned)
    if bvid:
        return "BV" + bvid.group(0)[2:]
    av = _AV_RE.search(cleaned)
    if av and (cleaned.lower().startswith("av") or "/av" in cleaned.lower()):
        return "av" + av.group(0)[2:]
    return cleaned


def extract_bilibili_sources(text: str) -> tuple[str, ...]:
    """Find video URLs and IDs in arbitrary text, preserving their order."""
    if not text:
        return ()
    found: list[str] = []
    seen: set[str] = set()
    matches: list[tuple[int, int, str]] = []
    accepted_spans: list[tuple[int, int]] = []

    def add(value: str) -> None:
        source = _canonical_source(value)
        if not source:
            return
        key = source.lower() if source.lower().startswith(("http://", "https://", "av")) else source
        if key not in seen:
            seen.add(key)
            found.append(source)

    for pattern in (_URL_RE, _BVID_RE, _AV_RE):
        matches.extend((match.start(), match.end(), match.group(0)) for match in pattern.finditer(text))
    for start, end, value in sorted(matches, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start >= accepted_start and end <= accepted_end for accepted_start, accepted_end in accepted_spans):
            continue
        accepted_spans.append((start, end))
        add(value)
    return tuple(found)


__all__ = ["extract_bilibili_sources"]
