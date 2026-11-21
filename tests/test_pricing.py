import io
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from importlib.metadata import version

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


def test_unpriced_details_group_by_agent_original_model_and_reason():
    pricing = catalog()
    rows = [
        event(provider="pi", model="radius/missing"),
        event(model="codex-auto-review"),
        event(model="codex-auto-review"),
        event(provider="omp", model="radius/missing"),
        event(cache_read_tokens=None),
        event(cache_write_tokens=1000001),
        event(),
    ]
    result = cost_summary([{**row, "_cost": pricing.estimate(row)} for row in rows])
    assert result["priced_events"] == 1
    assert result["known_amount"] == "1.970000"
    assert result["amount"] is None
    assert result["unpriced_reasons"] == {
        "model_not_priced": 4,
        "unknown_cache_usage": 1,
        "inconsistent_cache_usage": 1,
    }
    assert result["unpriced_details"] == [
        {
            "provider": "codex",
            "model": "codex-auto-review",
            "reason": "model_not_priced",
            "events": 2,
        },
        {
            "provider": "codex",
            "model": "test-model",
            "reason": "inconsistent_cache_usage",
            "events": 1,
        },
        {"provider": "codex", "model": "test-model", "reason": "unknown_cache_usage", "events": 1},
        {"provider": "omp", "model": "radius/missing", "reason": "model_not_priced", "events": 1},
        {"provider": "pi", "model": "radius/missing", "reason": "model_not_priced", "events": 1},
    ]
    assert sum(item["events"] for item in result["unpriced_details"]) == 6


def test_unpriced_details_respect_period_and_grouping():
    result = summarize(
        [
            event(model="codex-auto-review", timestamp="2026-11-10T12:00:00+00:00"),
            event(model="codex-auto-review", timestamp="2026-11-11T12:00:00+00:00"),
            event(model="outside-period", timestamp="2026-11-12T12:00:00+00:00"),
        ],
        datetime(2026, 11, 10, tzinfo=UTC),
        datetime(2026, 11, 12, tzinfo=UTC),
        "day",
        UTC,
        {},
        catalog(),
    )
    detail = {
        "provider": "codex",
        "model": "codex-auto-review",
        "reason": "model_not_priced",
        "events": 2,
    }
    assert result["totals"]["estimated_cost"]["unpriced_details"] == [detail]
    for group in result["groups"]:
        assert group["estimated_cost"]["unpriced_details"] == [{**detail, "events": 1}]


@pytest.mark.parametrize("rows", [[], [event()]])
def test_fully_priced_summary_has_no_unpriced_details(rows):
    pricing = catalog()
    result = cost_summary([{**row, "_cost": pricing.estimate(row)} for row in rows])
    assert result["unpriced_details"] == []
    assert result["unpriced_reasons"] == {}


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
        assert request.get_header("User-agent") == f"agent-usage/{version('agent-usage')}"
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


def gemini_catalog():
    pro_cost = {
        "input": 1.25,
        "output": 10,
        "cache_read": 0.125,
        "tiers": [
            {
                "input": 2.5,
                "output": 15,
                "cache_read": 0.25,
                "tier": {"type": "context", "size": 200000},
            }
        ],
    }
    flash_cost = {"input": 0.3, "output": 2.5, "cache_read": 0.03}
    return Pricing(
        {
            "google": {
                "gemini-2.5-pro": {"cost": pro_cost},
                "gemini-2.5-flash": {"cost": flash_cost},
            },
            "google-vertex": {
                "gemini-2.5-pro": {"cost": pro_cost},
            },
            "openrouter": {
                "google/gemini-2.5-pro": {"cost": {"input": 1.5, "output": 12, "cache_read": 0.15}},
            },
            "vercel": {
                "google/gemini-2.5-flash": {
                    "cost": {"input": 0.4, "output": 3, "cache_read": 0.04}
                },
            },
            "deepseek": {
                "deepseek-chat": {
                    "cost": {"input": 0.27, "output": 1.1, "cache_read": 0.07, "cache_write": 0.27}
                }
            },
            "mistral": {
                "codestral-latest": {"cost": {"input": 0.3, "output": 0.9, "cache_read": 0.03}}
            },
            "alibaba": {"qwen3-coder-plus": {"cost": {"input": 1, "output": 5, "cache_read": 0.1}}},
            "google-vertex-anthropic": {
                "claude-sonnet-4-5@20250929": {
                    "cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}
                }
            },
            "amazon-bedrock": {
                "us.anthropic.claude-opus-4-1-20250805-v1:0": {
                    "cost": {"input": 15, "output": 75, "cache_read": 1.5, "cache_write": 18.75}
                }
            },
        },
        "live",
        "2026-10-05T00:00:00+00:00",
    )


