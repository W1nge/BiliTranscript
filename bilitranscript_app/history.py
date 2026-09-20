from __future__ import annotations

"""Local history database for desktop and LAN API extraction tasks."""

import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PySide6.QtCore import QStandardPaths

from .models import TranscriptBundle


def sanitize_error(message: str, *, secrets: Iterable[str] = ()) -> str:
    """Remove configured credentials/hosts before an error reaches SQLite."""
    value = str(message or "").strip()
    for secret in sorted({str(item) for item in secrets if str(item)}, key=len, reverse=True):
        value = value.replace(secret, "[已隐藏]")
    # History is durable; avoid leaking an upstream ASR host even when a caller
    # forgot to pass its secret list.  API responses still use their own precise
    # sanitizer and retain actionable short messages.
    value = re.sub(r"(?i)https?://[^\s]+", "[地址已隐藏]", value)
    return value[:2000] or "任务失败"


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def history_database_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "BiliTranscript" / "history.sqlite3"
    root = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    if root:
        # Avoid the organization-name suffix so this remains compatible with the
        # documented %LOCALAPPDATA%\BiliTranscript location.
        return Path(root).parent / "BiliTranscript" / "history.sqlite3"
    return Path.home() / "AppData" / "Local" / "BiliTranscript" / "history.sqlite3"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_settings(value: Any) -> dict[str, Any]:
    """Whitelist settings so API/ASR secrets never reach disk."""
    if not isinstance(value, dict):
        return {}
    allowed = {
        "mode",
        "browser_ai",
        "asr_backend",
        "asr_model",
        "language",
        "theme",
        "timestamps",
        "batch_concurrency",
    }
    return {key: value[key] for key in allowed if key in value}


@dataclass(slots=True)
class HistoryEntry:
    id: str
    source_kind: str
    status: str
    source: str
    bvid: str = ""
    title: str = ""
    owner: str = ""
    duration: int = 0
    part_count: int = 0
    character_count: int = 0
    issues_count: int = 0
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    error: str = ""
    export_paths: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    bundle: TranscriptBundle | None = None

    @property
    def has_issues(self) -> bool:
        return bool(self.issues_count or (self.bundle and self.bundle.issues))

    @property
    def is_api(self) -> bool:
        return self.source_kind == "api"

    def summary_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_kind": self.source_kind,
            "status": self.status,
            "source": self.source,
            "bvid": self.bvid,
            "title": self.title,
            "owner": self.owner,
            "duration": self.duration,
            "part_count": self.part_count,
            "character_count": self.character_count,
            "issues_count": self.issues_count,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
            "export_paths": list(self.export_paths),
            "settings": dict(self.settings),
            "has_issues": self.has_issues,
        }


