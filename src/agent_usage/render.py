from datetime import UTC, datetime
from decimal import Decimal

from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text


def compact_number(value: int | float | Decimal) -> str:
    number = Decimal(str(value))
    suffixes = ("", "K", "M", "B", "T")
    index = 0
    while abs(number) >= 1000 and index < len(suffixes) - 1:
        number /= 1000
        index += 1
    rounded = number.quantize(Decimal("0.01"))
    if abs(rounded) >= 1000 and index < len(suffixes) - 1:
        rounded /= 1000
        index += 1
    text = (
        format(rounded, "f").rstrip("0").rstrip(".")
        if "." in format(rounded, "f")
        else str(rounded)
    )
    return text + suffixes[index]


def display_time(value: str | None, zone) -> str:
    if value is None:
        return "unknown"
    at = datetime.fromisoformat(value).astimezone(zone)
    seconds = int((at - datetime.now(UTC)).total_seconds())
    hours = seconds // 3600
    remaining = (
        f"{hours // 24}d {hours % 24}h" if hours >= 24 else f"{hours}h {(seconds % 3600) // 60}m"
    )
    countdown = f"in {remaining}" if seconds > 0 else "reset time passed"
    return f"{at:%m-%d %H:%M} · {countdown}"


def account_panel(account: dict, zone) -> Panel:
    age = account.get("age_seconds")
    status = account["status"]
    stamp = (
        "just now"
        if age is not None and age < 5
        else f"{age}s ago"
        if age is not None
        else "no snapshot"
    )
    contents = [Text(f"{status} · {stamp}", style="dim"), Text("")]
    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 1), leading=1)
    table.add_column("Allowance", ratio=2)
    table.add_column("Used", justify="right", ratio=1)
    table.add_column("Resets", ratio=3)
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
        label = window.get("label") or window["bucket"]
        if label in ("five_hour", "seven_day"):
            label = "All models"
        percent = window["used_percent"]
        table.add_row(
            Text(f"{label} · {duration}"),
            "unknown" if percent is None else f"{percent:g}%",
            display_time(window["resets_at"], zone),
        )
    contents.append(table if account["windows"] else Text("Allowance windows: not reported"))
    contents.append(Text(""))
    resets = account.get("resets")
    count = resets.get("available_count") if resets else None
    contents.append(Text(f"Available resets    {count if count is not None else 'not reported'}"))
    for item in (resets or {}).get("details") or []:
        expiry = item.get("expires_at")
        when = (
            datetime.fromisoformat(expiry).astimezone(zone).strftime("%Y-%m-%d %H:%M")
            if expiry
            else "unknown"
        )
        contents.append(Text(f"  Expires {when}", style="dim"))
    contents.append(Text(""))
    if not account["credits"]:
        contents.append(Text("Credits             not reported"))
    for credit in account["credits"]:
        if credit["enabled"] is False:
            state = "disabled"
        elif credit["unlimited"] is True:
            state = "unlimited"
        else:
            state = f"balance {credit['balance'] if credit['balance'] is not None else 'unknown'}"
        if credit.get("used") is not None or credit.get("limit") is not None:
            used = credit["used"] if credit.get("used") is not None else "unknown"
            limit = credit["limit"] if credit.get("limit") is not None else "unknown"
            state += f" · used {used} / limit {limit}"
        label = credit.get("label") or credit["bucket"]
        contents.append(Text(f"Credits · {label}    {state}"))
        if any(credit.get(key) is not None for key in ("balance", "used", "limit")):
            unit = credit["unit"].replace(
                "provider_units_unverified", "raw units; scale unverified"
            )
            contents.append(Text(f"  {unit}", style="dim"))
        if credit.get("expires_at"):
            expiry = datetime.fromisoformat(credit["expires_at"]).astimezone(zone)
            contents.append(Text(f"  Expires {expiry:%Y-%m-%d %H:%M}", style="dim"))
    if account.get("error"):
        contents.extend((Text(""), Text(account["error"], style="yellow")))
    for warning in account.get("warnings", []):
        contents.append(Text(warning, style="dim"))
    title = f"{account['provider'].upper()}  ·  {account.get('plan') or 'unknown plan'}"
    return Panel(
        Group(*contents),
        title=Text(title, style="bold"),
        title_align="left",
        padding=(1, 2),
        border_style="blue",
        expand=True,
    )


def render_quotas(report: dict, console: Console, zone):
    console.print()
    console.rule(Text(f"Account allowances · {zone}", style="bold"), align="left")
    console.print()
    panels = [account_panel(account, zone) for account in report["accounts"]]
    if console.width >= 140 and len(panels) == 2:
        layout = Table.grid(expand=True, padding=(0, 1))
        layout.add_column(ratio=1)
        layout.add_column(ratio=1)
        layout.add_row(*panels)
        console.print(layout)
    else:
        for panel in panels:
            console.print(panel)
            console.print()


