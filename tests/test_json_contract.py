import copy
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import agent_session, agent_usage, claude_usage, write_rows
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from test_cli import options
from typer.testing import CliRunner

from agent_usage.cli import app
from agent_usage.json_contract import load_schema
from agent_usage.models import AccountReport, CreditBalance, QuotaWindow, ResetAllowance
from agent_usage.pricing import Pricing
from agent_usage.reports import save_snapshot, snapshot_path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "docs/examples"
runner = CliRunner()


@pytest.fixture
def validator():
    schema = load_schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_result(result, validator):
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    data = json.loads(result.stdout)
    validator.validate(data)
    return data


def test_schema_command_is_offline_and_has_no_cache_side_effects(paths, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Printing the schema must not access reports or the index")

    for name in ("Index", "quota_report", "token_report", "load_pricing"):
        monkeypatch.setattr(f"agent_usage.cli.{name}", forbidden)
    result = runner.invoke(app, [*options(paths), "schema"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == load_schema()
    assert result.stderr == ""
    assert not paths.cache.exists()


@pytest.mark.parametrize("path", sorted(EXAMPLES.glob("*.json")), ids=lambda p: p.name)
def test_synthetic_examples_match_the_bundled_schema(validator, path):
    validator.validate(json.loads(path.read_text(encoding="utf-8")))


@pytest.mark.parametrize("group", ["provider", "model", "project", "session", "day"])
@pytest.mark.parametrize("pricing", ["disabled", "unavailable", "partial"])
def test_actual_token_outputs_and_unknown_semantics(paths, monkeypatch, validator, group, pricing):
    write_rows(paths.claude / "projects/example.jsonl", [claude_usage()])
    unpriced = agent_usage(reasoning=None)
    unpriced["message"]["model"] = "example-unpriced"
    write_rows(
        paths.pi / "sessions/-example/2026-10-01T00-00-00-000Z_example.jsonl",
        [agent_session(), unpriced],
    )
    if pricing == "partial":
        monkeypatch.setattr(
            "agent_usage.cli.load_pricing",
            lambda *args, **kwargs: Pricing(
                {
                    "anthropic": {
                        "example-model": {
                            "cost": {
                                "input": 1,
                                "output": 2,
                                "cache_read": 0.1,
                                "cache_write": 1.25,
                            }
                        }
                    }
                },
                "cached",
                "2026-10-01T00:00:00+00:00",
            ),
        )
    args = [*options(paths), "--offline"]
    if pricing == "disabled":
        args.append("--no-cost")
    result = runner.invoke(app, [*args, "tokens", "--all", "--by", group, "--json"])
    report = validate_result(result, validator)["tokens"]
    assert report["totals"]["events"] == 2
    assert sum(row["events"] for row in report["groups"]) == report["totals"]["events"]
    reasoning = report["totals"]["metrics"]["reasoning_tokens"]
    assert reasoning == {"tokens": None, "known_tokens": 3, "observed_events": 1, "total_events": 2}
    if pricing == "disabled":
        assert "estimated_cost" not in report["totals"] and "pricing" not in report
    else:
        cost = report["totals"]["estimated_cost"]
        assert cost["amount"] is None
        assert cost["priced_events"] == (1 if pricing == "partial" else 0)
        assert (
            sum(cost["unpriced_reasons"].values()) + cost["priced_events"] == cost["total_events"]
        )
        assert sum(d["events"] for d in cost["unpriced_details"]) == 2 - cost["priced_events"]


@pytest.mark.parametrize("provider", ["codex", "claude"])
@pytest.mark.parametrize("status", ["ok", "cached", "stale", "unavailable"])
def test_account_statuses_match_contract(paths, monkeypatch, validator, provider, status):
    home = getattr(paths, provider)
    (home / ("auth.json" if provider == "codex" else ".credentials.json")).write_text("{}")
    observed = datetime.now(UTC) - timedelta(hours=1 if status == "stale" else 0)
    bucket = "codex" if provider == "codex" else "five_hour"
    account = AccountReport(
        provider,
        "synthetic",
        observed_at=observed.isoformat(),
        windows=[QuotaWindow(bucket, "primary", 25, None, 300)],
        credits=[CreditBalance(bucket, balance="12.50")],
        resets=ResetAllowance(1, [{"scope": "example", "status": "available", "expires_at": None}]),
    )
    monkeypatch.setattr(f"agent_usage.reports.fetch_{provider}", lambda *args: account)
    if status in ("cached", "stale"):
        save_snapshot(snapshot_path(paths, provider), account.to_dict())
    flag = "--refresh" if status == "ok" else "--offline"
    result = runner.invoke(app, [*options(paths), "--provider", provider, flag, "quota", "--json"])
    data = validate_result(result, validator)
    assert data["quota"]["accounts"][0]["status"] == status


@pytest.mark.parametrize("command", [[], ["doctor"], ["doctor", "--rebuild-index"]])
def test_combined_and_doctor_outputs(paths, monkeypatch, validator, command):
    monkeypatch.setattr("agent_usage.cli.shutil.which", lambda _: None)
    result = runner.invoke(app, [*options(paths), "--offline", "--json", *command])
    data = validate_result(result, validator)
    if not command:
        assert set(data) == {"schema_version", "generated_at", "tokens", "quota"}
        assert data["tokens"]["totals"]["metrics"]["input_tokens"]["tokens"] == 0
    else:
        assert data["doctor"].get("index_rebuilt", False) == ("--rebuild-index" in command)


def sample():
    return json.loads((EXAMPLES / "combined-partial.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), 2),
        (("schema_version",), True),
        (("generated_at",), "2026-10-01T00:00:00"),
        (("generated_at",), "not-a-timestamp"),
        (("tokens", "totals", "metrics", "input_tokens", "tokens"), "100"),
        (("tokens", "totals", "metrics", "input_tokens", "known_tokens"), 1.5),
        (("tokens", "totals", "metrics", "input_tokens", "tokens"), False),
        (("tokens", "totals", "estimated_cost", "known_amount"), 0.1),
        (("tokens", "totals", "estimated_cost", "known_amount"), "0.1"),
        (("tokens", "coverage", "history_complete"), True),
        (("tokens", "coverage", "files"), -1),
        (("quota", "accounts", 0, "status"), "invented-status"),
    ],
)
def test_contract_rejects_wrong_types_versions_and_dates(validator, path, value):
    data = sample()
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(ValidationError):
        validator.validate(data)


@pytest.mark.parametrize(
    "field", ["schema_version", "generated_at", "metrics", "pricing", "estimated_cost"]
)
def test_contract_requires_existing_fields_and_consistent_cost_presence(validator, field):
    data = sample()
    if field in ("schema_version", "generated_at"):
        del data[field]
    elif field in ("metrics", "estimated_cost"):
        del data["tokens"]["totals"][field]
    else:
        del data["tokens"][field]
    with pytest.raises(ValidationError):
        validator.validate(data)
    with pytest.raises(ValidationError):
        validator.validate({"schema_version": 1, "generated_at": data.get("generated_at")})


def test_contract_tolerates_additive_fields_and_open_reason_codes(validator):
    data = copy.deepcopy(sample())
    data["future_field"] = {"example": 1}
    data["tokens"]["totals"]["metrics"]["input_tokens"]["future_field"] = True
    data["tokens"]["totals"]["estimated_cost"]["unpriced_details"][0]["reason"] = "future_reason"
    validator.validate(data)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_never_reach_json_stdout(paths, monkeypatch, number):
    monkeypatch.setattr("agent_usage.cli.quota_report", lambda _: {"value": number})
    result = runner.invoke(app, [*options(paths), "quota", "--json"])
    assert result.exit_code == 1
    assert result.stdout == ""
    assert "standard JSON" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("kind", ["valid", "wrong-shape", "nonfinite", "invalid-json"])
def test_offline_validation_script(tmp_path, kind):
    path = tmp_path / "report.json"
    data = sample()
    if kind == "wrong-shape":
        data["schema_version"] = "PRIVATE-REPORT-SENTINEL"
    if kind == "nonfinite":
        data["future_value"] = float("nan")
    path.write_text(json.dumps(data) if kind != "invalid-json" else "not JSON", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_report.py"), str(path)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == (0 if kind == "valid" else 1), result.stderr
    assert "PRIVATE-REPORT-SENTINEL" not in result.stdout + result.stderr
