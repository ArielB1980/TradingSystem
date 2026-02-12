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
        setup_type="test_setup",
        regime="trending",
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
        setup_type="test_setup",
        regime="trending",
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


def test_utilisation_boost_in_auction_mode():
    """When auction mode, skip_margin_check, and utilisation below target, risk-sized notional is boosted (clamped to single/aggregate)."""
    config = RiskConfig(
        auction_mode_enabled=True,
        sizing_method="leverage_based",
        risk_per_trade_pct=0.03,
        target_margin_util_min=0.70,
        utilisation_boost_max_factor=2.0,
        max_single_position_margin_pct_equity=0.25,
        max_aggregate_margin_pct_equity=2.0,
        target_leverage=7.0,
    )
    rm = RiskManager(config)
    rm.current_positions = []
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="BTC/USD",
        signal_type=SignalType.LONG,
        setup_type="test",
        regime="tight_smc",
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"),
        reasoning="test",
        higher_tf_bias="bullish",
        adx=Decimal("30"),
        atr=Decimal("500"),
        ema200_slope="up",
    )
    # Small equity: risk sizing yields 1000*7*0.03 = 210 notional (~3% util). Below 70% so boost applies.
    equity = Decimal("1000")
    decision = rm.validate_trade(
        signal, equity, Decimal("50000"), Decimal("50000"),
        notional_override=None,
        skip_margin_check=True,
    )
    assert decision.approved
    # Risk sizing alone would give 210; boost should increase it (toward 70% util), capped by 2x (420) and single_margin (1750).
    assert decision.position_notional > Decimal("210"), "Boost should increase notional above risk-sized 210"
    assert decision.position_notional <= Decimal("1750"), "Should be capped by single-position margin (25% of 1000 * 7)"
    assert decision.utilisation_boost_applied is True, "Utilisation boost must be applied in this scenario"


def test_utilisation_boost_skipped_when_not_leverage_based():
    """When sizing is stop-distance-based (e.g. fixed), boost must not apply so risk-per-trade is preserved."""
    config = RiskConfig(
        auction_mode_enabled=True,
        sizing_method="fixed",
        risk_per_trade_pct=0.03,
        target_margin_util_min=0.70,
        utilisation_boost_max_factor=2.0,
        max_single_position_margin_pct_equity=0.25,
        target_leverage=7.0,
    )
    rm = RiskManager(config)
    rm.current_positions = []
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="BTC/USD",
        signal_type=SignalType.LONG,
        setup_type="test",
        regime="tight_smc",
        entry_price=Decimal("50000"),
        stop_loss=Decimal("49000"),
        take_profit=Decimal("52000"),
        reasoning="test",
        higher_tf_bias="bullish",
        adx=Decimal("30"),
        atr=Decimal("500"),
        ema200_slope="up",
    )
    equity = Decimal("1000")
    decision = rm.validate_trade(
        signal, equity, Decimal("50000"), Decimal("50000"),
        notional_override=None,
        skip_margin_check=True,
    )
    assert decision.approved
    # Fixed sizing: notional = (equity * risk%) / stop_distance_pct = (1000*0.03)/0.02 = 1500. No boost (sizing_method != leverage_based).
    assert decision.utilisation_boost_applied is False
    assert decision.position_notional == Decimal("1500")
