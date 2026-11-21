"""Incremental, rebuildable SQLite index with per-file parser checkpoints."""

import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import ExitStack
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from .cache import REBUILD_HINT, CacheError, index_lock
from .models import UsageEvent
from .parsers import (
    METRICS,
    PARSER_VERSION,
    parse_antigravity_db,
    parse_claude,
    parse_codex,
    parse_omp,
    parse_pi,
)
from .paths import Paths

SCHEMA_VERSION = 2
MAX_LINE_BYTES = 32 * 1024 * 1024
PARSERS = {"codex": parse_codex, "claude": parse_claude, "pi": parse_pi, "omp": parse_omp}


def _utc_iso(value: datetime) -> tuple[str | None, int]:
    """Convert an aware datetime to UTC ISO text, or classify year-1/9999 overflow."""
    try:
        return value.astimezone(UTC).isoformat(), 0
    except OverflowError:
        offset = value.utcoffset()
        return None, (-1 if offset is not None and offset.total_seconds() > 0 else 1)


class Index:
    def __init__(self, cache: Path):
        self._resources = ExitStack()
        try:
            self._resources.enter_context(index_lock(cache))
            self.path = cache / "index.sqlite3"
            if self.path.is_symlink():
                raise CacheError("Usage index is a symbolic link; choose a different --cache-dir.")
            self.db = sqlite3.connect(self.path, timeout=1)
            self._resources.callback(self.db.close)
            if os.name != "nt":
                self.path.chmod(0o600)
            self._initialize()
        except BaseException:
            self._resources.close()
            raise

    def _initialize(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, SCHEMA_VERSION):
            raise CacheError(f"Unsupported usage index schema. {REBUILD_HINT}")
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
                request_input_tokens INTEGER,
                PRIMARY KEY(path, provider, event_id)
            );
            CREATE INDEX IF NOT EXISTS event_time ON events(timestamp);
            CREATE INDEX IF NOT EXISTS event_identity
                ON events(provider, event_id, path, timestamp);
            CREATE INDEX IF NOT EXISTS event_provider_time
                ON events(provider, timestamp, event_id, path);
        """)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        if version == 1 and "request_input_tokens" not in columns:
            self.db.execute("ALTER TABLE events ADD COLUMN request_input_tokens INTEGER")
        if version != SCHEMA_VERSION:
            self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self.db.commit()

    def close(self):
        self._resources.close()

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
        raw = asdict(event)
        if raw.pop("multi_request"):
            # Negative sentinel marks multi-request/iterated rows so they stay distinct
            # from NULL (pending cache fields that a later streaming update can complete).
            raw["request_input_tokens"] = -1
        data = {"path": path, **raw}
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
            "request_input_tokens=CASE "
            "WHEN COALESCE(events.request_input_tokens,0)<0 "
            "OR COALESCE(excluded.request_input_tokens,0)<0 THEN -1 "
            "WHEN events.request_input_tokens IS NULL THEN excluded.request_input_tokens "
            "WHEN excluded.request_input_tokens IS NULL THEN events.request_input_tokens "
            "ELSE MAX(events.request_input_tokens,excluded.request_input_tokens) END, "
            "timestamp=MIN(events.timestamp,excluded.timestamp)",
            list(data.values()),
        )

    def sync(
        self,
        paths: Paths,
        providers: list[str],
        *,
        files_by_provider: dict[str, list[Path]] | None = None,
    ) -> dict:
        result: dict = {
            "files": 0,
            "files_updated": 0,
            "bytes_read": 0,
            "issues": {},
            "providers": {},
        }
        saved_rows = {
            row[0]: row[1:]
            for row in self.db.execute(
                "SELECT path,identity,offset,mtime_ns,anchor,state,issues FROM files"
            )
        }
        total_issues: Counter = Counter()
        for provider in providers:
            files = (
                files_by_provider[provider]
                if files_by_provider is not None
                else paths.files(provider)
            )
            summary = {
                "files": len(files),
                "root_exists": paths.root_exists(provider),
                "issues": {},
            }
            issues: Counter = Counter()
            surviving: list[Path] = []
            for path in files:
                try:
                    sync_one = (
                        self._sync_sqlite_file if provider == "antigravity" else self._sync_file
                    )
                    updated, read, found = sync_one(path, provider, saved_rows.get(str(path)))
                    result["files_updated"] += updated
                    result["bytes_read"] += read
                    issues.update(found)
                    surviving.append(path)
                except OSError:
                    issues["unreadable_file"] += 1
                    try:
                        if path.is_file():
                            surviving.append(path)
                    except OSError:
                        pass
            if files_by_provider is not None:
                files_by_provider[provider] = surviving
            # Query only discoverable files; deleted logs don't become invisible history.
            summary["issues"] = dict(issues)
            result["providers"][provider] = summary
            result["files"] += len(files)
            total_issues.update(issues)
        result["issues"] = dict(total_issues)
        return result

    @staticmethod
    def _load_checkpoint(saved) -> tuple[dict, dict]:
        if saved is None:
            return {}, {}
        try:
            saved_state, saved_issues = json.loads(saved[4]), json.loads(saved[5])
            if (
                not isinstance(saved_state, dict)
                or not isinstance(saved_issues, dict)
                or any(type(n) is not int or n < 0 for n in saved_issues.values())
                or type(saved[1]) is not int
                or saved[1] < 0
            ):
                raise ValueError("Invalid checkpoint")
            return saved_state, saved_issues
        except (ValueError, TypeError) as exc:
            raise CacheError(f"Usage index checkpoint is damaged. {REBUILD_HINT}") from exc

    @staticmethod
    def _sqlite_anchor(handle, path: Path, size: int, mtime_ns: int) -> tuple[str, int]:
        handle.seek(0)
        header = handle.read(100)
        try:
            wal_stat = Path(f"{path}-wal").stat()
            wal_size, wal_mtime = wal_stat.st_size, wal_stat.st_mtime_ns
        except OSError:
            wal_size, wal_mtime = 0, 0
        payload = header + f"|{size}:{mtime_ns}:{wal_size}:{wal_mtime}".encode()
        return hashlib.sha256(payload).hexdigest(), wal_size

    def _sync_sqlite_file(
        self, path: Path, provider: str, saved: tuple | None = None
    ) -> tuple[int, int, dict]:
        filename = str(path)
        saved_state, saved_issues = self._load_checkpoint(saved)
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            identity = f"{stat.st_dev}:{stat.st_ino}"
            anchor, wal_size = self._sqlite_anchor(handle, path, stat.st_size, stat.st_mtime_ns)
        if (
            saved is not None
            and saved_state.get("parser_version") == PARSER_VERSION
            and saved[0] == identity
            and saved[1] == stat.st_size
            and saved[2] == stat.st_mtime_ns
            and saved[3] == anchor
        ):
            return 0, 0, dict(saved_issues)
        events, issues = parse_antigravity_db(path)
        with self.db:
            self.db.execute("DELETE FROM events WHERE path=?", (filename,))
            for event in events:
                try:
                    self.add(filename, event)
                except OverflowError:
                    issues["invalid_record"] += 1
            self.db.execute(
                "INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?,?,?)",
                (
                    filename,
                    provider,
                    identity,
                    stat.st_size,
                    stat.st_mtime_ns,
                    anchor,
                    json.dumps({"parser_version": PARSER_VERSION}),
                    json.dumps(dict(issues)),
                ),
            )
        return 1, stat.st_size + wal_size, dict(issues)

    def _sync_file(
        self, path: Path, provider: str, saved: tuple | None = None
    ) -> tuple[int, int, dict]:
        filename = str(path)
        saved_state, saved_issues = self._load_checkpoint(saved)
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            identity = f"{stat.st_dev}:{stat.st_ino}"
            offset, state = 0, {"parser_version": PARSER_VERSION}
            issues: Counter[str] = Counter()
            reusable = (
                saved is not None
                and saved_state.get("parser_version") == PARSER_VERSION
                and saved[0] == identity
                and stat.st_size >= saved[1]
                and not (stat.st_size == saved[1] and stat.st_mtime_ns != saved[2])
                and self.anchor(handle, saved[1]) == saved[3]
            )
            if reusable:
                assert saved is not None
                offset, state, issues = (
                    saved[1],
                    dict(saved_state),
                    Counter(saved_issues),
                )
                if stat.st_size == offset and stat.st_mtime_ns == saved[2]:
                    return 0, 0, dict(issues)
            with self.db:
                if not reusable:
                    self.db.execute("DELETE FROM events WHERE path=?", (filename,))
                start = offset
                pending_issues: Counter[str] = Counter()
                handle.seek(offset)
                parse = PARSERS[provider]
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

    def _load_active_files(
        self,
        paths: Paths,
        providers: list[str],
        files_by_provider: dict[str, list[Path]] | None = None,
    ) -> None:
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS active_files(path TEXT PRIMARY KEY)")
        self.db.execute("DELETE FROM active_files")
        active = [
            (str(path),)
            for provider in providers
            for path in (
                files_by_provider[provider]
                if files_by_provider is not None
                else paths.files(provider)
            )
        ]
        if active:
            self.db.executemany("INSERT OR IGNORE INTO active_files VALUES (?)", active)

    @staticmethod
    def _event_columns(alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        # Deduplicate copied/archived transcripts using provider request identities.
        columns = ",".join(f"MAX({prefix}{key}) AS {key}" for key in METRICS)
        columns += (
            f",CASE WHEN MIN(COALESCE({prefix}request_input_tokens,0))<0 THEN NULL "
            f"ELSE MAX({prefix}request_input_tokens) END AS request_input_tokens"
        )
        return columns

    def _fetch_all_active_events(self, providers: list[str]) -> list[dict]:
        if not providers:
            return []
        placeholders = ",".join("?" for _ in providers)
        self.db.row_factory = sqlite3.Row
        try:
            columns = self._event_columns()
            rows = self.db.execute(
                f"SELECT provider,event_id,MIN(session) AS session,MIN(timestamp) AS timestamp,"
                f"MIN(model) AS model,MIN(project) AS project,{columns} "
                "FROM events JOIN active_files USING(path) "
                f"WHERE provider IN ({placeholders}) "
                "GROUP BY provider,event_id",
                providers,
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            self.db.row_factory = None

    def events(
        self,
        paths: Paths,
        providers: list[str],
        *,
        files_by_provider: dict[str, list[Path]] | None = None,
    ) -> list[dict]:
        self._load_active_files(paths, providers, files_by_provider)
        return self._fetch_all_active_events(providers)

    def coverage_events(
        self,
        paths: Paths,
        providers: list[str],
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        files_by_provider: dict[str, list[Path]] | None = None,
    ) -> tuple[list[dict], dict]:
        if not providers:
            return [], {"indexed_events": 0, "earliest_record": None, "latest_record": None}
        self._load_active_files(paths, providers, files_by_provider)
        start_utc: str | None = None
        end_utc: str | None = None
        empty_window = False
        if start is not None:
            start_utc, start_overflow = _utc_iso(start)
            if start_overflow > 0:
                empty_window = True
        if end is not None:
            end_utc, end_overflow = _utc_iso(end)
            if end_overflow < 0:
                empty_window = True
        if not empty_window and start_utc is None:
            all_rows = self._fetch_all_active_events(providers)
            stats = {
                "indexed_events": len(all_rows),
                "earliest_record": min((e["timestamp"] for e in all_rows), default=None),
                "latest_record": max((e["timestamp"] for e in all_rows), default=None),
            }
            if end_utc is None:
                return all_rows, stats
            return [row for row in all_rows if row["timestamp"] < end_utc], stats
        placeholders = ",".join("?" for _ in providers)
        cov_row = self.db.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM ("
            "SELECT MIN(timestamp) AS ts FROM events JOIN active_files USING(path) "
            f"WHERE provider IN ({placeholders}) "
            "GROUP BY provider, event_id)",
            providers,
        ).fetchone()
        indexed_count, earliest, latest = cov_row if cov_row else (0, None, None)
        stats = {
            "indexed_events": indexed_count,
            "earliest_record": earliest,
            "latest_record": latest,
        }
        if not indexed_count or empty_window:
            return [], stats
        assert start_utc is not None
        self.db.row_factory = sqlite3.Row
        try:
            columns = self._event_columns("e")
            if end_utc is not None:
                rows = self.db.execute(
                    "WITH candidates AS ("
                    "SELECT DISTINCT e0.provider, e0.event_id "
                    "FROM events e0 JOIN active_files USING(path) "
                    f"WHERE e0.provider IN ({placeholders}) "
                    "AND e0.timestamp >= ? AND e0.timestamp < ?"
                    ") "
                    "SELECT e.provider,e.event_id,MIN(e.session) AS session,"
                    "MIN(e.timestamp) AS timestamp,MIN(e.model) AS model,"
                    f"MIN(e.project) AS project,{columns} "
                    "FROM candidates JOIN events e USING(provider,event_id) "
                    "JOIN active_files USING(path) "
                    "GROUP BY e.provider,e.event_id "
                    "HAVING MIN(e.timestamp) >= ? AND MIN(e.timestamp) < ?",
                    (*providers, start_utc, end_utc, start_utc, end_utc),
                ).fetchall()
            else:
                rows = self.db.execute(
                    "WITH candidates AS ("
                    "SELECT DISTINCT e0.provider, e0.event_id "
                    "FROM events e0 JOIN active_files USING(path) "
                    f"WHERE e0.provider IN ({placeholders}) AND e0.timestamp >= ?"
                    ") "
                    "SELECT e.provider,e.event_id,MIN(e.session) AS session,"
                    "MIN(e.timestamp) AS timestamp,MIN(e.model) AS model,"
                    f"MIN(e.project) AS project,{columns} "
                    "FROM candidates JOIN events e USING(provider,event_id) "
                    "JOIN active_files USING(path) "
                    "GROUP BY e.provider,e.event_id "
                    "HAVING MIN(e.timestamp) >= ?",
                    (*providers, start_utc, start_utc),
                ).fetchall()
            return [dict(row) for row in rows], stats
        finally:
            self.db.row_factory = None


def rebuild_index(paths: Paths, providers: list[str]) -> dict:
    """Build separately and replace only the derived index after a successful scan."""
    with index_lock(paths.cache):
        target = paths.cache / "index.sqlite3"
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise CacheError("Usage index is not a regular file; choose a different --cache-dir.")
        # Never remove SQLite recovery files or replace a database with pending WAL data.
        # Older tool versions and external SQLite clients do not honor index.lock.
        if any(os.path.lexists(f"{target}{suffix}") for suffix in ("-wal", "-shm", "-journal")):
            raise CacheError(
                "SQLite recovery files are present. Close other processes and try a normal "
                "token query first, or choose a new --cache-dir. No index was replaced."
            )
        with TemporaryDirectory(prefix=".index-rebuild-", dir=paths.cache) as directory:
            with Index(Path(directory)) as staged:
                coverage = staged.sync(paths, providers)
                if coverage["issues"].get("unreadable_file"):
                    raise CacheError(
                        "Rebuild encountered unreadable logs. Check source permissions and retry. "
                        "No index was replaced."
                    )
                if staged.db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise CacheError(
                        "Rebuilt index failed its integrity check. No index was replaced."
                    )
                staged_path = staged.path
            # Flush the completed database before the atomic, same-filesystem replacement.
            with staged_path.open("rb+") as handle:
                os.fsync(handle.fileno())
            os.replace(staged_path, target)
        return coverage
