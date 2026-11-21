import copy
import io
import re
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from rich.console import Console
from test_pricing import catalog, event

from agent_usage.models import AccountReport
from agent_usage.providers import non_subscription_account
from agent_usage.render import (
    cache_hit_text,
    compact_number,
    cost_text,
    metric_text,
    numeric_cell,
    render_quotas,
    render_tokens,
    unpriced_reason_text,
)
from agent_usage.reports import summarize


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (0, "0.00"),
        (1, "1.00"),
        (123, "123.00"),
        (1000, "1.00K"),
        (1100, "1.10K"),
        (1_000_000, "1.00M"),
        (1_000_000_000, "1.00B"),
        (1_000_000_000_000, "1.00T"),
        (Decimal("12.3"), "12.30"),
        (Decimal("999.995"), "1.00K"),
        (999_994, "999.99K"),
        (1234, "1.23K"),
        (1234567, "1.23M"),
        (1234567890, "1.23B"),
        (1234567890000, "1.23T"),
        (999999, "1.00M"),
    ],
)
def test_compact_token_units(number, expected):
    assert compact_number(number) == expected


@pytest.mark.parametrize("width", [80, 160])
def test_report_uses_terminal_width_and_keeps_cost_and_units_visible(width):
    report = summarize(
        [event()],
        None,
        datetime(2026, 11, 1, tzinfo=UTC),
        "provider",
        UTC,
        {"files": 1, "providers": {}, "issues": {}},
        catalog(),
    )
    stream = io.StringIO()
    render_tokens(report, Console(file=stream, width=width, color_system=None))
    text = stream.getvalue()
    assert "1.10M" in text
    assert "$1.97" in text
    assert "Last 30" not in text  # explicit all-history input is not relabeled
    assert max(map(len, text.splitlines())) == width
    assert "≥" not in text.split("API estimate · USD")[0]


def daily_report(*, end=datetime(2026, 10, 4, 12, tzinfo=UTC), start=None):
    events = [
        event(event_id="a", timestamp="2026-10-01T00:00:00+00:00"),
        event(event_id="b", timestamp="2026-10-01T05:00:00+00:00", provider="claude"),
        event(event_id="c", timestamp="2026-10-03T00:00:00+00:00"),
    ]
    return summarize(
        events, start, end, "day", UTC, {"files": 1, "providers": {}, "issues": {}}, catalog()
    )


def render(report, width, **options):
    stream = io.StringIO()
    render_tokens(report, Console(file=stream, width=width, color_system=None), UTC, **options)
    return stream.getvalue()


@pytest.mark.parametrize("width", [60, 80, 120, 200])
def test_daily_view_has_one_row_per_day_and_fits_width(width):
    text = render(daily_report(), width)
    rows = [line for line in text.splitlines() if line.strip().startswith("2026-10-0")]
    assert [row.split()[0] for row in rows] == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert "2.20M" in rows[0]  # both providers merged into the day total
    assert rows[1].split()[1:3] == ["Fri", "0.00"]  # empty days stay visible
    assert "Token breakdown" not in text
    assert max(map(len, text.splitlines())) == width


def test_daily_view_merges_costs_and_marks_partial_days():
    text = render(daily_report(start=datetime(2026, 10, 1, 1, tzinfo=UTC)), 200)
    rows = {line.split()[0]: line for line in text.splitlines() if "2026-10-0" in line}
    assert "·" in rows["2026-10-01"].split()[2]  # starts after midnight
    assert "·" not in rows["2026-10-03"]
    assert "$1.97" in rows["2026-10-03"]
    assert "█" in rows["2026-10-03"]
    assert "DAILY AVERAGE" in text


def test_daily_view_lists_partial_claude_cost_as_subtotal():
    # Only codex has a catalog price, so the mixed day is a known subtotal.
    text = render(daily_report(), 200)
    first = next(line for line in text.splitlines() if line.strip().startswith("2026-10-01"))
    assert "≥$1.97*" in first
    assert "Cache hit" in text and "60.00%" in first


