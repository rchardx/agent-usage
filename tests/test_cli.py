import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import agent_session, agent_usage, claude_usage, codex_meta, codex_usage, write_rows
from typer.testing import CliRunner

from agent_usage.cli import app
from agent_usage.models import AccountReport, CreditBalance, ProbeError, QuotaWindow
from agent_usage.pricing import Pricing
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
        "--pi-home",
        str(paths.pi),
        "--omp-home",
        str(paths.omp),
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
    expected = {"codex": "unavailable", "claude": "unavailable"}
    assert {a["provider"]: a["status"] for a in data["quota"]["accounts"]} == expected
    assert data["tokens"]["totals"]["events"] == 0


def test_pi_and_omp_tokens_without_quota(paths):
    root = paths.pi / "sessions/-example"
    write_rows(
        root / "2026-10-01T00-00-00-000Z_abc.jsonl",
        [agent_session(), agent_usage(stamp=recent())],
    )
    args = [*options(paths), "--provider", "pi", "--offline", "--no-cost", "--json"]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "quota" not in data
    groups = data["tokens"]["groups"]
    assert [g["provider"] for g in groups] == ["pi"]
    assert groups[0]["metrics"]["total_tokens"]["tokens"] == 19


@pytest.mark.parametrize("provider", ["pi", "omp"])
def test_token_only_providers_cannot_query_quota(paths, provider):
    result = runner.invoke(app, [*options(paths), "--provider", provider, "quota"])
    assert result.exit_code == 2
    result = runner.invoke(app, [*options(paths), "--provider", provider, "quota", "--json"])
    assert result.exit_code == 2


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


def test_default_report_lists_days_but_json_keeps_provider_totals(paths):
    write_rows(paths.claude / "projects" / "p" / "s.jsonl", [claude_usage(stamp=recent())])
    args = [*options(paths), "--provider", "claude", "--offline", "--no-cost"]
    terminal = runner.invoke(app, args)
    assert terminal.exit_code == 0, terminal.output
    assert "by day" in terminal.output and "Token breakdown" not in terminal.output
    machine = runner.invoke(app, [*args, "--json"])
    assert machine.exit_code == 0, machine.output
    tokens = json.loads(machine.output)["tokens"]
    assert tokens["group_by"] == "provider"
    assert [group["group"] for group in tokens["groups"]] == ["claude"]


def test_api_key_override_supersedes_cached_subscription_snapshot_online_and_offline(
    paths, monkeypatch
):
    (paths.claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"accessToken": "old-sub-token"}})
    )
    cached_sub = AccountReport(
        "claude",
        "cache",
        observed_at=datetime.now(UTC).isoformat(),
        plan="pro",
        auth_mode="claude.ai",
        windows=[QuotaWindow("five_hour", "five_hour", 42, None, 300)],
    ).to_dict()
    save_snapshot(snapshot_path(paths, "claude"), cached_sub)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-active-key-sentinel")

    for extra in ([], ["--offline"]):
        json_res = runner.invoke(
            app, [*options(paths), "--provider", "claude", "--no-cost", *extra, "--json"]
        )
        assert json_res.exit_code == 0, json_res.output
        account = json.loads(json_res.output)["quota"]["accounts"][0]
        assert account["status"] == "unavailable"
        assert account["auth_mode"] == "api_key"
        assert account["windows"] == []
        assert account["error"] is None
        assert "test-active-key-sentinel" not in json_res.output

    term_res = runner.invoke(app, [*options(paths), "--provider", "claude", "--no-cost"])
    assert term_res.exit_code == 0, term_res.output
    assert "CLAUDE  ·  API key" in term_res.output
    assert "API key mode · subscription allowances not applicable" in term_res.output
    assert "42%" not in term_res.output


