import errno
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import claude_usage, write_rows
from test_cli import options
from typer.testing import CliRunner

from agent_usage.cache import CacheError, cache_error_message
from agent_usage.cli import app
from agent_usage.index import Index, rebuild_index

runner = CliRunner()
PROVIDERS = ["codex", "claude", "antigravity", "pi", "omp"]


@pytest.fixture(autouse=True)
def no_external_queries(monkeypatch):
    monkeypatch.setattr("agent_usage.cli.shutil.which", lambda _: None)

    def forbidden(*args, **kwargs):
        pytest.fail("Cache recovery must not query accounts or prices")

    monkeypatch.setattr("agent_usage.reports.fetch_codex", forbidden)
    monkeypatch.setattr("agent_usage.reports.fetch_claude", forbidden)
    monkeypatch.setattr("agent_usage.cli.load_pricing", forbidden)


def seed(paths):
    source = paths.claude / "projects/example.jsonl"
    write_rows(source, [claude_usage()])
    with Index(paths.cache) as index:
        index.sync(paths, PROVIDERS)
    return source


def query(paths):
    return runner.invoke(app, [*options(paths), "--no-cost", "tokens", "--all", "--json"])


@pytest.mark.parametrize("damage", ["not-a-database", "schema", "checkpoint"])
def test_damaged_index_is_reported_then_explicitly_rebuilt(paths, damage):
    source = seed(paths)
    database = paths.cache / "index.sqlite3"
    if damage == "not-a-database":
        database.write_bytes(b"synthetic damaged cache")
    else:
        with sqlite3.connect(database) as db:
            if damage == "schema":
                db.execute("PRAGMA user_version=999")
            else:
                db.execute("UPDATE files SET state='not-json'")
        db.close()
    damaged = database.read_bytes()
    result = query(paths)
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "doctor --rebuild-index" in result.stderr
    assert "Traceback" not in result.stderr
    assert database.read_bytes() == damaged

    preserved = {
        source: source.read_bytes(),
        paths.cache / "models-dev-pricing.json": b'{"synthetic": "prices"}',
        paths.cache / "quota-example.json": b'{"synthetic": "quota"}',
        paths.claude / ".credentials.json": b'{"synthetic": "credentials"}',
    }
    for path, data in preserved.items():
        if path != source:
            path.write_bytes(data)
    result = runner.invoke(app, [*options(paths), "doctor", "--rebuild-index", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["doctor"]["index_rebuilt"] is True
    assert result.stderr == ""
    assert all(path.read_bytes() == data for path, data in preserved.items())
    result = query(paths)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["tokens"]
    assert report["totals"]["events"] == 1
    assert report["coverage"]["bytes_read"] == 0
    assert not list(paths.cache.glob(".index-rebuild-*"))
    if os.name != "nt":
        assert database.stat().st_mode & 0o777 == 0o600


def test_rebuild_repairs_early_same_length_edit_followed_by_append(paths):
    source = seed(paths)
    # The changed record lies outside the incremental index's tail anchor.
    source.write_bytes(source.read_bytes() + b" " * 5000 + b"\n")
    query(paths)
    data = source.read_bytes().replace(b'"input_tokens": 2,', b'"input_tokens": 9,')
    source.write_bytes(data + b"\n")
    rebuild_index(paths, PROVIDERS)
    with Index(paths.cache) as index:
        rebuilt = index.events(paths, PROVIDERS)
    with Index(paths.cache / "independent") as index:
        index.sync(paths, PROVIDERS)
        assert index.events(paths, PROVIDERS) == rebuilt
    assert rebuilt[0]["input_tokens"] == 129


@pytest.mark.parametrize("failure", ["scan", "disk", "replace", "interrupt", "unreadable"])
def test_failed_rebuild_preserves_original_and_cleans_staging(paths, monkeypatch, failure):
    seed(paths)
    database = paths.cache / "index.sqlite3"
    original = database.read_bytes()

    def fail(*args, **kwargs):
        if failure == "interrupt":
            raise KeyboardInterrupt
        if failure == "disk":
            raise OSError(errno.ENOSPC, "synthetic disk full")
        if failure == "replace":
            raise PermissionError("synthetic replace denied")
        if failure == "unreadable":
            return {"issues": {"unreadable_file": 1}}
        raise sqlite3.OperationalError("synthetic scan failure")

    target = {"replace": "agent_usage.index.os.replace", "disk": "agent_usage.index.os.fsync"}
    monkeypatch.setattr(target.get(failure, "agent_usage.index.Index.sync"), fail)
    expected = KeyboardInterrupt if failure == "interrupt" else (OSError, sqlite3.Error, CacheError)
    with pytest.raises(expected):
        rebuild_index(paths, PROVIDERS)
    assert database.read_bytes() == original
    assert not list(paths.cache.glob(".index-rebuild-*"))
    with Index(paths.cache):
        pass  # Failed rebuilds must also release the lock.


def test_reader_and_rebuilder_share_the_same_lock(paths):
    seed(paths)
    with Index(paths.cache):
        for command in (["tokens", "--json"], ["doctor", "--rebuild-index", "--json"]):
            result = runner.invoke(app, [*options(paths), "--no-cost", *command])
            assert result.exit_code == 1, result.output
            assert result.stdout == ""
            assert "busy" in result.stderr
            assert "--rebuild-index" not in result.stderr
    assert query(paths).exit_code == 0


def test_lock_excludes_other_processes_and_is_released_after_crash(paths):
    code = """
import os, sys
from pathlib import Path
from agent_usage.cache import CacheError
from agent_usage.index import Index
try:
    index = Index(Path(sys.argv[1]))
except CacheError as exc:
    print(str(exc))
    sys.exit(3)
os._exit(0)  # Simulate termination without Python cleanup.
"""
    command = [sys.executable, "-c", code, str(paths.cache)]
    with Index(paths.cache):
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        assert result.returncode == 3, result.stderr
        assert "busy" in result.stdout
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    with Index(paths.cache):
        pass
    assert (paths.cache / "index.lock").is_file()


def test_external_sqlite_writer_gets_busy_hint_and_releases_tool_lock(paths):
    seed(paths)
    external = sqlite3.connect(paths.cache / "index.sqlite3")
    try:
        external.execute("BEGIN EXCLUSIVE")
        result = query(paths)
        assert result.exit_code == 1, result.output
        assert result.stdout == ""
        assert "busy" in result.stderr
    finally:
        external.rollback()
        external.close()
    assert query(paths).exit_code == 0


def test_readonly_sqlite_failure_preserves_index_and_has_permission_hint(paths, monkeypatch):
    source = seed(paths)
    database = paths.cache / "index.sqlite3"
    original = database.read_bytes()
    write_rows(source, [claude_usage(output=20)])
    connect = sqlite3.connect

    def readonly(path, **kwargs):
        return connect(Path(path).as_uri() + "?mode=ro", uri=True, **kwargs)

    monkeypatch.setattr("agent_usage.index.sqlite3.connect", readonly)
    result = query(paths)
    assert result.exit_code == 1, result.output
    assert result.stdout == ""
    assert "permissions" in result.stderr
    assert database.read_bytes() == original


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_rebuild_does_not_discard_sqlite_recovery_files(paths, suffix):
    seed(paths)
    database = paths.cache / "index.sqlite3"
    before = database.read_bytes()
    sidecar = Path(f"{database}{suffix}")
    sidecar.write_bytes(b"synthetic recovery state")
    with pytest.raises(CacheError, match="recovery files"):
        rebuild_index(paths, PROVIDERS)
    assert database.read_bytes() == before
    assert sidecar.read_bytes() == b"synthetic recovery state"


def test_rebuild_requires_all_providers_before_touching_cache(paths):
    result = runner.invoke(
        app, [*options(paths), "--provider", "claude", "doctor", "--rebuild-index"]
    )
    assert result.exit_code == 2
    assert not paths.cache.exists()


def test_rebuild_of_empty_roots_succeeds_without_inventing_history(paths):
    result = runner.invoke(app, [*options(paths), "doctor", "--rebuild-index", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["doctor"]["coverage"]["files"] == 0
    report = json.loads(query(paths).stdout)["tokens"]
    assert report["totals"]["events"] == 0
    assert report["coverage"]["history_complete"] is False


@pytest.mark.parametrize(
    "error", [PermissionError("private-path"), OSError(errno.ENOSPC, "secret")]
)
def test_storage_errors_are_actionable_and_do_not_leak_raw_details(paths, monkeypatch, error):
    def fail(*args):
        raise error

    monkeypatch.setattr("agent_usage.cli.Index", fail)
    result = query(paths)
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "permissions" in result.stderr or "disk is full" in result.stderr
    assert "private-path" not in result.stderr and "secret" not in result.stderr


@pytest.mark.parametrize(
    ("code", "hint"),
    [
        (sqlite3.SQLITE_BUSY, "busy"),
        (sqlite3.SQLITE_READONLY, "permissions"),
        (sqlite3.SQLITE_FULL, "disk is full"),
        (sqlite3.SQLITE_CORRUPT, "--rebuild-index"),
    ],
)
def test_sqlite_error_codes_have_recovery_hints(code, hint):
    error = sqlite3.OperationalError("do not expose this raw message")
    error.sqlite_errorcode = code
    assert hint in cache_error_message(error)
    assert "raw message" not in cache_error_message(error)


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks may require Windows privileges")
def test_rebuild_refuses_dangling_recovery_symlink(paths, tmp_path):
    seed(paths)
    database = paths.cache / "index.sqlite3"
    original = database.read_bytes()
    sidecar = paths.cache / "index.sqlite3-wal"
    sidecar.symlink_to(tmp_path / "missing-recovery-file")
    with pytest.raises(CacheError, match="recovery files"):
        rebuild_index(paths, PROVIDERS)
    assert sidecar.is_symlink()
    assert database.read_bytes() == original


@pytest.mark.skipif(os.name == "nt", reason="Creating symlinks may require Windows privileges")
def test_rebuild_refuses_symlink_without_touching_target(paths, tmp_path):
    paths.cache.mkdir()
    target = tmp_path / "not-an-index"
    target.write_text("preserve me")
    (paths.cache / "index.sqlite3").symlink_to(target)
    with pytest.raises(CacheError, match="regular file"):
        rebuild_index(paths, PROVIDERS)
    assert target.read_text() == "preserve me"
    assert (paths.cache / "index.sqlite3").is_symlink()
