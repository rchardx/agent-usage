import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import claude_usage, write_rows
from typer.testing import CliRunner

from agent_usage.cli import app
from agent_usage.models import AccountReport, CreditBalance, ProbeError, QuotaWindow
from agent_usage.reports import save_snapshot, snapshot_path

runner = CliRunner()


@pytest.mark.parametrize("provider,known", [("claude", "five_hour"), ("codex", "codex")])
@pytest.mark.parametrize("command", [[], ["quota"]])
@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("mode", ["offline", "cached", "failed_refresh"])
def test_unknown_allowances_in_old_caches_never_reach_output(
    paths, monkeypatch, provider, known, command, as_json, mode
):
    home = getattr(paths, provider)
    (home / ("auth.json" if provider == "codex" else ".credentials.json")).write_text("{}")
    report = AccountReport(
        provider,
        "cache",
        observed_at=datetime.now(UTC).isoformat(),
        windows=[
            QuotaWindow(known, "primary", 12, None, 300),
            QuotaWindow("iguana_necktie", "primary", 99, None),
        ],
        credits=[CreditBalance("future_credit", balance="100")],
    ).to_dict()
    path = snapshot_path(paths, provider)
    save_snapshot(path, report)
    before = path.read_bytes()

    def fetch(*args):
        if mode == "failed_refresh":
            raise ProbeError("Request timed out.")
        pytest.fail("Cache should have served this request")

    monkeypatch.setattr(f"agent_usage.reports.fetch_{provider}", fetch)
    args = [*options(paths), "--provider", provider, "--no-cost"]
    if mode == "offline":
        args.append("--offline")
    elif mode == "failed_refresh":
        args.append("--refresh")
    if as_json:
        args.append("--json")
    result = runner.invoke(app, [*args, *command])
    assert result.exit_code == 0, result.output
    assert "iguana_necktie" not in result.output
    assert "future_credit" not in result.output
    assert path.read_bytes() == before
    if as_json:
        account = json.loads(result.output)["quota"]["accounts"][0]
        assert [w["bucket"] for w in account["windows"]] == [known]
        assert account["windows"][0]["used_percent"] == 12
        assert account["credits"] == []
        assert account["status"] == ("stale" if mode == "failed_refresh" else "cached")
    else:
        assert "12%" in result.output
        assert "All models" in result.output if provider == "claude" else "Codex" in result.output


def options(paths):
    return [
        "--codex-home",
        str(paths.codex),
        "--claude-home",
        str(paths.claude),
        "--cache-dir",
        str(paths.cache),
        "--timezone",
        "UTC",
    ]


def test_json_cli_and_incremental_reopen(paths):
    write_rows(paths.claude / "projects/main.jsonl", [claude_usage(), claude_usage()])
    args = [*options(paths), "--provider", "claude", "tokens", "--all", "--json"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["schema_version"] == 1
    assert data["tokens"]["totals"]["metrics"]["total_tokens"]["tokens"] == 132
    assert "PRIVATE-CONVERSATION-SENTINEL" not in result.output
    again = runner.invoke(app, args)
    assert json.loads(again.output)["tokens"]["coverage"]["bytes_read"] == 0


def test_offline_default_handles_missing_accounts_and_empty_logs(paths):
    result = runner.invoke(app, [*options(paths), "--offline", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert all(a["status"] == "unavailable" for a in data["quota"]["accounts"])
    assert data["tokens"]["totals"]["events"] == 0


def test_bad_dates_fail_before_index_creation(paths):
    result = runner.invoke(app, [*options(paths), "tokens", "--since", "invalid"])
    assert result.exit_code == 2
    assert not paths.cache.exists()


def test_mutually_exclusive_options(paths):
    result = runner.invoke(app, [*options(paths), "--offline", "--refresh", "quota"])
    assert result.exit_code == 2


def test_default_range_and_no_cost_skip_price_lookup(paths, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Price lookup attempted")

    monkeypatch.setattr("agent_usage.cli.load_pricing", forbidden)
    result = runner.invoke(app, [*options(paths), "--no-cost", "tokens", "--json"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)["tokens"]
    assert "pricing" not in report
    assert "estimated_cost" not in report["totals"]
    period = report["period"]
    assert period["label"] == "Last 30 days"
    assert datetime.fromisoformat(period["end_exclusive"]) - datetime.fromisoformat(
        period["start"]
    ) == timedelta(days=30)


def test_tokens_by_day_detail_flag(paths):
    write_rows(paths.claude / "projects" / "p" / "s.jsonl", [claude_usage()])
    args = [*options(paths), "--no-cost", "tokens", "--all", "--by", "day"]
    daily = runner.invoke(app, args)
    detail = runner.invoke(app, [*args, "--detail"])
    assert daily.exit_code == 0 and detail.exit_code == 0, daily.output + detail.output
    assert "by day" in daily.output and "Token breakdown" not in daily.output
    assert "Token breakdown" in detail.output
