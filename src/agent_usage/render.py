from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from rich import box
from rich.console import Console, Group
from rich.measure import Measurement
from rich.padding import Padding
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
    return f"{rounded:.2f}{suffixes[index]}"


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
    contents: list[Text | Table] = [Text(f"{status} · {stamp}", style="dim"), Text("")]
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


class NumericCell(Text):
    """A Text-compatible footer/cell whose suffix padding survives justification."""

    def __rich_measure__(self, console, options):
        return Measurement(self.cell_len, self.cell_len)

    def __rich_console__(self, console, options):
        # Rich strips trailing Text whitespace before right justification. Apply
        # the reserved suffix slots as renderable padding instead of text spaces.
        value = self.plain.rstrip(" ")
        yield Padding(
            Text(value, style=self.style, justify="right"),
            (0, len(self.plain) - len(value), 0, 0),
        )


def numeric_cell(value: str, *, style: str = "") -> Text:
    """Reserve suffix slots so units and coverage markers do not shift decimals.

    Formatted values have two fractional digits, followed by an optional unit
    (K/M/B/T or %) and an optional *. Unknown values have neither suffix.
    """
    _, point, fraction = value.partition(".")
    suffix_length = len(fraction) - 2 if point else 0
    return NumericCell(value + " " * (2 - suffix_length), style=style)


def merge_metrics(metrics: list[dict]) -> dict:
    return {
        "tokens": None
        if any(m["tokens"] is None for m in metrics)
        else sum(m["tokens"] for m in metrics),
        "known_tokens": sum(m["known_tokens"] for m in metrics),
        "observed_events": sum(m["observed_events"] for m in metrics),
        "total_events": sum(m["total_events"] for m in metrics),
    }


def merge_costs(costs: list[dict]) -> dict:
    return {
        "amount": None
        if any(c["amount"] is None for c in costs)
        else str(sum(Decimal(c["amount"]) for c in costs)),
        "known_amount": str(sum(Decimal(c["known_amount"]) for c in costs)),
        "priced_events": sum(c["priced_events"] for c in costs),
    }


def cache_hit_text(read: dict, total_input: dict) -> str:
    if not read["observed_events"] or not total_input["known_tokens"]:
        return "—"
    share = Decimal(read["known_tokens"]) / Decimal(total_input["known_tokens"]) * 100
    text = f"{share:.2f}%"
    return text if read["tokens"] is not None else f"≥{text}*"


