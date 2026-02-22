"""
Tests for Issue 3: ShockGuard wick/flash move protection.

Verifies detection thresholds and exposure reduction actions.
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from src.risk.shock_guard import (
    ShockGuard,
    ExposureAction,
    ExposureActionItem,
)
from src.domain.models import Position, Side


@pytest.fixture
def shock_guard():
    """Create a test ShockGuard."""
    return ShockGuard(
        shock_move_pct=0.025,  # 2.5%
        shock_range_pct=0.04,  # 4.0%
        basis_shock_pct=0.015,  # 1.5%
        shock_cooldown_minutes=30,
        emergency_buffer_pct=0.10,  # 10%
        trim_buffer_pct=0.18,  # 18%
        shock_marketwide_count=3,
        shock_marketwide_window_sec=60,
    )


@pytest.fixture
def sample_position():
    """Create a sample position."""
    return Position(
        symbol="BTC/USD:USD",
        side=Side.LONG,
        size=Decimal("1"),
        size_notional=Decimal("50000"),
        entry_price=Decimal("50000"),
        current_mark_price=Decimal("50000"),
        liquidation_price=Decimal("45000"),
        unrealized_pnl=Decimal("0"),
        leverage=Decimal("7"),
        margin_used=Decimal("7143"),
        opened_at=datetime.now(timezone.utc),
    )


def test_shock_detection_move_threshold(shock_guard):
    """Test that 1-minute move > threshold triggers shock."""
    from src.risk.shock_guard import MarkPriceSnapshot
    
    # Set up price history manually (snapshot must be >= 45s old for move detection)
    now = datetime.now(timezone.utc)
    fifty_sec_ago = now - timedelta(seconds=50)  # >= 45s old, within 1-minute window
    
    # Set initial price history (need at least 2 snapshots for comparison)
    # The evaluate() method calls update_mark_prices() which adds current price
    # So we need previous prices in history before calling evaluate()
    shock_guard.mark_price_history["BTC/USD:USD"] = [
        MarkPriceSnapshot(
            mark_price=Decimal("50000"),
            timestamp=fifty_sec_ago,
        ),
    ]
    
    # Now trigger shock with 3% move (51500 / 50000 - 1 = 3%) to exceed 2.5% threshold
    # evaluate() will call update_mark_prices() which adds 51500 to history
    # Then it compares current (51500) with snapshot >= 45s old (50000)
    mark_prices = {"BTC/USD:USD": Decimal("51500")}  # 3% move > 2.5% threshold
    shock_detected = shock_guard.evaluate(mark_prices)
    
    assert shock_detected is True
    assert shock_guard.shock_mode_active is True
    assert shock_guard.shock_until is not None


def test_shock_detection_basis_threshold(shock_guard):
    """Test that basis spike > threshold triggers shock."""
    # Basis detection doesn't require price history for move comparison
    # But we need at least 2 snapshots to pass the len check in the loop
    # Set up initial price history (within 1-minute window)
    from src.risk.shock_guard import MarkPriceSnapshot
    now = datetime.now(timezone.utc)
    thirty_sec_ago = now - timedelta(seconds=30)
    shock_guard.mark_price_history["BTC/USD:USD"] = [
        MarkPriceSnapshot(
            mark_price=Decimal("50000"),
            timestamp=thirty_sec_ago,
        ),
    ]
    
    # Add one more to ensure we have 2+ after update_mark_prices adds current
    shock_guard.update_mark_prices({"BTC/USD:USD": Decimal("50000")})
    
    # Trigger shock with 2% basis divergence (51000 / 50000 - 1 = 2%) to exceed 1.5% threshold
    mark_prices = {"BTC/USD:USD": Decimal("50000")}
    spot_prices = {"BTC/USD:USD": Decimal("51000")}  # 2% basis > 1.5% threshold
    
    shock_detected = shock_guard.evaluate(mark_prices, spot_prices)
    
    assert shock_detected is True
    assert shock_guard.shock_mode_active is True


def test_exposure_action_close(shock_guard, sample_position):
    """Test that positions with buffer < emergency threshold get CLOSE action."""
    shock_guard.shock_mode_active = True
    
    # Mark price very close to liquidation (5% buffer)
    mark_price = Decimal("47250")  # 5% above liquidation
    liquidation_price = Decimal("45000")
    
    action = shock_guard.evaluate_position_exposure(
        sample_position,
        mark_price,
        liquidation_price,
    )
    
    assert action == ExposureAction.CLOSE


def test_exposure_action_trim(shock_guard, sample_position):
    """Test that positions with buffer < trim threshold get TRIM action."""
    shock_guard.shock_mode_active = True
    
    # Mark price with 15% buffer (between trim and emergency)
    mark_price = Decimal("51750")  # 15% above liquidation
    liquidation_price = Decimal("45000")
    
    action = shock_guard.evaluate_position_exposure(
        sample_position,
        mark_price,
        liquidation_price,
    )
    
    assert action == ExposureAction.TRIM


def test_exposure_action_hold(shock_guard, sample_position):
    """Test that positions with sufficient buffer get HOLD action."""
    shock_guard.shock_mode_active = True
    
    # Mark price with 25% buffer (above trim threshold)
    mark_price = Decimal("56250")  # 25% above liquidation
    liquidation_price = Decimal("45000")
    
    action = shock_guard.evaluate_position_exposure(
        sample_position,
        mark_price,
        liquidation_price,
    )
    
    assert action == ExposureAction.HOLD


def test_should_pause_entries(shock_guard):
    """Test that should_pause_entries returns True during cooldown."""
    shock_guard.shock_mode_active = True
    shock_guard.shock_until = datetime.now(timezone.utc) + timedelta(minutes=30)
    
    assert shock_guard.should_pause_entries() is True
    
    # After cooldown expires
    shock_guard.shock_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    assert shock_guard.should_pause_entries() is False
    assert shock_guard.shock_mode_active is False


def test_shock_guard_exposure_reduction_integration():
    """Integration test: verify reduce-only orders are issued for CLOSE/TRIM."""
    from unittest.mock import AsyncMock, MagicMock
    
    shock_guard = ShockGuard(
        shock_move_pct=0.025,
        emergency_buffer_pct=0.10,
        trim_buffer_pct=0.18,
    )
    
    # Activate shock
    shock_guard.shock_mode_active = True
    shock_guard.shock_until = datetime.now(timezone.utc) + timedelta(minutes=30)
    
    # Create positions with different buffer levels
    positions = [
        Position(
            symbol="BTC/USD:USD",
            side=Side.LONG,
            size=Decimal("1"),
            size_notional=Decimal("50000"),
            entry_price=Decimal("50000"),
            current_mark_price=Decimal("47250"),  # 5% buffer (CLOSE)
            liquidation_price=Decimal("45000"),
            unrealized_pnl=Decimal("-2750"),
            leverage=Decimal("7"),
            margin_used=Decimal("7143"),
            opened_at=datetime.now(timezone.utc),
        ),
        Position(
            symbol="ETH/USD:USD",
            side=Side.LONG,
            size=Decimal("10"),
            size_notional=Decimal("30000"),
            entry_price=Decimal("3000"),
            current_mark_price=Decimal("3105"),  # 15% buffer (TRIM)
            liquidation_price=Decimal("2700"),
            unrealized_pnl=Decimal("1050"),
            leverage=Decimal("7"),
            margin_used=Decimal("4286"),
            opened_at=datetime.now(timezone.utc),
        ),
    ]
    
    mark_prices = {
        "BTC/USD:USD": Decimal("47250"),
        "ETH/USD:USD": Decimal("3105"),
    }
    
    liquidation_prices = {
        "BTC/USD:USD": Decimal("45000"),
        "ETH/USD:USD": Decimal("2700"),
    }
    
    actions = shock_guard.get_exposure_reduction_actions(
        positions=positions,
        mark_prices=mark_prices,
        liquidation_prices=liquidation_prices,
    )
    
    # Should have 2 actions: CLOSE for BTC, TRIM for ETH
    assert len(actions) == 2
    
    btc_action = next(a for a in actions if a.symbol == "BTC/USD:USD")
    assert btc_action.action == ExposureAction.CLOSE
    assert btc_action.buffer_pct < Decimal("0.10")
    
    eth_action = next(a for a in actions if a.symbol == "ETH/USD:USD")
    assert eth_action.action == ExposureAction.TRIM
    assert eth_action.buffer_pct < Decimal("0.18")
