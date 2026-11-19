"""CLI contracts that must survive parser-library upgrades."""

import json
from importlib.metadata import version

import pytest
from rich.text import Text
from test_cli import options
from typer.testing import CliRunner

from agent_usage.cli import app
from agent_usage.pricing import load_pricing

runner = CliRunner()


def forbid_reports(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Argument parsing unexpectedly dispatched a report")

    monkeypatch.setattr("agent_usage.cli.token_report", forbidden)
    monkeypatch.setattr("agent_usage.cli.quota_report", forbidden)
    monkeypatch.setattr("agent_usage.cli.Index", forbidden)


@pytest.mark.parametrize("command", [[], ["tokens"], ["quota"], ["doctor"]])
def test_help_is_successful_and_has_no_report_side_effects(paths, monkeypatch, command):
    forbid_reports(monkeypatch)
    result = runner.invoke(app, [*options(paths), *command, "--help"], color=False)
    assert result.exit_code == 0, result.output
    # GitHub Actions forces Rich styling even when CliRunner has color=False.
    help_text = Text.from_ansi(result.stdout).plain
    assert "Usage:" in help_text
    assert "--help" in help_text
    assert result.stderr == ""
    assert not paths.cache.exists()


def test_eager_version_has_no_report_side_effects(paths, monkeypatch):
    forbid_reports(monkeypatch)
    result = runner.invoke(app, [*options(paths), "--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout.strip() == version("agent-usage")
    assert result.stderr == ""
    assert not paths.cache.exists()


def test_show_completion_does_not_dispatch_reports(paths, monkeypatch):
    forbid_reports(monkeypatch)
    # This eager flag detects the parent shell; hosted runners may have none.
    monkeypatch.setattr("shellingham.detect_shell", lambda: ("bash", "/bin/bash"))
    result = runner.invoke(app, [*options(paths), "--show-completion"], prog_name="agent-usage")
    assert result.exit_code == 0, result.output
    assert "agent-usage" in result.stdout
    assert "complete" in result.stdout
    assert result.stderr == ""
    assert not paths.cache.exists()


@pytest.mark.parametrize(
    ("args", "fragment"),
    [
        (["--provider", "not-an-agent", "tokens"], "--provider"),
        (["tokens", "--by", "not-a-group"], "--by"),
        (["--timeout", "not-a-number", "tokens"], "--timeout"),
        (["--timeout", "0", "tokens"], "--timeout"),
        (["--timeout", "-1", "tokens"], "--timeout"),
        (["--timeout", "120.1", "tokens"], "--timeout"),
        (["--timeout"], "--timeout"),
        (["tokens", "--since"], "--since"),
        (["tokens", "--provider", "codex"], "--provider"),
        (["tokens", "--unknown-option"], "--unknown-option"),
        (["--json=true", "tokens"], "--json"),
        (["unknown-command"], "unknown-command"),
        (["--timezone", "Not/AZone", "tokens"], "Unknown timezone"),
        (["--offline", "--refresh", "tokens"], "cannot be combined"),
    ],
)
def test_invalid_arguments_fail_cleanly_before_reports(paths, monkeypatch, args, fragment):
    forbid_reports(monkeypatch)
    result = runner.invoke(app, [*options(paths), *args], color=False)
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    error = " ".join(Text.from_ansi(result.stderr).plain.split())
    assert fragment in error
    assert "Traceback" not in error
    assert not paths.cache.exists()


@pytest.mark.parametrize("timeout", ["0.1", "10", "1e1", "120"])
def test_timeout_boundaries_and_types_reach_settings(paths, monkeypatch, timeout):
    observed = []

    def report(settings, *args):
        observed.append(settings)
        return {}

    monkeypatch.setattr("agent_usage.cli.token_report", report)
    result = runner.invoke(
        app, [*options(paths), "--json", "--timeout", timeout, "tokens", "--all"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["tokens"] == {}
    settings = observed[0]
    assert settings.timeout == float(timeout)
    assert isinstance(settings.timeout, float)
    assert settings.json is True
    assert settings.offline is False and settings.refresh is False
    assert settings.costs is True
    assert settings.providers == ["codex", "claude", "pi", "omp"]


def test_optional_paths_boolean_flags_and_enum_values(paths, monkeypatch, tmp_path):
    observed = []
    # A Unicode path with spaces exercises Path validation without real agent data.
    custom = tmp_path / "test data 日本語"

    def report(settings, *args):
        observed.append(settings)
        return {}

    monkeypatch.setattr("agent_usage.cli.token_report", report)
    result = runner.invoke(
        app,
        [
            *options(paths),
            "--pi-home",
            str(custom),
            "--provider",
            "pi",
            "--offline",
            "--no-cost",
            "--json",
            "tokens",
        ],
    )
    assert result.exit_code == 0, result.output
    settings = observed[0]
    assert settings.paths.pi == custom.resolve()
    assert settings.paths.codex == paths.codex
    assert settings.providers == ["pi"]
    assert settings.offline is True and settings.refresh is False
    assert settings.costs is False
    assert settings.timeout == 10
    assert str(settings.zone) == "UTC"
    assert not custom.exists()


@pytest.mark.parametrize("command", [[], ["tokens"], ["quota"], ["doctor"]])
@pytest.mark.parametrize("global_json", [False, True])
def test_offline_json_is_clean_and_never_fetches(paths, monkeypatch, command, global_json):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Offline command attempted network or account access")

    monkeypatch.setattr("agent_usage.reports.fetch_codex", forbidden)
    monkeypatch.setattr("agent_usage.reports.fetch_claude", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    monkeypatch.setattr("agent_usage.cli.shutil.which", lambda _: None)
    # Exercise the real offline price loader, not conftest's default CLI stub.
    monkeypatch.setattr("agent_usage.cli.load_pricing", load_pricing)
    args = [*options(paths), "--offline"]
    if global_json:
        args.append("--json")
    args.extend(command)
    if not global_json:
        args.append("--json")
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["schema_version"] == 1
    assert result.stderr == ""
    assert not calls
    if "tokens" in data:
        assert data["tokens"]["totals"]["events"] == 0
        assert data["tokens"]["pricing"]["status"] == "unavailable"
    if "quota" in data:
        assert {a["status"] for a in data["quota"]["accounts"]} == {"unavailable"}
    if "doctor" in data:
        assert data["doctor"]["version"] == version("agent-usage")
        assert data["doctor"]["timezone"] == "UTC"


@pytest.mark.parametrize(
    "args",
    [
        ["--today", "--all"],
        ["--month", "--since", "7d"],
        ["--since", "not-a-date"],
        ["--since", "2026-10-02", "--until", "2026-10-01"],
    ],
)
def test_invalid_periods_still_exit_two_before_creating_cache(paths, args):
    result = runner.invoke(app, [*options(paths), "--no-cost", "tokens", *args])
    assert result.exit_code == 2, result.output
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert not paths.cache.exists()


def test_iso_date_strings_keep_application_timezone_semantics(paths):
    result = runner.invoke(
        app,
        [
            *options(paths),
            "--timezone",
            "Asia/Tokyo",
            "--no-cost",
            "tokens",
            "--since",
            "2026-10-01",
            "--until",
            "2026-10-02",
            "--by",
            "day",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)["tokens"]
    assert report["period"]["start"] == "2026-10-01T00:00:00+09:00"
    assert report["period"]["end_exclusive"] == "2026-10-02T00:00:00+09:00"
    assert report["group_by"] == "day"
    assert "pricing" not in report
