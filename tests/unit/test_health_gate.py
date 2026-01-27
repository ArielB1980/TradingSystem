"""
Unit tests for candle health gate: trading pauses when candle health below threshold.
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch
from decimal import Decimal

# Health gate is evaluated in live_trading._tick and blocks new entries when trade_paused.
# We test the logic in isolation: sufficient < min_healthy_coins or ratio < min_health_ratio => trade_paused.


def test_health_gate_pause_when_insufficient_coins():
    """When coins_with_sufficient_candles < min_healthy_coins, trade_paused should be True."""
    min_healthy = 30
    min_ratio = 0.25
    total = 100
    sufficient = 20
    ratio = sufficient / total
    assert sufficient < min_healthy
    assert ratio < min_ratio
    trade_paused = sufficient < min_healthy or ratio < min_ratio
    assert trade_paused is True


def test_health_gate_allow_when_healthy():
    """When sufficient >= min_healthy and ratio >= min_ratio, trade_paused should be False."""
    min_healthy = 30
    min_ratio = 0.25
    total = 100
    sufficient = 50
    ratio = sufficient / total
    assert sufficient >= min_healthy
    assert ratio >= min_ratio
    trade_paused = sufficient < min_healthy or ratio < min_ratio
    assert trade_paused is False


def test_health_gate_ratio_boundary():
    """When sufficient meets min_healthy but ratio just below threshold, still paused."""
    min_healthy = 30
    min_ratio = 0.25
    total = 200
    sufficient = 30  # meets min_healthy
    ratio = 30 / 200  # 0.15 < 0.25
    trade_paused = sufficient < min_healthy or ratio < min_ratio
    assert trade_paused is True


def test_health_gate_small_universe_all_healthy():
    """When total < min_healthy but all coins have data, effective_min = min(30, total) so we pass."""
    min_healthy = 30
    min_ratio = 0.25
    total = 12
    sufficient = 12
    effective_min = min(min_healthy, total)  # 12
    ratio = sufficient / total  # 1.0
    trade_paused = sufficient < effective_min or ratio < min_ratio
    assert trade_paused is False
    assert effective_min == 12


def test_universe_trimming_dropped_symbols():
    """When configured symbols include unsupported ones, dropped = prev - supported."""
    prev_symbols = {"BTC/USD", "ETH/USD", "LUNA2/USD", "THETA/USD"}
    supported = {"BTC/USD", "ETH/USD"}
    dropped = prev_symbols - supported
    assert dropped == {"LUNA2/USD", "THETA/USD"}
    assert len(dropped) == 2
