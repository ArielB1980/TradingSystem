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
    """When auction provides notional_override, utilisation below target, boost fires."""
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
    # Auction provides the base-sized notional as override.
    equity = Decimal("1000")
    base_notional = equity * Decimal("7") * Decimal("0.03")  # 210
    decision = rm.validate_trade(
        signal, equity, Decimal("50000"), Decimal("50000"),
        notional_override=base_notional,
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
    # Even with notional_override, boost must NOT fire for non-leverage_based sizing
    fixed_notional = (equity * Decimal("0.03")) / Decimal("0.02")  # 1500
    decision = rm.validate_trade(
        signal, equity, Decimal("50000"), Decimal("50000"),
        notional_override=fixed_notional,
        skip_margin_check=True,
    )
    assert decision.approved
    # Fixed sizing: boost must not apply (sizing_method != leverage_based).
    assert decision.utilisation_boost_applied is False


def test_utilisation_boost_does_not_fire_without_auction():
    """When auction_mode_enabled=False, boost must never fire — even with notional_override."""
    config = RiskConfig(
        auction_mode_enabled=False,
        sizing_method="leverage_based",
        risk_per_trade_pct=0.03,
        target_margin_util_min=0.70,
        utilisation_boost_max_factor=2.0,
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
    base_notional = equity * Decimal("7") * Decimal("0.03")  # 210
    decision = rm.validate_trade(
        signal, equity, Decimal("50000"), Decimal("50000"),
        notional_override=base_notional,
        skip_margin_check=True,
    )
    assert decision.approved
    # With auction_mode_enabled=False, boost MUST NOT fire
    assert decision.utilisation_boost_applied is False
    # Notional should be the override (or capped below), never boosted above it
    assert decision.position_notional <= base_notional


def test_utilisation_boost_does_not_fire_without_notional_override():
    """Non-auction (no notional_override) trades must never get boosted."""
    config = RiskConfig(
        auction_mode_enabled=True,
        sizing_method="leverage_based",
        risk_per_trade_pct=0.03,
        target_margin_util_min=0.70,
        utilisation_boost_max_factor=2.0,
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
    # No notional_override (non-auction path)
    decision = rm.validate_trade(
        signal, equity, Decimal("50000"), Decimal("50000"),
        available_margin=Decimal("950"),
    )
    assert decision.approved
    # Without notional_override, boost must NOT fire
    assert decision.utilisation_boost_applied is False
    # Base sizing: 1000 * 7 * 0.03 = 210
    assert decision.position_notional == Decimal("210")


def test_utilisation_boost_capped_by_available_margin():
    """Boost must never exceed 95% of available margin * leverage."""
    config = RiskConfig(
        auction_mode_enabled=True,
        sizing_method="leverage_based",
        risk_per_trade_pct=0.03,
        target_margin_util_min=0.70,
        utilisation_boost_max_factor=2.0,
        max_single_position_margin_pct_equity=0.50,  # generous to not bind here
        max_aggregate_margin_pct_equity=5.0,          # generous to not bind here
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
    base_notional = equity * Decimal("7") * Decimal("0.03")  # 210
    # Tiny available margin: only $35 available → 35 * 0.95 * 7 = 232.75 max notional
    small_available = Decimal("35")
    decision = rm.validate_trade(
        signal, equity, Decimal("50000"), Decimal("50000"),
        available_margin=small_available,
        notional_override=base_notional,
        skip_margin_check=True,
    )
    assert decision.approved
    # 2x boost would give 420, but available_margin cap = 35 * 0.95 * 7 = 232.75
    max_from_avail = small_available * Decimal("0.95") * Decimal("7")
    assert decision.position_notional <= max_from_avail, (
        f"Boosted notional {decision.position_notional} exceeds available margin cap {max_from_avail}"
    )
    assert decision.utilisation_boost_applied is True


def test_utilisation_boost_max_factor_binding():
    """When max_factor is the tightest cap, boost is exactly factor * base."""
    config = RiskConfig(
        auction_mode_enabled=True,
        sizing_method="leverage_based",
        risk_per_trade_pct=0.03,
        target_margin_util_min=0.70,
        utilisation_boost_max_factor=1.5,  # tight factor cap
        max_single_position_margin_pct_equity=0.50,
        max_aggregate_margin_pct_equity=5.0,
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
    base_notional = equity * Decimal("7") * Decimal("0.03")  # 210
    decision = rm.validate_trade(
        signal, equity, Decimal("50000"), Decimal("50000"),
        notional_override=base_notional,
        skip_margin_check=True,
        available_margin=Decimal("500"),
    )
    assert decision.approved
    assert decision.utilisation_boost_applied is True
    # 1.5x of 210 = 315. Other caps are much higher, so max_factor should bind.
    assert decision.position_notional == Decimal("315"), (
        f"Expected 315 (1.5x * 210), got {decision.position_notional}"
    )
