import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_path


@dataclass(frozen=True)
class Paths:
    codex: Path
    claude: Path
    cache: Path

    @classmethod
    def discover(
        cls, codex: Path | None = None, claude: Path | None = None, cache: Path | None = None
    ) -> "Paths":
        return cls(
            (codex or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve(),
            (claude or Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")))
            .expanduser()
            .resolve(),
            (cache or user_cache_path("agent-usage")).expanduser().resolve(),
        )

    def files(self, provider: str) -> list[Path]:
        roots = (
            [self.codex / "sessions", self.codex / "archived_sessions"]
            if provider == "codex"
            else [self.claude / "projects"]
        )
        return sorted({p.resolve() for root in roots for p in root.rglob("*.jsonl") if p.is_file()})
