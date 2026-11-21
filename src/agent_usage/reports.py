import hashlib
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from .allowances import recognized_account
from .cache import write_json as save_snapshot
from .models import AccountReport, ProbeError, utc_now
from .parsers import METRICS
from .paths import Paths
from .pricing import Pricing, cost_summary, money, resolve_catalog
from .providers import (
    antigravity_non_subscription_mode,
    antigravity_override_mode,
    claude_non_subscription_mode,
    claude_override_mode,
    codex_config_override,
    codex_non_subscription_mode,
    fetch_antigravity,
    fetch_claude,
    fetch_codex,
    non_subscription_account,
)

# pi/omp are multi-provider harnesses, not subscription accounts; they have no
# account quota concept and contribute local token history only.
ACCOUNT_PROVIDERS = ("codex", "claude", "antigravity")
CREDENTIAL_FILES = {"codex": "auth.json", "claude": ".credentials.json"}


def snapshot_path(paths: Paths, provider: str) -> Path:
    home = getattr(paths, provider)
    cred_name = CREDENTIAL_FILES.get(provider)
    if cred_name is not None:
        try:
            stat = (home / cred_name).stat()
            identity = f"{home}:{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            identity = f"{home}:no-file-credentials"
    else:
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
        return recognized_account(data)
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _non_subscription_override(paths: Paths, provider: str) -> tuple[str, str] | None:
    if provider == "codex":
        return codex_config_override(paths.codex)
    if provider == "antigravity":
        return antigravity_override_mode(paths.antigravity)
    return claude_override_mode(paths.claude)


def _local_non_subscription(paths: Paths, provider: str) -> AccountReport | None:
    if provider == "codex":
        found = codex_non_subscription_mode(paths.codex)
    elif provider == "antigravity":
        found = antigravity_non_subscription_mode(paths.antigravity)
    else:
        found = claude_non_subscription_mode(paths.claude)
    return non_subscription_account(provider, found[1], found[0]) if found is not None else None


def account_report(
    paths: Paths, provider: str, *, refresh: bool, offline: bool, timeout: float
) -> dict:
    cache_path = snapshot_path(paths, provider)
    home = getattr(paths, provider)
    cred_name = CREDENTIAL_FILES.get(provider)
    override = _non_subscription_override(paths, provider)
    # Without a file identity we cannot safely bind a cache to an OS-keychain account;
    # an active API-key/gateway override also supersedes any cached subscription snapshot.
    cacheable = cred_name is not None and (home / cred_name).is_file() and override is None
    cached = read_snapshot(cache_path) if cacheable else None
    if cached and (offline or (not refresh and cached["age_seconds"] < 120)):
        cached["status"] = "stale" if cached["age_seconds"] >= 120 else "cached"
        return cached
    if offline:
        local = _local_non_subscription(paths, provider)
        if local is not None:
            return {**local.to_dict(), "age_seconds": 0}
        return AccountReport(
            provider, "local-cache", status="unavailable", error="No matching cached snapshot."
        ).to_dict()
    try:
        fetcher = {
            "codex": fetch_codex,
            "claude": fetch_claude,
            "antigravity": fetch_antigravity,
        }[provider]
        report = fetcher(home, timeout).to_dict()
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
    providers = [name for name in providers if name in ACCOUNT_PROVIDERS]
    with ThreadPoolExecutor(max_workers=len(providers)) as pool:
        reports = list(pool.map(lambda name: account_report(paths, name, **options), providers))
    return {"scope": "account", "accounts": reports}


def period(
    since: str | None, until: str | None, month: bool, all_time: bool, zone, *, today=False
) -> tuple:
    if sum((since is not None, month, all_time, today)) > 1:
        raise ValueError("Choose only one of --since, --month, --today, or --all.")
    now = datetime.now(zone)
    end = datetime.fromisoformat(until.replace("Z", "+00:00")) if until else now
    if end.tzinfo is None:
        end = end.replace(tzinfo=zone)
    if all_time:
        start = None
    elif month:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif today:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif since:
        if since.endswith("d") and since[:-1].isdigit():
            days = int(since[:-1])
            if days < 1:
                raise ValueError("Day count must be positive.")
            start = (end.astimezone(UTC) - timedelta(days=days)).astimezone(zone)
        else:
            start = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if start.tzinfo is None:
                start = start.replace(tzinfo=zone)
    else:
        start = (end.astimezone(UTC) - timedelta(days=30)).astimezone(zone)
    if start is not None and start >= end:
        raise ValueError("Start must precede the exclusive end time.")
    return start, end