def test_gemini_implicit_cache_thinking_and_200k_tiers_across_all_four_harnesses():
    pricing = gemini_catalog()
    # 100k input (40k cached read, 0 write), 20k output (including 8k thinking tokens) <= 200k tier
    base_row = event(
        input_tokens=100_000,
        request_input_tokens=100_000,
        cache_read_tokens=40_000,
        cache_write_tokens=0,
        output_tokens=20_000,
        reasoning_tokens=8_000,
    )
    # 60k * 1.25/M ($0.075) + 40k * 0.125/M ($0.005) + 20k * 10/M ($0.20) = $0.28
    for agent, model, expected_channel in (
        ("pi", "google/gemini-2.5-pro", "google"),
        ("omp", "google-vertex/gemini-2.5-pro", "google-vertex"),
        ("codex", "google/gemini-2.5-pro", "google"),
        ("codex", "gcp/gemini-2.5-pro", "google-vertex"),
        ("codex", "gemini-2.5-pro", "google"),
        ("claude", "gemini-2.5-pro", "google"),
        ("claude", "google/gemini-2.5-pro", "google"),
    ):
        est = pricing.estimate(base_row | {"provider": agent, "model": model})
        assert est["pricing_provider"] == expected_channel, (agent, model)
        assert est["amount"] == Decimal("0.28"), (agent, model)

    # > 200k single-request context triggers the 200k tier:
    # 200k fresh * 2.5/M ($0.50) + 100k read * 0.25/M ($0.025) + 20k out * 15/M ($0.30) = $0.825
    high_tier = base_row | {
        "provider": "pi",
        "model": "google/gemini-2.5-pro",
        "input_tokens": 300_000,
        "request_input_tokens": 300_000,
        "cache_read_tokens": 100_000,
    }
    assert pricing.estimate(high_tier)["amount"] == Decimal("0.825")

    # OpenRouter and Vercel AI Gateway use their own catalog entries, not direct Google rates.
    or_est = pricing.estimate(
        base_row | {"provider": "pi", "model": "openrouter/google/gemini-2.5-pro"}
    )
    assert or_est["pricing_provider"] == "openrouter"
    assert or_est["model"] == "google/gemini-2.5-pro"
    assert or_est["amount"] == Decimal("0.336")

    vercel_est = pricing.estimate(
        base_row | {"provider": "omp", "model": "vercel-ai-gateway/google/gemini-2.5-flash"}
    )
    assert vercel_est["pricing_provider"] == "vercel"
    assert vercel_est["model"] == "google/gemini-2.5-flash"
    assert vercel_est["amount"] == Decimal("0.0856")


