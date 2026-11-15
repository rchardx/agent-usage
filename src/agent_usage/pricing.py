"""Current standard API-equivalent cost, calculated per request before aggregation."""

import json
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .cache import write_json
from .models import utc_now

SOURCE = "https://models.dev/api.json?type=all"
CACHE_SECONDS = 86400
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
PROVIDERS = {"codex": "openai", "claude": "anthropic"}
# First-party channels, in preference order. Gateway events whose upstream is not
# in the catalog (for example "radius/kimi-k3") fall back to an exact model-id
# match in these providers, so the model is priced at its own channel's rates.
FIRST_PARTY = (
    "anthropic",
    "openai",
    "google",
    "deepseek",
    "moonshotai",
    "zai",
    "xai",
    "minimax",
    "qwen",
    "meta",
    "mistralai",
)
MILLION = Decimal(1_000_000)


def resolve_catalog(provider: str, model: str, models: dict) -> tuple[str | None, str]:
    """Catalog provider and model id for an agent event; no alias guessing."""
    catalog = PROVIDERS.get(provider)
    if catalog:
        return catalog, model.removeprefix(f"{catalog}/")
    # pi/omp run many upstreams; trust only an explicit "provider/model" prefix.
    prefix, separator, rest = model.partition("/")
    if separator and rest:
        if prefix in models:
            return prefix, rest
        # Unknown upstream (a gateway such as radius): price the real model at
        # its first-party channel, exact id only; gateway-only aliases stay
        # unpriced.
        for candidate in FIRST_PARTY:
            if rest in models.get(candidate, {}):
                return candidate, rest
    return None, model


def money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.000001")), "f")


def rate(cost: dict, field: str) -> Decimal:
    value = cost.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"missing_{field}_price")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("invalid_price")
    return result


def catalog_models(data: dict) -> dict:
    result = {}
    # Keep every catalog provider: pi/omp events carry explicit upstream prefixes.
    for provider, entry in data.items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models", {})
        result[provider] = {
            name: {"cost": model["cost"]}
            for name, model in models.items()
            if isinstance(model, dict) and isinstance(model.get("cost"), dict)
        }
    if not any(result.values()):
        raise ValueError("No supported pricing models")
    return result


@dataclass
class Pricing:
    models: dict
    status: str
    fetched_at: str | None = None
    error: str | None = None

    def metadata(self) -> dict:
        return {
            "source": SOURCE,
            "status": self.status,
            "fetched_at": self.fetched_at,
            "currency": "USD",
            "basis": "current_standard_api_rates",
            "error": self.error,
            "note": (
                "Current standard API-equivalent estimate, not subscription charges. "
                "Excludes priority/fast/batch adjustments, regional modifiers, tool fees, and tax."
            ),
        }

    def estimate(self, event: dict) -> dict:
        provider, model_id = resolve_catalog(event["provider"], event["model"], self.models)
        models = self.models.get(provider, {})
        model = models.get(model_id) if isinstance(models, dict) else None
        if not isinstance(model, dict):
            return {"amount": None, "reason": "model_not_priced", "model": model_id}
        try:
            cost = self._rates(model["cost"], event)
            read, write = event.get("cache_read_tokens"), event.get("cache_write_tokens")
            if read is None or write is None:
                raise ValueError("unknown_cache_usage")
            fresh = event["input_tokens"] - read - write
            if fresh < 0:
                raise ValueError("inconsistent_cache_usage")
            parts = {
                "fresh_input": fresh * rate(cost, "input") / MILLION,
                "cache_read": read * rate(cost, "cache_read") / MILLION if read else Decimal(0),
                "cache_write": self._writes(cost, event, write, event["provider"]),
            }
            output_price = rate(cost, "output")
            reasoning_price = rate(cost, "reasoning") if "reasoning" in cost else output_price
            if reasoning_price != output_price:
                reasoning = event.get("reasoning_tokens")
                if reasoning is None:
                    raise ValueError("unknown_reasoning_usage")
                parts["output"] = (
                    (event["output_tokens"] - reasoning) * output_price
                    + reasoning * reasoning_price
                ) / MILLION
            else:
                parts["output"] = event["output_tokens"] * output_price / MILLION
            return {
                "amount": sum(parts.values()),
                "components": parts,
                "model": model_id,
                "pricing_provider": provider,
            }
        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "unsupported_price_structure"
            return {"amount": None, "reason": reason, "model": model_id}

    @staticmethod
    def _rates(base: dict, event: dict) -> dict:
        tiers = base.get("tiers")
        if tiers is None and base.get("context_over_200k"):
            tiers = [{**base["context_over_200k"], "tier": {"type": "context", "size": 200000}}]
        if not tiers:
            return base
        size = event.get("request_input_tokens")
        if size is None:
            raise ValueError("unknown_request_size_for_tier")
        for item in tiers:
            threshold = item["tier"]["size"]
            if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold < 0:
                raise ValueError("invalid_price_tier")
        selected = base
        for tier in sorted(tiers, key=lambda item: item["tier"]["size"]):
            selector = tier["tier"]
            if selector.get("type", "context") != "context":
                raise ValueError("unsupported_price_tier")
            if size > selector["size"]:
                # Tiers are full price tables; absent cache rates must remain unknown.
                selected = tier
        return selected

    @staticmethod
    def _writes(cost: dict, event: dict, write: int, agent: str | None) -> Decimal:
        if write == 0:
            return Decimal(0)
        # Only Claude Code records carry a 5m/1h TTL split; other agents' writes
        # use the catalog cache_write (5m) rate even on the anthropic channel.
        if agent != "claude":
            return write * rate(cost, "cache_write") / MILLION
        short = event.get("cache_write_5m_tokens")
        long = event.get("cache_write_1h_tokens")
        if short is None or long is None:
            raise ValueError("unknown_cache_write_ttl")
        if short + long != write:
            raise ValueError("inconsistent_cache_write_ttl")
        # models.dev cache_write describes 5m writes. Anthropic documents 1h at 2x input.
        short_cost = short * rate(cost, "cache_write") if short else Decimal(0)
        long_cost = long * rate(cost, "input") * 2 if long else Decimal(0)
        return (short_cost + long_cost) / MILLION


