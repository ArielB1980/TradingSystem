"""
Tests for InvariantMonitor - the hard safety limit enforcement system.

These tests verify that critical production safety invariants are properly enforced.
"""
import os
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
import asyncio

from src.safety.invariant_monitor import (
    InvariantMonitor,
    SystemInvariants,
    SystemState,
    InvariantViolation,
    get_invariant_monitor,
    init_invariant_monitor,
)


@pytest.fixture(autouse=True)
def isolate_peak_equity_persistence(tmp_path, monkeypatch):
    """Prevent peak equity persistence from leaking between tests.
    
    Points the state file to a temp directory so tests don't read/write
    the real ~/.trading_system/peak_equity_state.json.
    """
    monkeypatch.setenv("PEAK_EQUITY_STATE_PATH", str(tmp_path / "peak_equity_state.json"))


class MockPosition:
    """Mock position object for testing."""
    def __init__(self, symbol: str, size_notional: Decimal):
        self.symbol = symbol
        self.size_notional = size_notional


class MockKillSwitch:
    """Mock kill switch for testing."""
    def __init__(self):
        self.activated = False
        self.emergency = False
        self.reason = None
    
    async def activate(self, reason, emergency=False):
        self.activated = True
        self.emergency = emergency
        self.reason = reason


class TestSystemInvariants:
    """Test SystemInvariants dataclass."""
    
    def test_default_values(self):
        """Test default invariant values are sensible."""
        inv = SystemInvariants()
        
        assert inv.max_equity_drawdown_pct == Decimal("0.15")  # 15%
        assert inv.max_open_notional_usd == Decimal("500000")
        assert inv.max_concurrent_positions == 27  # Must be >= auction_max_positions (25)
        assert inv.max_margin_utilization_pct == Decimal("0.92")
        assert inv.max_rejected_orders_per_cycle == 5
        assert inv.max_api_errors_per_minute == 10
    
    def test_custom_values(self):
        """Test custom invariant values."""
        inv = SystemInvariants(
            max_equity_drawdown_pct=Decimal("0.10"),
            max_concurrent_positions=5,
        )
        
        assert inv.max_equity_drawdown_pct == Decimal("0.10")
        assert inv.max_concurrent_positions == 5


