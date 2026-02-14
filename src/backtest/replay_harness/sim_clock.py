"""
SimClock — Deterministic time control for replay.

Replaces datetime.now(), time.time(), and asyncio.sleep() with
a simulated clock that advances only when explicitly told to.

Usage:
    clock = SimClock(start=datetime(2025, 1, 1, tzinfo=timezone.utc))
    clock.advance(seconds=60)   # advance 1 minute
    clock.now()                 # 2025-01-01T00:01:00+00:00
    clock.time()                # unix timestamp
    clock.sleep(10)             # no-op (records but doesn't wait)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional, Callable


class SimClock:
    """Deterministic simulated clock.

    Thread-safe within a single asyncio loop (no threading).
    All time queries return simulated time.
    All sleeps return immediately (or call a step callback).
    """

    def __init__(self, start: datetime, *, step_callback: Optional[Callable[["SimClock", float], None]] = None):
        """
        Args:
            start: Initial simulated time (must be timezone-aware).
            step_callback: Optional callback(clock, requested_seconds) called on each sleep.
                          Useful for the runner to advance the clock on sleep.
        """
        if start.tzinfo is None:
            raise ValueError("SimClock start must be timezone-aware")
        self._current: datetime = start
        self._start: datetime = start
        self._step_callback = step_callback
        self._total_sleeps: int = 0
        self._total_sleep_seconds: float = 0.0

    # -- Time queries --

    def now(self) -> datetime:
        """Return current simulated UTC time."""
        return self._current

    def time(self) -> float:
        """Return current simulated time as Unix timestamp."""
        return self._current.timestamp()

    def perf_counter(self) -> float:
        """Return elapsed seconds since clock start (for latency measurement)."""
        return (self._current - self._start).total_seconds()

    # -- Time advancement --

    def advance(self, *, seconds: float = 0, minutes: float = 0, to: Optional[datetime] = None) -> None:
        """Advance simulated time.

        Args:
            seconds: Seconds to advance.
            minutes: Minutes to advance.
            to: Advance to a specific time (must be >= current).
        """
        if to is not None:
            if to < self._current:
                raise ValueError(f"Cannot advance backwards: {to} < {self._current}")
            self._current = to
        else:
            delta = timedelta(seconds=seconds, minutes=minutes)
            if delta < timedelta(0):
                raise ValueError("Cannot advance by negative delta")
            self._current += delta

    def set(self, t: datetime) -> None:
        """Set simulated time to a specific value (for episode jumps)."""
        if t.tzinfo is None:
            raise ValueError("Time must be timezone-aware")
        self._current = t

    # -- Sleep replacement --

    async def sleep(self, seconds: float) -> None:
        """Replacement for asyncio.sleep(). Returns immediately.

        If a step_callback is set, it's called (useful for auto-advancing the clock).
        """
        self._total_sleeps += 1
        self._total_sleep_seconds += seconds
        if self._step_callback:
            self._step_callback(self, seconds)
        # Yield control to event loop (0-second sleep) to allow task switching
        await asyncio.sleep(0)

    # -- Context manager for monkey-patching --

    def patch_datetime_now(self) -> Callable:
        """Return a replacement for datetime.now() that uses simulated time."""
        clock = self

        def _now(tz=None):
            if tz is None:
                return clock._current.replace(tzinfo=None)
            return clock._current.astimezone(tz)

        return _now

    # -- Stats --

    @property
    def elapsed(self) -> timedelta:
        """Total simulated time elapsed since start."""
        return self._current - self._start

    @property
    def stats(self) -> dict:
        return {
            "start": self._start.isoformat(),
            "current": self._current.isoformat(),
            "elapsed_seconds": self.elapsed.total_seconds(),
            "total_sleeps": self._total_sleeps,
            "total_sleep_seconds": self._total_sleep_seconds,
        }

    def __repr__(self) -> str:
        return f"SimClock(now={self._current.isoformat()}, elapsed={self.elapsed})"
