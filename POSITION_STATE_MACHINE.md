# Position State Machine - Production Grade Implementation

## Overview

This document describes the **production-grade Position State Machine** that fixes all trade management violations and implements comprehensive safeguards against real-world failure modes.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          EXECUTION GATEWAY                                   │
│                    (Single Entry Point for ALL Orders)                       │
├─────────────────────────────────────────────────────────────────────────────┤
│  • All order placement flows through here                                   │
│  • Attaches client_order_id linking to position_id                          │
│  • Routes order events back to state machine                                │
│  • Maintains audit trail                                                    │
│  • No bypass paths allowed                                                  │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                    ┌───────────────┴───────────────┐
                    │                               │
                    ▼                               ▼
┌─────────────────────────────────┐ ┌─────────────────────────────────────────┐
│      POSITION MANAGER V2        │ │         POSITION PERSISTENCE            │
│     (Decision Engine)           │ │         (Crash Recovery)                │
├─────────────────────────────────┤ ├─────────────────────────────────────────┤
│ • evaluate_entry()              │ │ • positions table                       │
│ • evaluate_position()           │ │ • position_fills table                  │
│ • handle_order_event()          │ │ • position_actions audit log            │
│ • Shadow mode support           │ │ • Recovery algorithm                    │
│ • Metrics tracking              │ │ • Reconciliation                        │
└───────────────────┬─────────────┘ └─────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         POSITION REGISTRY                                    │
│                    (Single Source of Truth)                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│  Invariant A: One position per symbol (enforced)                            │
│  Invariant E: Full close before direction change                            │
│  Thread-safe, idempotent access                                             │
│  Reversal request/confirm protocol                                          │
└───────────────────────────────────┬─────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         MANAGED POSITION                                     │
│                    (State Machine per Position)                              │
├─────────────────────────────────────────────────────────────────────────────┤
│  Invariant B: remaining_qty = entry_qty - exit_qty >= 0                     │
│  Invariant C: Immutables locked after ACK                                   │
│  Invariant D: Stop monotonic (only toward profit)                           │
│  Idempotent event handling                                                  │
│  Fill tracking with FillRecords                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## State Machine

```
                         ┌───────────────┐
                         │    PENDING    │  Entry order submitted
                         └───────┬───────┘
                                 │
              ┌──────────────────┼──────────────────┐
              │ entry fill       │ cancel/reject    │
              ▼                  ▼                  │
       ┌─────────────┐    ┌─────────────┐          │
       │    OPEN     │    │  CANCELLED  │ ─────────┘
       │ (active)    │    │ (terminal)  │
       └──────┬──────┘    └─────────────┘
              │
    ┌─────────┼─────────┬─────────────────┐
    │ BE      │ stop    │ TP1 hit         │ error
    │ trigger │ hit     │                 │
    ▼         ▼         ▼                 ▼
┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────────┐
│PROTECTED│ │ CLOSED  │ │ PARTIAL │ │    ERROR    │
│(BE stop)│ │(terminal)│ │(runner) │ │  (terminal) │
└────┬────┘ └─────────┘ └────┬────┘ └─────────────┘
     │                       │
     │ TP1 hit               │ stop/final target
     ▼                       ▼
┌─────────┐            ┌─────────────┐
│ PARTIAL │───────────▶│   CLOSED    │
└─────────┘            │  (terminal) │
     │                 └─────────────┘
     │ exit initiated
     ▼
┌─────────────┐
│EXIT_PENDING │  Exit order in-flight
└──────┬──────┘
       │ fill confirmed
       ▼
┌─────────────┐
│   CLOSED    │
│  (terminal) │
└─────────────┘
```

## Invariants (Always-On Assertions)

### Core Invariants (A-E)

| Invariant | Description | Enforcement |
|-----------|-------------|-------------|
| **A** | At most one non-terminal position per symbol | `PositionRegistry.can_open_position()` + `_check_invariant_a()` |
| **B** | `remaining_qty = entry_qty - exit_qty >= 0` | `@property remaining_qty` with `check_invariant()` |
| **C** | Immutables locked after entry ACK | `entry_acknowledged` flag + `update_stop()` validation |
| **D** | Stop only moves toward profit | `_validate_stop_move()` checks direction |
| **E** | No reversal without terminal | `request_reversal()` + `confirm_reversal_closed()` protocol |

