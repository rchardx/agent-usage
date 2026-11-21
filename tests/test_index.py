import json
import os
import sqlite3
from pathlib import Path

import pytest
from conftest import (
    agent_session,
    agent_usage,
    antigravity_gen_blob,
    antigravity_step_blob,
    antigravity_usage_blob,
    claude_usage,
    codex_meta,
    codex_usage,
    write_antigravity_db,
    write_rows,
)

from agent_usage.index import Index
from agent_usage.paths import Paths


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
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(second[:50])
    with Index(paths.cache) as index:
        report = index.sync(paths, ["codex"])
        assert report["issues"]["pending_bytes"] == 50
        assert len(index.events(paths, ["codex"])) == 1
        with path.open("a", encoding="utf-8", newline="\n") as handle:
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


def test_codex_custom_model_provider_and_default_openai_namespacing(paths):
    gemini_turn = {
        "type": "turn_context",
        "payload": {"cwd": "/example", "model": "models/gemini-2.5-pro"},
    }
    openai_turn = {"type": "turn_context", "payload": {"cwd": "/example", "model": "gpt-5.4"}}
    write_rows(
        paths.codex / "sessions/gemini.jsonl",
        [codex_meta("gem-s", model_provider="google"), gemini_turn, codex_usage(100, 20)],
    )
    write_rows(
        paths.codex / "sessions/openai.jsonl",
        [codex_meta("oai-s", model_provider="openai"), openai_turn, codex_usage(80, 10)],
    )
    with Index(paths.cache) as index:
        index.sync(paths, ["codex"])
        by_session = {e["session"]: e for e in index.events(paths, ["codex"])}
    assert by_session["gem-s"]["model"] == "google/gemini-2.5-pro"
    assert by_session["oai-s"]["model"] == "gpt-5.4"


def test_pi_and_omp_preserve_gateway_prefix_on_slashed_models_and_strip_models_prefix(paths):
    openrouter_row = agent_usage("resp-or", cacheWrite=0)
    openrouter_row["message"]["provider"] = "openrouter"
    openrouter_row["message"]["model"] = "google/gemini-2.5-pro"

    vertex_change = {
        "type": "model_change",
        "provider": "google-vertex",
        "modelId": "gemini-3.1-pro-preview",
    }
    inherited_row = agent_usage("resp-vtx", cacheWrite=0)
    del inherited_row["message"]["provider"]
    del inherited_row["message"]["model"]

    overridden_provider_row = agent_usage("resp-ovr", cacheWrite=0)
    overridden_provider_row["message"]["provider"] = "google"
    del overridden_provider_row["message"]["model"]

    write_rows(
        paths.pi / "sessions/-example/2026-10-01T00-00-00-000Z_gem.jsonl",
        [
            agent_session("pi-gem"),
            openrouter_row,
            vertex_change,
            inherited_row,
            overridden_provider_row,
        ],
    )

    omp_row = agent_usage("resp-omp", cacheWrite=0)
    omp_row["message"]["provider"] = "google"
    omp_row["message"]["model"] = "gemini-2.5-flash"
    omp_row["message"]["responseModel"] = "models/gemini-2.5-flash"
    write_rows(
        paths.omp / "sessions/-example/2026-10-01T00-00-00-000Z_omp.jsonl",
        [agent_session("omp-gem"), omp_row],
    )

    with Index(paths.cache) as index:
        index.sync(paths, ["pi", "omp"])
        events = index.events(paths, ["pi", "omp"])
    models = {(e["provider"], e["model"]) for e in events}
    assert models == {
        ("pi", "openrouter/google/gemini-2.5-pro"),
        ("pi", "google-vertex/gemini-3.1-pro-preview"),
        ("pi", "google/gemini-3.1-pro-preview"),
        ("omp", "google/gemini-2.5-flash"),
    }


def test_claude_gateway_omitted_cache_fields_keep_unknowns_without_dropping_event(paths):
    gateway_row = claude_usage("msg-gw")
    gateway_row["message"]["model"] = "models/gemini-2.5-pro"
    gateway_row["message"]["usage"] = {"input_tokens": 50, "output_tokens": 12}

    missing_core = claude_usage("msg-bad")
    missing_core["message"]["usage"] = {"input_tokens": 50}

    write_rows(paths.claude / "projects/gateway.jsonl", [gateway_row, missing_core])
    with Index(paths.cache) as index:
        coverage = index.sync(paths, ["claude"])
        events = index.events(paths, ["claude"])
    assert coverage["issues"] == {"claude_incomplete_usage": 1}
    assert len(events) == 1
    assert events[0]["model"] == "gemini-2.5-pro"
    assert events[0]["input_tokens"] == 50
    assert events[0]["output_tokens"] == 12
    assert events[0]["cache_read_tokens"] is None
    assert events[0]["cache_write_tokens"] is None
    assert events[0]["request_input_tokens"] is None