def bar(value: int, peak: int, width: int) -> str:
    if value <= 0 or peak <= 0:
        return ""
    eighths = max(1, round(value * width * 8 / peak))
    return "█" * (eighths // 8) + ("", "▏", "▎", "▍", "▌", "▋", "▊", "▉")[eighths % 8]


def render_daily(report: dict, console: Console, zone) -> None:
    groups = report["groups"]
    by_day: dict[date, dict[str, dict]] = {}
    for group in groups:
        by_day.setdefault(date.fromisoformat(group["group"]), {})[group["provider"]] = group
    first, last = min(by_day), max(by_day)
    days = [first + timedelta(days=offset) for offset in range((last - first).days + 1)]
    period = report["period"]
    start = datetime.fromisoformat(period["start"]) if period["start"] else None
    end = datetime.fromisoformat(period["end_exclusive"])
    priced = "pricing" in report
    # Fixed metric columns scale to any number of providers; optional columns are
    # added while they fit: input, output, cache read, cache write, hit rate, bar.
    spare = console.width - 22 - 13 * (2 + priced)
    show_input = spare >= 13
    spare -= 13 * show_input
    show_output = spare >= 13
    spare -= 13 * show_output
    show_cache_read = spare >= 13
    spare -= 13 * show_cache_read
    show_cache_write = spare >= 13
    spare -= 13 * show_cache_write
    show_hit = spare >= 13
    spare -= 13 * show_hit
    bar_width = min(40, spare - 4) if spare >= 14 else 0

    totals = report["totals"]["metrics"]
    table = Table(box=box.SIMPLE_HEAD, expand=True, padding=(0, 2), show_footer=True)
    table.add_column("Day", footer=Text("Total", style="bold"), justify="left", no_wrap=True)
    if show_input:
        table.add_column(
            "Input", footer=numeric_cell(metric_text(totals["input_tokens"])), justify="right"
        )
    if show_cache_read:
        table.add_column(
            "Cache read",
            footer=numeric_cell(metric_text(totals["cache_read_tokens"])),
            justify="right",
        )
    if show_cache_write:
        table.add_column(
            "Cache write",
            footer=numeric_cell(metric_text(totals["cache_write_tokens"])),
            justify="right",
        )
    if show_hit:
        table.add_column(
            "Cache hit",
            footer=numeric_cell(
                cache_hit_text(totals["cache_read_tokens"], totals["input_tokens"])
            ),
            justify="right",
        )
    if show_output:
        table.add_column(
            "Output", footer=numeric_cell(metric_text(totals["output_tokens"])), justify="right"
        )
    table.add_column(
        "Total",
        footer=numeric_cell(metric_text(totals["total_tokens"]), style="bold"),
        justify="right",
    )
    if priced:
        table.add_column(
            "USD",
            footer=numeric_cell(
                cost_text(report["totals"].get("estimated_cost")), style="bold green"
            ),
            justify="right",
        )
    if bar_width:
        table.add_column("", width=bar_width, justify="left", no_wrap=True)

    rows = []
    for day in days:
        entries = by_day.get(day, {})
        metrics = {
            key: merge_metrics([g["metrics"][key] for g in entries.values()])
            for key in (
                "total_tokens",
                "input_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "output_tokens",
            )
        }
        day_start = datetime.combine(day, time(), zone)
        day_end = datetime.combine(day + timedelta(days=1), time(), zone)
        partial = (start is not None and day_start < start) or day_end > end
        rows.append((day, entries, metrics, partial))
    peak = max(row[2]["total_tokens"]["known_tokens"] for row in rows)
    for day, entries, metrics, partial in rows:
        cells: list[Text | str] = [Text(f"{day:%Y-%m-%d %a}" + (" ·" if partial else "  "))]
        if show_input:
            cells.append(numeric_cell(metric_text(metrics["input_tokens"])))
        if show_cache_read:
            cells.append(numeric_cell(metric_text(metrics["cache_read_tokens"])))
        if show_cache_write:
            cells.append(numeric_cell(metric_text(metrics["cache_write_tokens"])))
        if show_hit:
            cells.append(
                numeric_cell(cache_hit_text(metrics["cache_read_tokens"], metrics["input_tokens"]))
            )
        if show_output:
            cells.append(numeric_cell(metric_text(metrics["output_tokens"])))
        cells.append(numeric_cell(metric_text(metrics["total_tokens"]), style="bold"))
        if priced:
            costs = [g["estimated_cost"] for g in entries.values() if "estimated_cost" in g]
            cells.append(
                numeric_cell(cost_text(merge_costs(costs) if costs else None), style="green")
            )
        if bar_width:
            cells.append(
                Text(bar(metrics["total_tokens"]["known_tokens"], peak, bar_width), style="cyan")
            )
        table.add_row(*cells, style="dim" if not entries else None)
    console.print(table)
    console.print()
    notes = ["  · partial day: the selected period starts or ends within it."]
    if bar_width:
        notes.append("  Bars scale to the busiest day.")
    if show_hit:
        notes.append("  Cache hit = cache read ÷ input.")
    notes.append("  All providers merge into each day; use --detail for per-provider breakdowns.")
    for note in notes:
        console.print(Text(note, style="dim"))


def render_tokens(report: dict, console: Console, zone=None, detail: bool = False):
    period = report["period"]
    daily = report["group_by"] == "day" and not detail and bool(report["groups"])
    console.print()
    console.rule(
        Text(
            f"Local token usage · {period.get('label', 'Selected period')}"
            + (" · by day" if daily else ""),
            style="bold",
        ),
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
    average = daily and console.width >= 100
    if average:
        summary.add_column(ratio=1)
    summary.add_row(
        Text(f"TOTAL TOKENS\n{total}", style="bold cyan"),
        *(
            [Text(f"API ESTIMATE · USD\n{price}", style="bold green")]
            if "pricing" in report
            else []
        ),
        Text(f"SESSIONS\n{report['totals']['sessions']}", style="bold"),
        *([daily_average(report)] if average else []),
    )
    console.print(Panel(summary, box=box.ROUNDED, border_style="dim", padding=(0, 1)))
    console.print()
    if daily:
        render_daily(report, console, zone or ZoneInfo(report["timezone"]))
    else:
        render_breakdown(report, console)
    render_notes(report, console, daily)


def daily_average(report: dict) -> Text:
    period = report["period"]
    if period["start"]:
        span = datetime.fromisoformat(period["end_exclusive"]) - datetime.fromisoformat(
            period["start"]
        )
        count = Decimal(span.total_seconds()) / 86400
    else:
        days = sorted(date.fromisoformat(group["group"]) for group in report["groups"])
        count = Decimal((days[-1] - days[0]).days + 1)
    tokens = report["totals"]["metrics"]["total_tokens"]
    average = compact_number(round(tokens["known_tokens"] / count))
    text = average if tokens["tokens"] is not None else f"≥{average}*"
    cost = report["totals"].get("estimated_cost")
    if "pricing" in report and cost and cost["priced_events"]:
        amount = Decimal(cost["amount"] or cost["known_amount"]) / count
        text += f" · ${amount:,.2f}" if cost["amount"] is not None else f" · ≥${amount:,.2f}*"
    return Text(f"DAILY AVERAGE\n{text}", style="bold")


def render_breakdown(report: dict, console: Console):
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
        table.add_column("Token breakdown", justify="left", ratio=2)
        for group in batch:
            label = group["provider"].upper()
            if report["group_by"] != "provider":
                label += f"\n{group['group']}"
            table.add_column(Text(label), justify="right", ratio=2)
        for label, key in fields:
            bold = key in ("input_tokens", "output_tokens", "total_tokens")
            table.add_row(
                label,
                *(numeric_cell(metric_text(group["metrics"][key])) for group in batch),
                style="bold" if bold else None,
                end_section=key
                in ("cache_write_1h_tokens", "non_reasoning_output_tokens", "total_tokens"),
            )
        if "pricing" in report:
            table.add_row(
                "API estimate · USD",
                *(numeric_cell(cost_text(g.get("estimated_cost"))) for g in batch),
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


def unpriced_reason_text(reason: str) -> str:
    descriptions = {
        "model_not_priced": "missing model price",
        "unknown_cache_usage": "unknown cache usage",
        "inconsistent_cache_usage": "inconsistent cache usage",
        "unknown_reasoning_usage": "unknown reasoning usage",
        "unknown_request_size_for_tier": "unknown request size for context pricing tier",
        "unknown_cache_write_ttl": "unknown cache write TTL",
        "inconsistent_cache_write_ttl": "inconsistent cache write TTL",
        "invalid_price": "invalid price",
        "invalid_price_tier": "invalid pricing tier",
        "unsupported_price_tier": "unsupported pricing tier",
        "unsupported_price_structure": "unsupported price structure",
    }
    return descriptions.get(reason, reason.replace("_", " "))


def render_notes(report: dict, console: Console, daily: bool):
    console.print(
        Text(
            "  K / M / B / T = 10³ / 10⁶ / 10⁹ / 10¹² tokens. JSON retains exact counts.",
            style="dim",
        )
    )
    console.print(
        Text(
            "  Total = input + output; input includes cache, output includes reasoning."
            if daily
            else "  ¹ Input includes cache. ² Output includes reasoning. Total = input + output.",
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
        cost = report["totals"].get("estimated_cost", {})
        details = cost.get("unpriced_details", [])
        if details:
            console.print(Text("  Unpriced records:", style="yellow"))
            for item in details:
                count = item["events"]
                records = "record" if count == 1 else "records"
                console.print(
                    Text(
                        f"    {item['provider']} / {item['model']}: {count:,} {records}"
                        f" — {unpriced_reason_text(item['reason'])}",
                        style="yellow",
                    )
                )
        elif cost.get("unpriced_reasons"):
            # Older JSON reports may not contain the per-model breakdown.
            console.print(Text(f"  Unpriced records: {cost['unpriced_reasons']}", style="yellow"))
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
