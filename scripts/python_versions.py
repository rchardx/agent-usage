"""Resolve the workflow runtimes from the repository's Python declarations."""

import re
import tomllib
from pathlib import Path


def python_versions(root: Path) -> tuple[str, str]:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    minor = r"[1-9][0-9]*\.(?:0|[1-9][0-9]*)"
    floor = re.fullmatch(rf">=({minor})", project["requires-python"])
    if not floor:
        raise ValueError("expected project.requires-python in the form >=MAJOR.MINOR")
    minimum = floor[1]
    development = (root / ".python-version").read_text(encoding="utf-8").strip()
    if not re.fullmatch(minor, development):
        raise ValueError("expected .python-version in the form MAJOR.MINOR")
    if tuple(map(int, development.split("."))) < tuple(map(int, minimum.split("."))):
        raise ValueError(".python-version is below the project's Python floor")
    return minimum, development


if __name__ == "__main__":
    minimum, development = python_versions(Path.cwd())
    print(f"minimum={minimum}")
    print(f"development={development}")
