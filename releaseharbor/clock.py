"""Injectable clock and sleep helpers for deterministic runs."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    """Format a timezone-aware datetime as a stable UTC timestamp."""
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


@dataclass
class Clock:
    """Returns either a fixed timestamp or wall-clock UTC time."""

    mode: str = "fixed"
    fixed: str = "2026-01-15T12:00:00Z"
    sleeper: object = time.sleep

    @classmethod
    def from_settings(cls, mode: str, fixed: str) -> "Clock":
        return cls(mode=mode, fixed=fixed, sleeper=time.sleep)

    def now(self) -> str:
        if self.mode == "fixed":
            return self.fixed
        return format_utc(_utc_now())

    def compact(self) -> str:
        stamp = self.now()
        return stamp.replace("-", "").replace(":", "").replace("Z", "Z")

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            self.sleeper(seconds)
