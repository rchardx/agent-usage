import io
from datetime import UTC, datetime

import pytest
from rich.console import Console
from test_pricing import catalog, event

from agent_usage.render import compact_number, render_tokens, unpriced_reason_text
from agent_usage.reports import summarize


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        (0, "0"),
        (123, "123"),
        (1234, "1.23K"),
        (1234567, "1.23M"),
        (1234567890, "1.23B"),
        (1234567890000, "1.23T"),
        (999999, "1M"),
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
    assert "1.1M" in text
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
    assert "2.2M" in rows[0]  # both providers merged into the day total
    assert rows[1].split()[1:3] == ["Fri", "0"]  # empty days stay visible
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
    assert "Cache hit" in text and "60.0%" in first


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
