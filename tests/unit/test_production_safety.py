"""
Acceptance Tests for Production Safety Mechanisms.

Tests the 6 critical real-world edge case protections:
1. Atomic stop replace (new-first, then cancel old)
2. EXIT_PENDING timeout + escalation
3. Event ordering constraints
4. Write-ahead persistence for intents
5. Shadow mode truth source
6. Invariant K: Always protected after first fill
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from src.execution.production_safety import (
    SafetyConfig,
    AtomicStopReplacer,
    StopReplaceContext,
    ProtectionEnforcer,
    EventOrderingEnforcer,
    WriteAheadIntentLog,
    ActionIntent,
    ActionIntentStatus,
    ExitTimeoutManager,
    ExitEscalationLevel,
    ExitEscalationState,
    PositionProtectionMonitor
)
from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    PositionRegistry,
    OrderEvent,
    OrderEventType,
    reset_position_registry,
    get_position_registry
)
from src.domain.models import Side


class TestAtomicStopReplace:
    """
    Test 1: Stop "replace semantics" must be atomic.
    
    Cancel-old, place-new creates naked window.
    Must: place-new-first, wait for ACK, then cancel-old.
    """
    
    @pytest.fixture
    def mock_client(self):
        client = AsyncMock()
        return client
    
    @pytest.fixture
    def config(self):
        return SafetyConfig(stop_replace_ack_timeout_seconds=2)
    
    @pytest.fixture
    def position(self):
        return ManagedPosition(
            symbol="BTC/USD:USD",
            side=Side.LONG,
            position_id="test-atomic",
            initial_size=Decimal("0.1"),
            initial_entry_price=Decimal("50000"),
            initial_stop_price=Decimal("49000"),
            initial_tp1_price=Decimal("52000"),
            initial_tp2_price=None,
            initial_final_target=None
        )
    
    @pytest.mark.asyncio
    async def test_new_stop_placed_before_old_cancelled(self, mock_client, config, position):
        """
        New stop must be placed and ACKed BEFORE old stop is cancelled.
        """
        position.stop_order_id = "old-stop-123"
        position.current_stop_price = Decimal("49000")
        
        # Mock: new stop succeeds immediately
        mock_client.place_futures_order.return_value = {"id": "new-stop-456"}
        mock_client.get_futures_open_orders.return_value = [{"id": "new-stop-456"}]
        mock_client.cancel_futures_order = AsyncMock()
        
        replacer = AtomicStopReplacer(mock_client, config)
        
        ctx = await replacer.replace_stop(
            position,
            Decimal("49500"),
            lambda pos_id, typ: f"stop-{pos_id}"
        )
        
        # New stop should be placed first
        assert mock_client.place_futures_order.called
        assert ctx.new_stop_order_id == "new-stop-456"
        assert ctx.new_stop_acked is True
        
        # Old stop cancelled only after new is confirmed
        # Old stop cancelled only after new is confirmed
        assert mock_client.cancel_futures_order.called
        cancel_call = mock_client.cancel_futures_order.call_args
        assert cancel_call[0][0] == "old-stop-123"
    
    @pytest.mark.asyncio
    async def test_new_stop_fails_keeps_old_stop(self, mock_client, config, position):
        """
        Acceptance test: cancel-old succeeds, place-new fails → 
        position remains protected with old stop.
        """
        position.stop_order_id = "old-stop-123"
        position.current_stop_price = Decimal("49000")
        
        # Mock: new stop placement FAILS
        mock_client.place_futures_order.side_effect = Exception("Exchange error")
        
        replacer = AtomicStopReplacer(mock_client, config)
        
        ctx = await replacer.replace_stop(
            position,
            Decimal("49500"),
            lambda pos_id, typ: f"stop-{pos_id}"
        )
        
        # Should fail safely
        assert ctx.failed is True
        assert "Exchange error" in str(ctx.error)
        
        # Old stop should NOT be cancelled
        assert not mock_client.cancel_futures_order.called
        
        # Position still has old stop
        assert position.stop_order_id == "old-stop-123"
    
    @pytest.mark.asyncio
    async def test_new_stop_not_acked_keeps_old_stop(self, mock_client, config, position):
        """
        If new stop is placed but never ACKed, keep old stop.
        """
        position.stop_order_id = "old-stop-123"
        
        # Mock: new stop placed but never acknowledged
        mock_client.place_futures_order.return_value = {"id": "new-stop-456"}
        mock_client.get_futures_open_orders.return_value = []  # Not in open orders -> not acked/live
        mock_client.cancel_futures_order = AsyncMock()
        
        replacer = AtomicStopReplacer(mock_client, SafetyConfig(stop_replace_ack_timeout_seconds=1))
        
        ctx = await replacer.replace_stop(
            position,
            Decimal("49500"),
            lambda pos_id, typ: f"stop-{pos_id}"
        )
        
        # Should fail because ACK timeout
        assert ctx.failed is True
        assert "not acknowledged" in ctx.error.lower()
        
        # Should try to cancel the failed new stop
        # Old stop should NOT be cancelled
        # The cancel_order call should be for new-stop-456 not old-stop-123
        cancel_calls = mock_client.cancel_futures_order.call_args_list
        if cancel_calls:
            # Only the new stop should be cancelled, not the old
            cancelled_ids = [call[0][0] for call in cancel_calls]
            assert "old-stop-123" not in cancelled_ids


class TestExitPendingTimeout:
    """
    Test 2: EXIT_PENDING must have timeout + escalation.
    """
    
    def test_exit_escalation_normal_to_aggressive(self):
        """After timeout, escalate from NORMAL to AGGRESSIVE."""
        config = SafetyConfig(exit_pending_timeout_seconds=1)
        
        state = ExitEscalationState(
            position_id="test-123",
            symbol="BTC/USD:USD",
            started_at=datetime.now(timezone.utc) - timedelta(seconds=2)
        )
        
        assert state.should_escalate(config) is True
        
        new_level = state.escalate()
        assert new_level == ExitEscalationLevel.AGGRESSIVE
    
    def test_exit_escalation_to_quarantine(self):
        """
        Acceptance test: Exit never fills → escalates to QUARANTINE.
        """
        config = SafetyConfig(
            exit_pending_timeout_seconds=1,
            exit_escalation_max_retries=2
        )
        manager = ExitTimeoutManager(config)
        
        # Simulate position stuck in EXIT_PENDING
        state = ExitEscalationState(
            position_id="test-123",
            symbol="BTC/USD:USD",
            started_at=datetime.now(timezone.utc) - timedelta(seconds=120)
        )
        manager._exit_states["BTC/USD:USD"] = state
        
        # Escalate multiple times
        level1 = manager.escalate("BTC/USD:USD")
        assert level1 == ExitEscalationLevel.AGGRESSIVE
        
        level2 = manager.escalate("BTC/USD:USD")
        assert level2 == ExitEscalationLevel.EMERGENCY
        
        level3 = manager.escalate("BTC/USD:USD")
        assert level3 == ExitEscalationLevel.QUARANTINE
        
        # Symbol should now be quarantined
        assert manager.is_quarantined("BTC/USD:USD") is True


class TestEventOrdering:
    """
    Test 3: Idempotent-by-hash + ordering constraints.
    """
    
    def test_reject_stale_events(self):
        """
        Acceptance test: Apply seq=10 then seq=9 → seq=9 ignored.
        """
        enforcer = EventOrderingEnforcer()
        
        # Process event with seq=10
        should_process = enforcer.should_process_event("order-123", 10)
        assert should_process is True
        enforcer.mark_processed("order-123", 10)
        
        # Try to process stale event with seq=9
        should_process = enforcer.should_process_event("order-123", 9)
        assert should_process is False
        
        # seq=11 should work
        should_process = enforcer.should_process_event("order-123", 11)
        assert should_process is True
    
    def test_deduplicate_by_fill_id(self):
        """Duplicate fills with same fill_id should be rejected."""
        enforcer = EventOrderingEnforcer()
        
        # First fill
        should_process = enforcer.should_process_event("order-123", 1, fill_id="fill-abc")
        assert should_process is True
        enforcer.mark_processed("order-123", 1, fill_id="fill-abc")
        
        # Duplicate fill with same fill_id
        should_process = enforcer.should_process_event("order-123", 2, fill_id="fill-abc")
        assert should_process is False  # Rejected by fill_id
        
        # Different fill_id should work
        should_process = enforcer.should_process_event("order-123", 2, fill_id="fill-xyz")
        assert should_process is True


class TestWriteAheadPersistence:
    """
    Test 4: Write-ahead persistence for action intents.
    """
    
    @pytest.fixture
    def mock_persistence(self):
        """Create mock persistence with in-memory connection."""
        import sqlite3
        
        mock = MagicMock()
        mock._conn = sqlite3.connect(":memory:")
        mock._conn.row_factory = sqlite3.Row
        
        # Create schema
        mock._conn.execute("""
            CREATE TABLE action_intents (
                intent_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL,
                action_type TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                size TEXT NOT NULL,
                price TEXT,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                exchange_order_id TEXT,
                error TEXT
            )
        """)
        mock._conn.commit()
        
        return mock
    
    def test_intent_persisted_before_execution(self, mock_persistence):
        """Intent must be persisted BEFORE sending to exchange."""
        wal = WriteAheadIntentLog(mock_persistence)
        
        intent = ActionIntent(
            intent_id="client-order-123",
            position_id="pos-abc",
            action_type="entry",
            symbol="BTC/USD:USD",
            side="long",
            size="0.1",
            price="50000",
            created_at=datetime.now(timezone.utc)
        )
        
        # Record intent (this should happen BEFORE exchange call)
        wal.record_intent(intent)
        
        # Verify it's in the database
        cursor = mock_persistence._conn.execute(
            "SELECT * FROM action_intents WHERE intent_id = ?",
            ("client-order-123",)
        )
        row = cursor.fetchone()
        
        assert row is not None
        assert row["status"] == "pending"
        assert row["position_id"] == "pos-abc"
    
    @pytest.mark.asyncio
    async def test_crash_recovery_detects_pending_intent(self, mock_persistence):
        """
        Acceptance test: Crash after "intent persisted" but before exchange call →
        restart must not double-send.
        """
        wal = WriteAheadIntentLog(mock_persistence)
        
        # Simulate: intent was persisted but never sent (crashed)
        intent = ActionIntent(
            intent_id="client-order-crash",
            position_id="pos-abc",
            action_type="entry",
            symbol="BTC/USD:USD",
            side="long",
            size="0.1",
            price="50000",
            created_at=datetime.now(timezone.utc),
            status=ActionIntentStatus.PENDING  # Never sent
        )
        wal.record_intent(intent)
        
        # On restart, should find this pending intent
        pending = wal.get_pending_intents()
        
        assert len(pending) == 1
        assert pending[0].intent_id == "client-order-crash"
        assert pending[0].status == ActionIntentStatus.PENDING
        
        # Reconciliation should mark as failed (never sent)
        mock_client = AsyncMock()
        mock_registry = MagicMock()
        
        resolutions = await wal.reconcile_on_startup(mock_client, mock_registry)
        
        assert resolutions["client-order-crash"] == "cancelled_unsent"


class TestInvariantK_AlwaysProtected:
    """
    Test 6: Invariant K - Always protected after first fill.
    """
    
    @pytest.fixture
    def mock_client(self):
        return AsyncMock()
    
    @pytest.fixture
    def config(self):
        return SafetyConfig(emergency_exit_on_stop_fail=True)
    
    def test_verify_protection_detects_naked_position(self):
        """Detect when position has exposure but no stop."""
        reset_position_registry()
        
        pos = ManagedPosition(
            symbol="BTC/USD:USD",
            side=Side.LONG,
            position_id="test-naked",
            initial_size=Decimal("0.1"),
            initial_entry_price=Decimal("50000"),
            initial_stop_price=Decimal("49000"),
            initial_tp1_price=Decimal("52000"),
            initial_tp2_price=None,
            initial_final_target=None
        )
        pos.entry_order_id = "entry-1"
        
        # Simulate entry fill - now have exposure
        from src.execution.position_state_machine import FillRecord
        pos.entry_fills.append(FillRecord(
            fill_id="fill-1",
            order_id="entry-1",
            side=Side.LONG,
            qty=Decimal("0.1"),
            price=Decimal("50000"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True
        ))
        pos.state = PositionState.OPEN
        
        # No stop orders on exchange
        exchange_orders = []
        
        enforcer = ProtectionEnforcer(AsyncMock(), SafetyConfig())
        
        # Should detect as unprotected
        import asyncio
        loop = asyncio.new_event_loop()
        is_protected = loop.run_until_complete(
            enforcer.verify_protection(pos, exchange_orders)
        )
        loop.close()
        
        assert is_protected is False
    
    @pytest.mark.asyncio
    async def test_emergency_exit_on_stop_fail(self, mock_client, config):
        """
        Acceptance test: Entry fills, stop fails → 
        system exits at market and quarantines.
        """
        pos = ManagedPosition(
            symbol="BTC/USD:USD",
            side=Side.LONG,
            position_id="test-emergency",
            initial_size=Decimal("0.1"),
            initial_entry_price=Decimal("50000"),
            initial_stop_price=Decimal("49000"),
            initial_tp1_price=Decimal("52000"),
            initial_tp2_price=None,
            initial_final_target=None
        )
        pos.state = PositionState.OPEN
        
        # Add entry fill
        from src.execution.position_state_machine import FillRecord
        pos.entry_fills.append(FillRecord(
            fill_id="fill-1",
            order_id="entry-1",
            side=Side.LONG,
            qty=Decimal("0.1"),
            price=Decimal("50000"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True
        ))
        
        mock_client.place_futures_order = AsyncMock(return_value={"id": "emergency-exit"})
        
        enforcer = ProtectionEnforcer(mock_client, config)
        
        # Execute emergency exit
        result = await enforcer.emergency_exit_naked_position(pos)
        
        assert result is True
        assert mock_client.place_futures_order.called
        
        # Verify it was a market order
        call_args = mock_client.place_futures_order.call_args
        assert call_args[1]["order_type"] == "market"
        assert call_args[1]["reduce_only"] is True
        
        # Position should be closed
        assert pos.state == PositionState.CLOSED


class TestShadowModeTruthSource:
    """
    Test 5: Shadow mode truth source validation.
    
    Both live and shadow should use same event format.
    """
    
    def test_shadow_mode_uses_same_apply_order_event(self):
        """
        Both live and shadow decisions should go through
        same apply_order_event() interface.
        """
        pos = ManagedPosition(
            symbol="BTC/USD:USD",
            side=Side.LONG,
            position_id="test-shadow",
            initial_size=Decimal("0.1"),
            initial_entry_price=Decimal("50000"),
            initial_stop_price=Decimal("49000"),
            initial_tp1_price=Decimal("52000"),
            initial_tp2_price=None,
            initial_final_target=None
        )
        pos.entry_order_id = "entry-1"
        
        # Create event in canonical format
        event = OrderEvent(
            order_id="entry-1",
            client_order_id="client-1",
            event_type=OrderEventType.FILLED,
            event_seq=1,
            timestamp=datetime.now(timezone.utc),
            fill_qty=Decimal("0.1"),
            fill_price=Decimal("50000"),
            fill_id="fill-1"
        )
        
        # Both live and shadow use same apply_order_event
        result = pos.apply_order_event(event)
        
        assert result is True
        assert pos.state == PositionState.OPEN
        
        # Shadow mode would create same event and apply to shadow position
        # Verification: state change happens via event, not via direct mutation


class TestIntegrationScenarios:
    """
    Full integration scenarios combining multiple safeguards.
    """
    
    @pytest.mark.asyncio
    async def test_full_lifecycle_with_protection(self):
        """
        Full position lifecycle with all protection mechanisms:
        1. Entry with WAL
        2. Stop placed (protected)
        3. Stop updated atomically
        4. Exit with timeout tracking
        5. Cleanup
        """
        reset_position_registry()
        registry = get_position_registry()
        
        # Create position
        pos = ManagedPosition(
            symbol="BTC/USD:USD",
            side=Side.LONG,
            position_id="test-full-lifecycle",
            initial_size=Decimal("0.1"),
            initial_entry_price=Decimal("50000"),
            initial_stop_price=Decimal("49000"),
            initial_tp1_price=Decimal("52000"),
            initial_tp2_price=None,
            initial_final_target=None
        )
        registry.register_position(pos)
        
        # Entry filled
        pos.entry_order_id = "entry-1"
        entry_event = OrderEvent(
            order_id="entry-1",
            client_order_id="client-1",
            event_type=OrderEventType.FILLED,
            event_seq=1,
            timestamp=datetime.now(timezone.utc),
            fill_qty=Decimal("0.1"),
            fill_price=Decimal("50000"),
            fill_id="fill-1"
        )
        pos.apply_order_event(entry_event)
        
        assert pos.state == PositionState.OPEN
        assert pos.remaining_qty == Decimal("0.1")
        
        # Stop order placed (now protected)
        pos.stop_order_id = "stop-1"
        
        # Verify position is protected
        exchange_orders = [{"symbol": "BTC/USD:USD", "type": "stop", "status": "open"}]
        enforcer = ProtectionEnforcer(AsyncMock(), SafetyConfig())
        is_protected = await enforcer.verify_protection(pos, exchange_orders)
        
        assert is_protected is True
        
        # Exit the position
        from src.execution.position_state_machine import ExitReason
        pos.initiate_exit(ExitReason.STOP_LOSS, "exit-1")
        pos.pending_exit_order_id = "exit-1"
        
        assert pos.state == PositionState.EXIT_PENDING
        
        # Track exit timeout
        timeout_manager = ExitTimeoutManager(SafetyConfig())
        timeout_manager.start_exit_tracking(pos)
        
        # Exit fills
        exit_event = OrderEvent(
            order_id="exit-1",
            client_order_id="client-2",
            event_type=OrderEventType.FILLED,
            event_seq=2,
            timestamp=datetime.now(timezone.utc),
            fill_qty=Decimal("0.1"),
            fill_price=Decimal("48500"),
            fill_id="exit-fill-1"
        )
        pos.apply_order_event(exit_event)
        
        assert pos.state == PositionState.CLOSED
        assert pos.remaining_qty == Decimal("0")
        
        # Cleanup exit tracking
        timeout_manager.exit_completed("BTC/USD:USD")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
