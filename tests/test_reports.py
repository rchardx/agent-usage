from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import claude_usage

from agent_usage.models import AccountReport, ProbeError
from agent_usage.parsers import parse_claude
from agent_usage.providers import normalize_claude, normalize_codex
from agent_usage.reports import (
    account_report,
    aggregate,
    period,
    save_snapshot,
    snapshot_path,
    summarize,
)


def test_default_month_is_rolling_thirty_days_anchored_to_exclusive_end():
    zone = ZoneInfo("America/New_York")
    start, end = period(None, "2026-03-20T12:00:00-04:00", False, False, zone)
    assert end.astimezone(UTC) - start.astimezone(UTC) == timedelta(days=30)
    assert start.isoformat() == "2026-02-18T11:00:00-05:00"
    assert period("30d", end.isoformat(), False, False, zone) == (start, end)


def test_today_and_calendar_month_remain_explicit_choices():
    zone = ZoneInfo("Asia/Tokyo")
    start, end = period(None, None, False, False, zone, today=True)
    assert start == end.replace(hour=0, minute=0, second=0, microsecond=0)
    start, end = period(None, None, True, False, zone)
    assert start == end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    with pytest.raises(ValueError, match="Choose only one"):
        period("30d", None, False, False, zone, today=True)


def test_codex_multi_bucket_reset_count_and_unknown_credit_unit():
    bucket = {
        "primary": {"usedPercent": 28, "windowDurationMins": 10080, "resetsAt": 1791593860},
        "credits": {"hasCredits": True, "unlimited": False, "balance": "123.45"},
    }
    result = normalize_codex(
        {"account": {"type": "chatgpt", "planType": "pro"}},
        {
            "rateLimits": bucket,
            "rateLimitsByLimitId": {"codex": bucket, "reserve": {"primary": {"usedPercent": 0}}},
            "rateLimitResetCredits": {"availableCount": 2, "credits": []},
        },
    )
    assert len(result.windows) == 2  # single-bucket compatibility view is not counted twice
    assert result.windows[1].used_percent == 0
    assert result.windows[1].resets_at is None
    assert result.resets.available_count == 2  # count may exceed returned detail rows
    assert result.credits[0].balance == "123.45"
    assert result.credits[0].unit == "provider_units_unverified"
    assert result.credits[0].has_credits is True
    assert result.credits[0].enabled is None


def test_claude_disabled_extra_usage_is_not_zero_balance():
    result = normalize_claude(
        {
            "five_hour": {"utilization": 0, "resets_at": "2026-10-01T05:00:00Z"},
            "seven_day": None,
            "extra_usage": {"is_enabled": False, "monthly_limit": None, "used_credits": None},
        },
        "pro",
    )
    assert result.windows[0].used_percent == 0
    assert len(result.windows) == 1
    assert result.credits[0].enabled is False
    assert result.credits[0].balance is None
    assert result.resets is None


def test_totals_do_not_double_count_cache_or_reasoning_and_keep_field_coverage():
    event, _ = parse_claude(claude_usage(), {}, "fallback")
    second = asdict(event)
    second["reasoning_tokens"] = None
    result = aggregate([asdict(event), second])["metrics"]
    assert result["total_tokens"]["tokens"] == 264
    assert result["input_tokens"]["tokens"] == 244
    assert result["fresh_input_tokens"]["tokens"] == 4
    assert result["reasoning_tokens"] == {
        "tokens": None,
        "known_tokens": 3,
        "observed_events": 1,
        "total_events": 2,
    }
    assert result["cache_write_5m_tokens"]["tokens"] == 10


def test_date_filter_uses_local_timezone_and_exclusive_end():
    zone = ZoneInfo("Asia/Tokyo")
    start, end = period("2026-10-02", "2026-10-03", False, False, zone)
    events = []
    for stamp in ["2026-10-01T14:59:59Z", "2026-10-01T15:00:00Z", "2026-10-02T15:00:00Z"]:
        event, _ = parse_claude(claude_usage(stamp=stamp), {}, "fallback")
        events.append(asdict(event))
    result = summarize(events, start, end, "day", zone, {})
    assert result["totals"]["events"] == 1
    assert result["groups"][0]["group"] == "2026-10-02"


@pytest.mark.parametrize(
    "args",
    [
        ("0d", None, False, False),
        ("1d", None, True, False),
        ("2026-10-03", "2026-10-02", False, False),
    ],
)
def test_invalid_periods(args):
    with pytest.raises(ValueError):
        period(*args, ZoneInfo("UTC"))


def test_quota_failure_returns_dated_stale_snapshot_without_secret(paths, monkeypatch):
    (paths.codex / "auth.json").write_text("{}")
    report = AccountReport(
        "codex",
        "codex-app-server",
        observed_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat(),
    ).to_dict()
    save_snapshot(snapshot_path(paths, "codex"), report)

    def fail(*args):
        raise ProbeError("Request timed out.")

    monkeypatch.setattr("agent_usage.reports.fetch_codex", fail)
    result = account_report(paths, "codex", refresh=True, offline=False, timeout=1)
    assert result["status"] == "stale"
    assert result["age_seconds"] >= 3600
    assert result["error"] == "Request timed out."


def test_offline_never_queries_provider_or_reuses_other_identity(paths, monkeypatch):
    (paths.codex / "auth.json").write_text("{}")

    def forbidden(*args):
        pytest.fail("Network access attempted")

    monkeypatch.setattr("agent_usage.reports.fetch_codex", forbidden)
    save_snapshot(
        snapshot_path(paths, "codex"),
        AccountReport("codex", "cache", observed_at=datetime.now(UTC).isoformat()).to_dict(),
    )
    assert (
        account_report(paths, "codex", refresh=False, offline=True, timeout=1)["status"] == "cached"
    )
    (paths.codex / "auth.json").write_text('{"account": "different"}')
    assert (
        account_report(paths, "codex", refresh=False, offline=True, timeout=1)["status"]
        == "unavailable"
    )


def test_claude_legacy_and_unified_windows_are_not_duplicated():
    result = normalize_claude(
        {
            "five_hour": {"utilization": 32, "resets_at": "2026-10-01T05:00:00Z"},
            "limits": [
                {
                    "kind": "session",
                    "group": "session",
                    "percent": 32,
                    "resets_at": "2026-10-01T05:00:00Z",
                },
                {"kind": "spend", "group": "monthly", "percent": 12, "resets_at": None},
            ],
        },
        "pro",
    )
    assert [w.bucket for w in result.windows] == ["five_hour", "spend"]