def test_claude_streaming_completion_recovers_request_size_unless_iterated(paths):
    from test_pricing import gemini_catalog

    partial = claude_usage("msg-stream")
    partial["message"]["model"] = "models/gemini-2.5-pro"
    partial["message"]["usage"] = {"input_tokens": 200_000, "output_tokens": 10}

    complete = claude_usage("msg-stream", output=50)
    complete["message"]["model"] = "models/gemini-2.5-pro"
    complete["message"]["usage"] = {
        "input_tokens": 200_000,
        "cache_read_input_tokens": 100_000,
        "cache_creation_input_tokens": 0,
        "output_tokens": 50,
    }

    iterated_partial = claude_usage("msg-iter")
    iterated_partial["requestId"] = "req-iter"
    iterated_partial["message"]["usage"] = {
        "input_tokens": 50,
        "output_tokens": 5,
        "iterations": [{"input_tokens": 25}],
    }
    iterated_complete = claude_usage("msg-iter", output=20)
    iterated_complete["requestId"] = "req-iter"

    write_rows(
        paths.claude / "projects/stream.jsonl",
        [partial, complete, iterated_partial, iterated_complete],
    )
    with Index(paths.cache) as index:
        index.sync(paths, ["claude"])
        by_msg = {e["output_tokens"]: e for e in index.events(paths, ["claude"])}

    recovered = by_msg[50]
    assert recovered["request_input_tokens"] == 300_000
    assert gemini_catalog().estimate(recovered)["amount"] is not None

    # A row with iterations stays disqualified even after a later row supplies cache fields.
    assert by_msg[20]["request_input_tokens"] is None


def test_parser_v3_checkpoint_rebuilds_for_v4_model_namespaces(paths):
    row = agent_usage("resp-or", cacheWrite=0)
    row["message"]["provider"] = "openrouter"
    row["message"]["model"] = "google/gemini-2.5-pro"
    write_rows(
        paths.pi / "sessions/-example/2026-10-01T00-00-00-000Z_v3.jsonl",
        [agent_session("pi-v3"), row],
    )
    with Index(paths.cache) as index:
        index.sync(paths, ["pi"])
        state = json.loads(index.db.execute("SELECT state FROM files").fetchone()[0])
        state["parser_version"] = 3
        index.db.execute("UPDATE files SET state=?", (json.dumps(state),))
        index.db.execute("UPDATE events SET model='google/gemini-2.5-pro'")
        index.db.commit()
        coverage = index.sync(paths, ["pi"])
        events = index.events(paths, ["pi"])
    assert coverage["files_updated"] == 1
    assert events[0]["model"] == "openrouter/google/gemini-2.5-pro"