def load_pricing(cache: Path, *, offline=False, refresh=False, timeout=10) -> Pricing:
    path = cache / "models-dev-pricing.json"
    snapshot = None
    try:
        candidate = json.loads(path.read_text())
        age = (datetime.now(UTC) - datetime.fromisoformat(candidate["fetched_at"])).total_seconds()
        if (
            candidate.get("source") == SOURCE
            and isinstance(candidate.get("models"), dict)
            and age >= 0
        ):
            candidate["models"] = catalog_models(
                {provider: {"models": models} for provider, models in candidate["models"].items()}
            )
            snapshot = candidate
            if offline or (not refresh and age < CACHE_SECONDS):
                return Pricing(
                    candidate["models"],
                    "cached" if age < CACHE_SECONDS else "stale",
                    candidate["fetched_at"],
                )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    if offline:
        return Pricing({}, "unavailable", error="No cached models.dev prices; run online once.")
    request = urllib.request.Request(
        SOURCE, headers={"User-Agent": "agent-usage/0.1.0", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Oversized pricing response")
        models = catalog_models(json.loads(raw))
        fetched_at = utc_now()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        error: str | None = (
            f"models.dev request failed (HTTP {exc.code})."
            if isinstance(exc, urllib.error.HTTPError)
            else "models.dev prices are unavailable."
        )
        if snapshot:
            return Pricing(snapshot["models"], "stale", snapshot["fetched_at"], error)
        return Pricing({}, "unavailable", error=error)
    error = None
    try:
        write_json(path, {"source": SOURCE, "fetched_at": fetched_at, "models": models})
    except OSError:
        error = "Could not cache models.dev prices."
    return Pricing(models, "live", fetched_at, error)


def cost_summary(events: list[dict]) -> dict:
    total = Decimal(0)
    priced = 0
    reasons: Counter = Counter()
    details: Counter = Counter()
    components = {key: Decimal(0) for key in ("fresh_input", "cache_read", "cache_write", "output")}
    for event in events:
        estimate = event["_cost"]
        if estimate["amount"] is None:
            reasons[estimate["reason"]] += 1
            details[(event["provider"], event["model"], estimate["reason"])] += 1
        else:
            priced += 1
            total += estimate["amount"]
            for key, amount in estimate["components"].items():
                components[key] += amount
    return {
        "currency": "USD",
        "amount": money(total) if priced == len(events) else None,
        "known_amount": money(total),
        "priced_events": priced,
        "total_events": len(events),
        "unpriced_reasons": dict(reasons),
        "unpriced_details": [
            {"provider": provider, "model": model, "reason": reason, "events": count}
            for (provider, model, reason), count in sorted(details.items())
        ],
        "known_components": {key: money(value) for key, value in components.items()},
    }