def test_models_without_cache_write_rate_handle_omitted_cache_write_honestly():
    pricing = gemini_catalog()
    # Models with no separate cache_write rate bill all non-read input at the input rate.
    omitted_write = event(
        provider="codex",
        model="google/gemini-2.5-flash",
        input_tokens=1_000_000,
        request_input_tokens=1_000_000,
        cache_read_tokens=600_000,
        cache_write_tokens=None,
        output_tokens=100_000,
    )
    est = pricing.estimate(omitted_write)
    # 400k * 0.3/M ($0.12) + 600k * 0.03/M ($0.018) + 100k * 2.5/M ($0.25) = $0.388
    assert est["amount"] == Decimal("0.388")
    assert omitted_write["cache_write_tokens"] is None

    # Positive cache_write_tokens when catalog has no cache_write rate stays unknown.
    positive_write = omitted_write | {"cache_write_tokens": 10_000}
    assert pricing.estimate(positive_write)["reason"] == "missing_cache_write_price"

    # Models that DO have a cache_write rate still require cache_write_tokens.
    with_write_rate = omitted_write | {"model": "deepseek/deepseek-chat"}
    assert pricing.estimate(with_write_rate)["reason"] == "unknown_cache_usage"

    # A >200k tier missing cache_write when base cost defines cache_write must stay unknown.
    tiered_incomplete = Pricing(
        {
            "openai": {
                "tiered-model": {
                    "cost": {
                        "input": 3,
                        "output": 15,
                        "cache_read": 0.3,
                        "cache_write": 3.75,
                        "tiers": [
                            {
                                "input": 6,
                                "output": 22.5,
                                "cache_read": 0.6,
                                "tier": {"type": "context", "size": 200_000},
                            }
                        ],
                    }
                }
            }
        },
        "live",
        "2026-10-05T00:00:00+00:00",
    )
    high_tier_unknown_write = event(
        provider="codex",
        model="tiered-model",
        input_tokens=300_000,
        request_input_tokens=300_000,
        cache_read_tokens=100_000,
        cache_write_tokens=None,
        output_tokens=5_000,
    )
    assert tiered_incomplete.estimate(high_tier_unknown_write)["reason"] == "unknown_cache_usage"

    # Claude events reconstruct total input from fresh + cache; missing cache_write stays unknown.
    claude_omitted_write = omitted_write | {"provider": "claude", "model": "gemini-2.5-flash"}
    assert pricing.estimate(claude_omitted_write)["reason"] == "unknown_cache_usage"


def test_two_level_gateways_first_party_keys_and_non_anthropic_claude_proxies():
    pricing = gemini_catalog()
    assert (
        pricing.estimate(
            event(
                provider="pi",
                model="custom-proxy/google/gemini-2.5-flash",
                cache_write_tokens=0,
            )
        )["pricing_provider"]
        == "google"
    )
    assert (
        pricing.estimate(
            event(provider="omp", model="custom-proxy/codestral-latest", cache_write_tokens=0)
        )["pricing_provider"]
        == "mistral"
    )
    assert (
        pricing.estimate(
            event(provider="pi", model="custom-proxy/qwen3-coder-plus", cache_write_tokens=0)
        )["pricing_provider"]
        == "alibaba"
    )

    # Explicit known sub-prefixes never fall back across channels.
    assert (
        pricing.estimate(
            event(provider="pi", model="custom-proxy/google/deepseek-chat", cache_write_tokens=0)
        )["reason"]
        == "model_not_priced"
    )

    # Non-Anthropic model proxied through Claude Code uses catalog cache_write without 5m/1h TTLs.
    proxied_claude = event(
        provider="claude",
        model="deepseek/deepseek-chat",
        cache_write_5m_tokens=None,
        cache_write_1h_tokens=None,
    )
    est = pricing.estimate(proxied_claude)
    assert est["pricing_provider"] == "deepseek"
    assert est["amount"] is not None

    # Official Vertex and Bedrock Claude model IDs resolve on Claude Code and enforce TTL rules.
    for cloud_model, expected_cloud in (
        ("claude-sonnet-4-5@20250929", "google-vertex-anthropic"),
        ("us.anthropic.claude-opus-4-1-20250805-v1:0", "amazon-bedrock"),
    ):
        cloud_row = event(
            provider="claude",
            model=cloud_model,
            cache_write_5m_tokens=40_000,
            cache_write_1h_tokens=60_000,
        )
        cloud_est = pricing.estimate(cloud_row)
        assert cloud_est["pricing_provider"] == expected_cloud
        assert cloud_est["amount"] is not None


