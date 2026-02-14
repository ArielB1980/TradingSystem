"""
Circuit breaker pattern for resilient error handling.

Two breaker flavours:
  1. CircuitBreaker / CircuitBreakerManager — per-coin health tracking (existing)
  2. APICircuitBreaker — global API-level breaker that guards all outbound
     KrakenClient calls.  Opens on consecutive 5xx / timeout / 429 errors,
     fails-fast with CircuitOpenError (OperationalError subclass) while open,
     allows a single probe after cooldown.

See P2.1 in the hardening plan for design rationale.
"""
from datetime import datetime, timezone, timedelta
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar
from enum import Enum
from dataclasses import dataclass, field
import asyncio
import functools
from src.monitoring.logger import get_logger
from src.exceptions import CircuitOpenError

logger = get_logger(__name__)

T = TypeVar("T")


class CircuitState(Enum):
    """Circuit breaker states."""
    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, skip processing
    HALF_OPEN = "half_open"  # Testing if recovered


@dataclass
class CircuitBreaker:
    """
    Circuit breaker for tracking coin health.
    
    Prevents processing coins that are consistently failing.
    """
    failure_threshold: int = 5  # Open circuit after N consecutive failures
    success_threshold: int = 2  # Close circuit after N consecutive successes
    timeout_seconds: int = 300  # Time before attempting half-open (5 minutes)
    
    failure_count: int = 0
    success_count: int = 0
    state: CircuitState = CircuitState.CLOSED
    last_failure_time: Optional[datetime] = None
    last_success_time: Optional[datetime] = None
    
    def record_success(self):
        """Record a successful operation."""
        self.last_success_time = datetime.now(timezone.utc)
        
        if self.state == CircuitState.HALF_OPEN:
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                logger.info("Circuit breaker closed - coin recovered", 
                          failures=self.failure_count)
                self.state = CircuitState.CLOSED
                self.failure_count = 0
                self.success_count = 0
        elif self.state == CircuitState.CLOSED:
            # Reset failure count on success
            self.failure_count = 0
    
    def record_failure(self):
        """Record a failed operation."""
        self.last_failure_time = datetime.now(timezone.utc)
        self.failure_count += 1
        self.success_count = 0
        
        if self.state == CircuitState.CLOSED:
            if self.failure_count >= self.failure_threshold:
                logger.warning(
                    "Circuit breaker opened - coin failing repeatedly",
                    failures=self.failure_count,
                    threshold=self.failure_threshold
                )
                self.state = CircuitState.OPEN
        elif self.state == CircuitState.HALF_OPEN:
            # Failed during test - reopen
            logger.warning("Circuit breaker reopened - recovery failed")
            self.state = CircuitState.OPEN
            self.failure_count = self.failure_threshold
    
    def can_process(self) -> bool:
        """Check if coin can be processed."""
        if self.state == CircuitState.CLOSED:
            return True
        
        if self.state == CircuitState.OPEN:
            # Check if timeout has passed
            if self.last_failure_time:
                elapsed = (datetime.now(timezone.utc) - self.last_failure_time).total_seconds()
                if elapsed >= self.timeout_seconds:
                    logger.info("Circuit breaker half-open - testing recovery")
                    self.state = CircuitState.HALF_OPEN
                    self.success_count = 0
                    return True
            return False
        
        # HALF_OPEN - allow processing to test recovery
        return True
    
    def get_state_info(self) -> Dict:
        """Get current state information."""
        return {
            "state": self.state.value,
            "failure_count": self.failure_count,
            "success_count": self.success_count,
            "last_failure": self.last_failure_time.isoformat() if self.last_failure_time else None,
            "last_success": self.last_success_time.isoformat() if self.last_success_time else None,
        }


class CircuitBreakerManager:
    """Manages circuit breakers for multiple coins."""
    
    def __init__(self, failure_threshold: int = 5, timeout_seconds: int = 300):
        self.breakers: Dict[str, CircuitBreaker] = {}
        self.failure_threshold = failure_threshold
        self.timeout_seconds = timeout_seconds
    
    def get_breaker(self, symbol: str) -> CircuitBreaker:
        """Get or create circuit breaker for a symbol."""
        if symbol not in self.breakers:
            self.breakers[symbol] = CircuitBreaker(
                failure_threshold=self.failure_threshold,
                timeout_seconds=self.timeout_seconds
            )
        return self.breakers[symbol]
    
    def can_process(self, symbol: str) -> bool:
        """Check if symbol can be processed."""
        breaker = self.get_breaker(symbol)
        return breaker.can_process()
    
    def record_success(self, symbol: str):
        """Record successful processing for symbol."""
        breaker = self.get_breaker(symbol)
        breaker.record_success()
    
    def record_failure(self, symbol: str):
        """Record failed processing for symbol."""
        breaker = self.get_breaker(symbol)
        breaker.record_failure()
    
    def get_health_stats(self) -> Dict[str, Dict]:
        """Get health statistics for all tracked symbols."""
        return {
            symbol: breaker.get_state_info()
            for symbol, breaker in self.breakers.items()
        }
    
    def reset(self, symbol: str):
        """Reset circuit breaker for a symbol."""
        if symbol in self.breakers:
            del self.breakers[symbol]


