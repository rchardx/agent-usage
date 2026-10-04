import json

from conftest import claude_usage, codex_meta, codex_usage, write_rows

from agent_usage.index import Index


def test_codex_deltas_duplicate_snapshots_and_copied_logs(paths):
    path = paths.codex / "sessions/a.jsonl"
    first = codex_usage(100, 10)
    second = codex_usage(220, 30, "2026-10-01T00:02:00Z")
    write_rows(path, [codex_meta(), first, first, second])
    write_rows(paths.codex / "archived_sessions/copy.jsonl", [codex_meta(), first, first, second])
    with Index(paths.cache) as index:
        index.sync(paths, ["codex"])
        events = index.events(paths, ["codex"])
    assert len(events) == 2
    assert sum(row["input_tokens"] for row in events) == 220
    assert sum(row["output_tokens"] for row in events) == 30
    assert sum(row["reasoning_tokens"] for row in events) == 15


def test_claude_streaming_blocks_subagents_and_cache_breakdown(paths):
    path = paths.claude / "projects/main.jsonl"
    write_rows(path, [claude_usage(), claude_usage(), claude_usage(output=30)])
    write_rows(paths.claude / "projects/session/subagents/agent.jsonl", [claude_usage("msg-child")])
    with Index(paths.cache) as index:
        index.sync(paths, ["claude"])
        events = index.events(paths, ["claude"])
    assert len(events) == 2
    assert sum(row["input_tokens"] for row in events) == 244
    assert sum(row["output_tokens"] for row in events) == 40
    assert sum(row["cache_write_1h_tokens"] for row in events) == 30
    assert b"PRIVATE-CONVERSATION-SENTINEL" not in (paths.cache / "index.sqlite3").read_bytes()


def test_incremental_append_and_unfinished_last_line(paths):
    path = paths.codex / "sessions/a.jsonl"
    write_rows(path, [codex_meta(), codex_usage(100, 10)])
    second = json.dumps(codex_usage(220, 30, "2026-10-01T00:02:00Z"))
    with path.open("a") as handle:
        handle.write(second[:50])
    with Index(paths.cache) as index:
        report = index.sync(paths, ["codex"])
        assert report["issues"]["pending_bytes"] == 50
        assert len(index.events(paths, ["codex"])) == 1
        with path.open("a") as handle:
            handle.write(second[50:] + "\n")
        appended = index.sync(paths, ["codex"])
        assert appended["bytes_read"] == len(second.encode()) + 1
        assert len(index.events(paths, ["codex"])) == 2
        assert index.sync(paths, ["codex"])["bytes_read"] == 0


def test_file_truncation_replacement_and_deletion(paths):
    path = paths.claude / "projects/main.jsonl"
    write_rows(path, [claude_usage("old-a"), claude_usage("old-b")])
    with Index(paths.cache) as index:
        index.sync(paths, ["claude"])
        write_rows(path, [claude_usage("new-a")])
        index.sync(paths, ["claude"])
        assert len(index.events(paths, ["claude"])) == 1
        replacement = path.with_suffix(".tmp")
        write_rows(replacement, [claude_usage("new-b"), claude_usage("new-c")])
        replacement.replace(path)
        index.sync(paths, ["claude"])
        assert len(index.events(paths, ["claude"])) == 2
        path.unlink()
        index.sync(paths, ["claude"])
        assert index.events(paths, ["claude"]) == []


def test_invalid_lines_are_reported_without_losing_valid_usage(paths):
    path = paths.claude / "projects/main.jsonl"
    write_rows(path, [claude_usage()])
    with path.open("a") as handle:
        handle.write("{bad json}\n")
    with Index(paths.cache) as index:
        report = index.sync(paths, ["claude"])
        assert report["issues"]["invalid_record"] == 1
        assert len(index.events(paths, ["claude"])) == 1
        assert index.sync(paths, ["claude"])["issues"]["invalid_record"] == 1


def test_codex_fork_excludes_replayed_parent_history(paths):
    path = paths.codex / "sessions/child.jsonl"
    current = codex_usage(150, 30, "2026-10-01T00:03:00Z")
    write_rows(
        path,
        [
            codex_meta("child", forked_from_id="parent", timestamp="2026-10-01T00:02:00Z"),
            codex_usage(100, 10),
            current,
        ],
    )
    with Index(paths.cache) as index:
        result = index.sync(paths, ["codex"])
        events = index.events(paths, ["codex"])
    assert result["issues"]["fork_history_excluded"] == 1
    assert len(events) == 1
    assert events[0]["input_tokens"] == 50
    assert events[0]["output_tokens"] == 20


def test_codex_counter_reset_counts_last_request_and_reports_uncertainty(paths):
    path = paths.codex / "sessions/a.jsonl"
    write_rows(
        path, [codex_meta(), codex_usage(100, 10), codex_usage(20, 2, "2026-10-01T00:02:00Z")]
    )
    with Index(paths.cache) as index:
        result = index.sync(paths, ["codex"])
        events = index.events(paths, ["codex"])
    assert sum(e["input_tokens"] for e in events) == 120
    assert result["issues"]["codex_counter_reset_last_request_only"] == 1


def test_missing_reasoning_is_unknown_not_zero(paths):
    path = paths.claude / "projects/main.jsonl"
    write_rows(path, [claude_usage(output_tokens_details={})])
    with Index(paths.cache) as index:
        index.sync(paths, ["claude"])
        events = index.events(paths, ["claude"])
    assert events[0]["reasoning_tokens"] is None


def test_pending_oversized_row_does_not_inflate_issue_count(paths, monkeypatch):
    path = paths.claude / "projects/main.jsonl"
    write_rows(path, [claude_usage()])
    monkeypatch.setattr("agent_usage.index.MAX_LINE_BYTES", 30)
    with Index(paths.cache) as index:
        first = index.sync(paths, ["claude"])
        second = index.sync(paths, ["claude"])
    assert first["issues"]["oversized_line"] == second["issues"]["oversized_line"] == 1