def test_antigravity_sqlite_reads_gen_metadata_step_timestamps_and_keeps_missing_unknown(paths):
    db_path = paths.antigravity / "conversations/session-alpha.db"
    primary = antigravity_usage_blob(
        fresh_input=40,
        cache_read=60,
        cache_write=5,
        total_output=12,
        visible_output=8,
        reasoning=4,
        response_id="resp-shared",
    )
    distinct_retry = antigravity_usage_blob(
        fresh_input=10,
        cache_read=0,
        cache_write=0,
        total_output=3,
        visible_output=3,
        reasoning=0,
        response_id="resp-retry",
    )
    missing_model_usage = antigravity_usage_blob(
        fresh_input=15,
        cache_read=25,
        cache_write=0,
        total_output=6,
        visible_output=4,
        reasoning=2,
        response_id=None,
    )
    undated_usage = antigravity_usage_blob(
        fresh_input=9,
        cache_read=0,
        cache_write=0,
        total_output=2,
        visible_output=2,
        reasoning=0,
        response_id="resp-undated",
    )
    write_antigravity_db(
        db_path,
        generations=[
            (
                0,
                antigravity_gen_blob(
                    model="gemini-3.8-flash",
                    display_model="Gemini 3.8 Flash (High)",
                    usage=primary,
                    # Primary mirror in retry_infos must be dropped; distinct retry is retained.
                    retries=[primary, distinct_retry],
                    timestamp=None,
                    step_indices=[1],
                ),
            ),
            (
                1,
                antigravity_gen_blob(
                    model=None,
                    usage=missing_model_usage,
                    timestamp=(1_790_812_920, 0),
                    step_indices=[2],
                ),
            ),
            (
                2,
                antigravity_gen_blob(
                    model="claude-opus-5-5-medium",
                    display_model="Claude Opus 5.5 (Medium)",
                    usage=undated_usage,
                    timestamp=None,
                    step_indices=[99],
                ),
            ),
        ],
        steps=[
            (
                1,
                antigravity_step_blob(
                    model=None,
                    usage=primary,
                    timestamp=(1_790_812_860, 0),
                ),
            ),
        ],
        # Trajectory creation timestamp must NOT be guessed onto undated generation 2.
        trajectories=[(1_790_812_800, 0)],
    )

    # Non-conversation summary DB and empty DBs must be ignored cleanly.
    with sqlite3.connect(paths.antigravity / "conversations/summary.db") as summary:
        summary.execute("CREATE TABLE summaries (id TEXT)")
    summary.close()
    (paths.antigravity / "conversations/empty.db").write_bytes(b"")

    with Index(paths.cache) as index:
        first_cov = index.sync(paths, ["antigravity"])
        events = sorted(
            index.events(paths, ["antigravity"]), key=lambda e: (e["timestamp"], e["input_tokens"])
        )
        second_cov = index.sync(paths, ["antigravity"])

    assert first_cov["files"] == 3
    assert first_cov["issues"] == {"missing_timestamp": 1}
    assert second_cov["bytes_read"] == 0
    assert len(events) == 3
    assert b"PRIVATE-CONVERSATION-SENTINEL" not in (paths.cache / "index.sqlite3").read_bytes()

    by_input = {e["input_tokens"]: e for e in events}
    assert by_input[105]["model"] == "gemini-3.8-flash-high"
    assert by_input[105]["output_tokens"] == 12
    assert by_input[105]["cache_read_tokens"] == 60
    assert by_input[105]["cache_write_tokens"] == 5
    assert by_input[105]["reasoning_tokens"] == 4
    assert by_input[105]["request_input_tokens"] == 105
    assert by_input[105]["session"] == "session-alpha"
    assert by_input[105]["project"] == "unknown"
    assert by_input[105]["timestamp"] == "2026-10-01T00:01:00+00:00"

    # Missing model stays "unknown" rather than inheriting another row's model.
    assert by_input[40]["model"] == "unknown"
    assert by_input[10]["output_tokens"] == 3


def test_antigravity_multi_root_deduplication_and_in_place_sqlite_updates(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    discovered = Paths.discover(
        codex=tmp_path / "codex",
        claude=tmp_path / "claude",
        pi=tmp_path / "pi",
        omp=tmp_path / "omp",
        cache=tmp_path / "cache",
    )

    cli_root = tmp_path / ".gemini/antigravity-cli"
    app_root = tmp_path / ".gemini/antigravity"
    ide_root = tmp_path / ".gemini/antigravity-ide"
    usage_a = antigravity_usage_blob(fresh_input=50, cache_read=10, cache_write=0, response_id="r1")
    write_antigravity_db(
        cli_root / "conversations/s1.db",
        generations=[(0, antigravity_gen_blob(model="models/gemini-2.5-pro", usage=usage_a))],
    )
    # Identical copy across App and CLI roots deduplicates; root-level DBs are ignored.
    write_antigravity_db(
        app_root / "conversations/s1.db",
        generations=[(0, antigravity_gen_blob(model="models/gemini-2.5-pro", usage=usage_a))],
    )
    write_antigravity_db(
        app_root / "unrelated_root_level.db",
        generations=[(0, antigravity_gen_blob(model="gemini-2.5-pro", usage=usage_a))],
    )
    usage_b = antigravity_usage_blob(fresh_input=30, cache_read=0, cache_write=0, response_id="r2")
    write_antigravity_db(
        ide_root / "conversations/s2.db",
        generations=[(0, antigravity_gen_blob(model=None, model_id=246, usage=usage_b))],
    )

    with Index(discovered.cache) as index:
        index.sync(discovered, ["antigravity"])
        events = index.events(discovered, ["antigravity"])
        assert len(events) == 2
        assert {e["model"] for e in events} == {"gemini-2.5-pro"}

        # In-place SQLite insert into s2.db must invalidate checkpoint and index the new row.
        usage_c = antigravity_usage_blob(
            fresh_input=70, cache_read=0, cache_write=0, response_id="r3"
        )
        with sqlite3.connect(ide_root / "conversations/s2.db") as conn:
            conn.execute(
                "INSERT INTO gen_metadata (idx, data) VALUES (?, ?)",
                (1, antigravity_gen_blob(model="claude-opus-5-5-medium", usage=usage_c)),
            )
        conn.close()
        updated_cov = index.sync(discovered, ["antigravity"])
        assert updated_cov["files_updated"] == 1
        assert len(index.events(discovered, ["antigravity"])) == 3


def test_antigravity_corrupt_and_unsupported_databases_report_issues_cleanly(paths):
    (paths.antigravity / "conversations/corrupt.db").write_bytes(b"not a sqlite file")
    with sqlite3.connect(paths.antigravity / "conversations/steps_only.db") as steps_only:
        steps_only.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, metadata BLOB)")
        steps_only.execute("INSERT INTO steps VALUES (0, X'0801')")
    steps_only.close()

    valid_db = paths.antigravity / "conversations/mixed.db"
    write_antigravity_db(
        valid_db,
        generations=[
            (0, antigravity_gen_blob(usage=antigravity_usage_blob(response_id="ok"))),
            (1, b"\xff\xff\xff"),
            (
                2,
                antigravity_gen_blob(
                    usage=antigravity_usage_blob(response_id="undated"),
                    timestamp=None,
                ),
            ),
        ],
    )

    with Index(paths.cache) as index:
        cov = index.sync(paths, ["antigravity"])
        events = index.events(paths, ["antigravity"])

    assert cov["issues"] == {
        "antigravity_unsupported_db_schema": 1,
        "invalid_record": 2,
        "missing_timestamp": 1,
    }
    assert len(events) == 1