@pytest.mark.parametrize("width", [60, 80, 160])
@pytest.mark.parametrize("detail", [False, True])
def test_unpriced_records_show_agent_model_count_and_readable_reason(width, detail):
    report = summarize(
        [event(model="codex-auto-review") for _ in range(36)] + [event()],
        None,
        datetime(2026, 11, 1, tzinfo=UTC),
        "day",
        UTC,
        {"files": 1, "providers": {}, "issues": {}},
        catalog(),
    )
    text = render(report, width, detail=detail)
    normalized = " ".join(text.split())
    assert "Unpriced records:" in text
    assert "codex / codex-auto-review: 36 records — missing model price" in normalized
    assert "model_not_priced" not in text
    assert "≥$1.97*" in text


def test_priced_report_has_no_unpriced_warning():
    report = daily_report()
    report["totals"]["estimated_cost"]["unpriced_details"] = []
    report["totals"]["estimated_cost"]["unpriced_reasons"] = {}
    assert "Unpriced records:" not in render(report, 80)


def test_older_report_keeps_unpriced_reason_fallback():
    report = daily_report()
    del report["totals"]["estimated_cost"]["unpriced_details"]
    assert "Unpriced records: {'model_not_priced': 1}" in render(report, 80)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("unknown_cache_write_ttl", "unknown cache write TTL"),
        ("missing_cache_read_price", "missing cache read price"),
        ("future_reason", "future reason"),
    ],
)
def test_unpriced_reason_descriptions(reason, expected):
    assert unpriced_reason_text(reason) == expected


def test_detail_keeps_full_daily_breakdown():
    text = render(daily_report(), 160, detail=True)
    assert "Token breakdown" in text
    assert "DAILY AVERAGE" not in text


@pytest.mark.parametrize(
    ("value", "padding"),
    [
        ("0.00", 2),
        ("123.00", 2),
        ("1.00K", 1),
        ("1.10M", 1),
        ("1.00B", 1),
        ("1.00T", 1),
        ("≥1.20K*", 0),
        ("≥12.00*", 1),
        ("60.00%", 1),
        ("≥60.00%*", 0),
        ("$1.20", 2),
        ("≥$1.20*", 1),
        ("<$0.01", 2),
        ("—", 2),
    ],
)
def test_numeric_cells_reserve_unit_and_coverage_slots(value, padding):
    cell = numeric_cell(value, style="green")
    assert cell.plain == value + " " * padding
    assert cell.style == "green"
    if "." in value:
        assert cell.plain.rjust(20).index(".") == 15


def test_two_decimal_formatting_preserves_unknowns_and_partial_coverage():
    full = {"tokens": 1200, "known_tokens": 1200, "observed_events": 1, "total_events": 1}
    partial = {**full, "tokens": None, "total_events": 2}
    unknown = {"tokens": None, "known_tokens": 0, "observed_events": 0, "total_events": 1}
    assert metric_text(full) == "1.20K"
    assert metric_text(partial) == "≥1.20K*"
    assert metric_text(unknown) == "—"
    denominator = {**full, "tokens": 3600, "known_tokens": 3600}
    assert cache_hit_text(full, denominator) == "33.33%"
    assert cache_hit_text(partial, denominator) == "≥33.33%*"
    assert cache_hit_text(unknown, denominator) == "—"
    assert cost_text({"priced_events": 1, "amount": "0.001000"}) == "<$0.01"
    assert cost_text({"priced_events": 1, "amount": None, "known_amount": "1.200000"}) == "≥$1.20*"
    assert cost_text({"priced_events": 0, "amount": None}) == "—"


NUMBER = re.compile(r"[≥<]?\$?\d[\d,]*\.\d{2}[KMBT%]?\*?")


def decimal_columns(line):
    return {match.start() + match.group().index(".") for match in NUMBER.finditer(line)}