ALL_METRICS = (*METRICS, "fresh_input_tokens", "non_reasoning_output_tokens", "total_tokens")
COST_COMPONENTS = ("fresh_input", "cache_read", "cache_write", "output")


@dataclass
class _AggregateState:
    n_events: int = 0
    sessions: set[tuple[str, str]] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ALL_METRICS, 0))
    sums: dict[str, int] = field(default_factory=lambda: dict.fromkeys(ALL_METRICS, 0))
    has_cost: bool = False
    priced: int = 0
    cost_total: Decimal = Decimal(0)
    cost_components: dict[str, Decimal] = field(
        default_factory=lambda: dict.fromkeys(COST_COMPONENTS, Decimal(0))
    )
    unpriced_reasons: Counter[str] = field(default_factory=Counter)
    unpriced_details: Counter[tuple[str, str, str]] = field(default_factory=Counter)

    def merge(self, other: "_AggregateState") -> None:
        self.n_events += other.n_events
        self.sessions.update(other.sessions)
        for key in ALL_METRICS:
            self.counts[key] += other.counts[key]
            self.sums[key] += other.sums[key]
        if other.has_cost:
            self.has_cost = True
            self.priced += other.priced
            self.cost_total += other.cost_total
            for comp in COST_COMPONENTS:
                self.cost_components[comp] += other.cost_components[comp]
            self.unpriced_reasons.update(other.unpriced_reasons)
            self.unpriced_details.update(other.unpriced_details)

    def to_dict(self) -> dict:
        n = self.n_events
        result: dict[str, Any] = {
            "events": n,
            "sessions": len(self.sessions),
            "metrics": {
                key: {
                    "tokens": self.sums[key] if self.counts[key] == n else None,
                    "known_tokens": self.sums[key],
                    "observed_events": self.counts[key],
                    "total_events": n,
                }
                for key in ALL_METRICS
            },
        }
        if self.has_cost:
            result["estimated_cost"] = {
                "currency": "USD",
                "amount": money(self.cost_total) if self.priced == n else None,
                "known_amount": money(self.cost_total),
                "priced_events": self.priced,
                "total_events": n,
                "unpriced_reasons": dict(self.unpriced_reasons),
                "unpriced_details": [
                    {"provider": provider, "model": model, "reason": reason, "events": count}
                    for (provider, model, reason), count in sorted(self.unpriced_details.items())
                ],
                "known_components": {
                    key: money(value) for key, value in self.cost_components.items()
                },
            }
        return result


def _accumulate(events: list[dict], costs: list[dict] | None = None) -> _AggregateState:
    state = _AggregateState(n_events=len(events))
    counts = state.counts
    sums = state.sums
    sessions = state.sessions
    has_inline_cost = costs is None and bool(events) and "_cost" in events[0]
    if costs is not None and events:
        state.has_cost = True
    elif has_inline_cost:
        state.has_cost = True
    for idx, event in enumerate(events):
        sessions.add((event["provider"], event["session"]))
        inp = event.get("input_tokens")
        out = event.get("output_tokens")
        read = event.get("cache_read_tokens")
        write = event.get("cache_write_tokens")
        reas = event.get("reasoning_tokens")
        cw5 = event.get("cache_write_5m_tokens")
        cw1 = event.get("cache_write_1h_tokens")
        if inp is not None:
            counts["input_tokens"] += 1
            sums["input_tokens"] += inp
        if out is not None:
            counts["output_tokens"] += 1
            sums["output_tokens"] += out
        if read is not None:
            counts["cache_read_tokens"] += 1
            sums["cache_read_tokens"] += read
        if write is not None:
            counts["cache_write_tokens"] += 1
            sums["cache_write_tokens"] += write
        if reas is not None:
            counts["reasoning_tokens"] += 1
            sums["reasoning_tokens"] += reas
            if out is not None:
                counts["non_reasoning_output_tokens"] += 1
                sums["non_reasoning_output_tokens"] += out - reas
        if cw5 is not None:
            counts["cache_write_5m_tokens"] += 1
            sums["cache_write_5m_tokens"] += cw5
        if cw1 is not None:
            counts["cache_write_1h_tokens"] += 1
            sums["cache_write_1h_tokens"] += cw1
        if inp is not None and read is not None and write is not None:
            counts["fresh_input_tokens"] += 1
            sums["fresh_input_tokens"] += inp - read - write
        if inp is not None and out is not None:
            counts["total_tokens"] += 1
            sums["total_tokens"] += inp + out
        estimate = (
            costs[idx] if costs is not None else (event["_cost"] if has_inline_cost else None)
        )
        if estimate is not None:
            if estimate["amount"] is None:
                reason = estimate["reason"]
                state.unpriced_reasons[reason] += 1
                state.unpriced_details[(event["provider"], event["model"], reason)] += 1
            else:
                state.priced += 1
                state.cost_total += estimate["amount"]
                comps = estimate["components"]
                for comp in COST_COMPONENTS:
                    state.cost_components[comp] += comps[comp]
    return state


