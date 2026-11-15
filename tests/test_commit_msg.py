import runpy
import subprocess
import sys
from pathlib import Path

import pytest

CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "check_commit_msg.py"
validate = runpy.run_path(str(CHECKER))["validate_commit_message"]
VALID = (
    "feat(pricing): explain unpriced records\n\n"
    "Identify missing prices without changing cost calculations.\n\n"
    "Key changes:\n"
    "- Group unknown costs by model.\n"
    "- Preserve reason counts.\n"
)


def test_valid_message_and_git_comments():
    assert validate(VALID) == []
    assert validate(VALID.replace("\n", "\r\n")) == []
    assert validate(VALID + "\n# ignored comment\n") == []
    assert validate(VALID + "# ------------------------ >8 ------------------------\nignored") == []


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("", "empty"),
        ("# only a comment", "empty"),
        (VALID.replace("feat(pricing)", "style"), "types:"),
        (VALID.replace("feat(pricing)", "feat(Bad Scope)"), "types:"),
        (VALID.replace("explain unpriced records", "x" * 72), "under 72"),
        (VALID.replace("explain unpriced records", "解释费用"), "English"),
        (VALID.replace("records\n\n", "records\n"), "blank line"),
        (VALID.replace("Key changes:", "Changes:"), "exactly one"),
        (VALID + "\nKey changes:\n- Another section.\n", "exactly one"),
        (VALID.replace("Key changes:\n-", "Key changes:\n\n-"), "immediately"),
        (VALID.replace("- Preserve", "\n- Preserve"), "contiguous"),
        (VALID.replace("- Group unknown costs by model.", "- "), "immediately"),
        (
            VALID.replace("- Group unknown costs by model.", "- Describe a concrete change."),
            "placeholders",
        ),
        (VALID.replace("feat(pricing):", "feat(pricing)!:"), "Migration:"),
        (VALID + "\nBREAKING CHANGE: New report schema.\n", "Migration:"),
        (VALID + "\nBREAKING CHANGE: New report schema.\nMigration:\n", "Migration:"),
        (VALID + "Testing: pytest\n", "footer"),
    ],
)
def test_invalid_messages(message, error):
    assert any(error in item for item in validate(message))


def test_summary_must_be_a_single_paragraph():
    message = VALID.replace(
        "Identify missing prices", "First paragraph.\n\nIdentify missing prices"
    )
    assert any("one summary paragraph" in error for error in validate(message))


def test_breaking_change_documents_upgrade():
    message = VALID.replace("feat(pricing):", "feat(pricing)!:")
    message += "\nMigration: Update consumers to the new JSON schema.\n"
    assert validate(message) == []


def test_optional_validation_footer_and_technical_unicode():
    message = VALID.replace("cost calculations", "input × rate calculations")
    assert validate(message + "\nTesting: uv run --locked pytest -q\n") == []


def test_cli_exit_codes_and_sanitized_errors(tmp_path):
    def run(*args):
        return subprocess.run(
            [sys.executable, str(CHECKER), *map(str, args)],
            capture_output=True,
            text=True,
            check=False,
        )

    message = tmp_path / "message"
    message.write_text(VALID, encoding="utf-8")
    assert run(message).returncode == 0
    message.write_text("PRIVATE-MESSAGE-SENTINEL", encoding="utf-8")
    failed = run(message)
    assert failed.returncode == 1
    assert "PRIVATE-MESSAGE-SENTINEL" not in failed.stderr
    assert run().returncode == 2
    assert run(tmp_path / "missing").returncode == 2
    message.write_bytes(b"\xff")
    assert run(message).returncode == 2
