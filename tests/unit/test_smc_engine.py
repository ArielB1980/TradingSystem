"""
Test: SMC Engine deterministic behavior.
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from src.domain.models import Candle
from src.strategy.smc_engine import SMCEngine
from src.config.config import StrategyConfig


def test_smc_engine_deterministic():
    """Test that same input produces same output."""
    # Create test candles
    candles = []
    base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)
    
    for i in range(100):
        candles.append(
            Candle(
                timestamp=base_time + timedelta(hours=i),
                symbol="BTC/USD",
                timeframe="1h",
                open=Decimal("50000"),
                high=Decimal("50500"),
                low=Decimal("49500"),
                close=Decimal("50000"),
                volume=Decimal("100"),
            )
        )
    
    config = StrategyConfig()
    engine = SMCEngine(config)
    
    # Generate signal twice with same inputs
    signal1 = engine.generate_signal("BTC/USD", candles[:50], candles[:10], candles[:50], candles)
    signal2 = engine.generate_signal("BTC/USD", candles[:50], candles[:10], candles[:50], candles)
    
    # Should be identical
    assert signal1.signal_type == signal2.signal_type
    assert signal1.entry_price == signal2.entry_price
    assert signal1.reasoning == signal2.reasoning