def test_antigravity_retry_with_distinct_model_id_uses_retry_model(paths):
    db_path = paths.antigravity / "conversations/retry-models.db"
    primary = antigravity_usage_blob(
        fresh_input=80, cache_read=0, cache_write=0, model_id=246, response_id="resp-primary"
    )
    same_id_retry = antigravity_usage_blob(
        fresh_input=20, cache_read=0, cache_write=0, model_id=246, response_id="resp-same-model"
    )
    fallback_flash_retry = antigravity_usage_blob(
        fresh_input=30, cache_read=0, cache_write=0, model_id=312, response_id="resp-flash-retry"
    )
    unknown_id_retry = antigravity_usage_blob(
        fresh_input=15, cache_read=0, cache_write=0, model_id=9999, response_id="resp-unknown-id"
    )
    text_only_primary = antigravity_usage_blob(
        fresh_input=50, cache_read=0, cache_write=0, model_id=None, response_id="resp-text-primary"
    )
    text_only_flash_retry = antigravity_usage_blob(
        fresh_input=25, cache_read=0, cache_write=0, model_id=312, response_id="resp-text-flash"
    )
    write_antigravity_db(
        db_path,
        generations=[
            (
                0,
                antigravity_gen_blob(
                    model="gemini-2.5-pro",
                    display_model="Gemini 2.5 Pro (High)",
                    model_id=246,
                    usage=primary,
                    retries=[same_id_retry, fallback_flash_retry, unknown_id_retry],
                ),
            ),
            (
                1,
                antigravity_gen_blob(
                    model="gemini-2.5-pro",
                    display_model="Gemini 2.5 Pro (High)",
                    model_id=None,
                    usage=text_only_primary,
                    retries=[text_only_flash_retry],
                ),
            ),
        ],
    )

    with Index(paths.cache) as index:
        assert index.sync(paths, ["antigravity"])["issues"] == {}
        by_input = {e["input_tokens"]: e["model"] for e in index.events(paths, ["antigravity"])}

    assert by_input == {
        80: "gemini-2.5-pro-high",
        20: "gemini-2.5-pro-high",
        30: "gemini-2.5-flash",
        15: "unknown",
        50: "gemini-2.5-pro-high",
        25: "gemini-2.5-flash",
    }


