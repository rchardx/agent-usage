"""Validate a report offline against the schema bundled with the installed package."""

import argparse
import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from agent_usage.json_contract import load_schema


def reject_constant(value: str):
    raise ValueError("Non-finite numbers are not JSON")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path, help="JSON report file to validate")
    args = parser.parse_args()
    try:
        data = json.loads(args.report.read_text(encoding="utf-8"), parse_constant=reject_constant)
    except (OSError, ValueError):
        print("Cannot read report as standard UTF-8 JSON.", file=sys.stderr)
        return 1
    validator = Draft202012Validator(load_schema(), format_checker=FormatChecker())
    try:
        validator.validate(data)
    except ValidationError as exc:
        # Do not print the offending value or the full, potentially private report.
        print(f"Report does not match schema version 1 at {exc.json_path}.", file=sys.stderr)
        return 1
    print("Valid agent-usage schema version 1 report.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