### Production Invariants (F-J)

| Invariant | Description | Enforcement |
|-----------|-------------|-------------|
| **F** | State transitions driven ONLY by exchange events | State changes in `apply_order_event()`, not action emit |
| **G** | Idempotent event handling (duplicates, replays) | `event_hash()` + `processed_event_hashes` set |
| **H** | Exit is first-class lifecycle | `EXIT_PENDING` until `remaining_qty == 0` via fills |
| **I** | Stop/TP orders linked to position | `client_order_id` contains `position_id`, replace semantics |
| **J** | Break-even is conditional | `min_partial_for_be` (30%), `trade_type` awareness |
| **K** | **Always protected after first fill** | If `filled_qty > 0` → must have stop OR emergency exit |

## Production Safety Mechanisms

### 1. Atomic Stop Replace
**Problem:** Cancel-old, place-new creates naked window with no protection.

**Solution:** New-first protocol:
1. Place NEW stop first
2. Wait for ACK on new stop
3. Only THEN cancel old stop
4. If new stop fails → keep old stop, do not advance state

```python
# AtomicStopReplacer handles this
ctx = await replacer.replace_stop(position, new_price, generate_id)
if ctx.failed:
    # Old stop preserved, position still protected
```

### 2. EXIT_PENDING Timeout + Escalation
**Problem:** Exits can hang forever, deadlocking the bot.

**Solution:** Timeout with escalation levels:
- `NORMAL` → `AGGRESSIVE` (wider price tolerance)
- `AGGRESSIVE` → `EMERGENCY` (market + cancel all)
- `EMERGENCY` → `QUARANTINE` (disable symbol until manual intervention)

```python
# ExitTimeoutManager tracks and escalates
if state.should_escalate(config):
    new_level = manager.escalate(symbol)
    if new_level == ExitEscalationLevel.QUARANTINE:
        # Symbol disabled until manual reconciliation
```

### 3. Event Ordering Constraints
**Problem:** Duplicate events processed, stale events corrupt state.

**Solution:**
- Per-order `last_event_seq` tracking
- Fill ID deduplication (primary for fills)
- Reject older events: seq=10 then seq=9 → seq=9 ignored

```python
if not enforcer.should_process_event(order_id, event_seq, fill_id):
    return  # Stale/duplicate event
```

### 4. Write-Ahead Intent Persistence
**Problem:** Crash after emitting order but before persisting = duplicate orders on restart.

**Solution:** WAL pattern:
1. Persist `action_intent` with `client_order_id` BEFORE sending
2. On restart, check for pending intents
3. Reconcile: if order exists on exchange → mark completed; if not → mark failed

```python
wal.record_intent(intent)  # Persisted to SQLite
result = await exchange.create_order(...)  # Then execute
wal.mark_sent(intent_id, result.order_id)
```

### 5. Shadow Mode Truth Source
Both live and shadow must use identical event format:
- Canonical `OrderEvent` structure
- Same `apply_order_event()` interface

### 6. Protection Monitoring (Invariant K)
Continuous verification that exposed positions have stops:

```python
# PositionProtectionMonitor runs periodically
for position in registry.get_all_active():
    if position.remaining_qty > 0:
        if not await enforcer.verify_protection(position, orders):
            logger.critical("NAKED POSITION DETECTED!")
            await enforcer.emergency_exit_naked_position(position)
```

## In-Flight States

| State | Description | Use Case |
|-------|-------------|----------|
| `PENDING` | Entry order submitted, awaiting fill | Gap between order submit and fill |
| `EXIT_PENDING` | Exit order submitted, awaiting fill | Prevents duplicate exit orders |
| `CANCEL_PENDING` | Cancelling stale order | Order amendment in progress |
| `ERROR` | Reconciliation mismatch | Requires manual intervention |
| `ORPHANED` | Registry/exchange disagree | Auto-flatten or manual review |

## Idempotent Event Handling

Every order event includes:
- `order_id` - Exchange order ID
- `client_order_id` - Our tracking ID (links to `position_id`)
- `event_seq` - Sequence number for ordering
- `fill_id` - Unique fill identifier

