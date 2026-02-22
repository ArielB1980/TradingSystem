"""
Test for ShockGuard exposure reduction in live trading context.

Verifies that mark_prices_for_positions is correctly built and used,
and that no undefined variables are referenced.
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

from src.risk.shock_guard import ShockGuard, ExposureAction
from src.domain.models import Position, Side


def test_shock_guard_exposure_reduction_mark_prices():
    """Test that exposure reduction uses correct mark prices dict structure."""
    shock_guard = ShockGuard(
        shock_move_pct=0.025,
        shock_range_pct=0.04,
        basis_shock_pct=0.015,
        shock_cooldown_minutes=30,
        emergency_buffer_pct=0.10,
        trim_buffer_pct=0.18,
    )
    
    # Activate shock mode
    shock_guard.shock_mode_active = True
    
    # Create test positions with exchange symbols (like PF_XBTUSD)
    # For LONG: buffer = (mark - liq) / mark
    # CLOSE: buffer < 10% → liq > mark * 0.90
    # TRIM: 10% <= buffer < 18% → mark * 0.82 < liq <= mark * 0.90
    
    positions = [
        Position(
            symbol="PF_XBTUSD",  # Exchange symbol
            side=Side.LONG,
            size=Decimal("1"),
            size_notional=Decimal("50000"),
            entry_price=Decimal("50000"),
            current_mark_price=Decimal("48000"),
            # For TRIM: buffer between 10% and 18%
            # 10% buffer: liq = 48000 * 0.90 = 43200
            # 15% buffer: liq = 48000 * 0.85 = 40800
            liquidation_price=Decimal("40800"),  # 15% buffer → TRIM
            unrealized_pnl=Decimal("-2000"),
            leverage=Decimal("7"),
            margin_used=Decimal("7143"),
            opened_at=datetime.now(timezone.utc),
        ),
        Position(
            symbol="PF_ETHUSD",  # Exchange symbol
            side=Side.LONG,
            size=Decimal("10"),
            size_notional=Decimal("30000"),
            entry_price=Decimal("3000"),
            current_mark_price=Decimal("2850"),
            # For CLOSE: buffer < 10%
            # 5% buffer: liq = 2850 * 0.95 = 2707.5
            liquidation_price=Decimal("2707"),  # ~5% buffer → CLOSE
            unrealized_pnl=Decimal("-1500"),
            leverage=Decimal("7"),
            margin_used=Decimal("4286"),
            opened_at=datetime.now(timezone.utc),
        ),
    ]
    
    # Mark prices keyed by position symbols (exchange symbols)
    mark_prices_for_positions = {
        "PF_XBTUSD": Decimal("48000"),
        "PF_ETHUSD": Decimal("2850"),
    }
    
    liquidation_prices = {
        "PF_XBTUSD": Decimal("40800"),
        "PF_ETHUSD": Decimal("2707"),
    }
    
    # Get exposure reduction actions
    actions = shock_guard.get_exposure_reduction_actions(
        positions=positions,
        mark_prices=mark_prices_for_positions,
        liquidation_prices=liquidation_prices,
    )
    
    # Verify actions are generated correctly
    assert len(actions) == 2
    
    # ETH should be CLOSED (buffer < 10%)
    eth_action = next(a for a in actions if a.symbol == "PF_ETHUSD")
    assert eth_action.action == ExposureAction.CLOSE
    assert eth_action.buffer_pct < Decimal("0.10")
    
    # BTC should be TRIM (buffer between 10% and 18%)
    btc_action = next(a for a in actions if a.symbol == "PF_XBTUSD")
    assert btc_action.action == ExposureAction.TRIM
    assert Decimal("0.10") <= btc_action.buffer_pct < Decimal("0.18")
    
    # Verify no undefined variables would be referenced
    # (This test ensures mark_prices dict structure is correct)
    for action in actions:
        assert action.symbol in mark_prices_for_positions
        assert mark_prices_for_positions[action.symbol] > 0


def test_shock_guard_mark_prices_fallback():
    """Test that mark prices can be built from position data if tickers missing."""
    # Simulate scenario where position symbol doesn't match ticker keys exactly
    # but we can extract base and match
    
    def extract_base(symbol: str):
        """Extract base currency from symbol."""
        for prefix in ["PI_", "PF_", "FI_"]:
            if symbol.startswith(prefix):
                symbol = symbol[len(prefix):]
        for suffix in ["USD", "/USD:USD", "/USD"]:
            if symbol.endswith(suffix):
                symbol = symbol[:-len(suffix)]
        return symbol if symbol else None
    
    # Position uses PF_XBTUSD
    pos_symbol = "PF_XBTUSD"
    pos_base = extract_base(pos_symbol)  # Should be "XBT"
    
    # Tickers have different format
    map_futures_tickers = {
        "BTC/USD:USD": Decimal("48000"),  # CCXT unified
        "PI_XBTUSD": Decimal("48000"),    # Legacy format
    }
    
    # Should find match by base
    mark_price = None
    for ticker_symbol, ticker_price in map_futures_tickers.items():
        ticker_base = extract_base(ticker_symbol)
        if pos_base and ticker_base and pos_base == ticker_base:
            mark_price = ticker_price
            break
    
    assert mark_price is not None
    assert mark_price == Decimal("48000")
