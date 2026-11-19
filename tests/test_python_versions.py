import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "python_versions.py"


def resolve(root, requirement, development):
    (root / "pyproject.toml").write_text(
        f'[project]\nrequires-python = "{requirement}"\n', encoding="utf-8"
    )
    (root / ".python-version").write_text(development + "\n", encoding="utf-8")
    return subprocess.run([sys.executable, SCRIPT], cwd=root, capture_output=True, text=True)


def test_emits_versions_from_declarations(tmp_path):
    result = resolve(tmp_path, ">=3.9", "3.15")
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["minimum=3.9", "development=3.15"]


@pytest.mark.parametrize(
    ("requirement", "development"),
    [(">=3.9,<3.14", "3.13"), (">=3.9", "latest"), (">=3.10", "3.9")],
)
def test_unsupported_declarations_fail_without_outputs(tmp_path, requirement, development):
    result = resolve(tmp_path, requirement, development)
    assert result.returncode != 0
    assert not result.stdout
