import io
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from agent_usage.cache import write_json
from agent_usage.models import UsageEvent
from agent_usage.pricing import SOURCE, Pricing, cost_summary, load_pricing
from agent_usage.reports import summarize


def event(**changes):
    return (
        asdict(
            UsageEvent(
                provider="codex",
                event_id="e",
                session="s",
                timestamp="2026-10-01T00:00:00+00:00",
                model="test-model",
                project="test",
                input_tokens=1000000,
                output_tokens=100000,
                cache_read_tokens=600000,
                cache_write_tokens=100000,
                reasoning_tokens=40000,
                request_input_tokens=1000000,
            )
        )
        | changes
    )


def catalog(cost=None, provider="openai"):
    return Pricing(
        {
            provider: {
                "test-model": {
                    "cost": cost
                    or {
                        "input": 2,
                        "output": 10,
                        "cache_read": 0.2,
                        "cache_write": 2.5,
                    }
                }
            }
        },
        "live",
        "2026-10-05T00:00:00+00:00",
    )


def test_cache_and_reasoning_are_not_billed_twice():
    result = catalog().estimate(event())
    assert result["amount"] == Decimal("1.97")
    assert result["components"] == {
        "fresh_input": Decimal("0.6"),
        "cache_read": Decimal("0.12"),
        "cache_write": Decimal("0.25"),
        "output": Decimal("1"),
    }


def test_separate_reasoning_rate_replaces_output_rate_for_reasoning_tokens():
    pricing = catalog(
        {"input": 2, "output": 10, "reasoning": 20, "cache_read": 0.2, "cache_write": 2.5}
    )
    assert pricing.estimate(event())["amount"] == Decimal("2.37")
    assert pricing.estimate(event(reasoning_tokens=None))["reason"] == "unknown_reasoning_usage"


def test_anthropic_mixed_cache_write_ttls_use_distinct_prices():
    row = event(provider="claude", cache_write_5m_tokens=40000, cache_write_1h_tokens=60000)
    result = catalog(provider="anthropic").estimate(row)
    assert result["components"]["cache_write"] == Decimal("0.34")
    assert result["amount"] == Decimal("2.06")
    row["cache_write_1h_tokens"] = None
    assert catalog(provider="anthropic").estimate(row)["reason"] == "unknown_cache_write_ttl"


def test_tiers_use_request_size_and_explicit_threshold_not_legacy_200k_alias():
    pricing = catalog(
        {
            "input": 2,
            "output": 10,
            "cache_read": 0.2,
            "tiers": [
                {
                    "input": 4,
                    "output": 15,
                    "cache_read": 0.4,
                    "tier": {"type": "context", "size": 272000},
                }
            ],
            "context_over_200k": {"input": 999, "output": 999},
        }
    )
    rows = [
        event(
            input_tokens=250000,
            request_input_tokens=250000,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=1000,
        )
        for _ in range(4)
    ]
    for row in rows:
        row["_cost"] = pricing.estimate(row)
    assert cost_summary(rows)["amount"] == "2.040000"  # 1M monthly input isn't a 1M request
    large = event(
        input_tokens=300000,
        request_input_tokens=300000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=1000,
    )
    assert pricing.estimate(large)["amount"] == Decimal("1.215")
    boundary = large | {"input_tokens": 272000, "request_input_tokens": 272000}
    assert pricing.estimate(boundary)["amount"] == Decimal("0.554")
    assert (
        pricing.estimate(large | {"request_input_tokens": None})["reason"]
        == "unknown_request_size_for_tier"
    )


