"""Current standard API-equivalent cost, calculated per request before aggregation."""

import json
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from . import USER_AGENT
from .cache import write_json
from .models import utc_now
from .parsers import ANTIGRAVITY_EFFORTS

SOURCE = "https://models.dev/api.json?type=all"
CACHE_SECONDS = 86400
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
PROVIDERS = {"codex": "openai", "claude": "anthropic"}
# Explicit harness upstream identifiers that map to a different models.dev key.
UPSTREAM_ALIASES = {
    "amazonBedrock": "amazon-bedrock",
    "bedrock": "amazon-bedrock",
    "fireworks": "fireworks-ai",
    "gcp": "google-vertex",
    "mistralai": "mistral",
    "openai-codex": "openai",
    "qwen": "alibaba",
    "together": "togetherai",
    "vercel-ai-gateway": "vercel",
    "vertex": "google-vertex",
}
# First-party and cloud channels, in preference order. Gateway events whose
# upstream is not in the catalog (for example "radius/gemini-2.5-pro") fall back
# to an exact model-id match in these providers at the model's own channel rates.
FIRST_PARTY = (
    "anthropic",
    "openai",
    "google",
    "google-vertex",
    "google-vertex-anthropic",
    "amazon-bedrock",
    "deepseek",
    "moonshotai",
    "zai",
    "xai",
    "minimax",
    "alibaba",
    "qwen",
    "meta",
    "mistral",
    "mistralai",
)
ANTHROPIC_TTL_PROVIDERS = frozenset({"anthropic", "google-vertex-anthropic", "amazon-bedrock"})
KNOWN_UPSTREAMS = frozenset(
    (*FIRST_PARTY, *UPSTREAM_ALIASES, *UPSTREAM_ALIASES.values(), "openrouter", "vercel", "azure")
)
MILLION = Decimal(1_000_000)


def _strip_models_prefix(model: str) -> str:
    if model.startswith("models/") and len(model) > len("models/"):
        return model.removeprefix("models/")
    return model


def _strip_effort_suffix(model: str) -> str | None:
    for effort in ANTIGRAVITY_EFFORTS:
        suffix = f"-{effort}"
        if model.endswith(suffix) and len(model) > len(suffix):
            return model.removesuffix(suffix)
    return None


def _has_catalog_cost(catalog_provider: str | None, model_id: str, models: dict) -> bool:
    if catalog_provider is None:
        return False
    entry = models.get(catalog_provider, {}).get(model_id)
    cost = entry.get("cost") if isinstance(entry, dict) else None
    return isinstance(cost, dict) and bool(cost)


def _catalog_key(prefix: str, models: dict) -> str | None:
    if prefix in models:
        return prefix
    mapped = UPSTREAM_ALIASES.get(prefix)
    if mapped and mapped in models:
        return mapped
    return None


def _first_party_match(model_id: str, models: dict) -> tuple[str, str] | None:
    for candidate in FIRST_PARTY:
        if model_id in models.get(candidate, {}):
            return candidate, model_id
    return None


def _resolve_exact(provider: str, model: str, models: dict) -> tuple[str | None, str]:
    clean = _strip_models_prefix(model)
    prefix, separator, rest = clean.partition("/")
    if separator and rest:
        catalog = _catalog_key(prefix, models)
        if catalog is not None:
            return catalog, _strip_models_prefix(rest)
        if prefix in KNOWN_UPSTREAMS:
            return UPSTREAM_ALIASES.get(prefix, prefix), _strip_models_prefix(rest)
        # Unknown gateway prefix (for example "radius/gemini-2.5-pro" or
        # "my-proxy/google/gemini-2.5-pro"): resolve an explicit catalog
        # sub-prefix or an exact first-party model id; gateway aliases stay unpriced.
        rest_clean = _strip_models_prefix(rest)
        sub_prefix, sub_sep, sub_rest = rest_clean.partition("/")
        if sub_sep and sub_rest:
            sub_catalog = _catalog_key(sub_prefix, models)
            if sub_catalog is not None:
                return sub_catalog, _strip_models_prefix(sub_rest)
            if sub_prefix in KNOWN_UPSTREAMS:
                return UPSTREAM_ALIASES.get(sub_prefix, sub_prefix), _strip_models_prefix(sub_rest)
        found = _first_party_match(rest_clean, models)
        if found is not None:
            return found
        return (PROVIDERS[provider], clean) if provider in PROVIDERS else (None, clean)

    default_catalog = PROVIDERS.get(provider)
    if default_catalog:
        if clean in models.get(default_catalog, {}):
            return default_catalog, clean
        if provider == "claude":
            for cloud in ("google-vertex-anthropic", "amazon-bedrock"):
                if clean in models.get(cloud, {}):
                    return cloud, clean
        if clean.startswith("gemini-"):
            for google_channel in ("google", "google-vertex"):
                if clean in models.get(google_channel, {}):
                    return google_channel, clean
        return default_catalog, clean
    if provider == "antigravity":
        if clean.startswith("gemini-"):
            for google_channel in ("google", "google-vertex"):
                if clean in models.get(google_channel, {}):
                    return google_channel, clean
            return "google", clean
        if clean.startswith("claude-"):
            for anthropic_channel in ("anthropic", "google-vertex-anthropic"):
                if clean in models.get(anthropic_channel, {}):
                    return anthropic_channel, clean
            return "anthropic", clean
        if clean.startswith(("gpt-", "o1", "o3", "o4")):
            return "openai", clean
    return None, clean


