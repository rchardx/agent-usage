import json

import pytest

from agent_usage.paths import Paths
from agent_usage.pricing import Pricing


@pytest.fixture(autouse=True)
def offline_cli_prices(monkeypatch):
    """CLI tests never need the public pricing service; pricing I/O has its own tests."""
    monkeypatch.setattr(
        "agent_usage.cli.load_pricing", lambda *args, **kwargs: Pricing({}, "unavailable")
    )


@pytest.fixture
def paths(tmp_path):
    roots = Paths(
        tmp_path / "codex",
        tmp_path / "claude",
        tmp_path / "pi",
        tmp_path / "omp",
        tmp_path / "cache",
    )
    (roots.codex / "sessions").mkdir(parents=True)
    (roots.claude / "projects").mkdir(parents=True)
    (roots.pi / "sessions" / "-example").mkdir(parents=True)
    (roots.omp / "sessions" / "-example").mkdir(parents=True)
    return roots


def write_rows(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8", newline="\n")


def codex_meta(session="session-a", **extra):
    return {
        "type": "session_meta",
        "payload": {
            "id": session,
            "cwd": "/example",
            "timestamp": "2026-10-01T00:00:00Z",
            **extra,
        },
    }


def codex_usage(input_tokens, output_tokens, stamp="2026-10-01T00:01:00Z", **extra):
    counts = {
        "input_tokens": input_tokens,
        "cached_input_tokens": input_tokens // 2,
        "cache_write_input_tokens": 0,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": output_tokens // 2,
        "total_tokens": input_tokens + output_tokens,
        **extra,
    }
    return {
        "type": "event_msg",
        "timestamp": stamp,
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": counts,
                "last_token_usage": counts.copy(),
            },
        },
    }


def claude_usage(message="msg-a", stamp="2026-10-01T00:01:00Z", output=10, **usage):
    return {
        "type": "assistant",
        "sessionId": "claude-session",
        "requestId": "req-a",
        "timestamp": stamp,
        "cwd": "/example",
        "message": {
            "id": message,
            "model": "example-model",
            "content": [{"type": "text", "text": "PRIVATE-CONVERSATION-SENTINEL"}],
            "usage": {
                "input_tokens": 2,
                "cache_creation_input_tokens": 20,
                "cache_read_input_tokens": 100,
                "output_tokens": output,
                "output_tokens_details": {"thinking_tokens": 3},
                "cache_creation": {"ephemeral_5m_input_tokens": 5, "ephemeral_1h_input_tokens": 15},
                **usage,
            },
        },
    }


def agent_session(session="agent-session", cwd="/example"):
    """pi/omp share one session schema."""
    return {
        "type": "session",
        "version": 3,
        "id": session,
        "timestamp": "2026-10-01T00:00:00Z",
        "cwd": cwd,
    }


def agent_usage(response="resp-a", stamp="2026-10-01T00:01:00Z", **usage):
    return {
        "type": "message",
        "id": f"row-{response}",
        "timestamp": stamp,
        "message": {
            "role": "assistant",
            "provider": "example-upstream",
            "model": "example-model",
            "responseId": response,
            "content": [{"type": "text", "text": "PRIVATE-CONVERSATION-SENTINEL"}],
            "usage": {
                "input": 10,
                "output": 5,
                "cacheRead": 3,
                "cacheWrite": 1,
                "reasoning": 2,
                **usage,
            },
        },
    }
