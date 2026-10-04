"""Parse usage metadata only. Conversation contents are never persisted."""

import hashlib
import json
from typing import Any

from .models import UsageEvent, iso_time

METRICS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
)
PARSER_VERSION = 3


def count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("Invalid token count")
    return value


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def parse_codex(
    row: dict, state: dict, fallback_session: str
) -> tuple[UsageEvent | None, str | None]:
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None, None
    if row.get("type") == "session_meta":
        # Fork transcripts can embed ancestor session_meta after their own header.
        # Ownership belongs to the first metadata record, including after index resume.
        if state.get("session_meta_seen"):
            return None, None
        state.update(
            session_meta_seen=True,
            session=payload.get("id") or payload.get("session_id") or fallback_session,
            project=payload.get("cwd", "unknown"),
            forked=bool(payload.get("forked_from_id")),
            started_at=iso_time(payload.get("timestamp")),
        )
        return None, None
    if row.get("type") == "turn_context":
        state["model"] = payload.get("model", "unknown")
        state["project"] = payload.get("cwd", state.get("project", "unknown"))
        return None, None
    if payload.get("type") != "token_count" or not payload.get("info"):
        return None, None
    info = payload["info"]
    total = info.get("total_token_usage")
    if not isinstance(total, dict):
        return None, "codex_missing_cumulative_usage"
    total = {k: count(v) for k, v in total.items() if k.endswith("tokens")}
    if total.get("input_tokens") is None or total.get("output_tokens") is None:
        return None, "codex_missing_input_or_output"
    previous = state.get("previous")
    if previous == total:
        return None, None
    timestamp = iso_time(row.get("timestamp"))
    if timestamp is None:
        return None, "missing_timestamp"
    # A fork may carry ancestor history. Keep its baseline, never count replayed rows.
    if state.get("forked") and state.get("started_at") and timestamp < state["started_at"]:
        state["previous"] = total
        return None, "fork_history_excluded"
    warning = None
    if previous is not None:
        reset = any(
            total.get(k) is not None and previous.get(k) is not None and total[k] < previous[k]
            for k in ("input_tokens", "output_tokens")
        )
        if reset:
            # Counts after a reset can include inherited context. Count only the last request.
            delta = info.get("last_token_usage")
            warning = "codex_counter_reset_last_request_only"
        else:
            delta = {
                k: v - previous[k]
                if v is not None and previous.get(k) is not None and v >= previous[k]
                else None
                for k, v in total.items()
            }
    elif state.get("forked"):
        delta = info.get("last_token_usage")
        if delta != total:
            warning = "codex_fork_baseline_excluded"
    else:
        delta = total
        if total != info.get("last_token_usage"):
            warning = "codex_initial_cumulative_attribution"
    state["previous"] = total
    if not isinstance(delta, dict):
        return None, "codex_missing_last_request"
    usage = {k: count(v) for k, v in delta.items()}
    input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
    if input_tokens is None or output_tokens is None:
        return None, "codex_incomplete_delta"
    if input_tokens + output_tokens == 0:
        return None, warning
    session = state.get("session", fallback_session)
    event = UsageEvent(
        provider="codex",
        event_id=digest([session, timestamp, total]),
        session=session,
        timestamp=timestamp,
        model=state.get("model", "unknown"),
        project=state.get("project", "unknown"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=usage.get("cached_input_tokens"),
        cache_write_tokens=usage.get("cache_write_input_tokens"),
        reasoning_tokens=usage.get("reasoning_output_tokens"),
        request_input_tokens=input_tokens
        if all(
            usage.get(key) == (info.get("last_token_usage") or {}).get(key)
            for key in ("input_tokens", "output_tokens")
        )
        else None,
    )
    validate(event)
    return event, warning


def parse_claude(
    row: dict, state: dict, fallback_session: str
) -> tuple[UsageEvent | None, str | None]:
    message = row.get("message")
    if row.get("type") != "assistant" or not isinstance(message, dict):
        return None, None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None, None
    if message.get("model") == "<synthetic>":
        return None, None
    fresh = count(usage.get("input_tokens"))
    output = count(usage.get("output_tokens"))
    read = count(usage.get("cache_read_input_tokens"))
    write = count(usage.get("cache_creation_input_tokens"))
    if fresh is None or output is None or read is None or write is None:
        return None, "claude_incomplete_usage"
    if fresh + output + read + write == 0:
        return None, None
    timestamp = iso_time(row.get("timestamp"))
    if timestamp is None:
        return None, "missing_timestamp"
    session = row.get("sessionId") or row.get("session_id") or fallback_session
    request_id, message_id = row.get("requestId"), message.get("id")
    if not request_id and not message_id:
        return None, "claude_missing_request_identity"
    details = usage.get("output_tokens_details") or {}
    creation = usage.get("cache_creation") or {}
    event = UsageEvent(
        provider="claude",
        # Same message is often repeated for text, thinking, and tool content blocks.
        event_id=digest([request_id, message_id]),
        session=session,
        timestamp=timestamp,
        model=message.get("model") or "unknown",
        project=row.get("cwd") or "unknown",
        input_tokens=fresh + read + write,
        output_tokens=output,
        cache_read_tokens=read,
        cache_write_tokens=write,
        reasoning_tokens=count(details.get("thinking_tokens")),
        cache_write_5m_tokens=count(creation.get("ephemeral_5m_input_tokens")),
        cache_write_1h_tokens=count(creation.get("ephemeral_1h_input_tokens")),
        request_input_tokens=fresh + read + write if not usage.get("iterations") else None,
    )
    validate(event)
    return event, None


def validate(event: UsageEvent):
    if event.cache_read_tokens is not None and event.cache_read_tokens > event.input_tokens:
        raise ValueError("Cache reads exceed total input")
    if event.cache_read_tokens is not None and event.cache_write_tokens is not None:
        if event.cache_read_tokens + event.cache_write_tokens > event.input_tokens:
            raise ValueError("Cache components exceed total input")
    if event.reasoning_tokens is not None and event.reasoning_tokens > event.output_tokens:
        raise ValueError("Reasoning exceeds output")
