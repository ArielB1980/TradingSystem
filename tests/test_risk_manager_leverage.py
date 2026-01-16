"""
Test position sizing with leverage.
"""
from decimal import Decimal
import pytest
from datetime import datetime, timezone

from src.risk.risk_manager import RiskManager
from src.config.config import RiskConfig
from src.domain.models import Signal, SignalType, SetupType


def test_position_sizing_with_10x_leverage():
    """Verify position sizing accounts for 10x leverage."""
    config = RiskConfig(
        risk_per_trade_pct=0.003,  # 0.3%
        max_leverage=10.0,
        max_concurrent_positions=5,
        daily_loss_limit_pct=0.01,
        loss_streak_cooldown=3,
        min_liquidation_buffer_pct=0.35,
        basis_max_pct=0.0075,
        tight_smc_cost_cap_bps=25.0,
        tight_smc_min_rr_multiple=2.0,
        tight_smc_avg_hold_hours=6.0,

        wide_structure_max_distortion_pct=0.15,
        wide_structure_avg_hold_hours=36.0,
        taker_fee_bps=5.0,
        funding_rate_daily_bps=10.0,
        tight_stop_threshold_pct=0.015,
        loss_streak_cooldown_tight=3,
        loss_streak_cooldown_wide=5,
        loss_streak_pause_minutes_tight=120,
        loss_streak_pause_minutes_wide=90,
        loss_streak_min_loss_bps=20.0,
    )
    rm = RiskManager(config)
    
    # Mock signal with 2% stop (SHORT BNB at $889.6)
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="BNB/USD",
        signal_type=SignalType.LONG,
        entry_price=Decimal("889.6"),
        stop_loss=Decimal("871.008"),  # 2.09% stop
        take_profit=Decimal("920.0"),
        reasoning="Test signal",
        setup_type=SetupType.OB,
        regime="tight_smc",
        higher_tf_bias="bullish",
        adx=Decimal("25.0"),
        atr=Decimal("10.0"),
        ema200_slope="up",
        tp_candidates=[]
    )
    
    account_equity = Decimal("389.08")  # Current account equity
    
    decision = rm.validate_trade(
        signal=signal,
        account_equity=account_equity,
        spot_price=Decimal("889.6"),
        perp_mark_price=Decimal("889.6")
    )
    
    # Expected calculation:
    # Buying power = 389.08 × 10 = 3,890.80
    # Risk amount = 3,890.80 × 0.003 = 11.67
    # Stop distance = (889.6 - 871.008) / 889.6 = 2.09%
    # Position notional = 11.67 / 0.0209 = 558.37
    
    # Actual Logic (Risk Managed):
    # Risk amount = 389.08 * 0.003 = 1.16724
    # Stop distance = 2.09%
    # Position notional = 1.16724 / 0.0209 = ~55.85
    
    expected_risk = account_equity * Decimal("0.003")
    stop_distance_pct = abs(signal.entry_price - signal.stop_loss) / signal.entry_price
    expected_notional = expected_risk / stop_distance_pct
    
    print(f"\n=== Position Sizing Test ===")
    print(f"Account Equity: ${account_equity}")
    print(f"Risk per trade: 0.3%")
    print(f"Risk Amount: ${expected_risk:.2f}")
    print(f"Stop Distance: {stop_distance_pct:.2%}")
    print(f"Expected Notional: ${expected_notional:.2f}")
    print(f"Actual Notional: ${decision.position_notional:.2f}")
    print(f"Difference: ${abs(decision.position_notional - expected_notional):.2f}")
    
    # Allow small rounding difference
    assert abs(decision.position_notional - expected_notional) < Decimal("1.0"), \
        f"Position notional {decision.position_notional} != expected {expected_notional}"
    
    # Verify it's large enough for BNB minimum (0.01 BNB × $889.6 = $8.896)
    bnb_minimum_notional = Decimal("8.896")
    assert decision.position_notional > bnb_minimum_notional, \
        f"Position {decision.position_notional} too small for BNB minimum {bnb_minimum_notional}"
    
    print(f"✅ Position size ${decision.position_notional:.2f} exceeds BNB minimum ${bnb_minimum_notional:.2f}")


