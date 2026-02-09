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


class TestApplyOrderEventTruthSource:
    """
    Test 5: apply_order_event truth source.
    State transitions happen via event application, not direct mutation.
    """
    
    def test_apply_order_event_transitions_state(self):
        """apply_order_event drives state transition (ACK -> OPEN on fill)."""
        pos = ManagedPosition(
            symbol="BTC/USD:USD",
            side=Side.LONG,
            position_id="test-apply-event",
            initial_size=Decimal("0.1"),
            initial_entry_price=Decimal("50000"),
            initial_stop_price=Decimal("49000"),
            initial_tp1_price=Decimal("52000"),
            initial_tp2_price=None,
            initial_final_target=None
        )
        pos.entry_order_id = "entry-1"
        
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
        
        result = pos.apply_order_event(event)
        
        assert result is True
        assert pos.state == PositionState.OPEN


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


class TestStopFillNotNaked:
    """
    Test: Stop loss fill should NOT trigger NAKED POSITION kill switch.

    Root cause of the 2026-02-09 XRP/USD kill switch incident:
    Stop loss was filled by exchange (expected behavior), but the system
    treated the missing stop order as a safety violation.

    These tests verify the multi-layer defense:
    1. Stop order polling detects fills (poll_and_process_order_updates)
    2. Protection monitor verifies stop status before declaring naked
    3. Graceful position closure when stop fill is confirmed
    """

    @pytest.fixture
    def mock_client(self):
        client = AsyncMock()
        return client

    @pytest.fixture
    def position(self):
        pos = ManagedPosition(
            symbol="XRP/USD",
            side=Side.LONG,
            position_id="test-stop-fill",
            initial_size=Decimal("100"),
            initial_entry_price=Decimal("2.50"),
            initial_stop_price=Decimal("2.40"),
            initial_tp1_price=Decimal("2.70"),
            initial_tp2_price=None,
            initial_final_target=None,
        )
        pos.entry_order_id = "entry-xrp-1"
        pos.stop_order_id = "stop-xrp-1"
        pos.state = PositionState.OPEN
        # Add entry fill so remaining_qty > 0
        from src.execution.position_state_machine import FillRecord
        pos.entry_fills.append(FillRecord(
            fill_id="fill-entry-xrp",
            order_id="entry-xrp-1",
            side=Side.LONG,
            qty=Decimal("100"),
            price=Decimal("2.50"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True,
        ))
        return pos

    @pytest.mark.asyncio
    async def test_stop_filled_not_treated_as_naked(self, mock_client, position):
        """
        When stop loss fills and exchange still shows a brief position,
        the system should verify the stop was filled and NOT flag as naked.
        """
        reset_position_registry()
        registry = get_position_registry()
        registry.register_position(position)

        # Exchange state: position still briefly exists, but stop order is gone
        mock_client.get_futures_open_orders.return_value = []  # No open orders
        mock_client.get_all_futures_positions.return_value = [
            {"symbol": "PF_XRPUSD", "contracts": 100}  # Still shows position
        ]
        # Stop order was FILLED (this is the key verification)
        mock_client.fetch_order.return_value = {
            "id": "stop-xrp-1",
            "status": "closed",
            "filled": 100,
            "average": 2.40,
            "price": 2.40,
        }

        enforcer = ProtectionEnforcer(mock_client, SafetyConfig())
        mock_persistence = MagicMock()
        monitor = PositionProtectionMonitor(
            mock_client, registry, enforcer, persistence=mock_persistence,
        )

        results = await monitor.check_all_positions()

        # Should be treated as PROTECTED (stop filled = expected behavior)
        assert results.get("XRP/USD") is True, (
            f"Expected XRP/USD to be treated as protected, got: {results}"
        )

    @pytest.mark.asyncio
    async def test_stop_cancelled_still_treated_as_naked(self, mock_client, position):
        """
        If the stop was cancelled (not filled), it IS a genuinely naked position.
        """
        reset_position_registry()
        registry = get_position_registry()
        registry.register_position(position)

        # Exchange: position exists, no stop orders
        mock_client.get_futures_open_orders.return_value = []
        mock_client.get_all_futures_positions.return_value = [
            {"symbol": "PF_XRPUSD", "contracts": 100}
        ]
        # Stop was CANCELLED (not filled -- this is a real problem)
        mock_client.fetch_order.return_value = {
            "id": "stop-xrp-1",
            "status": "canceled",
            "filled": 0,
        }

        enforcer = ProtectionEnforcer(mock_client, SafetyConfig())
        monitor = PositionProtectionMonitor(mock_client, registry, enforcer)

        results = await monitor.check_all_positions()

        # Should be flagged as NAKED (genuinely dangerous)
        assert results.get("XRP/USD") is False, (
            f"Expected XRP/USD to be flagged as naked, got: {results}"
        )

    @pytest.mark.asyncio
    async def test_stop_fill_closes_position_gracefully(self, mock_client, position):
        """
        When a stop fill is confirmed, the position should be transitioned
        to CLOSED with exit_reason=STOP_LOSS.
        """
        reset_position_registry()
        registry = get_position_registry()
        registry.register_position(position)

        mock_client.get_futures_open_orders.return_value = []
        mock_client.get_all_futures_positions.return_value = [
            {"symbol": "PF_XRPUSD", "contracts": 100}
        ]
        mock_client.fetch_order.return_value = {
            "id": "stop-xrp-1",
            "status": "closed",
            "filled": 100,
            "average": 2.40,
            "price": 2.40,
        }

        enforcer = ProtectionEnforcer(mock_client, SafetyConfig())
        mock_persistence = MagicMock()
        monitor = PositionProtectionMonitor(
            mock_client, registry, enforcer, persistence=mock_persistence,
        )

        await monitor.check_all_positions()

        # Position should now be CLOSED
        from src.execution.position_state_machine import ExitReason
        assert position.state == PositionState.CLOSED
        assert position.exit_reason == ExitReason.STOP_LOSS
        assert len(position.exit_fills) == 1
        assert position.exit_fills[0].qty == Decimal("100")

        # Persistence should have been called
        mock_persistence.save_position.assert_called_once_with(position)

    @pytest.mark.asyncio
    async def test_position_closed_on_exchange_treated_as_protected(self, mock_client, position):
        """
        If exchange position size = 0 (stop filled and position fully closed),
        should be treated as protected (Layer 1 defense).
        """
        reset_position_registry()
        registry = get_position_registry()
        registry.register_position(position)

        mock_client.get_futures_open_orders.return_value = []
        mock_client.get_all_futures_positions.return_value = [
            {"symbol": "PF_XRPUSD", "contracts": 0}  # Position gone
        ]

        enforcer = ProtectionEnforcer(mock_client, SafetyConfig())
        monitor = PositionProtectionMonitor(mock_client, registry, enforcer)

        results = await monitor.check_all_positions()

        # Layer 1: position closed on exchange = protected
        assert results.get("XRP/USD") is True

    @pytest.mark.asyncio
    async def test_fetch_order_failure_treats_as_naked(self, mock_client, position):
        """
        If we can't verify the stop order status (API failure),
        treat as naked (fail closed / safe).
        """
        reset_position_registry()
        registry = get_position_registry()
        registry.register_position(position)

        mock_client.get_futures_open_orders.return_value = []
        mock_client.get_all_futures_positions.return_value = [
            {"symbol": "PF_XRPUSD", "contracts": 100}
        ]
        # fetch_order fails
        mock_client.fetch_order.side_effect = Exception("API timeout")

        enforcer = ProtectionEnforcer(mock_client, SafetyConfig())
        monitor = PositionProtectionMonitor(mock_client, registry, enforcer)

        results = await monitor.check_all_positions()

        # Should fail closed: treat as naked when we can't verify
        assert results.get("XRP/USD") is False


class TestStopOrderPolling:
    """
    Test: poll_and_process_order_updates now polls stop orders too.
    """

    @pytest.mark.asyncio
    async def test_stop_orders_are_polled(self):
        """
        Stop orders (STOP_INITIAL, STOP_UPDATE) should be included in polling,
        not just ENTRY orders.
        """
        from src.execution.execution_gateway import (
            ExecutionGateway,
            PendingOrder,
            OrderPurpose,
        )
        from src.domain.models import OrderType

        mock_client = AsyncMock()
        mock_registry = MagicMock()
        mock_persistence = MagicMock()

        gateway = ExecutionGateway.__new__(ExecutionGateway)
        gateway.client = mock_client
        gateway.registry = mock_registry
        gateway.persistence = mock_persistence
        gateway.metrics = {
            "orders_submitted": 0, "orders_filled": 0,
            "orders_cancelled": 0, "orders_rejected": 0,
            "events_processed": 0,
        }
        gateway._pending_orders = {}
        gateway._order_id_map = {}
        gateway._event_enforcer = None
        gateway._wal = None

        # Add a stop order to pending
        stop_pending = PendingOrder(
            client_order_id="stop-client-1",
            position_id="pos-btc",
            symbol="BTC/USD",
            purpose=OrderPurpose.STOP_INITIAL,
            side=Side.SHORT,
            size=Decimal("0.1"),
            price=Decimal("49000"),
            order_type=OrderType.STOP_LOSS,
            submitted_at=datetime.now(timezone.utc),
            exchange_order_id="stop-exch-1",
            status="submitted",
            exchange_symbol="BTC/USD:USD",
        )
        gateway._pending_orders["stop-client-1"] = stop_pending

        # Mock fetch_order to return stop as filled
        mock_client.fetch_order.return_value = {
            "id": "stop-exch-1",
            "clientOrderId": "stop-client-1",
            "status": "closed",
            "filled": 0.1,
            "remaining": 0,
            "average": 49000,
            "trades": [{"id": "trade-1"}],
        }

        # Mock position for process_order_update
        mock_pos = MagicMock()
        mock_pos.stop_order_id = "stop-exch-1"
        mock_pos.stop_client_order_id = "stop-client-1"
        mock_pos.entry_order_id = None
        mock_pos.pending_exit_order_id = None
        mock_pos.pending_exit_client_order_id = None
        mock_pos.side = Side.LONG
        mock_pos.remaining_qty = Decimal("0.1")
        mock_pos.is_terminal = False
        mock_pos.processed_event_hashes = set()
        mock_registry.get_position.return_value = mock_pos

        # The stop order should be polled (not skipped)
        mock_client.fetch_order.assert_not_called()
        processed = await gateway.poll_and_process_order_updates()

        # fetch_order should have been called for the stop order
        mock_client.fetch_order.assert_called_once_with("stop-exch-1", "BTC/USD:USD")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