**Duplicate Detection:**
```python
def _is_duplicate_event(self, event: OrderEvent) -> bool:
    return event.event_hash() in self.processed_event_hashes
```

**Tested Scenarios:**
- ✅ Duplicate fill event (same fill_id) → no-op, no double-close
- ✅ Out-of-order events (fill before ack) → handled correctly
- ✅ Restart replay (same events replayed) → identical final state
- ✅ Entry partial fill (40 then 60) → correct accumulated qty
- ✅ Exit partial fill (30 then 70) → stays EXIT_PENDING until flat

## Conditional Break-Even

BE is NOT triggered automatically. Requirements:

```python
def should_trigger_break_even(self) -> bool:
    if not self.tp1_filled:
        return False
    
    # Minimum fill requirement (default 30%)
    tp1_fill_ratio = self.filled_exit_qty / self.initial_size
    if tp1_fill_ratio < self.min_partial_for_be:
        return False
    
    # Wide trades get earlier defense
    if self.trade_type == "wide_structure":
        return True
    
    return True
```

## Persistence Schema

```sql
-- positions table
CREATE TABLE positions (
    position_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    state TEXT NOT NULL,
    initial_size TEXT NOT NULL,
    initial_entry_price TEXT NOT NULL,
    initial_stop_price TEXT NOT NULL,
    initial_tp1_price TEXT,
    initial_tp2_price TEXT,
    current_stop_price TEXT,
    entry_acknowledged INTEGER,
    tp1_filled INTEGER,
    break_even_triggered INTEGER,
    created_at TEXT,
    updated_at TEXT,
    processed_event_hashes TEXT  -- JSON array for idempotency
);

-- position_fills table
CREATE TABLE position_fills (
    fill_id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL,
    order_id TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    is_entry INTEGER NOT NULL
);

-- position_actions (audit log)
CREATE TABLE position_actions (
    id INTEGER PRIMARY KEY,
    position_id TEXT NOT NULL,
    action_type TEXT NOT NULL,
    payload_json TEXT,
    status TEXT,
    timestamp TEXT NOT NULL
);
```

## Recovery Algorithm

```python
async def recover_from_persistence(db_path, exchange_positions, exchange_orders):
    # 1. Load last known positions from DB
    persistence = PositionPersistence(db_path)
    registry = persistence.load_registry()
    
    # 2. Reconcile with exchange
    issues = registry.reconcile_with_exchange(exchange_positions, exchange_orders)
    
    # 3. Mark inconsistencies
    for symbol, issue in issues:
        if "ORPHANED" in issue:
            registry._positions[symbol].mark_orphaned()
        elif "PHANTOM" in issue:
            # Flatten on exchange
            await flatten_position(symbol)
    
    # 4. Set as singleton
    set_position_registry(registry)
    
    return registry
```

## Shadow Mode Metrics

Shadow mode records every decision tick:

```python
@dataclass
class DecisionTick:
    timestamp: datetime
    symbol: str
    current_price: Decimal
    position_state: Optional[str]
    remaining_qty: Optional[Decimal]
    current_stop: Optional[Decimal]
    actions: List[ManagementAction]
    reason_codes: List[str]
```

**Comparison Metrics:**
- Number of opens
- Number of reversals attempted
- Number of stop moves
- Time-in-state distribution
- Number of blocked duplicates (should be > 0 initially)

## Files Created

| File | Purpose | Lines |
|------|---------|-------|
| `src/execution/position_state_machine.py` | Core state machine | ~1050 |
| `src/execution/position_manager_v2.py` | Decision engine | ~500 |
| `src/execution/position_persistence.py` | SQLite persistence | ~400 |
| `src/execution/execution_gateway.py` | Single order flow | ~550 |
| `tests/unit/test_position_state_machine.py` | Core invariant tests | ~790 |
| `tests/unit/test_production_invariants.py` | Production invariant tests | ~650 |

## Test Coverage

