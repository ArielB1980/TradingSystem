"""
Unit tests for OHLCV fetcher: cooldown disables repeated failures.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock

from src.data.ohlcv_fetcher import OHLCVFetcher, _is_retryable, _is_symbol_not_found


@pytest.mark.asyncio
async def test_ohlcv_cooldown_after_k_failures():
    """After K consecutive failures, fetch returns [] during cooldown."""
    client = MagicMock()
    client.get_spot_ohlcv = AsyncMock(side_effect=Exception("Timeout"))
    config = MagicMock()
    config.data = MagicMock()
    config.data.ohlcv_max_retries = 1
    config.data.ohlcv_failure_disable_after = 2
    config.data.ohlcv_symbol_cooldown_minutes = 60
    config.data.max_concurrent_ohlcv = 8
    config.data.ohlcv_min_delay_ms = 0

    fetcher = OHLCVFetcher(client, config)
    # First call: fails, records failure
    with pytest.raises(Exception):
        await fetcher.fetch_spot_ohlcv("XBT/USD", "15m", None, 300)
    # Second call: fails again, should hit disable_after and enter cooldown
    with pytest.raises(Exception):
        await fetcher.fetch_spot_ohlcv("XBT/USD", "15m", None, 300)
    # Third call: in cooldown, returns [] without calling client
    out = await fetcher.fetch_spot_ohlcv("XBT/USD", "15m", None, 300)
    assert out == []
    assert client.get_spot_ohlcv.call_count == 2  # two attempts, third skipped


def test_is_retryable_classifies_rate_limit():
    assert _is_retryable(Exception("Too many requests")) is True
    assert _is_retryable(Exception("rate limit")) is True
    assert _is_retryable(Exception("429")) is True


def test_is_symbol_not_found_classifies_bad_symbol():
    assert _is_symbol_not_found(Exception("kraken does not have market symbol X")) is True
    assert _is_symbol_not_found(Exception("Bad symbol")) is True