class HistoryStore:
    """SQLite-backed history with WAL and lazy bundle loading."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or history_database_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS history (
                    id TEXT PRIMARY KEY,
                    source_kind TEXT NOT NULL DEFAULT 'home',
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    bvid TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    owner TEXT NOT NULL DEFAULT '',
                    duration INTEGER NOT NULL DEFAULT 0,
                    part_count INTEGER NOT NULL DEFAULT 0,
                    character_count INTEGER NOT NULL DEFAULT 0,
                    issues_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    error TEXT NOT NULL DEFAULT '',
                    export_paths TEXT NOT NULL DEFAULT '[]',
                    settings_json TEXT NOT NULL DEFAULT '{}',
                    bundle_json TEXT
                )
                """
            )
            # Databases created by pre-0.7 development builds may not have the
            # denormalized issue count; migrate them without touching正文 JSON.
            columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(history)").fetchall()}
            if "issues_count" not in columns:
                connection.execute("ALTER TABLE history ADD COLUMN issues_count INTEGER NOT NULL DEFAULT 0")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_history_finished ON history(finished_at DESC, created_at DESC)")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_history_kind ON history(source_kind, finished_at DESC)")
            connection.commit()

    @staticmethod
    def _entry_from_row(row: sqlite3.Row, *, include_bundle: bool = False) -> HistoryEntry:
        bundle = None
        raw_bundle = row["bundle_json"]
        if include_bundle and raw_bundle:
            try:
                bundle = TranscriptBundle.from_json(raw_bundle)
            except (TypeError, ValueError, json.JSONDecodeError):
                bundle = None
        try:
            export_paths = list(json.loads(row["export_paths"] or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            export_paths = []
        try:
            settings = _safe_settings(json.loads(row["settings_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            settings = {}
        return HistoryEntry(
            id=str(row["id"]),
            source_kind=str(row["source_kind"] or "home"),
            status=str(row["status"]),
            source=str(row["source"]),
            bvid=str(row["bvid"] or ""),
            title=str(row["title"] or ""),
            owner=str(row["owner"] or ""),
            duration=int(row["duration"] or 0),
            part_count=int(row["part_count"] or 0),
            character_count=int(row["character_count"] or 0),
            issues_count=int(row["issues_count"] or 0) if "issues_count" in row.keys() else 0,
            created_at=str(row["created_at"] or ""),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            error=str(row["error"] or ""),
            export_paths=[str(item) for item in export_paths],
            settings=settings,
            bundle=bundle,
        )

    def _upsert(self, entry: HistoryEntry) -> None:
        bundle = entry.bundle.to_json() if entry.bundle else None
        video = entry.bundle.video if entry.bundle else None
        bvid = entry.bvid or (video.bvid if video else "")
        title = entry.title or (video.title if video else "")
        owner = entry.owner or (video.owner if video else "")
        duration = entry.duration or (video.duration if video else 0)
        part_count = entry.part_count or (len(entry.bundle.video.parts) if entry.bundle else 0) or (len(entry.bundle.parts) if entry.bundle else 0)
        char_count = entry.character_count or (entry.bundle.character_count if entry.bundle else 0)
        issues_count = entry.issues_count or (len(entry.bundle.issues) if entry.bundle else 0)
        settings = _safe_settings(entry.settings)
        with self._lock, self._connection() as connection:
            connection.execute(
                """
                INSERT INTO history
                (id, source_kind, status, source, bvid, title, owner, duration,
                 part_count, character_count, issues_count, created_at, started_at, finished_at,
                 error, export_paths, settings_json, bundle_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  source_kind=excluded.source_kind, status=excluded.status,
                  source=excluded.source, bvid=excluded.bvid, title=excluded.title,
                  owner=excluded.owner, duration=excluded.duration,
                  part_count=excluded.part_count, character_count=excluded.character_count,
                  issues_count=excluded.issues_count,
                  started_at=excluded.started_at, finished_at=excluded.finished_at,
                  error=excluded.error, export_paths=excluded.export_paths,
                  settings_json=excluded.settings_json, bundle_json=excluded.bundle_json
                """,
                (
                    entry.id,
                    "api" if entry.source_kind == "api" else "home",
                    entry.status,
                    entry.source,
                    bvid,
                    title,
                    owner,
                    int(duration or 0),
                    int(part_count or 0),
                    int(char_count or 0),
                    int(issues_count or 0),
                    entry.created_at or _now_iso(),
                    entry.started_at,
                    entry.finished_at,
                    sanitize_error(entry.error),
                    _json([str(item) for item in entry.export_paths]),
                    _json(settings),
                    bundle,
                ),
            )
            connection.commit()

    def save(self, entry: HistoryEntry) -> HistoryEntry:
        if not entry.id:
            entry.id = uuid.uuid4().hex
        self._upsert(entry)
        return entry

    def save_pending(
        self,
        source: str,
        *,
        source_kind: str = "home",
        entry_id: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> HistoryEntry:
        entry = HistoryEntry(
            id=entry_id or uuid.uuid4().hex,
            source_kind="api" if source_kind == "api" else "home",
            status="queued",
            source=str(source),
            settings=_safe_settings(settings or {}),
        )
        return self.save(entry)

    def mark_running(self, entry_id: str, *, started_at: str | None = None) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                "UPDATE history SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
                (started_at or _now_iso(), str(entry_id)),
            )
            connection.commit()
            return cursor.rowcount > 0

    def save_result(
        self,
        source: str,
        bundle: TranscriptBundle,
        *,
        source_kind: str = "home",
        entry_id: str | None = None,
        settings: dict[str, Any] | None = None,
        export_paths: Iterable[str | Path] = (),
        created_at: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> HistoryEntry:
        entry = HistoryEntry(
            id=entry_id or uuid.uuid4().hex,
            source_kind="api" if source_kind == "api" else "home",
            status="succeeded",
            source=str(source),
            created_at=created_at or bundle.created_at or _now_iso(),
            started_at=started_at,
            finished_at=finished_at or _now_iso(),
            settings=_safe_settings(settings or {}),
            export_paths=[str(item) for item in export_paths],
            bundle=bundle,
        )
        return self.save(entry)

    def save_terminal(
        self,
        source: str,
        status: str,
        *,
        error: str = "",
        source_kind: str = "home",
        entry_id: str | None = None,
        settings: dict[str, Any] | None = None,
        secrets: Iterable[str] = (),
        created_at: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
    ) -> HistoryEntry:
        normalized = status if status in {"failed", "cancelled", "queued", "running"} else "failed"
        return self.save(
            HistoryEntry(
                id=entry_id or uuid.uuid4().hex,
                source_kind="api" if source_kind == "api" else "home",
                status=normalized,
                source=str(source),
                error=sanitize_error(error, secrets=secrets),
                created_at=created_at or _now_iso(),
                started_at=started_at,
                finished_at=finished_at or _now_iso(),
                settings=_safe_settings(settings or {}),
            )
        )

    def get(self, entry_id: str, *, include_bundle: bool = True) -> HistoryEntry | None:
        with self._lock, self._connection() as connection:
            row = connection.execute("SELECT * FROM history WHERE id = ?", (str(entry_id),)).fetchone()
        return self._entry_from_row(row, include_bundle=include_bundle) if row else None

    def list_entries(
        self,
        *,
        source_kind: str = "home",
        query: str = "",
        limit: int = 50,
        offset: int = 0,
        include_bundle: bool = False,
    ) -> list[HistoryEntry]:
        kind = source_kind if source_kind in {"home", "api", "all"} else "home"
        limit = max(1, min(200, int(limit)))
        offset = max(0, int(offset))
        needle = f"%{str(query or '').strip()}%"
        kind_clause = "1=1" if kind == "all" else "source_kind = ?"
        sql = f"SELECT * FROM history WHERE {kind_clause} AND (? = '%%' OR title LIKE ? OR bvid LIKE ? OR source LIKE ?) ORDER BY COALESCE(finished_at, created_at) DESC, created_at DESC LIMIT ? OFFSET ?"
        # Empty query is represented by %% and the first predicate short-circuits.
        params = ((needle, needle, needle, needle, limit, offset) if kind == "all" else (kind, needle, needle, needle, needle, limit, offset))
        with self._lock, self._connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [self._entry_from_row(row, include_bundle=include_bundle) for row in rows]

    def count(self, *, source_kind: str = "home", query: str = "") -> int:
        kind = source_kind if source_kind in {"home", "api", "all"} else "home"
        needle = f"%{str(query or '').strip()}%"
        with self._lock, self._connection() as connection:
            if kind == "all":
                sql = "SELECT COUNT(*) FROM history WHERE (? = '%%' OR title LIKE ? OR bvid LIKE ? OR source LIKE ?)"
                params = (needle, needle, needle, needle)
            else:
                sql = "SELECT COUNT(*) FROM history WHERE source_kind = ? AND (? = '%%' OR title LIKE ? OR bvid LIKE ? OR source LIKE ?)"
                params = (kind, needle, needle, needle, needle)
            return int(connection.execute(sql, params).fetchone()[0])

    def delete(self, entry_id: str) -> bool:
        with self._lock, self._connection() as connection:
            cursor = connection.execute("DELETE FROM history WHERE id = ?", (str(entry_id),))
            connection.commit()
            return cursor.rowcount > 0

    def clear(self, *, source_kind: str | None = None) -> int:
        with self._lock, self._connection() as connection:
            if source_kind in {"home", "api"}:
                cursor = connection.execute("DELETE FROM history WHERE source_kind = ?", (source_kind,))
            else:
                cursor = connection.execute("DELETE FROM history")
            connection.commit()
            removed = int(cursor.rowcount)
            if source_kind not in {"home", "api"}:
                # A full clear should also release the disk space reported by
                # Settings instead of leaving an empty, oversized WAL/database.
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                connection.execute("VACUUM")
            return removed

    def mark_interrupted(self) -> int:
        """Close tasks left non-terminal by a previous process exit."""
        finished_at = _now_iso()
        with self._lock, self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE history
                   SET status = 'failed', finished_at = ?,
                       error = '程序上次运行时中断'
                 WHERE status IN ('queued', 'running')
                """,
                (finished_at,),
            )
            connection.commit()
            return int(cursor.rowcount)

    def storage_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                total += (self.path.parent / f"{self.path.name}{suffix}").stat().st_size
            except OSError:
                pass
        return total
