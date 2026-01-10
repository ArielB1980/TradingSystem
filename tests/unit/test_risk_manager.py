"""
Test: Risk Manager position sizing and validation.
"""
import pytest
from decimal import Decimal
from src.risk.risk_manager import RiskManager
from src.domain.models import Signal, SignalType
from src.config.config import RiskConfig
from datetime import datetime, timezone


def test_position_sizing_formula():
    """Test correct position sizing (leverage-independent)."""
    config = RiskConfig()
    risk_manager = RiskManager(config)
    
    # Create test signal
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="BTC/USD",
        signal_type=SignalType.LONG,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49000"),  # 2% stop distance
        take_profit=Decimal("52000"),
        reasoning="Test signal",
        higher_tf_bias="bullish",
        adx=Decimal("30"),
        atr=Decimal("500"),
        ema200_slope="up",
    )
    
    account_equity = Decimal("10000")
    spot_price = Decimal("50000")
    perp_price = Decimal("50000")
    
    decision = risk_manager.validate_trade(signal, account_equity, spot_price, perp_price)
    
    # Expected: (10000 * 0.005) / 0.02 = 50 / 0.02 = 2500 notional
    assert decision.position_notional == Decimal("2500")
    assert decision.approved is True


def test_leverage_cap_enforcement():
    """Test that leverage never exceeds 10×."""
    config = RiskConfig(max_leverage=10.0)
    risk_manager = RiskManager(config)
    
    # Very tight stop would cause high leverage
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="BTC/USD",
        signal_type=SignalType.LONG,
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49950"),  # 0.1% stop (would need 50× leverage)
        take_profit=None,
        reasoning="Test",
        higher_tf_bias="bullish",
        adx=Decimal("30"),
        atr=Decimal("500"),
        ema200_slope="up",
    )
    
    decision = risk_manager.validate_trade(
        signal,
        Decimal("10000"),
        Decimal("50000"),
        Decimal("50000"),
    )
    
    # Should be capped at 10×
    assert decision.leverage <= Decimal("10")
