"""
Resilient OHLCV fetch with retry, backoff, per-symbol cooldown, and rate limiting.

Used by CandleManager when provided; wraps client.get_spot_ohlcv with:
- Retry with exponential backoff on transient/rate-limit errors
- Per-symbol cooldown after K consecutive failures (skip fetch during cooldown)
- Global concurrency cap and min delay between requests
"""
import asyncio
import time
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Any, Dict
from decimal import Decimal

from src.monitoring.logger import get_logger
from src.domain.models import Candle

logger = get_logger(__name__)


def _is_retryable(err: Exception) -> bool:
    """Classify as retryable: rate limit, timeout, transient network."""
    msg = (str(err) or "").lower()
    if "rate" in msg or "too many" in msg or "429" in msg:
        return True
    if "timeout" in msg or "timed out" in msg:
        return True
    if "connection" in msg or "network" in msg:
        return True
    return False


def _is_symbol_not_found(err: Exception) -> bool:
    msg = (str(err) or "").lower()
    return "does not have market" in msg or "bad symbol" in msg or "invalid symbol" in msg


class OHLCVFetcher:
    """
    Wraps spot OHLCV fetch with retry, per-symbol cooldown, and rate limiting.
    """

    def __init__(self, client: Any, config: Any):
        self.client = client
        self.config = config
        data = getattr(config, "data", None) or config
        self.max_retries = getattr(data, "ohlcv_max_retries", 3)
        self.failure_disable_after = getattr(data, "ohlcv_failure_disable_after", 3)
        self.cooldown_minutes = getattr(data, "ohlcv_symbol_cooldown_minutes", 60)
        self.max_concurrent = getattr(data, "max_concurrent_ohlcv", 8)
        self.min_delay_ms = getattr(data, "ohlcv_min_delay_ms", 200)
        self._sem = asyncio.Semaphore(self.max_concurrent)
        self._last_fetch_time: float = 0
        self._failure_count: Dict[str, int] = {}
        self._cooldown_until: Dict[str, datetime] = {}
        self._cooldown_logged: Dict[str, datetime] = {}

    def _in_cooldown(self, symbol: str) -> bool:
        until = self._cooldown_until.get(symbol)
        if not until:
            return False
        if datetime.now(timezone.utc) < until:
            return True
        self._cooldown_until.pop(symbol, None)
        self._failure_count[symbol] = 0
        return False

    def _record_failure(self, symbol: str) -> None:
        self._failure_count[symbol] = self._failure_count.get(symbol, 0) + 1
        if self._failure_count[symbol] >= self.failure_disable_after:
            until = datetime.now(timezone.utc) + timedelta(minutes=self.cooldown_minutes)
            self._cooldown_until[symbol] = until
            logger.warning(
                "OHLCV cooldown started",
                symbol=symbol,
                failures=self._failure_count[symbol],
                cooldown_minutes=self.cooldown_minutes,
            )

    def _record_success(self, symbol: str) -> None:
        self._failure_count[symbol] = 0

    async def fetch_spot_ohlcv(
        self, symbol: str, timeframe: str, since_ms: Optional[int], limit: int = 300
    ) -> List[Any]:
        """
        Fetch spot OHLCV with retry, cooldown, and rate limit.
        Returns same type as client.get_spot_ohlcv (List[Candle] or []).
        """
        if self._in_cooldown(symbol):
            # Log at most once per cooldown window
            until = self._cooldown_until.get(symbol)
            last_log = self._cooldown_logged.get(symbol)
            if until and (not last_log or last_log < until - timedelta(minutes=self.cooldown_minutes - 1)):
                logger.debug("OHLCV skip (cooldown)", symbol=symbol, timeframe=timeframe)
                self._cooldown_logged[symbol] = datetime.now(timezone.utc)
            return []

        async with self._sem:
            now = time.monotonic()
            elapsed_ms = (now - self._last_fetch_time) * 1000
            if elapsed_ms < self.min_delay_ms:
                await asyncio.sleep((self.min_delay_ms - elapsed_ms) / 1000.0)
            self._last_fetch_time = time.monotonic()

            last_err: Optional[Exception] = None
            for attempt in range(self.max_retries):
                try:
                    out = await self.client.get_spot_ohlcv(
                        symbol, timeframe, since=since_ms, limit=limit
                    )
                    self._record_success(symbol)
                    return out if out else []
                except Exception as e:
                    last_err = e
                    if _is_symbol_not_found(e):
                        self._record_failure(symbol)
                        logger.debug("OHLCV symbol not found", symbol=symbol, error=str(e))
                        return []
                    if _is_retryable(e) and attempt < self.max_retries - 1:
                        delay = 2 ** attempt
                        await asyncio.sleep(delay)
                        continue
                    self._record_failure(symbol)
                    raise
            if last_err:
                self._record_failure(symbol)
                raise last_err
            return []
