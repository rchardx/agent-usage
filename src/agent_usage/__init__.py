"""Codex and Claude Code usage reporting."""

from importlib.metadata import version

__version__ = version("agent-usage")
USER_AGENT = f"agent-usage/{__version__}"
