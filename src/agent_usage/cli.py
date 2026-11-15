import json
import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import typer
from rich.console import Console
from tzlocal import get_localzone

from . import __version__
from .index import Index
from .paths import Paths
from .pricing import load_pricing
from .render import render_quotas, render_tokens
from .reports import ACCOUNT_PROVIDERS, envelope, period, quotas, summarize

app = typer.Typer(
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
    help="Codex, Claude Code, pi, and omp allowances and local token usage.",
)


class Provider(StrEnum):
    all = "all"
    codex = "codex"
    claude = "claude"
    pi = "pi"
    omp = "omp"


ALL_PROVIDERS = ("codex", "claude", "pi", "omp")


class Group(StrEnum):
    provider = "provider"
    model = "model"
    project = "project"
    day = "day"
    session = "session"


@dataclass
class Settings:
    paths: Paths
    providers: list[str]
    zone: ZoneInfo
    json: bool
    offline: bool
    refresh: bool
    timeout: float
    costs: bool = True


def token_report(
    settings: Settings,
    since=None,
    until=None,
    month=False,
    all_time=False,
    by="provider",
    today=False,
):
    try:
        start, end = period(since, until, month, all_time, settings.zone, today=today)
    except (ValueError, OverflowError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    with Index(settings.paths.cache) as index:
        coverage = index.sync(settings.paths, settings.providers)
        events = index.events(settings.paths, settings.providers)
    pricing = (
        load_pricing(
            settings.paths.cache,
            offline=settings.offline,
            refresh=settings.refresh,
            timeout=settings.timeout,
        )
        if settings.costs
        else None
    )
    result = summarize(events, start, end, by, settings.zone, coverage, pricing)
    result["period"]["label"] = (
        "Today"
        if today
        else "This month"
        if month
        else "All available history"
        if all_time
        else "Last 30 days"
        if since is None
        else f"Last {since[:-1]} days"
        if since.endswith("d")
        else "Selected period"
    )
    return result


def quota_report(settings: Settings):
    providers = [name for name in settings.providers if name in ACCOUNT_PROVIDERS]
    if not providers:
        raise typer.BadParameter(
            "pi and omp are multi-provider harnesses, not accounts; "
            "they only contribute local token history."
        )
    return quotas(
        settings.paths,
        providers,
        refresh=settings.refresh,
        offline=settings.offline,
        timeout=settings.timeout,
    )


def output(settings: Settings, result: dict, as_json: bool = False, detail: bool = False):
    if settings.json or as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    console = Console()
    if "quota" in result:
        render_quotas(result["quota"], console, settings.zone)
    if "tokens" in result:
        render_tokens(result["tokens"], console, settings.zone, detail)
    if "doctor" in result:
        console.print_json(json.dumps(result["doctor"]))


def version_callback(value: bool):
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    provider: Provider = typer.Option(Provider.all, help="Providers to query."),
    codex_home: Path | None = typer.Option(None, help="Override CODEX_HOME."),
    claude_home: Path | None = typer.Option(None, help="Override CLAUDE_CONFIG_DIR."),
    pi_home: Path | None = typer.Option(None, help="Override PI_CODING_AGENT_DIR."),
    omp_home: Path | None = typer.Option(None, help="Override the omp agent directory."),
    cache_dir: Path | None = typer.Option(
        None, help="Rebuildable local index and quota snapshots."
    ),
    timezone_name: str | None = typer.Option(
        None, "--timezone", help="IANA timezone; defaults to system."
    ),
    as_json: bool = typer.Option(False, "--json", help="Versioned JSON output."),
    offline: bool = typer.Option(False, help="Read cached allowances without network access."),
    refresh: bool = typer.Option(False, help="Refresh quota and models.dev pricing caches."),
    no_cost: bool = typer.Option(False, "--no-cost", help="Skip price lookup and cost estimates."),
    timeout: float = typer.Option(10, min=0.1, max=120, help="Seconds per provider request."),
    version: bool = typer.Option(False, "--version", callback=version_callback, is_eager=True),
):
    if offline and refresh:
        raise typer.BadParameter("--offline and --refresh cannot be combined.")
    try:
        zone = ZoneInfo(timezone_name) if timezone_name else get_localzone()
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise typer.BadParameter("Unknown timezone; use an IANA name such as Asia/Tokyo.") from exc
    ctx.obj = Settings(
        Paths.discover(codex_home, claude_home, pi_home, omp_home, cache_dir),
        list(ALL_PROVIDERS) if provider == Provider.all else [provider.value],
        zone,
        as_json,
        offline,
        refresh,
        timeout,
        not no_cost,
    )
    if ctx.invoked_subcommand is None:
        # Terminals get one row per day; JSON keeps provider totals for automation.
        by = "provider" if ctx.obj.json else "day"
        parts = {}
        if any(name in ACCOUNT_PROVIDERS for name in ctx.obj.providers):
            parts["quota"] = quota_report(ctx.obj)
        parts["tokens"] = token_report(ctx.obj, by=by)
        output(ctx.obj, envelope(**parts))


@app.command()
def quota(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")):
    """Account windows, automatic resets, reset allowances, and credits."""
    output(ctx.obj, envelope(quota=quota_report(ctx.obj)), as_json)


@app.command()
def tokens(
    ctx: typer.Context,
    since: str | None = typer.Option(None, help="Rolling duration (7d), ISO date, or timestamp."),
    until: str | None = typer.Option(None, help="Exclusive end date/time; defaults to now."),
    month: bool = typer.Option(False, help="Current calendar month in the selected timezone."),
    today: bool = typer.Option(False, help="Today in the selected timezone."),
    all_time: bool = typer.Option(False, "--all", help="All available local history."),
    by: Group = typer.Option(Group.provider, help="Group within each provider."),
    detail: bool = typer.Option(
        False, help="With --by day, show the full breakdown instead of one row per day."
    ),
    as_json: bool = typer.Option(False, "--json"),
):
    """Detailed local tokens and API-equivalent cost; defaults to the last 30 days."""
    result = token_report(ctx.obj, since, until, month, all_time, by.value, today)
    output(ctx.obj, envelope(tokens=result), as_json, detail)


@app.command()
def doctor(ctx: typer.Context, as_json: bool = typer.Option(False, "--json")):
    """Offline diagnostics: CLI versions, data paths, and parser coverage."""
    settings = ctx.obj
    versions = {}
    for provider in settings.providers:
        executable = shutil.which(provider)
        version = None
        if executable:
            try:
                response = subprocess.run(
                    [executable, "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                version = (
                    response.stdout.strip()[:100] if response.returncode == 0 else "unavailable"
                )
            except (OSError, subprocess.TimeoutExpired):
                version = "unavailable"
        versions[provider] = {"executable": executable, "version": version}
    with Index(settings.paths.cache) as index:
        coverage = index.sync(settings.paths, settings.providers)
    result = envelope(
        doctor={
            "version": __version__,
            "paths": {
                provider: str(getattr(settings.paths, provider))
                for provider in (*ALL_PROVIDERS, "cache")
            },
            "timezone": str(settings.zone),
            "providers": versions,
            "coverage": coverage,
            "claude_credentials_file_present": (
                settings.paths.claude / ".credentials.json"
            ).is_file(),
            "claude_keychain_supported": False,
        }
    )
    output(settings, result, as_json)


if __name__ == "__main__":
    app()
