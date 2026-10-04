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
from .render import render_quotas, render_tokens
from .reports import envelope, period, quotas, summarize

app = typer.Typer(
    no_args_is_help=False,
    pretty_exceptions_show_locals=False,
    help="Codex and Claude Code allowances and local token usage.",
)


class Provider(StrEnum):
    all = "all"
    codex = "codex"
    claude = "claude"


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


def token_report(
    settings: Settings, since=None, until=None, month=False, all_time=False, by="provider"
):
    try:
        start, end = period(since, until, month, all_time, settings.zone)
    except (ValueError, OverflowError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    with Index(settings.paths.cache) as index:
        coverage = index.sync(settings.paths, settings.providers)
        events = index.events(settings.paths, settings.providers)
    return summarize(events, start, end, by, settings.zone, coverage)


def quota_report(settings: Settings):
    return quotas(
        settings.paths,
        settings.providers,
        refresh=settings.refresh,
        offline=settings.offline,
        timeout=settings.timeout,
    )


def output(settings: Settings, result: dict, as_json: bool = False):
    if settings.json or as_json:
        typer.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return
    console = Console()
    if "quota" in result:
        render_quotas(result["quota"], console, settings.zone)
    if "tokens" in result:
        render_tokens(result["tokens"], console)
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
    cache_dir: Path | None = typer.Option(
        None, help="Rebuildable local index and quota snapshots."
    ),
    timezone_name: str | None = typer.Option(
        None, "--timezone", help="IANA timezone; defaults to system."
    ),
    as_json: bool = typer.Option(False, "--json", help="Versioned JSON output."),
    offline: bool = typer.Option(False, help="Read cached allowances without network access."),
    refresh: bool = typer.Option(False, help="Bypass the 120-second quota cache."),
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
        Paths.discover(codex_home, claude_home, cache_dir),
        ["codex", "claude"] if provider == Provider.all else [provider.value],
        zone,
        as_json,
        offline,
        refresh,
        timeout,
    )
    if ctx.invoked_subcommand is None:
        result = envelope(quota=quota_report(ctx.obj), tokens=token_report(ctx.obj))
        output(ctx.obj, result)


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
    all_time: bool = typer.Option(False, "--all", help="All available local history."),
    by: Group = typer.Option(Group.provider, help="Group within each provider."),
    as_json: bool = typer.Option(False, "--json"),
):
    """Local tokens with cache and reasoning details; defaults to today."""
    result = token_report(ctx.obj, since, until, month, all_time, by.value)
    output(ctx.obj, envelope(tokens=result), as_json)


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
                "codex": str(settings.paths.codex),
                "claude": str(settings.paths.claude),
                "cache": str(settings.paths.cache),
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
