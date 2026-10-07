"""Recognized account allowances; provider identifiers are not display names."""

WINDOW_LABELS = {
    "codex": {"codex": "Codex"},
    "claude": {
        "five_hour": "All models",
        "seven_day": "All models",
        "seven_day_sonnet": "Sonnet",
        "seven_day_opus": "Opus",
        "session": "All models",
        "weekly_all": "All models",
    },
}

CREDIT_LABELS = {
    "codex": {"codex": "Codex"},
    "claude": {"extra_usage": "Extra usage", "spend": "Spend balance"},
}


def recognized_account(report: dict) -> dict:
    """Apply current recognition rules to snapshots, including older cached reports."""
    result = dict(report)
    for field, labels in (("windows", WINDOW_LABELS), ("credits", CREDIT_LABELS)):
        known = labels.get(report["provider"], {})
        result[field] = [
            {**row, "label": known[row["bucket"]]}
            for row in report.get(field, [])
            if row["bucket"] in known
        ]
    return result