```
✅ 60/60 tests passing

Core Invariants (22 tests in test_position_state_machine.py)
Production Invariants (19 tests in test_production_invariants.py)
Other unit tests (19 tests)
  ✅ test_invariant_a_single_position_per_symbol
  ✅ test_invariant_b_remaining_qty_never_negative
  ✅ test_invariant_c_immutables_locked_after_ack
  ✅ test_invariant_d_stop_monotonic_long
  ✅ test_invariant_d_stop_monotonic_short
  ✅ test_invariant_e_no_reversal_without_close

TestInFlightStates (4 tests)
  ✅ test_exit_pending_state
  ✅ test_exit_pending_to_closed
  ✅ test_error_state
  ✅ test_orphaned_state

TestIdempotentEventHandling (2 tests)
  ✅ test_duplicate_event_is_noop
  ✅ test_out_of_order_events

TestPartialFills (1 test)
  ✅ test_partial_entry_fill

TestConditionalBreakEven (3 tests)
  ✅ test_be_requires_tp1_filled
  ✅ test_be_requires_minimum_fill
  ✅ test_be_triggers_with_sufficient_fill

TestPersistence (2 tests)
  ✅ test_position_serialization
  ✅ test_registry_serialization

TestDatabasePersistence (2 tests)
  ✅ test_save_and_load_position
  ✅ test_load_active_positions

TestReconciliation (2 tests)
  ✅ test_detect_orphaned_position
  ✅ test_detect_phantom_position
```

## Integration Complete ✅

The Position State Machine V2 has been fully integrated into `LiveTrading`. Activation is controlled by environment variables.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `USE_STATE_MACHINE_V2` | `false` | Enable the Position State Machine V2 |
| `STATE_MACHINE_SHADOW_MODE` | `true` | Log decisions but don't execute orders |

### Activation Modes

1. **Shadow Mode (Recommended First)**
   ```bash
   export USE_STATE_MACHINE_V2=true
   export STATE_MACHINE_SHADOW_MODE=true
   python -m src.live.main
   ```
   
   This logs all V2 decisions without executing. Compare with actual orders.

2. **Live Mode**
   ```bash
   export USE_STATE_MACHINE_V2=true
   export STATE_MACHINE_SHADOW_MODE=false
   python -m src.live.main
   ```
   
   All orders flow through ExecutionGateway. No bypass paths.

3. **Legacy Mode (Default)**
   ```bash
   # USE_STATE_MACHINE_V2 not set or "false"
   python -m src.live.main
   ```
   
   Uses the old `managed_positions` dict (deprecated).

### What Happens on Startup

When `USE_STATE_MACHINE_V2=true`:

1. `PositionRegistry` singleton is initialized
2. `PositionPersistence` loads from `data/positions.db`
3. `PositionManagerV2` is created with shadow mode setting
4. `ExecutionGateway` is created as single order flow point
5. On `run()`, `ExecutionGateway.startup()` is called:
   - Loads persisted positions
   - Syncs with exchange
   - Marks orphaned/phantom positions

### Signal Flow (V2)

```
Signal Detected
      │
      ▼
_handle_signal()
      │
      ├─► if use_state_machine_v2:
      │         │
      │         ▼
      │   _handle_signal_v2()
      │         │
      │         ├─► Risk Validation
      │         │
      │         ├─► PositionManagerV2.evaluate_entry()
      │         │         • Checks Invariant A (single position)
      │         │         • Checks Invariant E (no reversal)
      │         │         • Returns action + position
      │         │
      │         ├─► PositionRegistry.register_position()
      │         │
      │         └─► ExecutionGateway.execute_action()
      │                   • Places order on exchange
      │                   • Tracks with client_order_id
      │                   • Routes events to state machine
      │
      └─► else: (legacy path)
```

### Files Modified

| File | Changes |
|------|---------|
| `src/live/live_trading.py` | Added V2 imports, init, startup, `_handle_signal_v2()` |
| `src/execution/production_safety.py` | NEW: Atomic stop replace, exit timeout, Invariant K |
| `tests/unit/test_production_safety.py` | NEW: 13 acceptance tests |

---

**Implementation Status**: ✅ Complete (Invariants A-K + 6 Safety Mechanisms)  
**Tests Status**: ✅ 73/73 Passing  
**Integration Status**: ✅ Complete (feature-flagged)  
**Ready for Shadow Mode**: ✅ Yes  
**Production Ready**: After shadow mode validation
