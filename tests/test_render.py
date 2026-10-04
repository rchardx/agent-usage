import io
from datetime import UTC, datetime

import pytest
from rich.console import Console
from test_pricing import catalog, event

from agent_usage.render import compact_number, render_tokens
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
