"""Time helpers.

ProofLoop stores *naive UTC* datetimes in SQLite (SQLite has no time-zone type)
and always renders them with an explicit ``Z`` suffix at the API boundary.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current time as a naive UTC datetime (microsecond precision)."""
    return datetime.now(UTC).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt.isoformat(timespec="milliseconds") + "Z"


def utc_iso_now() -> str:
    return iso(utcnow())  # type: ignore[return-value]


class Stopwatch:
    """Tiny monotonic stopwatch returning elapsed milliseconds."""

    def __init__(self) -> None:
        self._start = time.perf_counter()

    @property
    def ms(self) -> int:
        return int(round((time.perf_counter() - self._start) * 1000))
