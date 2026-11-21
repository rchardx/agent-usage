import json
import sqlite3

import pytest

from agent_usage.paths import Paths
from agent_usage.pricing import Pricing

AUTH_ENV_VARS = (
    "CODEX_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
)


@pytest.fixture(autouse=True)
def offline_cli_prices(monkeypatch):
    """CLI tests never need the public pricing service; pricing I/O has its own tests."""
    for name in AUTH_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        "agent_usage.cli.load_pricing", lambda *args, **kwargs: Pricing({}, "unavailable")
    )


@pytest.fixture
def paths(tmp_path):
    agy = tmp_path / "antigravity"
    roots = Paths(
        tmp_path / "codex",
        tmp_path / "claude",
        tmp_path / "pi",
        tmp_path / "omp",
        tmp_path / "cache",
        agy,
        (agy,),
    )
    (roots.codex / "sessions").mkdir(parents=True)
    (roots.claude / "projects").mkdir(parents=True)
    (roots.antigravity / "conversations").mkdir(parents=True)
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


def _varint_bytes(value: int) -> bytes:
    out = bytearray()
    while value >= 0x80:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def proto_varint(number: int, value: int) -> bytes:
    return _varint_bytes(number << 3) + _varint_bytes(value)


def proto_bytes(number: int, payload: bytes) -> bytes:
    return _varint_bytes((number << 3) | 2) + _varint_bytes(len(payload)) + payload


def proto_timestamp(seconds: int, nanos: int = 0) -> bytes:
    return proto_varint(1, seconds) + (proto_varint(2, nanos) if nanos else b"")


def antigravity_usage_blob(
    *,
    fresh_input: int = 20,
    total_output: int = 8,
    cache_write: int = 2,
    cache_read: int = 30,
    reasoning: int = 3,
    visible_output: int = 5,
    model_id: int | None = None,
    response_id: str | None = "resp-agy-1",
    provider_message_id: str | None = None,
    message_id: str | None = None,
) -> bytes:
    parts = []
    if model_id is not None:
        parts.append(proto_varint(1, model_id))
    for field_num, val in (
        (2, fresh_input),
        (3, total_output),
        (4, cache_write),
        (5, cache_read),
        (9, reasoning),
        (10, visible_output),
    ):
        if val > 0:
            parts.append(proto_varint(field_num, val))
    if message_id:
        parts.append(proto_bytes(7, message_id.encode()))
    if response_id:
        parts.append(proto_bytes(11, response_id.encode()))
    if provider_message_id:
        parts.append(proto_bytes(12, provider_message_id.encode()))
    return b"".join(parts)


def antigravity_gen_blob(
    *,
    model: str | None = "gemini-2.5-flash",
    display_model: str | None = None,
    model_id: int | None = None,
    usage: bytes | None = None,
    retries: list[bytes] | None = None,
    timestamp: tuple[int, int] | None = (1_790_812_860, 0),
    step_indices: list[int] | None = None,
) -> bytes:
    chat = [proto_bytes(2, b"PRIVATE-CONVERSATION-SENTINEL")]
    if model_id is not None:
        chat.append(proto_varint(3, model_id))
    if usage is not None:
        chat.append(proto_bytes(4, usage))
    if timestamp is not None:
        gen_info = proto_bytes(4, proto_timestamp(*timestamp))
        chat.append(proto_bytes(9, gen_info))
    for retry in retries or []:
        chat.append(proto_bytes(17, proto_bytes(2, retry)))
    if model is not None:
        chat.append(proto_bytes(19, model.encode()))
    if display_model is not None:
        chat.append(proto_bytes(21, display_model.encode()))
    root = [proto_bytes(1, b"".join(chat))]
    for step_idx in step_indices or []:
        root.append(proto_varint(2, step_idx))
    return b"".join(root)


def antigravity_step_blob(
    *,
    model: str | None = "gemini-2.5-flash",
    model_id: int | None = None,
    usage: bytes | None = None,
    retries: list[bytes] | None = None,
    timestamp: tuple[int, int] | None = (1_790_812_860, 0),
) -> bytes:
    parts = [proto_bytes(5, b"PRIVATE-CONVERSATION-SENTINEL")]
    if timestamp is not None:
        parts.append(proto_bytes(8, proto_timestamp(*timestamp)))
    if usage is not None:
        parts.append(proto_bytes(9, usage))
    if model is not None or model_id is not None:
        info = []
        if model_id is not None:
            info.append(proto_varint(1, model_id))
        if model is not None:
            info.append(proto_bytes(12, model.encode()))
        parts.append(proto_bytes(24, b"".join(info)))
    for retry in retries or []:
        parts.append(proto_bytes(28, proto_bytes(2, retry)))
    return b"".join(parts)


def write_antigravity_db(
    path,
    *,
    generations: list[tuple[int, bytes]] = (),
    steps: list[tuple[int, bytes]] = (),
    trajectories: list[tuple[int, int]] = (),
):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB NOT NULL)")
        conn.execute("CREATE TABLE steps (idx INTEGER PRIMARY KEY, metadata BLOB)")
        conn.execute("CREATE TABLE trajectory_metadata_blob (data BLOB)")
        conn.executemany("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", generations)
        conn.executemany("INSERT INTO steps (idx, metadata) VALUES (?, ?)", steps)
        conn.executemany(
            "INSERT INTO trajectory_metadata_blob (data) VALUES (?)",
            [(proto_bytes(2, proto_timestamp(sec, ns)),) for sec, ns in trajectories],
        )
    conn.close()