# ---------------------------------------------------------------------------
# API-level circuit breaker (P2.1)
# ---------------------------------------------------------------------------

class APICircuitBreaker:
    """Global circuit breaker that guards all outbound KrakenClient calls.

    - CLOSED: normal operation; failures increment counter
    - OPEN: fail-fast with CircuitOpenError; no network calls
    - HALF_OPEN: allow exactly 1 probe; success closes, failure reopens

    Error classification:
      - 5xx, timeout, connection reset, DNS  -> breaker-triggering (5 strikes)
      - 429 rate-limit                       -> fast-trigger (2 strikes)
      - Business errors (bad symbol, min size, insufficient margin) -> NOT triggering
      - Auth failure -> NOT breaker-triggering (separate alert path)
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        rate_limit_threshold: int = 2,
        cooldown_seconds: float = 60.0,
        name: str = "kraken_api",
    ):
        self.failure_threshold = failure_threshold
        self.rate_limit_threshold = rate_limit_threshold
        self.cooldown_seconds = cooldown_seconds
        self.name = name

        self._state: CircuitState = CircuitState.CLOSED
        self._failure_count: int = 0
        self._rate_limit_count: int = 0
        self._last_failure_time: Optional[datetime] = None
        self._last_open_time: Optional[datetime] = None
        self._probe_in_flight: bool = False
        self._lock = asyncio.Lock()

    # -- public interface ---------------------------------------------------

    @property
    def state(self) -> CircuitState:
        return self._state

    async def can_execute(self) -> bool:
        """Return True if a call is allowed.  Raises CircuitOpenError if not.

        Transitions OPEN -> HALF_OPEN if cooldown elapsed and allows the probe.
        """
        async with self._lock:
            if self._state == CircuitState.CLOSED:
                return True

            if self._state == CircuitState.OPEN:
                if self._cooldown_elapsed():
                    self._state = CircuitState.HALF_OPEN
                    self._probe_in_flight = True
                    logger.info(
                        "API circuit breaker half-open, allowing probe",
                        breaker=self.name,
                        failures=self._failure_count,
                    )
                    return True
                raise CircuitOpenError(
                    f"API circuit breaker '{self.name}' is OPEN — "
                    f"last failure {self._last_failure_time}, "
                    f"cooldown {self.cooldown_seconds}s"
                )

            # HALF_OPEN: allow probe
            return True

    async def record_success(self) -> None:
        """Record a successful API call."""
        async with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                logger.info(
                    "API circuit breaker closed — probe succeeded",
                    breaker=self.name,
                )
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._rate_limit_count = 0
                self._probe_in_flight = False
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0
                self._rate_limit_count = 0

    async def record_failure(self, exc: BaseException, *, is_rate_limit: bool = False) -> None:
        """Record a failed API call.  Classifies and possibly opens the breaker."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            self._last_failure_time = now

            if self._state == CircuitState.HALF_OPEN:
                logger.warning(
                    "API circuit breaker reopened — probe failed",
                    breaker=self.name,
                    error=str(exc)[:200],
                )
                self._state = CircuitState.OPEN
                self._last_open_time = now
                self._probe_in_flight = False
                return

            if self._state == CircuitState.CLOSED:
                if is_rate_limit:
                    self._rate_limit_count += 1
                    if self._rate_limit_count >= self.rate_limit_threshold:
                        self._open(now, reason="rate_limit")
                        return
                else:
                    self._failure_count += 1
                    if self._failure_count >= self.failure_threshold:
                        self._open(now, reason="consecutive_failures")
                        return

    async def force_open(self, reason: str = "manual") -> None:
        """Force the breaker open (e.g., from invariant monitor)."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            self._open(now, reason=reason)

    async def force_close(self) -> None:
        """Force the breaker closed (e.g., after manual recovery)."""
        async with self._lock:
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._rate_limit_count = 0
            self._probe_in_flight = False
            logger.info("API circuit breaker force-closed", breaker=self.name)

    def get_state_info(self) -> Dict:
        """Return breaker state for metrics / logging."""
        return {
            "name": self.name,
            "state": self._state.value,
            "failure_count": self._failure_count,
            "rate_limit_count": self._rate_limit_count,
            "last_failure": self._last_failure_time.isoformat() if self._last_failure_time else None,
            "last_open": self._last_open_time.isoformat() if self._last_open_time else None,
            "cooldown_seconds": self.cooldown_seconds,
        }

    # -- internal -----------------------------------------------------------

    def _open(self, now: datetime, *, reason: str) -> None:
        logger.warning(
            "API circuit breaker OPENED",
            breaker=self.name,
            reason=reason,
            failure_count=self._failure_count,
            rate_limit_count=self._rate_limit_count,
        )
        self._state = CircuitState.OPEN
        self._last_open_time = now

    def _cooldown_elapsed(self) -> bool:
        if self._last_open_time is None:
            return True
        elapsed = (datetime.now(timezone.utc) - self._last_open_time).total_seconds()
        return elapsed >= self.cooldown_seconds
