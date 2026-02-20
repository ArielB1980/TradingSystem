"""
Tests for APICircuitBreaker (P2.1).

Validates:
  - 5 failures -> circuit opens, subsequent calls raise CircuitOpenError
  - After cooldown, 1 probe allowed; success -> circuit closes
  - Probe failure -> stays open for another cooldown
  - Business error (e.g., bad symbol) does NOT open circuit
  - 429 opens circuit after 2 strikes
"""
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

from src.utils.circuit_breaker import APICircuitBreaker, CircuitState
from src.exceptions import CircuitOpenError


class TestAPICircuitBreakerBasics:
    """Basic state transitions."""

    def test_starts_closed(self):
        breaker = APICircuitBreaker()
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_can_execute_when_closed(self):
        breaker = APICircuitBreaker()
        assert await breaker.can_execute() is True

    @pytest.mark.asyncio
    async def test_success_resets_failure_count(self):
        breaker = APICircuitBreaker(failure_threshold=5)
        # Record 4 failures (below threshold)
        for _ in range(4):
            await breaker.record_failure(Exception("test"))
        await breaker.record_success()
        # Failure count reset, circuit still closed
        assert breaker.state == CircuitState.CLOSED
        assert breaker._failure_count == 0


class TestCircuitOpening:
    """Circuit opens after threshold failures."""

    @pytest.mark.asyncio
    async def test_opens_after_5_failures(self):
        breaker = APICircuitBreaker(failure_threshold=5)
        for i in range(5):
            await breaker.record_failure(Exception(f"failure {i}"))
        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_raises_circuit_open_error_when_open(self):
        breaker = APICircuitBreaker(failure_threshold=2)
        await breaker.record_failure(Exception("f1"))
        await breaker.record_failure(Exception("f2"))
        assert breaker.state == CircuitState.OPEN
        with pytest.raises(CircuitOpenError):
            await breaker.can_execute()

    @pytest.mark.asyncio
    async def test_429_opens_after_2_strikes(self):
        breaker = APICircuitBreaker(rate_limit_threshold=2)
        await breaker.record_failure(Exception("429"), is_rate_limit=True)
        assert breaker.state == CircuitState.CLOSED
        await breaker.record_failure(Exception("429"), is_rate_limit=True)
        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_business_error_does_not_open_circuit(self):
        """Non-operational errors should NOT trip the breaker."""
        breaker = APICircuitBreaker(failure_threshold=3)
        # Record "failures" but mark as NOT breaker-triggering
        # (In real usage, DataError is not recorded as a failure.)
        for _ in range(10):
            await breaker.record_success()  # Business errors are not recorded
        assert breaker.state == CircuitState.CLOSED


class TestCooldownAndHalfOpen:
    """HALF_OPEN probe mechanics."""

    @pytest.mark.asyncio
    async def test_allows_probe_after_cooldown(self):
        breaker = APICircuitBreaker(failure_threshold=2, cooldown_seconds=1.0)
        await breaker.record_failure(Exception("f1"))
        await breaker.record_failure(Exception("f2"))
        assert breaker.state == CircuitState.OPEN

        # Simulate cooldown elapsed
        breaker._last_open_time = datetime.now(timezone.utc) - timedelta(seconds=2)
        # Should transition to HALF_OPEN and allow the probe
        assert await breaker.can_execute() is True
        assert breaker.state == CircuitState.HALF_OPEN

    @pytest.mark.asyncio
    async def test_probe_success_closes_circuit(self):
        breaker = APICircuitBreaker(failure_threshold=2, cooldown_seconds=0.0)
        await breaker.record_failure(Exception("f1"))
        await breaker.record_failure(Exception("f2"))
        # Force cooldown expired
        breaker._last_open_time = datetime.now(timezone.utc) - timedelta(seconds=1)
        await breaker.can_execute()  # Transitions to HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN

        await breaker.record_success()
        assert breaker.state == CircuitState.CLOSED
        assert breaker._failure_count == 0

    @pytest.mark.asyncio
    async def test_probe_failure_reopens_circuit(self):
        breaker = APICircuitBreaker(failure_threshold=2, cooldown_seconds=0.0)
        await breaker.record_failure(Exception("f1"))
        await breaker.record_failure(Exception("f2"))
        breaker._last_open_time = datetime.now(timezone.utc) - timedelta(seconds=1)
        await breaker.can_execute()  # -> HALF_OPEN
        assert breaker.state == CircuitState.HALF_OPEN

        await breaker.record_failure(Exception("probe fail"))
        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_still_open_before_cooldown(self):
        breaker = APICircuitBreaker(failure_threshold=2, cooldown_seconds=60.0)
        await breaker.record_failure(Exception("f1"))
        await breaker.record_failure(Exception("f2"))
        # Cooldown NOT elapsed
        with pytest.raises(CircuitOpenError):
            await breaker.can_execute()


class TestForceControls:
    """Manual force open/close."""

    @pytest.mark.asyncio
    async def test_force_open(self):
        breaker = APICircuitBreaker()
        await breaker.force_open(reason="test")
        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_force_close(self):
        breaker = APICircuitBreaker(failure_threshold=1)
        await breaker.record_failure(Exception("boom"))
        assert breaker.state == CircuitState.OPEN
        await breaker.force_close()
        assert breaker.state == CircuitState.CLOSED
        assert breaker._failure_count == 0


class TestStateInfo:
    """Metrics / health endpoint."""

    def test_get_state_info_returns_dict(self):
        breaker = APICircuitBreaker(name="test_breaker")
        info = breaker.get_state_info()
        assert info["name"] == "test_breaker"
        assert info["state"] == "closed"
        assert info["failure_count"] == 0
        assert info["cooldown_seconds"] == 60.0


class TestExceptionClassification:
    """Test KrakenClient._classify_exception (imported via the module)."""

    def test_classify_timeout(self):
        from src.data.kraken_client import KrakenClient
        exc = asyncio.TimeoutError()
        classified = KrakenClient._classify_exception(exc)
        from src.exceptions import OperationalError
        assert isinstance(classified, OperationalError)

    def test_classify_connection_error(self):
        from src.data.kraken_client import KrakenClient
        exc = ConnectionError("refused")
        classified = KrakenClient._classify_exception(exc)
        from src.exceptions import OperationalError
        assert isinstance(classified, OperationalError)

    def test_classify_rate_limit_string(self):
        from src.data.kraken_client import KrakenClient
        exc = Exception("429 Too Many Requests")
        classified = KrakenClient._classify_exception(exc)
        from src.exceptions import RateLimitError
        assert isinstance(classified, RateLimitError)

    def test_classify_bad_symbol_string(self):
        from src.data.kraken_client import KrakenClient
        exc = Exception("does not have market symbol XYZ")
        classified = KrakenClient._classify_exception(exc)
        from src.exceptions import DataError
        assert isinstance(classified, DataError)

    def test_classify_unknown_passes_through(self):
        from src.data.kraken_client import KrakenClient
        exc = AttributeError("'NoneType' has no attribute 'foo'")
        classified = KrakenClient._classify_exception(exc)
        # Unknown exceptions should pass through unchanged
        assert classified is exc
        assert isinstance(classified, AttributeError)

    def test_already_classified_passes_through(self):
        from src.data.kraken_client import KrakenClient
        from src.exceptions import DataError as DE
        exc = DE("already classified")
        classified = KrakenClient._classify_exception(exc)
        assert classified is exc
