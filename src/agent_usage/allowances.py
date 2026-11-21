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
    "antigravity": {
        "gemini-5h": "Gemini",
        "gemini-weekly": "Gemini",
        "3p-5h": "Claude & GPT",
        "3p-weekly": "Claude & GPT",
    },
}

CREDIT_LABELS = {
    "codex": {"codex": "Codex"},
    "claude": {"extra_usage": "Extra usage", "spend": "Spend balance"},
    "antigravity": {"ai_credits": "AI credits"},
}

NON_SUBSCRIPTION_LABELS = {
    **dict.fromkeys(("apiKey", "api_key"), "API key"),
    "customGateway": "Custom gateway",
    **dict.fromkeys(("gcp", "vertex"), "Google Cloud Vertex AI"),
    **dict.fromkeys(("amazonBedrock", "bedrock"), "Amazon Bedrock"),
    "foundry": "Anthropic Foundry",
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
