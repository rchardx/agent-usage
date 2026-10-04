import json
from datetime import datetime, timedelta

from conftest import claude_usage, write_rows
from typer.testing import CliRunner

from agent_usage.cli import app

runner = CliRunner()


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
