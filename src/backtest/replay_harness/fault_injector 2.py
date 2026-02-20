"""
FaultInjector — Scripted fault injection for replay harness.

Injects OperationalError, rate limits, and malformed data at
configurable time windows to test circuit breaker, exception hierarchy,
and kill switch behavior.

Usage:
    injector = FaultInjector([
        FaultSpec(
            start=datetime(2025, 1, 1, 2, 0, tzinfo=timezone.utc),
            end=datetime(2025, 1, 1, 2, 2, tzinfo=timezone.utc),
            fault_type="timeout",
            affected_methods=["place_futures_order", "get_all_futures_positions"],
        ),
        FaultSpec(
            start=datetime(2025, 1, 1, 3, 0, tzinfo=timezone.utc),
            end=datetime(2025, 1, 1, 3, 0, 30, tzinfo=timezone.utc),
            fault_type="rate_limit",
        ),
    ])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional, Set

from src.exceptions import OperationalError, DataError, RateLimitError


@dataclass
class FaultSpec:
    """Specification for a fault injection window."""
    start: datetime
    end: datetime
    fault_type: str  # "timeout", "rate_limit", "data_error", "attribute_error"
    affected_methods: Optional[List[str]] = None  # None = all methods
    message: str = ""
    probability: float = 1.0  # 1.0 = always trigger

    def __post_init__(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("FaultSpec times must be timezone-aware")
        if not self.message:
            self.message = f"Injected {self.fault_type}"


class FaultInjector:
    """Injects faults into API calls during replay.

    Check before each API call via maybe_inject().
    If a fault window is active, raises the appropriate exception.
    """

    def __init__(self, specs: Optional[List[FaultSpec]] = None):
        self._specs = specs or []
        self._specs.sort(key=lambda s: s.start)

        # Tracking
        self._injections_total: int = 0
        self._injections_by_type: dict = {}
        self._injections_log: List[dict] = []

    def add(self, spec: FaultSpec) -> None:
        """Add a fault spec."""
        self._specs.append(spec)
        self._specs.sort(key=lambda s: s.start)

    def maybe_inject(self, method_name: str, now: datetime) -> None:
        """Check if a fault should be injected. Raises if so."""
        for spec in self._specs:
            if now < spec.start:
                break  # sorted, no more active
            if now > spec.end:
                continue

            # Within window
            if spec.affected_methods and method_name not in spec.affected_methods:
                continue

            # Probability check
            if spec.probability < 1.0:
                import random
                if random.random() > spec.probability:
                    continue

            # Inject!
            self._injections_total += 1
            self._injections_by_type[spec.fault_type] = self._injections_by_type.get(spec.fault_type, 0) + 1
            self._injections_log.append({
                "time": now.isoformat(),
                "method": method_name,
                "fault_type": spec.fault_type,
            })

            if spec.fault_type == "timeout":
                raise OperationalError(f"[INJECTED] Timeout: {spec.message}")
            elif spec.fault_type == "rate_limit":
                raise RateLimitError(f"[INJECTED] 429 Too Many Requests: {spec.message}")
            elif spec.fault_type == "data_error":
                raise DataError(f"[INJECTED] Malformed response: {spec.message}")
            elif spec.fault_type == "attribute_error":
                raise AttributeError(f"[INJECTED] Bug simulation: {spec.message}")
            else:
                raise OperationalError(f"[INJECTED] {spec.fault_type}: {spec.message}")

    @property
    def stats(self) -> dict:
        return {
            "total_injections": self._injections_total,
            "by_type": dict(self._injections_by_type),
            "specs_count": len(self._specs),
        }

    @property
    def injection_log(self) -> List[dict]:
        return list(self._injections_log)
