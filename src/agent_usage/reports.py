import hashlib
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .models import AccountReport, ProbeError, utc_now
from .parsers import METRICS
from .paths import Paths
from .providers import fetch_claude, fetch_codex


def snapshot_path(paths: Paths, provider: str) -> Path:
    home = paths.codex if provider == "codex" else paths.claude
    credentials = home / ("auth.json" if provider == "codex" else ".credentials.json")
    try:
        stat = credentials.stat()
        identity = f"{home}:{stat.st_mtime_ns}:{stat.st_size}"
    except OSError:
        identity = f"{home}:no-file-credentials"
    key = hashlib.sha256(identity.encode()).hexdigest()[:20]
    return paths.cache / f"quota-{provider}-{key}.json"


def read_snapshot(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict) or not data.get("observed_at"):
            return None
        age = datetime.now(UTC) - datetime.fromisoformat(data["observed_at"])
        if age.total_seconds() < 0:
            return None
        data["age_seconds"] = round(age.total_seconds())
        return data
    except (OSError, ValueError, TypeError):
        return None


def save_snapshot(path: Path, report: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".quota-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(report, handle)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def account_report(
    paths: Paths, provider: str, *, refresh: bool, offline: bool, timeout: float
) -> dict:
    cache_path = snapshot_path(paths, provider)
    home = paths.codex if provider == "codex" else paths.claude
    credentials = home / ("auth.json" if provider == "codex" else ".credentials.json")
    # Without a file identity we cannot safely bind a cache to an OS-keychain account.
    cacheable = credentials.is_file()
    cached = read_snapshot(cache_path) if cacheable else None
    if cached and (offline or (not refresh and cached["age_seconds"] < 120)):
        cached["status"] = "stale" if cached["age_seconds"] >= 120 else "cached"
        return cached
    if offline:
        return AccountReport(
            provider, "local-cache", status="unavailable", error="No matching cached snapshot."
        ).to_dict()
    try:
        report = (
            fetch_codex(paths.codex, timeout)
            if provider == "codex"
            else fetch_claude(paths.claude, timeout)
        ).to_dict()
        if report["status"] == "ok" and cacheable and snapshot_path(paths, provider) == cache_path:
            try:
                save_snapshot(cache_path, report)
            except OSError:
                report["warnings"].append("Could not cache quota snapshot.")
        report["age_seconds"] = 0
        return report
    except (
        ProbeError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
    ) as exc:
        error = str(exc) if isinstance(exc, ProbeError) else "Unsupported provider response."
        if cached:
            cached.update(status="stale", error=error)
            return cached
        return AccountReport(provider, "live-query", status="unavailable", error=error).to_dict()


def quotas(paths: Paths, providers: list[str], **options) -> dict:
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        reports = list(pool.map(lambda name: account_report(paths, name, **options), providers))
    return {"scope": "account", "accounts": reports}


def period(since: str | None, until: str | None, month: bool, all_time: bool, zone) -> tuple:
    if sum((since is not None, month, all_time)) > 1:
        raise ValueError("Choose only one of --since, --month, or --all.")
    now = datetime.now(zone)
    if all_time:
        start = None
    elif month:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif since:
        if since.endswith("d") and since[:-1].isdigit():
            days = int(since[:-1])
            if days < 1:
                raise ValueError("Day count must be positive.")
            start = now - timedelta(days=days)
        else:
            start = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=zone)
    else:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromisoformat(until.replace("Z", "+00:00")) if until else now
    if end.tzinfo is None:
        end = end.replace(tzinfo=zone)
    if start is not None and start >= end:
        raise ValueError("Start must precede the exclusive end time.")
    return start, end


def summarize(events: list[dict], start, end, by: str, zone, coverage: dict) -> dict:
    selected = [
        event
        for event in events
        if (start is None or datetime.fromisoformat(event["timestamp"]) >= start)
        and datetime.fromisoformat(event["timestamp"]) < end
    ]
    groups: dict[tuple, list] = {}
    for event in selected:
        key = event["provider"]
        if by == "day":
            value = datetime.fromisoformat(event["timestamp"]).astimezone(zone).date().isoformat()
        elif by == "provider":
            value = key
        else:
            value = event[by]
        groups.setdefault((key, value), []).append(event)
    return {
        "scope": "local-records",
        "timezone": str(zone),
        "period": {"start": start.isoformat() if start else None, "end_exclusive": end.isoformat()},
        "group_by": by,
        "groups": [
            {"provider": key[0], "group": key[1], **aggregate(rows)}
            for key, rows in sorted(groups.items())
        ],
        "totals": aggregate(selected),
        "coverage": {
            **coverage,
            "indexed_events": len(events),
            "selected_events": len(selected),
            "earliest_record": min((e["timestamp"] for e in events), default=None),
            "latest_record": max((e["timestamp"] for e in events), default=None),
            "history_complete": False,
            "note": (
                "Only available local logs are counted; deleted logs and other devices are absent."
            ),
        },
    }


def aggregate(events: list[dict]) -> dict:
    metrics = (*METRICS, "fresh_input_tokens", "non_reasoning_output_tokens", "total_tokens")
    counts = {key: 0 for key in metrics}
    sums = {key: 0 for key in metrics}
    for event in events:
        values = dict(event)
        read, write = event["cache_read_tokens"], event["cache_write_tokens"]
        values["fresh_input_tokens"] = (
            event["input_tokens"] - read - write if read is not None and write is not None else None
        )
        reasoning = event["reasoning_tokens"]
        values["non_reasoning_output_tokens"] = (
            event["output_tokens"] - reasoning if reasoning is not None else None
        )
        values["total_tokens"] = event["input_tokens"] + event["output_tokens"]
        for key in metrics:
            if values.get(key) is not None:
                counts[key] += 1
                sums[key] += values[key]
    return {
        "events": len(events),
        "sessions": len({(e["provider"], e["session"]) for e in events}),
        "metrics": {
            key: {
                "tokens": sums[key] if counts[key] == len(events) else None,
                "known_tokens": sums[key],
                "observed_events": counts[key],
                "total_events": len(events),
            }
            for key in metrics
        },
    }


def envelope(**parts: Any) -> dict:
    return {"schema_version": 1, "generated_at": utc_now(), **parts}
