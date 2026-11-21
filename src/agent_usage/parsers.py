"""Parse usage metadata only. Conversation contents are never persisted."""

import hashlib
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
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
PARSER_VERSION = 5
ANTIGRAVITY_MODEL_IDS = {
    246: "gemini-2.5-pro",
    281: "claude-sonnet-4",
    282: "claude-sonnet-4",
    290: "claude-opus-4",
    291: "claude-opus-4",
    312: "gemini-2.5-flash",
    313: "gemini-2.5-flash-thinking",
    329: "gemini-2.5-flash-thinking",
    330: "gemini-2.5-flash-lite",
    333: "claude-sonnet-4-5",
    334: "claude-sonnet-4-5",
    340: "claude-haiku-4-5",
    341: "claude-haiku-4-5",
}
# Ordered longest-first so suffix stripping matches "-extra-low" before "-low".
ANTIGRAVITY_EFFORTS = (
    "extra-low",
    "xhigh",
    "thinking",
    "high",
    "medium",
    "low",
    "max",
)


def clean_model(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    name = value.strip()
    if name.startswith("models/") and len(name) > len("models/"):
        name = name.removeprefix("models/")
    return name or "unknown"


def namespace_model(model: Any, upstream: Any, *, default_upstream: str | None = None) -> str:
    cleaned = clean_model(model)
    if cleaned == "unknown":
        return "unknown"
    if isinstance(upstream, str) and upstream.strip():
        prefix = upstream.strip()
        if default_upstream and prefix == default_upstream:
            return cleaned
        if cleaned != prefix and not cleaned.startswith(f"{prefix}/"):
            return f"{prefix}/{cleaned}"
    return cleaned


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
            model_provider=payload.get("model_provider"),
            forked=bool(payload.get("forked_from_id")),
            started_at=iso_time(payload.get("timestamp")),
        )
        return None, None
    if row.get("type") == "turn_context":
        state["model"] = payload.get("model", "unknown")
        state["project"] = payload.get("cwd", state.get("project", "unknown"))
        if payload.get("model_provider"):
            state["model_provider"] = payload["model_provider"]
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
    single_request = all(
        usage.get(key) == (info.get("last_token_usage") or {}).get(key)
        for key in ("input_tokens", "output_tokens")
    )
    event = UsageEvent(
        provider="codex",
        event_id=digest([session, timestamp, total]),
        session=session,
        timestamp=timestamp,
        model=namespace_model(
            state.get("model", "unknown"),
            state.get("model_provider"),
            default_upstream="openai",
        ),
        project=state.get("project", "unknown"),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=usage.get("cached_input_tokens"),
        cache_write_tokens=usage.get("cache_write_input_tokens"),
        reasoning_tokens=usage.get("reasoning_output_tokens"),
        request_input_tokens=input_tokens if single_request else None,
        multi_request=not single_request,
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
    if fresh is None or output is None:
        return None, "claude_incomplete_usage"
    total_input = fresh + (read or 0) + (write or 0)
    if total_input + output == 0:
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
    iterated = bool(usage.get("iterations"))
    event = UsageEvent(
        provider="claude",
        # Same message is often repeated for text, thinking, and tool content blocks.
        event_id=digest([request_id, message_id]),
        session=session,
        timestamp=timestamp,
        model=clean_model(message.get("model")),
        project=row.get("cwd") or "unknown",
        input_tokens=total_input,
        output_tokens=output,
        cache_read_tokens=read,
        cache_write_tokens=write,
        reasoning_tokens=count(details.get("thinking_tokens")),
        cache_write_5m_tokens=count(creation.get("ephemeral_5m_input_tokens")),
        cache_write_1h_tokens=count(creation.get("ephemeral_1h_input_tokens")),
        request_input_tokens=total_input
        if (read is not None and write is not None and not iterated)
        else None,
        multi_request=iterated,
    )
    validate(event)
    return event, None


def parse_pi_style(
    row: dict, state: dict, fallback_session: str, provider: str
) -> tuple[UsageEvent | None, str | None]:
    """pi and omp share one session schema: per-request assistant usage, never cumulative."""
    if row.get("type") == "session":
        # Ownership belongs to the first session record, as with codex forks.
        if state.get("session_seen"):
            return None, None
        state.update(
            session_seen=True,
            session=row.get("id") or fallback_session,
            project=row.get("cwd", "unknown"),
        )
        return None, None
    if row.get("type") == "model_change":
        # pi reports provider + modelId; omp reports model.
        changed = row.get("model") or row.get("modelId")
        if changed:
            state["model"] = changed
            state["model_provider"] = row.get("provider")
        return None, None
    message = row.get("message")
    if row.get("type") != "message" or not isinstance(message, dict):
        return None, None
    if message.get("role") != "assistant":
        return None, None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None, None
    fresh = count(usage.get("input"))
    output = count(usage.get("output"))
    read = count(usage.get("cacheRead"))
    write = count(usage.get("cacheWrite"))
    reasoning = count(usage.get("reasoning", usage.get("reasoningTokens")))
    if fresh is None or output is None or read is None or write is None:
        return None, f"{provider}_incomplete_usage"
    if fresh + output + read + write == 0:
        return None, None
    timestamp = iso_time(row.get("timestamp"))
    if timestamp is None:
        return None, "missing_timestamp"
    identity = message.get("responseId") or row.get("id") or message.get("id")
    if not identity:
        return None, f"{provider}_missing_request_identity"
    if message.get("responseModel") or message.get("model"):
        raw_model = message.get("responseModel") or message.get("model")
        upstream = message.get("provider")
    else:
        raw_model = state.get("model")
        upstream = message.get("provider") or state.get("model_provider")
    # Preserve the serving upstream even when a gateway model ID itself contains "/".
    model = namespace_model(raw_model, upstream)
    event = UsageEvent(
        provider=provider,
        event_id=digest([provider, state.get("session", fallback_session), identity]),
        session=state.get("session", fallback_session),
        timestamp=timestamp,
        model=model,
        project=state.get("project", "unknown"),
        input_tokens=fresh + read + write,
        output_tokens=output,
        cache_read_tokens=read,
        cache_write_tokens=write,
        reasoning_tokens=reasoning,
        request_input_tokens=fresh + read + write,
    )
    validate(event)
    return event, None


def parse_pi(row: dict, state: dict, fallback_session: str) -> tuple[UsageEvent | None, str | None]:
    return parse_pi_style(row, state, fallback_session, "pi")


def parse_omp(
    row: dict, state: dict, fallback_session: str
) -> tuple[UsageEvent | None, str | None]:
    return parse_pi_style(row, state, fallback_session, "omp")


def _read_varint(buf: memoryview, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(buf):
            raise ValueError("Truncated protobuf varint")
        byte = buf[pos]
        pos += 1
        payload = byte & 0x7F
        if shift == 63 and payload > 1:
            raise ValueError("Protobuf varint overflow")
        value |= payload << shift
        if not (byte & 0x80):
            return value, pos
    raise ValueError("Protobuf varint overflow")


def _proto_fields(blob: bytes | memoryview) -> list[tuple[int, int | bytes]]:
    mv = memoryview(blob)
    pos = 0
    fields: list[tuple[int, int | bytes]] = []
    while pos < len(mv):
        tag, pos = _read_varint(mv, pos)
        number = tag >> 3
        wire = tag & 7
        if number <= 0 or number >= (1 << 29):
            raise ValueError("Invalid protobuf field number")
        if wire == 0:
            val, pos = _read_varint(mv, pos)
            fields.append((number, val))
        elif wire == 1:
            if pos + 8 > len(mv):
                raise ValueError("Truncated protobuf fixed64")
            pos += 8
        elif wire == 2:
            length, pos = _read_varint(mv, pos)
            if pos + length > len(mv):
                raise ValueError("Truncated protobuf bytes")
            fields.append((number, bytes(mv[pos : pos + length])))
            pos += length
        elif wire == 5:
            if pos + 4 > len(mv):
                raise ValueError("Truncated protobuf fixed32")
            pos += 4
        else:
            raise ValueError("Unsupported protobuf wire type")
    return fields


def _field_varint(fields: list[tuple[int, int | bytes]], number: int) -> int | None:
    for field_num, value in reversed(fields):
        if field_num == number and isinstance(value, int):
            return value
    return None


def _field_bytes(fields: list[tuple[int, int | bytes]], number: int) -> bytes | None:
    for field_num, value in fields:
        if field_num == number and isinstance(value, bytes):
            return value
    return None


def _field_bytes_all(fields: list[tuple[int, int | bytes]], number: int) -> list[bytes]:
    return [
        value for field_num, value in fields if field_num == number and isinstance(value, bytes)
    ]


def _field_text(fields: list[tuple[int, int | bytes]], number: int) -> str | None:
    for field_num, value in reversed(fields):
        if field_num == number and isinstance(value, bytes):
            try:
                text = value.decode("utf-8").strip()
            except UnicodeDecodeError:
                return None
            return text or None
    return None


def _parse_proto_timestamp(blob: bytes) -> str | None:
    fields = _proto_fields(blob)
    seconds = _field_varint(fields, 1)
    if seconds is None or seconds <= 0:
        return None
    nanos = min(_field_varint(fields, 2) or 0, 999_999_999)
    dt = datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanos // 1_000)
    return dt.isoformat()


@dataclass
class _AntigravityUsage:
    model_id: int | None
    fresh_input: int
    total_output: int
    cache_write: int
    cache_read: int
    reasoning: int
    visible_output: int
    message_id: str | None
    response_id: str | None
    provider_message_id: str | None

    def is_token_bearing(self) -> bool:
        return any(
            (
                self.fresh_input,
                self.total_output,
                self.cache_write,
                self.cache_read,
                self.reasoning,
                self.visible_output,
            )
        )

    def identity(self, fallback: str) -> str:
        if self.response_id:
            return f"response:{self.response_id}"
        if self.provider_message_id:
            return f"provider:{self.provider_message_id}"
        if self.message_id:
            return f"message:{self.message_id}"
        return fallback

    def is_mirror_of(self, primary: "_AntigravityUsage") -> bool:
        if (
            self.model_id != primary.model_id
            or self.fresh_input != primary.fresh_input
            or self.total_output != primary.total_output
            or self.cache_write != primary.cache_write
            or self.cache_read != primary.cache_read
            or self.reasoning != primary.reasoning
            or self.visible_output != primary.visible_output
        ):
            return False
        primary_ids = {
            v for v in (primary.response_id, primary.provider_message_id, primary.message_id) if v
        }
        retry_ids = {v for v in (self.response_id, self.provider_message_id, self.message_id) if v}
        return retry_ids <= primary_ids


def _parse_agy_model_usage(blob: bytes) -> _AntigravityUsage:
    fields = _proto_fields(blob)
    model_id = _field_varint(fields, 1)
    return _AntigravityUsage(
        model_id=model_id if model_id else None,
        fresh_input=_field_varint(fields, 2) or 0,
        total_output=_field_varint(fields, 3) or 0,
        cache_write=_field_varint(fields, 4) or 0,
        cache_read=_field_varint(fields, 5) or 0,
        reasoning=_field_varint(fields, 9) or 0,
        visible_output=_field_varint(fields, 10) or 0,
        message_id=_field_text(fields, 7),
        response_id=_field_text(fields, 11),
        provider_message_id=_field_text(fields, 12),
    )


def _parse_step_indices(fields: list[tuple[int, int | bytes]]) -> list[int]:
    indices: list[int] = []
    for field_num, value in fields:
        if field_num != 2:
            continue
        if isinstance(value, int):
            indices.append(value)
        elif isinstance(value, bytes):
            mv = memoryview(value)
            pos = 0
            while pos < len(mv):
                item, pos = _read_varint(mv, pos)
                indices.append(item)
    return indices


def _display_effort_suffix(text: str | None) -> str | None:
    if not isinstance(text, str) or " (" not in text or not text.endswith(")"):
        return None
    effort = "-".join(text.rsplit(" (", 1)[1][:-1].strip().lower().split())
    return effort if effort in ANTIGRAVITY_EFFORTS else None


def normalize_antigravity_model(
    slug: str | None, display: str | None = None, model_id: int | None = None
) -> str:
    effort = _display_effort_suffix(display) or _display_effort_suffix(slug)
    raw = slug if isinstance(slug, str) and slug.strip() else display
    if isinstance(raw, str) and raw.strip():
        cleaned = clean_model(raw)
        if " " in cleaned:
            family = cleaned.lower().split("(", 1)[0].strip()
            if family and (effort or family.startswith(("gemini ", "claude ", "gpt "))):
                base = "-".join(family.split())
                return f"{base}-{effort}" if effort and not base.endswith(f"-{effort}") else base
        if effort and not cleaned.lower().endswith(f"-{effort}"):
            return f"{cleaned}-{effort}"
        return cleaned
    if model_id is not None and model_id in ANTIGRAVITY_MODEL_IDS:
        return ANTIGRAVITY_MODEL_IDS[model_id]
    return "unknown"


def _parse_gen_row(blob: bytes) -> dict[str, Any]:
    root = _proto_fields(blob)
    chat_blob = _field_bytes(root, 1)
    if chat_blob is None:
        raise ValueError("Missing chat model in gen_metadata")
    chat = _proto_fields(chat_blob)
    usage_blob = _field_bytes(chat, 4)
    usage = _parse_agy_model_usage(usage_blob) if usage_blob is not None else None
    retries: list[_AntigravityUsage] = []
    for retry_blob in _field_bytes_all(chat, 17):
        inner = _field_bytes(_proto_fields(retry_blob), 2)
        if inner is not None:
            retries.append(_parse_agy_model_usage(inner))
    if usage is not None:
        for index, retry in enumerate(retries):
            if retry.is_mirror_of(usage):
                retries.pop(index)
                break
    gen_info = _field_bytes(chat, 9)
    ts_blob = _field_bytes(_proto_fields(gen_info), 4) if gen_info is not None else None
    timestamp = _parse_proto_timestamp(ts_blob) if ts_blob is not None else None
    model_id = _field_varint(chat, 3)
    return {
        "slug": _field_text(chat, 19),
        "display": _field_text(chat, 21),
        "model_id": model_id if model_id else None,
        "usage": usage,
        "retries": retries,
        "timestamp": timestamp,
        "step_indices": _parse_step_indices(root),
    }


_IO_SQLITE_CODES = (
    sqlite3.SQLITE_CANTOPEN,
    sqlite3.SQLITE_PERM,
    sqlite3.SQLITE_READONLY,
    sqlite3.SQLITE_IOERR,
    sqlite3.SQLITE_BUSY,
    sqlite3.SQLITE_LOCKED,
)


def _strip_agy_slug_effort(model: str) -> str:
    for effort in ANTIGRAVITY_EFFORTS:
        if effort == "thinking":
            continue
        suffix = f"-{effort}"
        if model.endswith(suffix) and len(model) > len(suffix):
            return model.removesuffix(suffix)
    return model


def _resolve_agy_usage_model(
    meta: dict[str, Any], usage: _AntigravityUsage, *, is_retry: bool
) -> str:
    primary_id = meta["model_id"] or (meta["usage"].model_id if meta["usage"] is not None else None)
    if usage.model_id is not None:
        if primary_id is not None and usage.model_id != primary_id:
            return normalize_antigravity_model(None, None, usage.model_id)
        if is_retry and primary_id is None and usage.model_id in ANTIGRAVITY_MODEL_IDS:
            retry_model = ANTIGRAVITY_MODEL_IDS[usage.model_id]
            display = meta["display"]
            display_base = (
                display.rsplit(" (", 1)[0]
                if isinstance(display, str) and _display_effort_suffix(display)
                else display
            )
            primary_base = _strip_agy_slug_effort(
                normalize_antigravity_model(meta["slug"], display_base, None)
            )
            if primary_base != retry_model:
                return retry_model
    return normalize_antigravity_model(meta["slug"], meta["display"], usage.model_id or primary_id)


def _sidecar_exists(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _db_file_signature(path: Path) -> tuple[int, int, int, int, bytes]:
    with path.open("rb") as handle:
        st = os.fstat(handle.fileno())
        header = handle.read(100)
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, header)


def parse_antigravity_db(path: Path) -> tuple[list[UsageEvent], Counter[str]]:
    """Read `gen_metadata` usage rows from a consistent read-only SQLite snapshot."""
    issues: Counter[str] = Counter()
    before_sig = _db_file_signature(path)
    if before_sig[2] == 0:
        return [], issues

    wal_path = Path(f"{path}-wal")
    shm_path = Path(f"{path}-shm")
    journal_path = Path(f"{path}-journal")
    if _sidecar_exists(journal_path):
        raise OSError("Unrecovered SQLite rollback journal")

    has_wal = _sidecar_exists(wal_path)
    if has_wal:
        if not _sidecar_exists(shm_path):
            raise OSError("Active SQLite WAL lacks shared-memory index")
        with wal_path.open("rb"):
            pass
        uri = f"{path.as_uri()}?mode=ro"
    else:
        uri = f"{path.as_uri()}?mode=ro&immutable=1"

    session_id = path.stem or "unknown"
    gen_rows: list[tuple[int, dict[str, Any]]] = []
    step_timestamps: dict[int, str] = {}
    resp_timestamps: dict[str, str] = {}

    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as conn:
            conn.execute("BEGIN DEFERRED")
            page_count = conn.execute("PRAGMA page_count").fetchone()
            if not page_count or page_count[0] == 0:
                return [], issues
            tables = {
                row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if "gen_metadata" not in tables:
                if "steps" in tables:
                    issues["antigravity_unsupported_db_schema"] += 1
                return [], issues

            if "steps" in tables:
                for idx, raw_blob in conn.execute(
                    "SELECT idx, metadata FROM steps WHERE metadata IS NOT NULL ORDER BY idx ASC"
                ):
                    if not isinstance(idx, int) or not isinstance(raw_blob, (bytes, memoryview)):
                        issues["invalid_record"] += 1
                        continue
                    try:
                        fields = _proto_fields(raw_blob)
                        ts_blob = _field_bytes(fields, 8) or _field_bytes(fields, 1)
                        ts = _parse_proto_timestamp(ts_blob) if ts_blob is not None else None
                        if ts is None:
                            continue
                        step_timestamps[idx] = ts
                        u_blob = _field_bytes(fields, 9)
                        if u_blob is not None:
                            rid = _field_text(_proto_fields(u_blob), 11)
                            if rid and (rid not in resp_timestamps or ts < resp_timestamps[rid]):
                                resp_timestamps[rid] = ts
                    except ValueError:
                        issues["invalid_record"] += 1

            for idx, raw_blob in conn.execute(
                "SELECT idx, data FROM gen_metadata ORDER BY idx ASC"
            ):
                if not isinstance(idx, int) or not isinstance(raw_blob, (bytes, memoryview)):
                    issues["invalid_record"] += 1
                    continue
                try:
                    gen_rows.append((idx, _parse_gen_row(bytes(raw_blob))))
                except ValueError:
                    issues["invalid_record"] += 1
    except sqlite3.Error as exc:
        if (getattr(exc, "sqlite_errorcode", 0) & 0xFF) in _IO_SQLITE_CODES:
            raise OSError("Unreadable Antigravity database") from exc
        issues["invalid_record"] += 1
        return [], issues

    if not has_wal and (
        _sidecar_exists(wal_path)
        or _sidecar_exists(journal_path)
        or _db_file_signature(path) != before_sig
    ):
        raise OSError("Antigravity database changed during immutable read")

    events: list[UsageEvent] = []
    for idx, meta in gen_rows:
        candidates = [(f"gen:{idx}", meta["usage"], False)] + [
            (f"gen-retry:{idx}:{r_idx}", retry, True) for r_idx, retry in enumerate(meta["retries"])
        ]
        for fallback, usage, is_retry in candidates:
            if usage is None or not usage.is_token_bearing():
                continue
            timestamp = (
                meta["timestamp"]
                or (resp_timestamps.get(usage.response_id) if usage.response_id else None)
                or next(
                    (step_timestamps[s] for s in meta["step_indices"] if s in step_timestamps), None
                )
            )
            if timestamp is None:
                issues["missing_timestamp"] += 1
                continue
            total_input = usage.fresh_input + usage.cache_read + usage.cache_write
            output_tokens = max(usage.total_output, usage.visible_output + usage.reasoning)
            reasoning_tokens = usage.reasoning
            model = _resolve_agy_usage_model(meta, usage, is_retry=is_retry)
            event = UsageEvent(
                provider="antigravity",
                event_id=digest(["antigravity", session_id, usage.identity(fallback)]),
                session=session_id,
                timestamp=timestamp,
                model=model,
                project="unknown",
                input_tokens=total_input,
                output_tokens=output_tokens,
                cache_read_tokens=usage.cache_read,
                cache_write_tokens=usage.cache_write,
                reasoning_tokens=reasoning_tokens,
                request_input_tokens=total_input,
            )
            validate(event)
            events.append(event)
    return events, issues


def validate(event: UsageEvent):
    if event.cache_read_tokens is not None and event.cache_read_tokens > event.input_tokens:
        raise ValueError("Cache reads exceed total input")
    if event.cache_write_tokens is not None and event.cache_write_tokens > event.input_tokens:
        raise ValueError("Cache writes exceed total input")
    if event.cache_read_tokens is not None and event.cache_write_tokens is not None:
        if event.cache_read_tokens + event.cache_write_tokens > event.input_tokens:
            raise ValueError("Cache components exceed total input")
    if event.reasoning_tokens is not None and event.reasoning_tokens > event.output_tokens:
        raise ValueError("Reasoning exceeds output")
