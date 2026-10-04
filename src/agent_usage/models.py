"""Public report types. Unknown values stay null, not zero."""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def iso_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value, UTC).isoformat()
    if isinstance(value, str):
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            raise ValueError("Timestamp requires a timezone")
        return dt.astimezone(UTC).isoformat()
    raise ValueError("Invalid timestamp")


@dataclass
class QuotaWindow:
    bucket: str
    window: str
    used_percent: float | None
    resets_at: str | None
    duration_minutes: int | None = None
    label: str | None = None


@dataclass
class CreditBalance:
    bucket: str
    balance: str | None = None
    unit: str = "provider_units_unverified"
    enabled: bool | None = None
    has_credits: bool | None = None
    unlimited: bool | None = None
    limit: str | None = None
    used: str | None = None
    expires_at: str | None = None


@dataclass
class ResetAllowance:
    available_count: int | None
    details: list[dict[str, Any]] | None = None


@dataclass
class AccountReport:
    provider: str
    source: str
    observed_at: str | None = None
    status: str = "ok"
    plan: str | None = None
    auth_mode: str | None = None
    windows: list[QuotaWindow] = field(default_factory=list)
    credits: list[CreditBalance] = field(default_factory=list)
    resets: ResetAllowance | None = None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UsageEvent:
    provider: str
    event_id: str
    session: str
    timestamp: str
    model: str
    project: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    reasoning_tokens: int | None
    cache_write_5m_tokens: int | None = None
    cache_write_1h_tokens: int | None = None
    request_input_tokens: int | None = None

    # input_tokens includes all cache components; reasoning is a subset of output.
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class ProbeError(Exception):
    """A safe, user-facing failure; never include credentials or raw response bodies."""