class TestInvariantMonitor:
    """Test InvariantMonitor core functionality."""
    
    @pytest.fixture
    def monitor(self):
        """Create a fresh monitor for each test."""
        return InvariantMonitor(SystemInvariants())
    
    @pytest.fixture
    def monitor_with_kill_switch(self):
        """Create monitor with mock kill switch."""
        kill_switch = MockKillSwitch()
        monitor = InvariantMonitor(SystemInvariants(), kill_switch=kill_switch)
        return monitor, kill_switch
    
    def test_initial_state_is_active(self, monitor):
        """Monitor should start in ACTIVE state."""
        assert monitor.state == SystemState.ACTIVE
        assert monitor.is_trading_allowed()
        assert monitor.is_management_allowed()
    
    @pytest.mark.asyncio
    async def test_normal_operation_stays_active(self, monitor):
        """Normal operations should keep system ACTIVE."""
        positions = [
            MockPosition("BTC/USD", Decimal("10000")),
            MockPosition("ETH/USD", Decimal("5000")),
        ]
        
        state = await monitor.check_all(
            current_equity=Decimal("100000"),
            open_positions=positions,
            margin_utilization=Decimal("0.30"),
            available_margin=Decimal("70000"),
        )
        
        assert state == SystemState.ACTIVE
        assert monitor.is_trading_allowed()
        assert len(monitor.violations) == 0
    
    @pytest.mark.asyncio
    async def test_equity_drawdown_triggers_halt(self, monitor_with_kill_switch):
        """Equity drawdown > 15% should trigger HALTED state."""
        monitor, kill_switch = monitor_with_kill_switch
        
        # Set peak equity
        await monitor.check_all(
            current_equity=Decimal("100000"),
            open_positions=[],
            margin_utilization=Decimal("0"),
            available_margin=Decimal("100000"),
        )
        
        # Now check with 20% drawdown
        state = await monitor.check_all(
            current_equity=Decimal("80000"),  # 20% drawdown
            open_positions=[],
            margin_utilization=Decimal("0"),
            available_margin=Decimal("80000"),
        )
        
        assert state == SystemState.HALTED
        assert not monitor.is_trading_allowed()
        assert monitor.is_management_allowed()
        assert kill_switch.activated
    
    @pytest.mark.asyncio
    async def test_max_notional_triggers_halt(self, monitor_with_kill_switch):
        """Exceeding max notional should trigger HALTED state."""
        monitor, kill_switch = monitor_with_kill_switch
        
        # Create positions exceeding $500k limit
        positions = [
            MockPosition("BTC/USD", Decimal("300000")),
            MockPosition("ETH/USD", Decimal("250000")),  # Total $550k
        ]
        
        state = await monitor.check_all(
            current_equity=Decimal("100000"),
            open_positions=positions,
            margin_utilization=Decimal("0.50"),
            available_margin=Decimal("50000"),
        )
        
        assert state == SystemState.HALTED
        assert not monitor.is_trading_allowed()
        assert kill_switch.activated
    
    @pytest.mark.asyncio
    async def test_max_positions_triggers_halt(self, monitor_with_kill_switch):
        """Exceeding max concurrent positions should trigger HALTED state."""
        monitor, kill_switch = monitor_with_kill_switch
        
        # Create 30 positions (exceeds 27 limit)
        positions = [
            MockPosition(f"COIN{i}/USD", Decimal("10000"))
            for i in range(30)
        ]
        
        state = await monitor.check_all(
            current_equity=Decimal("100000"),
            open_positions=positions,
            margin_utilization=Decimal("0.50"),
            available_margin=Decimal("50000"),
        )
        
        assert state == SystemState.HALTED
        assert not monitor.is_trading_allowed()
    
    @pytest.mark.asyncio
    async def test_margin_utilization_warning(self, monitor):
        """Multiple warning conditions should trigger DEGRADED state."""
        # Create 24 positions (above degraded threshold of 22) + high margin (above 85%)
        positions = [MockPosition(f"COIN{i}/USD", Decimal("10000")) for i in range(24)]
        
        state = await monitor.check_all(
            current_equity=Decimal("100000"),
            open_positions=positions,
            margin_utilization=Decimal("0.88"),  # Above 85% warning threshold
            available_margin=Decimal("12000"),
        )
        
        # Two warnings (positions=24 > degraded threshold 22, margin=88% > 85%)
        assert state == SystemState.DEGRADED
        assert not monitor.is_trading_allowed()  # No new entries in degraded
        assert monitor.is_management_allowed()
    
    @pytest.mark.asyncio
    async def test_multiple_critical_violations_trigger_emergency(self, monitor_with_kill_switch):
        """Multiple critical violations should trigger EMERGENCY state."""
        monitor, kill_switch = monitor_with_kill_switch
        
        # Set peak equity first
        await monitor.check_all(
            current_equity=Decimal("100000"),
            open_positions=[],
            margin_utilization=Decimal("0"),
            available_margin=Decimal("100000"),
        )
        
        # Now create multiple critical violations:
        # 1. 20% drawdown
        # 2. Exceeding max notional
        positions = [
            MockPosition("BTC/USD", Decimal("600000")),  # Over $500k limit
        ]
        
        state = await monitor.check_all(
            current_equity=Decimal("80000"),  # 20% drawdown
            open_positions=positions,
            margin_utilization=Decimal("0.90"),  # Over 85% limit
            available_margin=Decimal("8000"),
        )
        
        assert state == SystemState.EMERGENCY
        assert kill_switch.activated
        assert kill_switch.emergency  # Emergency flag set
    
    def test_record_order_rejection(self, monitor):
        """Test order rejection counter."""
        assert monitor._rejected_orders_this_cycle == 0
        
        monitor.record_order_rejection()
        assert monitor._rejected_orders_this_cycle == 1
        
        monitor.record_order_rejection()
        assert monitor._rejected_orders_this_cycle == 2
        
        monitor.reset_cycle_counters()
        assert monitor._rejected_orders_this_cycle == 0
    
    def test_record_api_error(self, monitor):
        """Test API error counter."""
        assert len(monitor._api_errors) == 0
        
        monitor.record_api_error()
        assert len(monitor._api_errors) == 1
        
        monitor.record_api_error()
        assert len(monitor._api_errors) == 2
    
    @pytest.mark.asyncio
    async def test_api_errors_trigger_halt(self, monitor_with_kill_switch):
        """Too many API errors should trigger HALTED state."""
        monitor, kill_switch = monitor_with_kill_switch
        
        # Record 11 API errors (exceeds 10 limit)
        for _ in range(11):
            monitor.record_api_error()
        
        state = await monitor.check_all(
            current_equity=Decimal("100000"),
            open_positions=[],
            margin_utilization=Decimal("0"),
            available_margin=Decimal("100000"),
        )
        
        assert state == SystemState.HALTED
        assert kill_switch.activated
    
    def test_get_status(self, monitor):
        """Test status dict generation."""
        status = monitor.get_status()
        
        assert "state" in status
        assert "trading_allowed" in status
        assert "management_allowed" in status
        assert status["state"] == "active"
        assert status["trading_allowed"] is True
    
    def test_get_violation_history(self, monitor):
        """Test violation history retrieval."""
        # Initially empty
        history = monitor.get_violation_history()
        assert len(history) == 0


class TestGlobalSingleton:
    """Test global singleton functions."""
    
    def test_get_invariant_monitor(self):
        """Test getting global monitor instance."""
        monitor1 = get_invariant_monitor()
        monitor2 = get_invariant_monitor()
        
        # Should return same instance
        assert monitor1 is monitor2
    
    def test_init_invariant_monitor(self):
        """Test initializing global monitor with custom settings."""
        custom_invariants = SystemInvariants(
            max_concurrent_positions=5
        )
        
        monitor = init_invariant_monitor(invariants=custom_invariants)
        assert monitor.invariants.max_concurrent_positions == 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