@pytest.mark.parametrize("width", [60, 80, 120, 200])
def test_daily_decimal_columns_align_across_rows_units_and_footer(width):
    text = render(daily_report(), width)
    rows = [line for line in text.splitlines() if line.strip().startswith("2026-10-0")]
    footer = next(line for line in text.splitlines() if line.strip().startswith("Total "))
    # A partial cost has a trailing *, an empty day has no unit, and totals have units.
    columns = decimal_columns(rows[0])
    assert columns and columns == decimal_columns(rows[2]) == decimal_columns(footer)
    assert decimal_columns(rows[1]) <= columns
    assert all(row.index("2026-") == footer.index("Total") for row in rows)
    if width == 200:
        assert rows[0].index("█") == rows[2].index("█")


@pytest.mark.parametrize("width", [80, 160])
def test_breakdown_decimals_align_across_metrics_partial_values_and_costs(width):
    report = summarize(
        [event(), event(event_id="unpriced", cache_read_tokens=None)],
        None,
        datetime(2026, 11, 1, tzinfo=UTC),
        "provider",
        UTC,
        {"files": 1, "providers": {}, "issues": {}},
        catalog(),
    )
    # Mix complete, partial and unknown metrics within the same numeric column.
    text = render(report, width)
    rows = [
        line
        for line in text.splitlines()
        if line.strip().startswith(
            ("Input¹", "Cache read", "Output²", "Reasoning", "Total ", "API estimate · USD")
        )
    ]
    assert len(rows) == 6
    assert len(set.union(*(decimal_columns(row) for row in rows))) == 1
    assert "≥600.00K*" in text
    assert "—" in text


@pytest.mark.parametrize("detail", [False, True])
def test_rendering_does_not_change_exact_json_values(detail):
    report = daily_report()
    before = copy.deepcopy(report)
    render(report, 200, detail=detail)
    assert report == before
    assert report["totals"]["metrics"]["total_tokens"]["tokens"] == 3_300_000
    assert report["totals"]["estimated_cost"]["known_amount"] == "3.940000"


@pytest.mark.parametrize(
    ("provider", "auth_mode", "expected_label"),
    [
        ("codex", "apiKey", "API key"),
        ("codex", "customGateway", "Custom gateway"),
        ("claude", "api_key", "API key"),
        ("claude", "vertex", "Google Cloud Vertex AI"),
        ("claude", "bedrock", "Amazon Bedrock"),
        ("antigravity", "apiKey", "API key"),
        ("antigravity", "gcp", "Google Cloud Vertex AI"),
    ],
)
@pytest.mark.parametrize("width", [80, 160])
def test_non_subscription_account_panel_avoids_login_error_noise(
    provider, auth_mode, expected_label, width
):
    account = {**non_subscription_account(provider, "test", auth_mode).to_dict(), "age_seconds": 0}
    other = {
        **AccountReport(
            "claude" if provider == "codex" else "codex",
            "test",
            status="unavailable",
            error="Not logged in.",
        ).to_dict(),
    }
    stream = io.StringIO()
    render_quotas(
        {"accounts": [account, other]}, Console(file=stream, width=width, color_system=None), UTC
    )
    text = stream.getvalue()
    assert f"{provider.upper()}  ·  {expected_label}" in text
    assert f"{expected_label} mode · subscription allowances not applicable" in text
    assert "Use local token reports and API cost estimates" in text
    assert max(map(len, text.splitlines())) <= width


@pytest.mark.parametrize("width", [80, 160, 220])
def test_three_account_panels_render_within_width(width):
    accounts = [
        AccountReport(name, "test", status="unavailable", error="Not logged in.").to_dict()
        for name in ("codex", "claude", "antigravity")
    ]
    stream = io.StringIO()
    render_quotas({"accounts": accounts}, Console(file=stream, width=width, color_system=None), UTC)
    text = stream.getvalue()
    for title in ("CODEX", "CLAUDE", "ANTIGRAVITY"):
        assert title in text
    assert max(map(len, text.splitlines())) <= width
    if width >= 140:
        assert not text.endswith("\n\n")
