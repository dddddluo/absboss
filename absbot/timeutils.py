from __future__ import annotations

from datetime import datetime, timezone

UTC = timezone.utc


def utc_now() -> datetime:
    return datetime.now(UTC)


def from_millis(value: int | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def ensure_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def max_datetime(*values: datetime | None) -> datetime | None:
    present = [ensure_utc(value) for value in values if value is not None]
    if not present:
        return None
    return max(present)


def format_dt(value: datetime | None) -> str:
    normalized = ensure_utc(value)
    if normalized is None:
        return "从未"
    return normalized.astimezone().strftime("%Y-%m-%d %H:%M:%S")
