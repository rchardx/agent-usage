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