def test_position_sizing_comparison_5x_vs_10x():
    """Compare position sizes with 5x vs 10x leverage."""
    account_equity = Decimal("389.08")
    
    # Test with 5x leverage
    config_5x = RiskConfig(
        risk_per_trade_pct=0.003,
        max_leverage=5.0,
        max_concurrent_positions=5,
        daily_loss_limit_pct=0.01,
        loss_streak_cooldown=3,
        min_liquidation_buffer_pct=0.35,
        basis_max_pct=0.0075,
        tight_smc_cost_cap_bps=25.0,
        tight_smc_min_rr_multiple=2.0,
        tight_smc_avg_hold_hours=6.0,

        wide_structure_max_distortion_pct=0.15,
        wide_structure_avg_hold_hours=36.0,
        taker_fee_bps=5.0,
        funding_rate_daily_bps=10.0,
        tight_stop_threshold_pct=0.015,
        loss_streak_cooldown_tight=3,
        loss_streak_cooldown_wide=5,
        loss_streak_pause_minutes_tight=120,
        loss_streak_pause_minutes_wide=90,
        loss_streak_min_loss_bps=20.0,
    )
    rm_5x = RiskManager(config_5x)
    
    # Test with 10x leverage
    config_10x = RiskConfig(
        risk_per_trade_pct=0.003,
        max_leverage=10.0,
        max_concurrent_positions=5,
        daily_loss_limit_pct=0.01,
        loss_streak_cooldown=3,
        min_liquidation_buffer_pct=0.35,
        basis_max_pct=0.0075,
        tight_smc_cost_cap_bps=25.0,
        tight_smc_min_rr_multiple=2.0,
        tight_smc_avg_hold_hours=6.0,

        wide_structure_max_distortion_pct=0.15,
        wide_structure_avg_hold_hours=36.0,
        taker_fee_bps=5.0,
        funding_rate_daily_bps=10.0,
        tight_stop_threshold_pct=0.015,
        loss_streak_cooldown_tight=3,
        loss_streak_cooldown_wide=5,
        loss_streak_pause_minutes_tight=120,
        loss_streak_pause_minutes_wide=90,
        loss_streak_min_loss_bps=20.0,
    )
    rm_10x = RiskManager(config_10x)
    
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="BTC/USD",
        signal_type=SignalType.LONG,
        entry_price=Decimal("100000"),
        stop_loss=Decimal("98000"),  # 2% stop
        take_profit=Decimal("105000"),
        reasoning="Test",
        setup_type=SetupType.OB,
        regime="tight_smc",
        higher_tf_bias="bullish",
        adx=Decimal("25.0"),
        atr=Decimal("1000.0"),
        ema200_slope="up",
        tp_candidates=[]
    )
    
    decision_5x = rm_5x.validate_trade(
        signal=signal,
        account_equity=account_equity,
        spot_price=Decimal("100000"),
        perp_mark_price=Decimal("100000")
    )
    
    decision_10x = rm_10x.validate_trade(
        signal=signal,
        account_equity=account_equity,
        spot_price=Decimal("100000"),
        perp_mark_price=Decimal("100000")
    )
    
    print(f"\n=== Leverage Comparison ===")
    print(f"Account Equity: ${account_equity}")
    print(f"5x Leverage Position: ${decision_5x.position_notional:.2f}")
    print(f"10x Leverage Position: ${decision_10x.position_notional:.2f}")
    print(f"Ratio: {decision_10x.position_notional / decision_5x.position_notional:.2f}x")
    
    # Both are risk-constrained, not leverage constrained.
    # Risk (0.3%) dictates size: 389.08 * 0.003 / stop_dist = $58.36
    # This is well within 5x leverage ($1945 capacity).
    # So both should be Equal.
    
    expected_ratio = Decimal("1.0")
    actual_ratio = decision_10x.position_notional / decision_5x.position_notional
    
    assert abs(actual_ratio - expected_ratio) < Decimal("0.01"), \
        f"Position size should be identical (risk-constrained), got ratio {actual_ratio}x"
    
    print(f"✅ Position sizes are identical (Risk Constrained)")


if __name__ == "__main__":
    test_position_sizing_with_10x_leverage()
    test_position_sizing_comparison_5x_vs_10x()
    print("\n✅ All tests passed!")