def test_antigravity_effort_suffixes_prefer_exact_then_priced_base_without_affecting_other_agents():
    pricing = Pricing(
        {
            "google": {
                "gemini-3.8-flash": {"cost": {"input": 0.3, "output": 2.5, "cache_read": 0.03}},
                "gemini-3.8-flash-xhigh": {
                    "cost": {"input": 0.6, "output": 5.0, "cache_read": 0.06}
                },
                "gemini-3.5-flash": {"cost": {"input": 0.2, "output": 1.5, "cache_read": 0.02}},
            },
            "anthropic": {
                "claude-sonnet-4-6": {
                    "cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}
                }
            },
            "openai": {
                "gpt-5.1-codex": {"cost": {"input": 1.25, "output": 10, "cache_read": 0.125}},
                "o3-mini": {"cost": {"input": 1.1, "output": 4.4, "cache_read": 0.55}},
            },
        },
        "live",
        "2026-10-05T00:00:00+00:00",
    )
    sample = event(
        provider="antigravity",
        input_tokens=1_000_000,
        request_input_tokens=1_000_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=100_000,
        reasoning_tokens=20_000,
    )

    # 1. Exact suffixed entry takes precedence over base rates.
    exact = pricing.estimate(sample | {"model": "gemini-3.8-flash-xhigh"})
    assert exact["pricing_provider"] == "google"
    assert exact["model"] == "gemini-3.8-flash-xhigh"
    assert exact["amount"] == Decimal("1.1")

    # 2. Absent suffixed variant on Antigravity falls back when stripped base is priced.
    for suffix in ("high", "medium", "low", "thinking", "max"):
        fallback = pricing.estimate(sample | {"model": f"gemini-3.8-flash-{suffix}"})
        assert fallback["pricing_provider"] == "google"
        assert fallback["model"] == "gemini-3.8-flash"
        assert fallback["amount"] == Decimal("0.55")

    assert pricing.estimate(sample | {"model": "gemini-3.5-flash-extra-low"})["amount"] == Decimal(
        "0.35"
    )
    assert pricing.estimate(sample | {"model": "claude-sonnet-4-6-thinking"})["amount"] == Decimal(
        "4.5"
    )

    # Report preserves the recorded suffixed model id while exposing the resolved base rate table.
    report = summarize(
        [sample | {"model": "gemini-3.8-flash-high"}],
        None,
        datetime(2026, 11, 1, tzinfo=UTC),
        "model",
        UTC,
        {},
        pricing,
    )
    assert [g["group"] for g in report["groups"]] == ["gemini-3.8-flash-high"]
    assert report["pricing"]["models"][0]["model"] == "gemini-3.8-flash-high"
    assert report["pricing"]["models"][0]["cost_per_million_usd"] == {
        "input": 0.3,
        "output": 2.5,
        "cache_read": 0.03,
    }

    # 3. Unpriced base stays unpriced and retains the original suffixed model in diagnostics.
    assert pricing.estimate(sample | {"model": "gemini-4-carbon-medium"}) == {
        "amount": None,
        "reason": "model_not_priced",
        "model": "gemini-4-carbon-medium",
    }

    # 4. Non-Antigravity providers keep suffixed IDs exact and never strip -max / -high / -thinking.
    for other_agent, other_model in (
        ("codex", "gpt-5.1-codex-max"),
        ("codex", "o3-mini-high"),
        ("claude", "claude-sonnet-4-6-thinking"),
        ("pi", "google/gemini-3.8-flash-high"),
        ("omp", "openai/o3-mini-high"),
    ):
        other_res = pricing.estimate(sample | {"provider": other_agent, "model": other_model})
        assert other_res["reason"] == "model_not_priced", (other_agent, other_model)


