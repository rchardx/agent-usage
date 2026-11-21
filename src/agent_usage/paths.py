import os
import re
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_cache_path

# pi/omp name session transcripts "2026-11-15T05-26-36-485Z_<id>.jsonl"; sidecar
# directories next to them hold files like changes.jsonl that must not be indexed.
SESSION_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}.*\.jsonl$")
DEFAULT_ANTIGRAVITY_ROOTS = (
    "~/.gemini/antigravity",
    "~/.gemini/antigravity-cli",
    "~/.gemini/antigravity-ide",
)


@dataclass(frozen=True)
class Paths:
    codex: Path
    claude: Path
    pi: Path
    omp: Path
    cache: Path
    antigravity: Path
    antigravity_roots: tuple[Path, ...]

    @classmethod
    def discover(
        cls,
        codex: Path | None = None,
        claude: Path | None = None,
        pi: Path | None = None,
        omp: Path | None = None,
        cache: Path | None = None,
        antigravity: Path | None = None,
    ) -> "Paths":
        if antigravity is not None:
            agy_primary = antigravity.expanduser().resolve()
            agy_roots: tuple[Path, ...] = (agy_primary,)
        else:
            agy_primary = Path("~/.gemini/antigravity-cli").expanduser().resolve()
            agy_roots = tuple(
                dict.fromkeys(Path(p).expanduser().resolve() for p in DEFAULT_ANTIGRAVITY_ROOTS)
            )
        return cls(
            (codex or Path(os.environ.get("CODEX_HOME", "~/.codex"))).expanduser().resolve(),
            (claude or Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")))
            .expanduser()
            .resolve(),
            (pi or Path(os.environ.get("PI_CODING_AGENT_DIR", "~/.pi/agent")))
            .expanduser()
            .resolve(),
            # omp and Antigravity have no documented dir env vars; use --*-home if relocated.
            (omp or Path("~/.omp/agent")).expanduser().resolve(),
            (cache or user_cache_path("agent-usage")).expanduser().resolve(),
            agy_primary,
            agy_roots,
        )

    def root_exists(self, provider: str) -> bool:
        if provider == "antigravity":
            return any(root.is_dir() for root in self.antigravity_roots)
        return getattr(self, provider).is_dir()

    def files(self, provider: str) -> list[Path]:
        if provider == "antigravity":
            return sorted(
                {
                    p.resolve()
                    for root in self.antigravity_roots
                    if (root / "conversations").is_dir()
                    for p in (root / "conversations").rglob("*.db")
                    if p.is_file()
                }
            )
        if provider == "codex":
            roots = [self.codex / "sessions", self.codex / "archived_sessions"]
            session_named = False
        elif provider == "claude":
            roots = [self.claude / "projects"]
            session_named = False
        else:
            roots = [getattr(self, provider) / "sessions"]
            session_named = True
        return sorted(
            {
                p.resolve()
                for root in roots
                for p in root.rglob("*.jsonl")
                if p.is_file() and (not session_named or SESSION_FILE.match(p.name))
            }
        )