def test_doctor_reports_non_subscription_auth_without_exposing_secrets(paths, monkeypatch):
    monkeypatch.setattr("agent_usage.cli.shutil.which", lambda _: None)
    codex_key_field = "OPENAI" + "_API_KEY"
    (paths.codex / "auth.json").write_text(json.dumps({codex_key_field: "test-codex-sentinel"}))
    (paths.claude / "settings.json").write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_AUTH_TOKEN": "test-claude-sentinel",
                    "ANTHROPIC_BASE_URL": "https://gateway.example.test",
                }
            }
        )
    )
    result = runner.invoke(app, [*options(paths), "doctor", "--json"])
    assert result.exit_code == 0, result.output
    doctor = json.loads(result.output)["doctor"]
    assert doctor["auth"] == {
        "codex": {"auth_mode": "apiKey", "source": "codex-auth-file"},
        "claude": {"auth_mode": "customGateway", "source": "claude-settings"},
    }
    assert doctor["claude_credentials_file_present"] is False
    assert "test-codex-sentinel" not in result.output
    assert "test-claude-sentinel" not in result.output


def test_gemini_tokens_and_costs_across_all_four_harnesses(paths, monkeypatch):
    stamp = recent()
    # 1. Codex with custom google model_provider
    write_rows(
        paths.codex / "sessions/gem.jsonl",
        [
            codex_meta("codex-gem", model_provider="google"),
            {"type": "turn_context", "payload": {"cwd": "/p", "model": "gemini-2.5-flash"}},
            codex_usage(1000, 200, stamp=stamp),
        ],
    )
    # 2. Claude Code via custom gateway running Gemini
    claude_gem = claude_usage("msg-gem", stamp=stamp, output=200)
    claude_gem["message"]["model"] = "models/gemini-2.5-flash"
    claude_gem["message"]["usage"]["cache_creation_input_tokens"] = 0
    claude_gem["message"]["usage"]["cache_creation"] = {}
    write_rows(paths.claude / "projects/gem.jsonl", [claude_gem])
    # 3. pi via direct Google Gemini API
    pi_gem = agent_usage("resp-pi-gem", stamp=stamp, cacheWrite=0)
    pi_gem["message"]["provider"] = "google"
    pi_gem["message"]["model"] = "gemini-2.5-flash"
    write_rows(
        paths.pi / "sessions/-p/2026-10-01T00-00-00-000Z_pi.jsonl",
        [agent_session("pi-s"), pi_gem],
    )
    # 4. omp via OpenRouter Gemini
    omp_gem = agent_usage("resp-omp-gem", stamp=stamp, cacheWrite=0)
    omp_gem["message"]["provider"] = "openrouter"
    omp_gem["message"]["model"] = "google/gemini-2.5-flash"
    write_rows(
        paths.omp / "sessions/-p/2026-10-01T00-00-00-000Z_omp.jsonl",
        [agent_session("omp-s"), omp_gem],
    )

    monkeypatch.setattr(
        "agent_usage.cli.load_pricing",
        lambda *args, **kwargs: Pricing(
            {
                "google": {
                    "gemini-2.5-flash": {"cost": {"input": 0.3, "output": 2.5, "cache_read": 0.03}}
                },
                "openrouter": {
                    "google/gemini-2.5-flash": {
                        "cost": {"input": 0.35, "output": 2.6, "cache_read": 0.035}
                    }
                },
            },
            "cached",
            "2026-10-05T00:00:00+00:00",
        ),
    )
    result = runner.invoke(app, [*options(paths), "tokens", "--all", "--by", "model", "--json"])
    assert result.exit_code == 0, result.output
    tokens = json.loads(result.output)["tokens"]
    assert tokens["totals"]["events"] == 4
    assert tokens["totals"]["estimated_cost"]["priced_events"] == 4
    assert tokens["totals"]["estimated_cost"]["amount"] is not None
    assert tokens["totals"]["estimated_cost"]["unpriced_details"] == []
    grouped = {(g["provider"], g["group"]) for g in tokens["groups"]}
    assert grouped == {
        ("codex", "google/gemini-2.5-flash"),
        ("claude", "gemini-2.5-flash"),
        ("pi", "google/gemini-2.5-flash"),
        ("omp", "openrouter/google/gemini-2.5-flash"),
    }


def recent():
    return (datetime.now(UTC) - timedelta(hours=1)).isoformat().replace("+00:00", "Z")