def test_antigravity_suffix_fallback_handles_empty_cost_zero_rates_and_channel_boundaries():
    pricing = Pricing(
        {
            "google": {
                "gemini-2.5-pro": {"cost": {"input": 1.25, "output": 10}},
                "gemini-2.5-pro-high": {"cost": {}},
                "gemini-free-preview": {"cost": {"input": 0, "output": 0}},
                "gemini-free-exp": {"cost": {"input": 1, "output": 5}},
                "gemini-free-exp-low": {"cost": {"input": 0, "output": 0}},
                "gemini-vertex-only": {"cost": {"input": 1.25, "output": 10}},
            },
            "google-vertex": {
                "gemini-2.5-pro": {"cost": {"input": 2.0, "output": 12.0}},
                "gemini-2.5-pro-medium": {"cost": {}},
                "gemini-vertex-only-high": {"cost": {}},
            },
            "anthropic": {
                "claude-sonnet-4-6": {
                    "cost": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}
                }
            },
            "google-vertex-anthropic": {
                "claude-sonnet-4-6-max": {"cost": {}},
            },
        },
        "live",
        "2026-10-05T00:00:00+00:00",
    )
    sample = event(
        provider="antigravity",
        input_tokens=1_000_000,
        request_input_tokens=1_000_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        output_tokens=100_000,
        reasoning_tokens=0,
    )

    # Empty cost: {} falls back to the same channel's priced base model.
    same_channel = pricing.estimate(sample | {"model": "gemini-2.5-pro-high"})
    assert same_channel["pricing_provider"] == "google"
    assert same_channel["model"] == "gemini-2.5-pro"
    assert same_channel["amount"] == Decimal("2.25")

    # Explicit zero rates remain valid on both suffixed entries and base fallbacks.
    zero_suffixed = pricing.estimate(sample | {"model": "gemini-free-exp-low"})
    assert zero_suffixed["model"] == "gemini-free-exp-low"
    assert zero_suffixed["amount"] == Decimal("0")

    zero_base = pricing.estimate(sample | {"model": "gemini-free-preview-high"})
    assert zero_base["model"] == "gemini-free-preview"
    assert zero_base["amount"] == Decimal("0")

    # Vertex-matched suffixed entries stay in google-vertex and never drift to google rates.
    vertex_base = pricing.estimate(sample | {"model": "gemini-2.5-pro-medium"})
    assert vertex_base["pricing_provider"] == "google-vertex"
    assert vertex_base["model"] == "gemini-2.5-pro"
    assert vertex_base["amount"] == Decimal("3.2")

    vertex_unpriced = pricing.estimate(sample | {"model": "gemini-vertex-only-high"})
    assert vertex_unpriced["amount"] is None

    vertex_claude_unpriced = pricing.estimate(sample | {"model": "claude-sonnet-4-6-max"})
    assert vertex_claude_unpriced["amount"] is None


def test_compiled_pricing_cache_reuses_catalog_lookup_across_tiers_and_errors(monkeypatch):
    pricing = gemini_catalog()
    calls = []
    real_resolve = __import__("agent_usage.pricing", fromlist=["resolve_catalog"]).resolve_catalog

    def counting_resolve(provider, model, models):
        calls.append((provider, model))
        return real_resolve(provider, model, models)

    monkeypatch.setattr("agent_usage.pricing.resolve_catalog", counting_resolve)
    small = event(
        provider="pi",
        model="google/gemini-2.5-pro",
        input_tokens=100_000,
        request_input_tokens=100_000,
        cache_read_tokens=40_000,
        cache_write_tokens=0,
        output_tokens=20_000,
    )
    large = small | {
        "input_tokens": 300_000,
        "request_input_tokens": 300_000,
        "cache_read_tokens": 100_000,
    }
    unknown_tier = small | {"request_input_tokens": None}

    assert pricing.estimate(small)["amount"] == Decimal("0.28")
    assert pricing.estimate(large)["amount"] == Decimal("0.825")
    assert pricing.estimate(unknown_tier)["reason"] == "unknown_request_size_for_tier"
    assert calls == [("pi", "google/gemini-2.5-pro")]


def test_repeated_unpriced_rate_and_tier_errors_do_not_retain_exceptions():
    pricing = Pricing(
        {
            "openai": {
                "missing-cache-rate": {"cost": {"input": 1, "output": 2}},
                "bad-tier": {"cost": {"input": 1, "output": 2, "tiers": [{"tier": {"size": -1}}]}},
                "bad-structure": {"cost": "not-a-dict"},
            }
        },
        "live",
    )
    for _ in range(100):
        assert (
            pricing.estimate(event(model="missing-cache-rate"))["reason"]
            == "missing_cache_read_price"
        )
        assert pricing.estimate(event(model="bad-tier"))["reason"] == "invalid_price_tier"
        assert (
            pricing.estimate(event(model="bad-structure"))["reason"]
            == "unsupported_price_structure"
        )

    missing_entry = pricing._compiled[("codex", "missing-cache-rate")]
    assert missing_entry.base_table is not None
    assert missing_entry.base_table.rates["cache_read"] == "missing_cache_read_price"
    assert all(not isinstance(v, BaseException) for v in missing_entry.base_table.rates.values())
    assert pricing._compiled[("codex", "bad-tier")].tier_error == "invalid_price_tier"
    assert pricing._compiled[("codex", "bad-structure")].structure_error == (
        "unsupported_price_structure"
    )
