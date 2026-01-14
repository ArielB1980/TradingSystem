"""
Circuit breaker pattern for resilient error handling.

Prevents repeatedly trying to process coins that are consistently failing.
"""
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
from enum import Enum
from dataclasses import dataclass, field
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


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
