"""Validate the shared commit and PR message contract using only the standard library."""

import re
import sys
from pathlib import Path

SUBJECT = re.compile(r"^(chore|docs|feat|fix|test|refactor)(?:\([a-z0-9._-]+\))?(!)?: \S.*$")
BREAKING = re.compile(r"^BREAKING(?: |-)?CHANGE:")
SCISSORS = "# ------------------------ >8 ------------------------"
PLACEHOLDERS = {
    "Describe the problem, resulting behavior and rationale in one paragraph.",
    "- Describe a concrete change.",
}
# This is a limited script check, not natural-language detection. English prose
# may still contain technical Unicode symbols, names and identifiers.
NON_ENGLISH = re.compile(
    "[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\U00020000-\U0003ffff]"
)


def validate_commit_message(message: str) -> list[str]:
    lines = []
    for line in message.splitlines():
        if line == SCISSORS:
            break
        if not line.startswith("#"):
            lines.append(line.rstrip())
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return ["message is empty"]

    errors = []
    match = SUBJECT.fullmatch(lines[0])
    if not match:
        errors.append(
            "subject must follow <type>[(scope)][!]: <subject>; "
            "types: chore, docs, feat, fix, test, refactor"
        )
    if len(lines[0]) >= 72:
        errors.append("subject must be under 72 characters")
    if NON_ENGLISH.search("\n".join(lines)):
        errors.append("write the subject and body in English")
    if any(line in PLACEHOLDERS for line in lines):
        errors.append("replace the template placeholders with the actual change")

    if lines.count("Key changes:") != 1:
        errors.append("include exactly one 'Key changes:' section")
        return errors
    section = lines.index("Key changes:")
    if len(lines) < 2 or lines[1] != "":
        errors.append("leave one blank line between subject and summary")
    summary = lines[2:section]
    if not summary or not any(summary):
        errors.append("include a summary paragraph before 'Key changes:'")
    elif summary[-1] != "" or not all(summary[:-1]):
        errors.append("use one summary paragraph, then one blank line before 'Key changes:'")

    bullets = lines[section + 1 :]
    count = 0
    for line in bullets:
        if not re.match(r"^[-*] \S", line):
            break
        count += 1
    if not count:
        errors.append("'Key changes:' must be followed immediately by nonempty bullets")
    remainder = bullets[count:]
    if any(re.match(r"^[-*] ", line) for line in remainder):
        errors.append("keep 'Key changes:' bullets contiguous")
    if remainder and remainder[0] != "":
        errors.append("leave a blank line between bullets and any footer")

    breaking = (match is not None and match.group(2)) or any(
        BREAKING.match(line) for line in lines[1:]
    )
    if breaking:
        migration = next(
            (
                line.removeprefix("Migration:").strip()
                for line in lines
                if line.startswith("Migration:")
            ),
            "",
        )
        if not migration:
            errors.append("breaking changes require 'Migration: <upgrade instructions>'")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python scripts/check_commit_msg.py MESSAGE_FILE", file=sys.stderr)
        return 2
    try:
        message = Path(argv[1]).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        print("Cannot read the UTF-8 message file.", file=sys.stderr)
        return 2
    errors = validate_commit_message(message)
    if errors:
        # Do not echo arbitrary message contents into CI logs.
        print("Message failed validation:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
