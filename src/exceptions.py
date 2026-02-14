"""
Custom exception hierarchy for the trading system.

Provides clear, specific exceptions for different error scenarios
to improve error handling and debugging.

Hierarchy (P2.2 hardening):

    TradingSystemError (base)
    ├── OperationalError   — transient/retryable (exchange, network, timeouts)
    │   ├── APIError       — API-specific operational errors
    │   │   ├── AuthenticationError
    │   │   └── RateLimitError
    │   ├── CircuitOpenError  — circuit breaker is open, fail-fast
    │   └── DataAcquisitionError
    ├── DataError          — bad data, skip symbol, don't halt
    │   ├── ValidationError
    │   ├── PositionLimitError
    │   └── OrderExecutionError
    └── InvariantError     — safety violation, halt immediately

Rules:
    - OperationalError: catch and retry/backoff, continue loop
    - DataError: catch, log, skip this symbol/coin, continue loop
    - InvariantError: catch, trigger kill switch, break loop
    - Everything else (AttributeError, TypeError, etc.): let crash.
      systemd restarts in 30s. Silent corruption is worse than downtime.
"""


class TradingSystemError(Exception):
    """Base exception for all trading system errors."""
    pass


# ============ OPERATIONAL (transient, retryable) ============

class OperationalError(TradingSystemError):
    """Transient/retryable error: exchange API, network, timeouts.
    
    Treatment: catch, log, retry with backoff, continue to next cycle.
    """
    pass


class APIError(OperationalError):
    """API-specific operational error (exchange returned error)."""
    pass


class AuthenticationError(APIError):
    """Raised when API authentication fails.
    
    Note: Despite being under APIError, auth failures should typically
    trigger an InvariantError / halt in production. Kept here for
    backward compatibility; callers should escalate.
    """
    pass


class RateLimitError(APIError):
    """Raised when API rate limit is exceeded."""
    pass


class CircuitOpenError(OperationalError):
    """Circuit breaker is open — fail-fast without hitting the network.
    
    Treatment: log once per cycle, skip processing, don't count as
    API error for invariant monitor (the breaker IS the protection).
    """
    pass


class DataAcquisitionError(OperationalError):
    """Raised when data acquisition fails (network/API layer)."""
    pass


# ============ DATA (bad input, skip symbol) ============

class DataError(TradingSystemError):
    """Bad data: symbol, precision, stale candles, invalid instrument.
    
    Treatment: catch, log, skip this symbol, continue loop.
    """
    pass


class ValidationError(DataError):
    """Raised when validation checks fail (bad input data)."""
    pass


class PositionLimitError(DataError):
    """Raised when position limits would be exceeded."""
    pass


class OrderExecutionError(DataError):
    """Raised when order execution fails (business logic rejection).
    
    Examples: insufficient margin, min size not met, symbol not tradeable.
    """
    pass


# ============ INVARIANT (safety violation, halt) ============

class InvariantError(TradingSystemError):
    """Safety invariant violation. Halt immediately.
    
    Treatment: catch, trigger kill switch, break trading loop.
    This should never be caught and silently continued.
    """
    pass
