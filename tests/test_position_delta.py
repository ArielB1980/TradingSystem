"""
Tests for PositionDeltaReconciler - strategy-execution decoupling.

These tests verify that the reconciliation layer correctly calculates
position deltas and prevents position drift.
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone

from src.reconciliation.position_delta import (
    PositionDeltaReconciler,
    PositionIntent,
    ExchangePosition,
    PositionDelta,
    DeltaAction,
    DeltaRejection,
    get_delta_reconciler,
    init_delta_reconciler,
)
from src.domain.models import Side


class MockSignal:
    """Mock signal for testing."""
    def __init__(
        self,
        symbol: str,
        signal_type: str,
        score_breakdown: dict = None,
        reasoning: str = "",
    ):
        self.symbol = symbol
        self.signal_type = type('MockType', (), {'value': signal_type})()
        self.score_breakdown = score_breakdown or {"pattern": 30, "confirmation": 25}
        self.reasoning = reasoning
        self.signal_id = f"sig_{symbol}_{signal_type}"


class TestPositionIntent:
    """Test PositionIntent dataclass."""
    
    def test_long_intent(self):
        """Test creating a long position intent."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5"),
            size_notional=Decimal("22500"),
        )
        
        assert intent.symbol == "BTC/USD"
        assert intent.side == Side.LONG
        assert intent.size == Decimal("0.5")
    
    def test_flat_intent(self):
        """Test creating a flat (no position) intent."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=None,
            size=Decimal("0"),
            size_notional=Decimal("0"),
            reason="close_signal",
        )
        
        assert intent.side is None
        assert intent.size == Decimal("0")


class TestExchangePosition:
    """Test ExchangePosition dataclass."""
    
    def test_open_position(self):
        """Test position with size is open."""
        pos = ExchangePosition(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5"),
            size_notional=Decimal("22500"),
        )
        
        assert pos.is_open is True
    
    def test_flat_position(self):
        """Test position with no size is not open."""
        pos = ExchangePosition(
            symbol="BTC/USD",
            side=None,
            size=Decimal("0"),
            size_notional=Decimal("0"),
        )
        
        assert pos.is_open is False


class TestPositionDeltaReconciler:
    """Test PositionDeltaReconciler core functionality."""
    
    @pytest.fixture
    def reconciler(self):
        """Create a fresh reconciler for each test."""
        return PositionDeltaReconciler(
            min_delta_threshold_usd=Decimal("10"),
            max_delta_per_order_usd=Decimal("50000"),
        )
    
    def test_both_flat_is_hold(self, reconciler):
        """When both intent and actual are flat, action should be HOLD."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=None,
            size=Decimal("0"),
            size_notional=Decimal("0"),
        )
        actual = ExchangePosition(
            symbol="BTC/USD",
            side=None,
            size=Decimal("0"),
            size_notional=Decimal("0"),
        )
        
        delta = reconciler.calculate_delta(intent, actual)
        
        assert delta.action == DeltaAction.HOLD
        assert delta.is_reconciled is True
        assert delta.delta_size == Decimal("0")
    
    def test_want_flat_have_long_is_close(self, reconciler):
        """When want flat but have long, action should be CLOSE."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=None,
            size=Decimal("0"),
            size_notional=Decimal("0"),
        )
        actual = ExchangePosition(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5"),
            size_notional=Decimal("22500"),
        )
        
        delta = reconciler.calculate_delta(intent, actual)
        
        assert delta.action == DeltaAction.CLOSE
        assert delta.is_reconciled is False
        assert delta.delta_size == Decimal("-0.5")  # Sell to close
        assert delta.allowed is True
    
    def test_want_long_have_flat_is_open(self, reconciler):
        """When want long but flat, action should be OPEN."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5"),
            size_notional=Decimal("22500"),
        )
        actual = ExchangePosition(
            symbol="BTC/USD",
            side=None,
            size=Decimal("0"),
            size_notional=Decimal("0"),
        )
        
        delta = reconciler.calculate_delta(intent, actual)
        
        assert delta.action == DeltaAction.OPEN
        assert delta.is_reconciled is False
        assert delta.delta_size == Decimal("0.5")  # Buy to open
        assert delta.allowed is True
    
    def test_same_side_larger_is_adjust(self, reconciler):
        """When want larger position same side, action should be ADJUST."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("1.0"),  # Want 1.0
            size_notional=Decimal("45000"),
        )
        actual = ExchangePosition(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5"),  # Have 0.5
            size_notional=Decimal("22500"),
        )
        
        delta = reconciler.calculate_delta(intent, actual)
        
        assert delta.action == DeltaAction.ADJUST
        assert delta.delta_size == Decimal("0.5")  # Buy 0.5 more
    
    def test_same_side_smaller_is_reduce(self, reconciler):
        """When want smaller position same side, action should be REDUCE."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.3"),  # Want 0.3
            size_notional=Decimal("13500"),
        )
        actual = ExchangePosition(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5"),  # Have 0.5
            size_notional=Decimal("22500"),
        )
        
        delta = reconciler.calculate_delta(intent, actual)
        
        assert delta.action == DeltaAction.REDUCE
        assert delta.delta_size == Decimal("-0.2")  # Sell 0.2
    
    def test_opposite_sides_is_flip(self, reconciler):
        """When want opposite side, action should be FLIP."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=Side.SHORT,  # Want short
            size=Decimal("0.5"),
            size_notional=Decimal("22500"),
        )
        actual = ExchangePosition(
            symbol="BTC/USD",
            side=Side.LONG,  # Have long
            size=Decimal("0.5"),
            size_notional=Decimal("22500"),
        )
        
        delta = reconciler.calculate_delta(intent, actual)
        
        assert delta.action == DeltaAction.FLIP
        assert delta.is_reconciled is False
    
    def test_small_delta_rejected(self, reconciler):
        """Delta below threshold should be rejected."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5001"),  # Very slightly more
            size_notional=Decimal("22504.50"),
        )
        actual = ExchangePosition(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5"),
            size_notional=Decimal("22500"),
        )
        
        delta = reconciler.calculate_delta(intent, actual)
        
        assert delta.allowed is False
        assert delta.rejection == DeltaRejection.DELTA_TOO_SMALL
    
    def test_create_intent_from_long_signal(self, reconciler):
        """Test creating intent from a long signal."""
        signal = MockSignal("BTC/USD", "LONG", reasoning="Strong momentum")
        
        intent = reconciler.create_intent_from_signal(
            signal=signal,
            size_notional=Decimal("10000"),
            size_base=Decimal("0.25"),
        )
        
        assert intent.symbol == "BTC/USD"
        assert intent.side == Side.LONG
        assert intent.size == Decimal("0.25")
        assert intent.signal_score == 55  # 30 + 25
    
    def test_create_intent_from_short_signal(self, reconciler):
        """Test creating intent from a short signal."""
        signal = MockSignal("BTC/USD", "SHORT")
        
        intent = reconciler.create_intent_from_signal(
            signal=signal,
            size_notional=Decimal("10000"),
            size_base=Decimal("0.25"),
        )
        
        assert intent.side == Side.SHORT
    
    def test_create_flat_intent(self, reconciler):
        """Test creating a flat intent."""
        intent = reconciler.create_flat_intent("BTC/USD", reason="take_profit")
        
        assert intent.symbol == "BTC/USD"
        assert intent.side is None
        assert intent.size == Decimal("0")
        assert intent.reason == "take_profit"
    
    def test_apply_system_state_check_halted(self, reconciler):
        """Test that HALTED state blocks new entries."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5"),
            size_notional=Decimal("22500"),
        )
        actual = ExchangePosition(
            symbol="BTC/USD",
            side=None,
            size=Decimal("0"),
            size_notional=Decimal("0"),
        )
        
        delta = reconciler.calculate_delta(intent, actual)
        assert delta.allowed is True  # Before system state check
        
        # Apply system state check
        reconciler.apply_system_state_check(
            delta=delta,
            system_state="halted",
            active_violations=["max_drawdown_exceeded"],
        )
        
        assert delta.allowed is False
        assert delta.rejection == DeltaRejection.SYSTEM_HALTED
    
    def test_apply_system_state_check_emergency_allows_close(self, reconciler):
        """Test that EMERGENCY state allows closing positions."""
        intent = PositionIntent(
            symbol="BTC/USD",
            side=None,  # Want to close
            size=Decimal("0"),
            size_notional=Decimal("0"),
        )
        actual = ExchangePosition(
            symbol="BTC/USD",
            side=Side.LONG,
            size=Decimal("0.5"),
            size_notional=Decimal("22500"),
        )
        
        delta = reconciler.calculate_delta(intent, actual)
        
        # Apply emergency state check
        reconciler.apply_system_state_check(
            delta=delta,
            system_state="emergency",
            active_violations=["multiple_critical"],
        )
        
        # CLOSE should still be allowed in emergency
        assert delta.allowed is True
        assert delta.action == DeltaAction.CLOSE
    
    def test_log_reconciliation_summary(self, reconciler):
        """Test reconciliation summary logging."""
        deltas = [
            PositionDelta(
                symbol="BTC/USD",
                intended_side=Side.LONG,
                intended_size=Decimal("0.5"),
                actual_side=None,
                actual_size=Decimal("0"),
                delta_size=Decimal("0.5"),
                delta_notional=Decimal("22500"),
                action=DeltaAction.OPEN,
                is_reconciled=False,
                allowed=True,
            ),
            PositionDelta(
                symbol="ETH/USD",
                intended_side=Side.LONG,
                intended_size=Decimal("2.0"),
                actual_side=Side.LONG,
                actual_size=Decimal("2.0"),
                delta_size=Decimal("0"),
                delta_notional=Decimal("0"),
                action=DeltaAction.HOLD,
                is_reconciled=True,
                allowed=True,
            ),
        ]
        
        summary = reconciler.log_reconciliation_summary(deltas, "cycle_123")
        
        assert summary["total_deltas"] == 2
        assert summary["reconciled"] == 1
        assert summary["allowed"] == 1
        assert summary["by_action"]["open"] == 1
        assert summary["by_action"]["hold"] == 1


class TestGlobalSingleton:
    """Test global singleton functions."""
    
    def test_get_delta_reconciler(self):
        """Test getting global reconciler instance."""
        rec1 = get_delta_reconciler()
        rec2 = get_delta_reconciler()
        
        assert rec1 is rec2
    
    def test_init_delta_reconciler(self):
        """Test initializing global reconciler with custom settings."""
        rec = init_delta_reconciler(
            min_delta_threshold_usd=Decimal("100"),
        )
        
        assert rec.min_delta_threshold == Decimal("100")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