def resolve_catalog(provider: str, model: str, models: dict) -> tuple[str | None, str]:
    """Catalog provider and model id; Antigravity effort suffixes fall back to priced base."""
    exact_provider, exact_id = _resolve_exact(provider, model, models)
    if _has_catalog_cost(exact_provider, exact_id, models):
        return exact_provider, exact_id
    if provider == "antigravity":
        if exact_provider is not None and exact_id in models.get(exact_provider, {}):
            base_id = _strip_effort_suffix(exact_id)
            if base_id is not None and _has_catalog_cost(exact_provider, base_id, models):
                return exact_provider, base_id
            return exact_provider, exact_id
        stripped = _strip_effort_suffix(model)
        if stripped is not None:
            base_provider, base_id = _resolve_exact(provider, stripped, models)
            if _has_catalog_cost(base_provider, base_id, models):
                return base_provider, base_id
    return exact_provider, exact_id


def _has_cache_write_rate(base_cost: dict) -> bool:
    if "cache_write" in base_cost:
        return True
    over_200k = base_cost.get("context_over_200k")
    if isinstance(over_200k, dict) and "cache_write" in over_200k:
        return True
    tiers = base_cost.get("tiers")
    return isinstance(tiers, list) and any(
        isinstance(item, dict) and "cache_write" in item for item in tiers
    )


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


def _error_reason(exc: Exception) -> str:
    return str(exc) if isinstance(exc, ValueError) else "unsupported_price_structure"


@dataclass
class _CompiledRateTable:
    raw: dict
    has_reasoning: bool
    rates: dict[str, Decimal | str] = field(default_factory=dict)

    def get(self, name: str) -> Decimal:
        cached = self.rates.get(name)
        if cached is None:
            try:
                cached = rate(self.raw, name)
            except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
                cached = _error_reason(exc)
            self.rates[name] = cached
        if isinstance(cached, str):
            raise ValueError(cached)
        return cached


@dataclass
class _CompiledModel:
    provider: str | None
    model_id: str
    priced: bool
    has_cache_write: bool = False
    base_table: _CompiledRateTable | None = None
    has_tiers: bool = False
    tier_error: str | None = None
    tiers: tuple[tuple[int, _CompiledRateTable], ...] = ()
    structure_error: str | None = None


def _compile_rate_table(raw: dict) -> _CompiledRateTable:
    return _CompiledRateTable(raw=raw, has_reasoning="reasoning" in raw)


