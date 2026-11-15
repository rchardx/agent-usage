"""The bundled, offline JSON Schema for public report envelopes."""

import json
from importlib.resources import files


def load_schema() -> dict:
    resource = files("agent_usage").joinpath("schemas/report-v1.schema.json")
    return json.loads(resource.read_text(encoding="utf-8"))
