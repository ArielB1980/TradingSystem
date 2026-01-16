import pytest
from decimal import Decimal
from datetime import datetime, timezone

from src.execution.position_manager import PositionManager, ActionType
from src.domain.models import Position, Side, OrderType

@pytest.fixture
def position_manager():
    return PositionManager()

@pytest.fixture
def long_position():
    return Position(
        symbol="BTC/USD",
        side=Side.LONG,
        size=Decimal("1.0"),
        size_notional=Decimal("50000"),
        entry_price=Decimal("50000"),
        current_mark_price=Decimal("50000"),
        liquidation_price=Decimal("40000"),
        unrealized_pnl=Decimal("0"),
        leverage=Decimal("1"),
        margin_used=Decimal("5000"),
        opened_at=datetime.now(timezone.utc),
        initial_stop_price=Decimal("49000"),
        tp1_price=Decimal("51000"),
        tp2_price=Decimal("52000"),
        final_target_price=Decimal("55000"),
        partial_close_pct=Decimal("0.5")
    )

def test_stop_loss_hit(position_manager, long_position):
    """Test standard Stop Loss execution."""
    price = Decimal("48900") # Below SL
    actions = position_manager.evaluate(long_position, price)
    
    assert len(actions) == 1
    assert actions[0].type == ActionType.CLOSE_POSITION
    assert "Stop Loss" in actions[0].reason

def test_tp1_execution(position_manager, long_position):
    """Test TP1 triggers Partial Close and BE Stop."""
    price = Decimal("51000") # Hit TP1
    actions = position_manager.evaluate(long_position, price)
    
    types = [a.type for a in actions]
    assert ActionType.PARTIAL_CLOSE in types
    assert ActionType.UPDATE_STOP in types
    
    # Check quantities
    partial_action = next(a for a in actions if a.type == ActionType.PARTIAL_CLOSE)
    assert partial_action.quantity == long_position.size * long_position.partial_close_pct
    
    # Check Stop Move (to Entry)
    stop_action = next(a for a in actions if a.type == ActionType.UPDATE_STOP)
    assert stop_action.price == long_position.entry_price

def test_final_target_execution(position_manager, long_position):
    """Test Final Target triggers full close."""
    price = Decimal("55001") # Hit Final Target
    actions = position_manager.evaluate(long_position, price)
    
    assert len(actions) == 1
    assert actions[0].type == ActionType.CLOSE_POSITION
    assert "Final Target" in actions[0].reason

def test_premise_invalidation(position_manager, long_position):
    """Test external premise invalidation triggers exit."""
    actions = position_manager.evaluate(
        long_position, 
        current_price=Decimal("50500"), 
        premise_invalidated=True
    )
    
    assert len(actions) == 1
    assert actions[0].type == ActionType.CLOSE_POSITION
    assert "Premise Invalidation" in actions[0].reason