def gateway_catalog():
    return Pricing(
        {
            "anthropic": {
                "claude-opus-5-5": {
                    "cost": {"input": 4, "output": 20, "cache_read": 0.2, "cache_write": 5}
                }
            },
            "moonshotai": {
                "kimi-k3": {"cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3}}
            },
        },
        "live",
        "2026-10-05T00:00:00+00:00",
    )


def test_unknown_upstream_falls_back_to_first_party_channel_by_exact_id():
    pricing = gateway_catalog()
    row = event(provider="pi", model="radius/claude-opus-5-5")
    result = pricing.estimate(row)
    assert result["pricing_provider"] == "anthropic"
    assert result["model"] == "claude-opus-5-5"
    # First-party anthropic rates, not a guessed gateway rate.
    assert result["components"]["fresh_input"] == Decimal("1.2")
    result = pricing.estimate(event(provider="omp", model="gateway/kimi-k3"))
    assert result["pricing_provider"] == "moonshotai"
    assert result["components"]["cache_read"] == Decimal("0.18")


def test_gateway_aliases_and_unknown_ids_stay_unpriced():
    pricing = gateway_catalog()
    for model in ("radius/balanced", "radius/recursive/flint", "unknown/kimi-k3.9"):
        result = pricing.estimate(event(provider="pi", model=model))
        assert result["reason"] == "model_not_priced", model
    # Known catalog prefixes never fall back to another channel.
    result = pricing.estimate(event(provider="pi", model="anthropic/kimi-k3"))
    assert result["reason"] == "model_not_priced"


def test_missing_model_and_missing_rates_are_unknown_but_explicit_zero_is_valid():
    pricing = catalog({"input": 0, "output": 0})
    assert pricing.estimate(event(model="no-guessing"))["reason"] == "model_not_priced"
    assert pricing.estimate(event())["reason"] == "missing_cache_read_price"
    assert pricing.estimate(event(cache_read_tokens=0, cache_write_tokens=0))["amount"] == 0


@pytest.mark.parametrize("price", [-1, float("nan"), float("inf"), True])
def test_invalid_prices_are_never_charged(price):
    result = catalog({"input": price, "output": 10}).estimate(event())
    assert result["amount"] is None


def test_partial_costs_report_coverage_and_are_not_labeled_as_complete():
    pricing = catalog()
    known, unknown = event(), event(model="missing")
    rows = [{**row, "_cost": pricing.estimate(row)} for row in (known, unknown)]
    result = cost_summary(rows)
    assert result["amount"] is None
    assert result["known_amount"] == "1.970000"
    assert result["priced_events"] == 1
    assert result["unpriced_reasons"] == {"model_not_priced": 1}


def test_costs_are_computed_before_grouping_and_model_rates_are_auditable():
    result = summarize(
        [event()], None, datetime(2026, 11, 1, tzinfo=UTC), "provider", UTC, {}, catalog()
    )
    assert result["totals"]["estimated_cost"]["amount"] == "1.970000"
    assert result["groups"][0]["estimated_cost"]["amount"] == "1.970000"
    assert result["pricing"]["models"][0]["cost_per_million_usd"]["input"] == 2


def test_price_fetch_caches_public_rates_and_offline_never_fetches(paths, monkeypatch):
    calls = []

    def fetch(request, timeout):
        calls.append(request)
        assert request.full_url == SOURCE
        assert request.get_header("Authorization") is None
        return io.BytesIO(json.dumps({"openai": {"models": catalog().models["openai"]}}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fetch)
    first = load_pricing(paths.cache)
    assert first.status == "live"
    assert first.estimate(event())["amount"] == Decimal("1.97")
    assert load_pricing(paths.cache).status == "cached"
    assert load_pricing(paths.cache, offline=True).status == "cached"
    assert len(calls) == 1


def test_price_fetch_failure_preserves_dated_stale_snapshot(paths, monkeypatch):
    fetched = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    write_json(
        paths.cache / "models-dev-pricing.json",
        {
            "source": SOURCE,
            "models": catalog().models,
            "fetched_at": fetched,
        },
    )

    def fail(*args, **kwargs):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", fail)
    pricing = load_pricing(paths.cache)
    assert pricing.status == "stale"
    assert pricing.fetched_at == fetched
    assert pricing.error is not None
    assert pricing.estimate(event())["amount"] == Decimal("1.97")


def test_offline_empty_cache_produces_unknown_cost(paths, monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: pytest.fail("network attempted"))
    pricing = load_pricing(paths.cache, offline=True)
    assert pricing.status == "unavailable"
    assert pricing.estimate(event())["amount"] is None


@pytest.mark.parametrize("models", [{"openai": []}, {"openai": {"broken": {"cost": None}}}])
def test_malformed_cached_catalog_is_unavailable_offline(paths, models):
    write_json(
        paths.cache / "models-dev-pricing.json",
        {"source": SOURCE, "fetched_at": datetime.now(UTC).isoformat(), "models": models},
    )
    assert load_pricing(paths.cache, offline=True).status == "unavailable"