def metric_text(metric: dict) -> str:
    if metric["tokens"] is not None:
        return compact_number(metric["tokens"])
    if metric["observed_events"]:
        return f"≥{compact_number(metric['known_tokens'])}*"
    return "—"


def cost_text(cost: dict | None) -> str:
    if not cost or not cost["priced_events"]:
        return "—"
    value = cost["amount"] if cost["amount"] is not None else cost["known_amount"]
    amount = Decimal(value)
    text = "<$0.01" if 0 < amount < Decimal("0.01") else f"${amount:,.2f}"
    if cost["amount"] is None:
        text = f"≥${amount:,.2f}*"
    return text


def render_tokens(report: dict, console: Console):
    period = report["period"]
    console.print()
    console.rule(
        Text(f"Local token usage · {period.get('label', 'Selected period')}", style="bold"),
        align="left",
    )
    start = (
        datetime.fromisoformat(period["start"]).strftime("%Y-%m-%d %H:%M")
        if period["start"]
        else "all history"
    )
    end = datetime.fromisoformat(period["end_exclusive"]).strftime("%Y-%m-%d %H:%M")
    console.print(Text(f"  {start} → {end}  ·  {report['timezone']}", style="dim"))
    console.print()
    summary = Table.grid(expand=True, padding=(1, 2))
    summary.add_column(ratio=1)
    summary.add_column(ratio=1)
    total = metric_text(report["totals"]["metrics"]["total_tokens"])
    price = cost_text(report["totals"].get("estimated_cost"))
    if "pricing" in report:
        summary.add_column(ratio=1)
    summary.add_row(
        Text(f"TOTAL TOKENS\n{total}", style="bold cyan"),
        *(
            [Text(f"API ESTIMATE · USD\n{price}", style="bold green")]
            if "pricing" in report
            else []
        ),
        Text(f"SESSIONS\n{report['totals']['sessions']}", style="bold"),
    )
    console.print(Panel(summary, box=box.ROUNDED, border_style="dim", padding=(0, 1)))
    console.print()
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
    batch_size = max(1, min(4, (console.width - 24) // 24))
    for offset in range(0, len(groups), batch_size):
        batch = groups[offset : offset + batch_size]
        table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 2))
        table.add_column("Token breakdown", ratio=2)
        for group in batch:
            label = group["provider"].upper()
            if report["group_by"] != "provider":
                label += f"\n{group['group']}"
            table.add_column(Text(label), justify="right", ratio=2)
        for label, key in fields:
            bold = key in ("input_tokens", "output_tokens", "total_tokens")
            table.add_row(
                label,
                *(metric_text(group["metrics"][key]) for group in batch),
                style="bold" if bold else None,
                end_section=key
                in ("cache_write_1h_tokens", "non_reasoning_output_tokens", "total_tokens"),
            )
        if "pricing" in report:
            table.add_row(
                "API estimate · USD",
                *(cost_text(g.get("estimated_cost")) for g in batch),
                style="bold green",
            )
            table.add_row(
                "  Priced records",
                *(
                    f"{g.get('estimated_cost', {}).get('priced_events', 0):,} / {g['events']:,}"
                    for g in batch
                ),
                style="dim",
            )
        console.print(table)
        console.print()
    if not groups:
        console.print("No token records in the selected period.")
    console.print(
        Text(
            "  K / M / B / T = 10³ / 10⁶ / 10⁹ / 10¹² tokens. JSON retains exact counts.",
            style="dim",
        )
    )
    console.print(
        Text(
            "  ¹ Input includes cache. ² Output includes reasoning. Total = input + output.",
            style="dim",
        )
    )
    console.print(
        Text("  — unknown; ≥…* is the known subtotal with incomplete coverage.", style="dim")
    )
    if "pricing" in report:
        pricing = report["pricing"]
        console.print()
        console.print(
            Text(
                f"  Pricing: models.dev · {pricing['status']} · "
                f"{pricing.get('fetched_at') or 'not fetched'}",
                style="dim",
            )
        )
        console.print(
            Text(
                "  Estimate uses current standard API rates; subscription charges may differ.",
                style="dim",
            )
        )
        if pricing.get("error"):
            console.print(Text(f"  {pricing['error']}", style="yellow"))
        reasons = report["totals"].get("estimated_cost", {}).get("unpriced_reasons", {})
        if reasons:
            console.print(Text(f"  Unpriced records: {reasons}", style="yellow"))
    coverage = report["coverage"]
    console.print()
    console.print(
        Text(
            f"  Coverage: {coverage['files']} files · {coverage['selected_events']:,} usage events"
            f" · {report['totals']['sessions']} sessions",
            style="dim",
        )
    )
    for provider, detail in coverage["providers"].items():
        if not detail["root_exists"] or detail["files"] == 0:
            console.print(Text(f"  {provider}: no local logs found", style="yellow"))
    if coverage["issues"]:
        console.print(Text(f"  Coverage notes: {coverage['issues']}", style="dim"))
    console.print(Text(f"  {coverage['note']}", style="dim"))
    console.print()
