"""
Domain protocols (interfaces) for dependency inversion.

These protocols define the contracts that infrastructure layers must implement,
allowing domain/strategy/risk code to depend on abstractions rather than
concrete storage implementations.
"""
from typing import Dict, Optional, Protocol, runtime_checkable
from datetime import datetime


@runtime_checkable
class EventRecorder(Protocol):
    """
    Protocol for recording system events (signals, risk decisions, etc.).

    Implemented by src.storage.repository.record_event in production.
    Can be replaced with a no-op or in-memory recorder in tests.
    """

    def __call__(
        self,
        event_type: str,
        symbol: str,
        details: Dict,
        decision_id: Optional[str] = None,
        timestamp: Optional[datetime] = None,
    ) -> None: ...


def _noop_event_recorder(
    event_type: str,
    symbol: str,
    details: Dict,
    decision_id: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> None:
    """No-op event recorder for use in tests or when persistence is unavailable."""
    pass