@dataclass
class Pricing:
    models: dict
    status: str
    fetched_at: str | None = None
    error: str | None = None
    _compiled: dict[tuple[str, str], _CompiledModel] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

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

    def _compile_model(self, agent: str, model_name: str) -> _CompiledModel:
        key = (agent, model_name)
        cached = self._compiled.get(key)
        if cached is not None:
            return cached
        provider, model_id = resolve_catalog(agent, model_name, self.models)
        models = self.models.get(provider, {})
        model = models.get(model_id) if isinstance(models, dict) else None
        if not isinstance(model, dict):
            compiled = _CompiledModel(provider=provider, model_id=model_id, priced=False)
            self._compiled[key] = compiled
            return compiled
        try:
            base_cost = model["cost"]
            if not isinstance(base_cost, dict):
                raise TypeError("Invalid cost mapping")
            has_cw = _has_cache_write_rate(base_cost)
            base_table = _compile_rate_table(base_cost)
            raw_tiers = base_cost.get("tiers")
            if raw_tiers is None and base_cost.get("context_over_200k"):
                raw_tiers = [
                    {**base_cost["context_over_200k"], "tier": {"type": "context", "size": 200000}}
                ]
            has_tiers = bool(raw_tiers)
            tier_error: str | None = None
            compiled_tiers: list[tuple[int, _CompiledRateTable]] = []
            if raw_tiers:
                try:
                    for item in raw_tiers:
                        threshold = item["tier"]["size"]
                        if (
                            isinstance(threshold, bool)
                            or not isinstance(threshold, int)
                            or threshold < 0
                        ):
                            raise ValueError("invalid_price_tier")
                    for tier in sorted(raw_tiers, key=lambda item: item["tier"]["size"]):
                        selector = tier["tier"]
                        if selector.get("type", "context") != "context":
                            raise ValueError("unsupported_price_tier")
                        compiled_tiers.append((selector["size"], _compile_rate_table(tier)))
                except (ValueError, TypeError, KeyError, AttributeError) as exc:
                    tier_error = _error_reason(exc)
            compiled = _CompiledModel(
                provider=provider,
                model_id=model_id,
                priced=True,
                has_cache_write=has_cw,
                base_table=base_table,
                has_tiers=has_tiers,
                tier_error=tier_error,
                tiers=tuple(compiled_tiers),
            )
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            compiled = _CompiledModel(
                provider=provider,
                model_id=model_id,
                priced=True,
                structure_error=_error_reason(exc),
            )
        self._compiled[key] = compiled
        return compiled

    def estimate(self, event: dict) -> dict:
        agent = event["provider"]
        compiled = self._compile_model(agent, event["model"])
        provider, model_id = compiled.provider, compiled.model_id
        if not compiled.priced:
            return {"amount": None, "reason": "model_not_priced", "model": model_id}
        if compiled.structure_error is not None:
            return {"amount": None, "reason": compiled.structure_error, "model": model_id}
        try:
            assert compiled.base_table is not None
            table = self._select_table(compiled, event)
            read, write = event.get("cache_read_tokens"), event.get("cache_write_tokens")
            if read is None:
                raise ValueError("unknown_cache_usage")
            if write is None:
                if (
                    agent == "claude"
                    or provider in ANTHROPIC_TTL_PROVIDERS
                    or compiled.has_cache_write
                ):
                    raise ValueError("unknown_cache_usage")
                effective_write = 0
            else:
                effective_write = write
            fresh = event["input_tokens"] - read - effective_write
            if fresh < 0:
                raise ValueError("inconsistent_cache_usage")
            fresh_cost = fresh * table.get("input") / MILLION
            read_cost = read * table.get("cache_read") / MILLION if read else Decimal(0)
            write_cost = self._writes_compiled(table, event, effective_write, agent, provider)
            output_price = table.get("output")
            reasoning_price = table.get("reasoning") if table.has_reasoning else output_price
            if reasoning_price != output_price:
                reasoning = event.get("reasoning_tokens")
                if reasoning is None:
                    raise ValueError("unknown_reasoning_usage")
                output_cost = (
                    (event["output_tokens"] - reasoning) * output_price
                    + reasoning * reasoning_price
                ) / MILLION
            else:
                output_cost = event["output_tokens"] * output_price / MILLION
            parts = {
                "fresh_input": fresh_cost,
                "cache_read": read_cost,
                "cache_write": write_cost,
                "output": output_cost,
            }
            return {
                "amount": fresh_cost + read_cost + write_cost + output_cost,
                "components": parts,
                "model": model_id,
                "pricing_provider": provider,
            }
        except (ValueError, TypeError, KeyError, AttributeError, InvalidOperation) as exc:
            reason = str(exc) if isinstance(exc, ValueError) else "unsupported_price_structure"
            return {"amount": None, "reason": reason, "model": model_id}

    @staticmethod
    def _select_table(compiled: _CompiledModel, event: dict) -> _CompiledRateTable:
        assert compiled.base_table is not None
        if not compiled.has_tiers:
            return compiled.base_table
        size = event.get("request_input_tokens")
        if size is None:
            raise ValueError("unknown_request_size_for_tier")
        if compiled.tier_error is not None:
            raise ValueError(compiled.tier_error)
        selected = compiled.base_table
        for threshold, tier_table in compiled.tiers:
            if size > threshold:
                # Tiers are full price tables; absent cache rates must remain unknown.
                selected = tier_table
        return selected

    @staticmethod
    def _writes_compiled(
        table: _CompiledRateTable,
        event: dict,
        write: int,
        agent: str | None,
        pricing_provider: str | None = None,
    ) -> Decimal:
        if write == 0:
            return Decimal(0)
        # Only Claude Code on Anthropic channels carries a 5m/1h TTL split; other
        # agents and non-Anthropic gateways use the catalog cache_write rate.
        if agent != "claude" or (
            pricing_provider is not None and pricing_provider not in ANTHROPIC_TTL_PROVIDERS
        ):
            return write * table.get("cache_write") / MILLION
        short = event.get("cache_write_5m_tokens")
        long = event.get("cache_write_1h_tokens")
        if short is None or long is None:
            raise ValueError("unknown_cache_write_ttl")
        if short + long != write:
            raise ValueError("inconsistent_cache_write_ttl")
        # models.dev cache_write describes 5m writes. Anthropic documents 1h at 2x input.
        short_cost = short * table.get("cache_write") if short else Decimal(0)
        long_cost = long * table.get("input") * 2 if long else Decimal(0)
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
        SOURCE, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
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