def summarize(
    events: list[dict],
    start,
    end,
    by: str,
    zone,
    coverage: dict,
    pricing: Pricing | None = None,
    *,
    prefiltered: bool = False,
) -> dict:
    selected_pairs: list[tuple[dict, datetime | None]] = []
    if prefiltered:
        selected_pairs = [(event, None) for event in events]
    else:
        for event in events:
            ts = datetime.fromisoformat(event["timestamp"])
            if (start is None or ts >= start) and ts < end:
                selected_pairs.append((event, ts))
    groups: dict[tuple, tuple[list[dict], list[dict] | None]] = {}
    unique_models: set[tuple[str, str]] = set()
    for event, parsed_ts in selected_pairs:
        provider = event["provider"]
        if pricing is not None:
            unique_models.add((provider, event["model"]))
        if by == "day":
            ts = parsed_ts if parsed_ts is not None else datetime.fromisoformat(event["timestamp"])
            value = ts.astimezone(zone).date().isoformat()
        elif by == "provider":
            value = provider
        else:
            value = event[by]
        bucket = groups.get((provider, value))
        if bucket is None:
            bucket = ([], [] if pricing is not None else None)
            groups[(provider, value)] = bucket
        bucket[0].append(event)
        if bucket[1] is not None and pricing is not None:
            bucket[1].append(pricing.estimate(event))
    total_state = _AggregateState()
    rendered_groups = []
    for key in sorted(groups):
        rows, row_costs = groups[key]
        group_state = _accumulate(rows, row_costs)
        total_state.merge(group_state)
        rendered_groups.append({"provider": key[0], "group": key[1], **group_state.to_dict()})
    n_selected = len(selected_pairs)
    report: dict = {
        "scope": "local-records",
        "timezone": str(zone),
        "period": {"start": start.isoformat() if start else None, "end_exclusive": end.isoformat()},
        "group_by": by,
        "groups": rendered_groups,
        "totals": total_state.to_dict(),
        "coverage": {
            **coverage,
            "indexed_events": coverage.get("indexed_events", len(events)),
            "selected_events": n_selected,
            "earliest_record": coverage["earliest_record"]
            if "earliest_record" in coverage
            else min((e["timestamp"] for e in events), default=None),
            "latest_record": coverage["latest_record"]
            if "latest_record" in coverage
            else max((e["timestamp"] for e in events), default=None),
            "history_complete": False,
            "note": (
                "Only available local logs are counted; deleted logs and other devices are absent."
            ),
        },
    }
    if pricing is not None:
        report["pricing"] = pricing.metadata()
        if not n_selected:
            report["totals"]["estimated_cost"] = cost_summary([])
        matches = []
        for provider, model in sorted(unique_models):
            catalog_provider, model_id = resolve_catalog(provider, model, pricing.models)
            match = pricing.models.get(catalog_provider, {}).get(model_id)
            matches.append(
                {
                    "agent": provider,
                    "provider": catalog_provider,
                    "model": model,
                    "cost_per_million_usd": match.get("cost") if match else None,
                }
            )
        report["pricing"]["models"] = matches
    return report


def aggregate(events: list[dict]) -> dict:
    return _accumulate(events).to_dict()


def envelope(**parts: Any) -> dict:
    return {"schema_version": 1, "generated_at": utc_now(), **parts}
