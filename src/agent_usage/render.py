from datetime import UTC, datetime

from rich.console import Console
from rich.table import Table
from rich.text import Text


def display_time(value: str | None, zone) -> str:
    if value is None:
        return "unknown"
    at = datetime.fromisoformat(value).astimezone(zone)
    seconds = int((at - datetime.now(UTC)).total_seconds())
    countdown = (
        f"in {seconds // 3600}h {(seconds % 3600) // 60}m" if seconds > 0 else "reset time passed"
    )
    return f"{at:%m-%d %H:%M} ({countdown})"


def render_quotas(report: dict, console: Console, zone):
    console.print(Text(f"Account allowances · {zone}", style="bold"))
    for account in report["accounts"]:
        title = f"{account['provider']} · {account.get('plan') or 'unknown plan'}"
        age = account.get("age_seconds")
        title += f" · {account['status']}"
        if age is not None:
            title += f" · observed {age}s ago"
        console.print(Text(title, style="bold"))
        table = Table("Bucket", "Window", "Used", "Resets", expand=False)
        for window in account["windows"]:
            minutes = window.get("duration_minutes")
            duration = (
                f"{minutes // 1440}d"
                if minutes and minutes % 1440 == 0
                else f"{minutes // 60}h"
                if minutes and minutes % 60 == 0
                else f"{minutes}m"
                if minutes
                else window["window"]
            )
            percent = window["used_percent"]
            table.add_row(
                Text(window.get("label") or window["bucket"]),
                Text(duration),
                "unknown" if percent is None else f"{percent:g}%",
                display_time(window["resets_at"], zone),
            )
        if account["windows"]:
            console.print(table)
        else:
            console.print("  Allowance windows: not reported")
        resets = account.get("resets")
        if resets is None:
            console.print("  Available resets: not reported")
        else:
            count = resets["available_count"]
            console.print(f"  Available resets: {count if count is not None else 'unknown'}")
            for item in resets.get("details") or []:
                expiry = item.get("expires_at")
                when = (
                    datetime.fromisoformat(expiry).astimezone(zone).strftime("%Y-%m-%d %H:%M")
                    if expiry
                    else "unknown"
                )
                console.print(Text(f"    {item.get('scope') or 'unknown scope'} · expires {when}"))
        if not account["credits"]:
            console.print("  Credits: not reported")
        for credit in account["credits"]:
            if credit["enabled"] is False:
                state = "disabled"
            elif credit["unlimited"] is True:
                state = "unlimited"
            else:
                state = (
                    f"balance {credit['balance'] if credit['balance'] is not None else 'unknown'}"
                )
            if credit.get("used") is not None or credit.get("limit") is not None:
                used = credit["used"] if credit.get("used") is not None else "unknown"
                limit = credit["limit"] if credit.get("limit") is not None else "unknown"
                state += f" · used {used} / limit {limit}"
            if any(credit.get(key) is not None for key in ("balance", "used", "limit")):
                unit = credit["unit"].replace(
                    "provider_units_unverified", "raw units; scale unverified"
                )
                state += f" · {unit}"
            if credit.get("expires_at"):
                expiry = datetime.fromisoformat(credit["expires_at"]).astimezone(zone)
                state += f" · expires {expiry:%Y-%m-%d %H:%M}"
            console.print(Text(f"  Credits ({credit['bucket']}): {state}"))
        if account.get("error"):
            console.print(Text(f"  {account['error']}", style="yellow"))
        for warning in account.get("warnings", []):
            console.print(Text(f"  Note: {warning}", style="dim"))


def metric_text(metric: dict) -> str:
    if metric["tokens"] is not None:
        return f"{metric['tokens']:,}"
    if metric["observed_events"]:
        return f"≥{metric['known_tokens']:,}*"
    return "—"


def render_tokens(report: dict, console: Console):
    period = report["period"]
    console.print(
        Text(
            f"Local tokens · {period['start'] or 'all available history'}"
            f" → {period['end_exclusive']}",
            style="bold",
        )
    )
    fields = (
        ("Input¹", "input_tokens"),
        ("  Fresh input", "fresh_input_tokens"),
        ("  Cache read", "cache_read_tokens"),
        ("  Cache write", "cache_write_tokens"),
        ("    Write: 5m TTL", "cache_write_5m_tokens"),
        ("    Write: 1h TTL", "cache_write_1h_tokens"),
        ("Output²", "output_tokens"),
        ("  Reasoning", "reasoning_tokens"),
        ("  Other output", "non_reasoning_output_tokens"),
        ("Total", "total_tokens"),
    )
    groups = report["groups"]
    # Transpose small reports so full counts stay legible in an 80-column terminal.
    batch_size = 3 if report["group_by"] == "provider" else 1
    for offset in range(0, len(groups), batch_size):
        batch = groups[offset : offset + batch_size]
        table = Table()
        table.add_column("Metric")
        for group in batch:
            label = group["provider"]
            if report["group_by"] != "provider":
                label += f" / {group['group']}"
            table.add_column(Text(label), justify="right")
        for label, key in fields:
            table.add_row(label, *(metric_text(group["metrics"][key]) for group in batch))
        console.print(table)
    if not groups:
        console.print("No token records in the selected period.")
    console.print(
        "¹ Input includes cache read/write. ² Output includes reasoning. Total = input + output."
    )
    console.print("— unknown; ≥…* observed subtotal with incomplete field coverage.")
    coverage = report["coverage"]
    console.print(
        f"Coverage: {coverage['files']} files · {coverage['selected_events']} usage events"
        f" · {report['totals']['sessions']} sessions · scanned {coverage['bytes_read']:,} bytes"
    )
    for provider, detail in coverage["providers"].items():
        if not detail["root_exists"] or detail["files"] == 0:
            console.print(Text(f"{provider}: no local logs found", style="yellow"))
    if coverage["issues"]:
        console.print(Text(f"Coverage notes: {coverage['issues']}", style="yellow"))
    console.print(Text(coverage["note"], style="dim"))
