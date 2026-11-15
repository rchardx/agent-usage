"""Atomic writes shared by quota and public pricing snapshots."""

import json
import os
import tempfile
from pathlib import Path


def write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
