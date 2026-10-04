"""Incremental, rebuildable SQLite index with per-file parser checkpoints."""

import hashlib
import json
import os
import sqlite3
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .models import UsageEvent
from .parsers import METRICS, parse_claude, parse_codex
from .paths import Paths

SCHEMA_VERSION = 1
MAX_LINE_BYTES = 32 * 1024 * 1024


class Index:
    def __init__(self, cache: Path):
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = cache / "index.sqlite3"
        self.db = sqlite3.connect(self.path, timeout=30)
        if os.name != "nt":
            self.path.chmod(0o600)
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            self.db.close()
            raise ValueError("Unsupported cache schema; use a new --cache-dir.")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, provider TEXT NOT NULL, identity TEXT NOT NULL,
                offset INTEGER NOT NULL, mtime_ns INTEGER NOT NULL, anchor TEXT NOT NULL,
                state TEXT NOT NULL, issues TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                path TEXT NOT NULL, provider TEXT NOT NULL, event_id TEXT NOT NULL,
                session TEXT NOT NULL, timestamp TEXT NOT NULL, model TEXT NOT NULL,
                project TEXT NOT NULL, input_tokens INTEGER NOT NULL,
                output_tokens INTEGER NOT NULL,
                cache_read_tokens INTEGER, cache_write_tokens INTEGER, reasoning_tokens INTEGER,
                cache_write_5m_tokens INTEGER, cache_write_1h_tokens INTEGER,
                PRIMARY KEY(path, provider, event_id)
            );
            CREATE INDEX IF NOT EXISTS event_time ON events(timestamp);
        """)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def anchor(handle, offset: int) -> str:
        start = max(0, offset - 4096)
        handle.seek(start)
        data = handle.read(offset - start)
        handle.seek(offset)
        return hashlib.sha256(data).hexdigest()

    def add(self, path: str, event: UsageEvent):
        data = {"path": path, **asdict(event)}
        fields = ",".join(data)
        placeholders = ",".join("?" for _ in data)
        # Streaming rows may update a message's counts. Keep monotonic maxima per component.
        updates = ",".join(
            f"{key}=CASE WHEN events.{key} IS NULL THEN excluded.{key} "
            f"WHEN excluded.{key} IS NULL THEN events.{key} "
            f"ELSE MAX(events.{key},excluded.{key}) END"
            for key in METRICS
        )
        self.db.execute(
            f"INSERT INTO events ({fields}) VALUES ({placeholders}) "
            f"ON CONFLICT(path,provider,event_id) DO UPDATE SET {updates}, "
            "timestamp=MIN(events.timestamp,excluded.timestamp)",
            list(data.values()),
        )

    def sync(self, paths: Paths, providers: list[str]) -> dict:
        result = {"files": 0, "files_updated": 0, "bytes_read": 0, "issues": {}, "providers": {}}
        total_issues: Counter = Counter()
        for provider in providers:
            files = paths.files(provider)
            root = paths.codex if provider == "codex" else paths.claude
            summary = {"files": len(files), "root_exists": root.is_dir(), "issues": {}}
            issues: Counter = Counter()
            for path in files:
                try:
                    updated, read, found = self._sync_file(path, provider)
                    result["files_updated"] += updated
                    result["bytes_read"] += read
                    issues.update(found)
                except OSError:
                    issues["unreadable_file"] += 1
            # Query only currently discoverable files; deleted logs don't become invisible history.
            summary["issues"] = dict(issues)
            result["providers"][provider] = summary
            result["files"] += len(files)
            total_issues.update(issues)
        result["issues"] = dict(total_issues)
        return result

    def _sync_file(self, path: Path, provider: str) -> tuple[int, int, dict]:
        filename = str(path)
        saved = self.db.execute(
            "SELECT identity,offset,mtime_ns,anchor,state,issues FROM files WHERE path=?",
            (filename,),
        ).fetchone()
        with path.open("rb") as handle, self.db:
            stat = os.fstat(handle.fileno())
            identity = f"{stat.st_dev}:{stat.st_ino}"
            offset, state, issues = 0, {}, Counter()
            reusable = (
                saved is not None
                and saved[0] == identity
                and stat.st_size >= saved[1]
                and self.anchor(handle, saved[1]) == saved[3]
                and not (stat.st_size == saved[1] and stat.st_mtime_ns != saved[2])
            )
            if reusable:
                offset, state, issues = (
                    saved[1],
                    json.loads(saved[4]),
                    Counter(json.loads(saved[5])),
                )
                if stat.st_size == offset and stat.st_mtime_ns == saved[2]:
                    return 0, 0, dict(issues)
            else:
                self.db.execute("DELETE FROM events WHERE path=?", (filename,))
            start = offset
            pending_issues: Counter = Counter()
            handle.seek(offset)
            parse = parse_codex if provider == "codex" else parse_claude
            while handle.tell() < stat.st_size:
                position = handle.tell()
                line = handle.readline(min(MAX_LINE_BYTES + 1, stat.st_size - position))
                if len(line) > MAX_LINE_BYTES:
                    pending_issues["oversized_line"] = 1
                    # Leave this row pending instead of holding unbounded transcript text.
                    handle.seek(position)
                    break
                if not line.endswith(b"\n"):
                    # A concurrent writer may not have finished the final JSON record.
                    handle.seek(position)
                    break
                offset = handle.tell()
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("Expected object")
                    event, warning = parse(row, state, path.stem)
                    if warning:
                        issues[warning] += 1
                    if event is not None:
                        self.add(filename, event)
                except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
                    issues["invalid_record"] += 1
            anchor = self.anchor(handle, offset)
            self.db.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)",
                (
                    filename,
                    provider,
                    identity,
                    offset,
                    stat.st_mtime_ns,
                    anchor,
                    json.dumps(state),
                    json.dumps(issues),
                ),
            )
            pending = stat.st_size - offset
            reported = dict(issues)
            reported.update(pending_issues)
            if pending:
                reported["pending_bytes"] = pending
            return 1, offset - start, reported

    def events(self, paths: Paths, providers: list[str]) -> list[dict]:
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS active_files(path TEXT PRIMARY KEY)")
        self.db.execute("DELETE FROM active_files")
        self.db.executemany(
            "INSERT OR IGNORE INTO active_files VALUES (?)",
            [(str(path),) for provider in providers for path in paths.files(provider)],
        )
        self.db.row_factory = sqlite3.Row
        # Deduplicate copied/archived transcripts using provider request identities.
        columns = ",".join(f"MAX({key}) AS {key}" for key in METRICS)
        rows = self.db.execute(
            f"SELECT provider,event_id,MIN(session) AS session,MIN(timestamp) AS timestamp,"
            f"MIN(model) AS model,MIN(project) AS project,{columns} "
            "FROM events JOIN active_files USING(path) GROUP BY provider,event_id"
        ).fetchall()
        self.db.row_factory = None
        return [dict(row) for row in rows]
