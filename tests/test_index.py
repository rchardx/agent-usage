import json

from conftest import agent_session, agent_usage, claude_usage, codex_meta, codex_usage, write_rows

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


def test_codex_fork_keeps_child_identity_when_parent_metadata_is_replayed(paths):
    path = paths.codex / "sessions/child.jsonl"
    write_rows(
        path,
        [
            codex_meta("child", forked_from_id="parent", timestamp="2026-10-01T00:02:00Z"),
            codex_meta("parent"),
            codex_usage(100, 10, "2026-10-01T00:03:00Z"),
        ],
    )
    with Index(paths.cache) as index:
        index.sync(paths, ["codex"])
        with path.open("a") as handle:
            handle.write(json.dumps(codex_meta("parent")) + "\n")
            handle.write(json.dumps(codex_usage(220, 30, "2026-10-01T00:04:00Z")) + "\n")
        index.sync(paths, ["codex"])
        events = index.events(paths, ["codex"])
    assert {event["session"] for event in events} == {"child"}
    assert sum(event["input_tokens"] for event in events) == 220


def test_parser_version_change_rebuilds_existing_index(paths):
    path = paths.codex / "sessions/a.jsonl"
    write_rows(path, [codex_meta(), codex_usage(100, 10)])
    with Index(paths.cache) as index:
        index.sync(paths, ["codex"])
        state = json.loads(index.db.execute("SELECT state FROM files").fetchone()[0])
        state["parser_version"] = -1
        index.db.execute("UPDATE files SET state=?", (json.dumps(state),))
        index.db.execute("UPDATE events SET input_tokens=999")
        index.db.commit()
        coverage = index.sync(paths, ["codex"])
        events = index.events(paths, ["codex"])
    assert coverage["files_updated"] == 1
    assert events[0]["input_tokens"] == 100


def test_v1_index_migrates_and_reparses_request_boundaries(paths):
    write_rows(paths.codex / "sessions/a.jsonl", [codex_meta(), codex_usage(100, 10)])
    with Index(paths.cache) as index:
        index.sync(paths, ["codex"])
        state = json.loads(index.db.execute("SELECT state FROM files").fetchone()[0])
        state["parser_version"] = 2
        index.db.execute("UPDATE files SET state=?", (json.dumps(state),))
        index.db.execute("ALTER TABLE events DROP COLUMN request_input_tokens")
        index.db.execute("PRAGMA user_version=1")
        index.db.commit()
    with Index(paths.cache) as index:
        result = index.sync(paths, ["codex"])
        events = index.events(paths, ["codex"])
        assert index.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert result["files_updated"] == 1
    assert len(events) == 1
    assert events[0]["input_tokens"] == events[0]["request_input_tokens"] == 100


def test_codex_aggregate_delta_does_not_claim_a_single_request_size(paths):
    second = codex_usage(220, 30, "2026-10-01T00:02:00Z")
    second["payload"]["info"]["last_token_usage"] = {
        "input_tokens": 50,
        "output_tokens": 5,
    }
    write_rows(paths.codex / "sessions/a.jsonl", [codex_meta(), codex_usage(100, 10), second])
    with Index(paths.cache) as index:
        index.sync(paths, ["codex"])
        events = sorted(index.events(paths, ["codex"]), key=lambda e: e["timestamp"])
    assert [e["input_tokens"] for e in events] == [100, 120]
    assert [e["request_input_tokens"] for e in events] == [100, None]


def test_claude_iterations_do_not_claim_a_single_request_size(paths):
    write_rows(
        paths.claude / "projects/main.jsonl",
        [claude_usage(), claude_usage("iterated", iterations=[{"input_tokens": 2}])],
    )
    with Index(paths.cache) as index:
        index.sync(paths, ["claude"])
        events = index.events(paths, ["claude"])
    assert {e["request_input_tokens"] for e in events} == {122, None}


def test_pi_session_usage_and_sidecar_exclusion(paths):
    root = paths.pi / "sessions/-example"
    session = "2026-10-01T00-00-00-000Z_abc"
    write_rows(root / f"{session}.jsonl", [agent_session(), agent_usage(), agent_usage("resp-b")])
    # Sidecar files next to a session transcript are never transcripts.
    write_rows(root / session / "changes.jsonl", [agent_usage("resp-sidecar")])
    write_rows(root / session / "Forecast.jsonl", [agent_usage("resp-report")])
    with Index(paths.cache) as index:
        index.sync(paths, ["pi"])
        events = index.events(paths, ["pi"])
    assert len(events) == 2
    assert events[0]["provider"] == "pi"
    assert events[0]["session"] == "agent-session"
    assert events[0]["project"] == "/example"
    assert events[0]["input_tokens"] == 14
    assert events[0]["output_tokens"] == 5
    assert events[0]["cache_read_tokens"] == 3
    assert events[0]["cache_write_tokens"] == 1
    assert events[0]["reasoning_tokens"] == 2
    assert events[0]["request_input_tokens"] == 14
    assert events[0]["model"] == "example-upstream/example-model"
    assert b"PRIVATE-CONVERSATION-SENTINEL" not in (paths.cache / "index.sqlite3").read_bytes()


def test_omp_reasoning_tokens_key_and_response_model_preference(paths):
    root = paths.omp / "sessions/-example"
    row = agent_usage(reasoningTokens=4)
    del row["message"]["usage"]["reasoning"]
    row["message"]["responseModel"] = "actual-model"
    write_rows(root / "2026-10-01T00-00-00-000Z_def.jsonl", [agent_session("omp-s"), row])
    with Index(paths.cache) as index:
        index.sync(paths, ["omp"])
        events = index.events(paths, ["omp"])
    assert len(events) == 1
    assert events[0]["provider"] == "omp"
    assert events[0]["reasoning_tokens"] == 4
    assert events[0]["model"] == "example-upstream/actual-model"


def test_pi_zero_usage_and_identity_fallback(paths):
    root = paths.pi / "sessions/-example"
    zero = agent_usage("resp-zero", input=0, output=0, cacheRead=0, cacheWrite=0, reasoning=0)
    no_id = agent_usage("resp-raw")
    del no_id["message"]["responseId"]
    write_rows(root / "2026-10-01T00-00-00-000Z_ghi.jsonl", [zero, no_id])
    with Index(paths.cache) as index:
        index.sync(paths, ["pi"])
        events = index.events(paths, ["pi"])
    assert len(events) == 1
    assert events[0]["session"] == "2026-10-01T00-00-00-000Z_ghi"


def test_streaming_and_copied_iteration_records_keep_request_size_unknown(paths):
    write_rows(
        paths.claude / "projects/main.jsonl",
        [claude_usage(), claude_usage(iterations=[{"input_tokens": 2}])],
    )
    write_rows(paths.claude / "projects/copy.jsonl", [claude_usage()])
    with Index(paths.cache) as index:
        index.sync(paths, ["claude"])
        events = index.events(paths, ["claude"])
    assert len(events) == 1
    assert events[0]["request_input_tokens"] is None