def test_antigravity_wal_databases_use_consistent_snapshot_and_preserve_index_without_shm(paths):
    conv_dir = paths.antigravity / "conversations"
    clean_wal_db = conv_dir / "clean-wal.db"
    with sqlite3.connect(clean_wal_db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute(
            "INSERT INTO gen_metadata VALUES (0, ?)",
            (antigravity_gen_blob(usage=antigravity_usage_blob(response_id="clean-1")),),
        )
    conn.close()
    assert sorted(p.name for p in conv_dir.iterdir()) == ["clean-wal.db"]

    with Index(paths.cache) as index:
        assert index.sync(paths, ["antigravity"])["issues"] == {}
        assert len(index.events(paths, ["antigravity"])) == 1
        # Scanning a cleanly closed WAL database must not create -wal or -shm in source dir.
        assert sorted(p.name for p in conv_dir.iterdir()) == ["clean-wal.db"]

        # Active WAL session with uncheckpointed frames uses SQLite WAL read snapshot
        # without creating extra files or mutating .db / -wal bytes.
        writer = sqlite3.connect(clean_wal_db)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute(
                "INSERT INTO gen_metadata VALUES (1, ?)",
                (
                    antigravity_gen_blob(
                        usage=antigravity_usage_blob(fresh_input=45, response_id="active-wal-2")
                    ),
                ),
            )
            writer.commit()
            wal_path = Path(f"{clean_wal_db}-wal")
            assert wal_path.stat().st_size > 0
            before_names = sorted(p.name for p in conv_dir.iterdir())
            before_db = clean_wal_db.read_bytes()
            before_wal = wal_path.read_bytes()

            active_cov = index.sync(paths, ["antigravity"])
            assert active_cov["issues"] == {}
            assert active_cov["files_updated"] == 1
            assert len(index.events(paths, ["antigravity"])) == 2
            assert sorted(p.name for p in conv_dir.iterdir()) == before_names
            assert clean_wal_db.read_bytes() == before_db
            assert wal_path.read_bytes() == before_wal

            # Write a 3rd row, save -wal bytes, close cleanly, then plant an orphaned -wal
            # without -shm: sync must preserve the 2 previously indexed events rather than
            # creating -shm in source dir or overwriting the index with partial data.
            writer.execute(
                "INSERT INTO gen_metadata VALUES (2, ?)",
                (
                    antigravity_gen_blob(
                        usage=antigravity_usage_blob(fresh_input=65, response_id="orphan-wal-3")
                    ),
                ),
            )
            writer.commit()
            orphan_wal_bytes = wal_path.read_bytes()
        finally:
            writer.close()

        Path(f"{clean_wal_db}-wal").write_bytes(orphan_wal_bytes)
        assert sorted(p.name for p in conv_dir.iterdir()) == ["clean-wal.db", "clean-wal.db-wal"]
        orphan_cov = index.sync(paths, ["antigravity"])
        assert orphan_cov["issues"] == {"unreadable_file": 1}
        assert sorted(p.name for p in conv_dir.iterdir()) == ["clean-wal.db", "clean-wal.db-wal"]
        assert len(index.events(paths, ["antigravity"])) == 2

        # Once a client recovers the WAL cleanly, the next sync indexes all 3 rows.
        Path(f"{clean_wal_db}-wal").unlink()
        recovered = index.sync(paths, ["antigravity"])
        assert recovered["issues"] == {}
        assert len(index.events(paths, ["antigravity"])) == 3


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="POSIX non-root file permissions required",
)
def test_antigravity_unreadable_wal_or_db_preserves_index_and_retries_after_chmod(paths):
    conv_dir = paths.antigravity / "conversations"
    db_path = conv_dir / "wal-session.db"
    writer = sqlite3.connect(db_path)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        writer.execute(
            "INSERT INTO gen_metadata VALUES (0, ?)",
            (antigravity_gen_blob(usage=antigravity_usage_blob(response_id="r1")),),
        )
        writer.commit()

        with Index(paths.cache) as index:
            assert index.sync(paths, ["antigravity"])["issues"] == {}
            assert len(index.events(paths, ["antigravity"])) == 1

            writer.execute(
                "INSERT INTO gen_metadata VALUES (1, ?)",
                (antigravity_gen_blob(usage=antigravity_usage_blob(response_id="r2")),),
            )
            writer.commit()
            wal_path = Path(f"{db_path}-wal")
            wal_path.chmod(0o000)
            try:
                failed_cov = index.sync(paths, ["antigravity"])
                assert failed_cov["issues"] == {"unreadable_file": 1}
                # Previously indexed event is preserved rather than wiped to zero.
                assert len(index.events(paths, ["antigravity"])) == 1
            finally:
                wal_path.chmod(0o644)

            # Restoring WAL read permission without new writes retries and indexes both rows.
            recovered_cov = index.sync(paths, ["antigravity"])
            assert recovered_cov["issues"] == {}
            assert recovered_cov["files_updated"] == 1
            assert len(index.events(paths, ["antigravity"])) == 2
    finally:
        writer.close()
