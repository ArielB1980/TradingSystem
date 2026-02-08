# Production Hardening Plan

## Summary

This document addresses 5 critical structural risks identified in the code review:

1. **Strategy logic implicitly coupled to execution** (HIGH PRIORITY)
2. **No hard "kill-switch invariant"** (HIGH PRIORITY)  
3. **Environment configuration risk** (MEDIUM PRIORITY)
4. **Rebalance/timing assumptions are fragile** (MEDIUM PRIORITY)
5. **Logging is good but not decision-complete** (LOW PRIORITY)

---

## Issue 1: Strategy-Execution Coupling

### Current State
- Strategy decisions sometimes assume execution success
- Execution adapters sometimes infer strategy intent
- Risk of position drift if orders fail, partial fills occur, or exchange behavior diverges

### Root Cause
Strategy produces signals, execution acts on them, but there's no **explicit reconciliation layer** that compares:
- `INTENDED_POSITION` (what strategy wants)
- `ACTUAL_POSITION` (what's on exchange)
- `DELTA` (what needs to change)

### Proposed Fix: Position Delta Reconciliation Layer

```python
@dataclass
class PositionDelta:
    """Represents the delta between intended and actual position."""
    symbol: str
    intended_side: Optional[Side]  # None = flat
    intended_size: Decimal
    actual_side: Optional[Side]
    actual_size: Decimal
    delta_size: Decimal  # +ve = need to buy, -ve = need to sell
    action: Literal["HOLD", "OPEN", "CLOSE", "ADJUST", "FLIP"]
    is_reconciled: bool  # True if intended == actual
    allowed: bool  # True if delta is within allowed thresholds
    rejection_reason: Optional[str]
```

**Files to Create/Modify:**
1. Create `src/reconciliation/position_delta.py` - Delta calculation engine
2. Modify `src/live/live_trading.py` - Add delta-based execution
3. Modify `src/execution/executor.py` - Only act on reconciled deltas

**Implementation:**
```python
class PositionDeltaReconciler:
    """
    Reconciles intended positions (from strategy) with actual positions (from exchange).
    
    Execution should ONLY act on reconciled deltas, never on raw strategy signals.
    """
    
    def __init__(self, config: Config):
        self.config = config
        self.allowed_delta_threshold = Decimal("0.05")  # 5% allowed drift
        
    async def calculate_delta(
        self,
        symbol: str,
        intended: Optional[PositionIntent],
        actual: Optional[ExchangePosition],
    ) -> PositionDelta:
        """
        Calculate the delta between intended and actual position.
        
        This is the ONLY input that execution should use.
        """
        # ... implementation
```

---

## Issue 2: No Hard Kill-Switch Invariant

### Current State
- Kill switch exists but is triggered by individual events
- No single authoritative stop condition
- Halting is implicit, not enforced

### Proposed Fix: Hard Invariant Monitor

**New Module:** `src/safety/invariant_monitor.py`

```python
@dataclass
class SystemInvariants:
    """Hard limits that trigger immediate system halt."""
    
    # Equity-based
    max_equity_drawdown_pct: Decimal = Decimal("0.20")  # 20% max drawdown
    min_equity_floor_usd: Optional[Decimal] = None  # Optional absolute floor
    
    # Exposure-based
    max_open_notional_usd: Decimal = Decimal("500000")  # Max total exposure
    max_concurrent_positions: int = 10
    max_margin_utilization_pct: Decimal = Decimal("0.85")
    
    # Operational
    max_rejected_orders_per_cycle: int = 5
    max_api_errors_per_minute: int = 10
    max_latency_ms: int = 5000
    
    # Position-specific
    max_single_position_pct_equity: Decimal = Decimal("0.25")  # 25% of equity in one position


class SystemState(str, Enum):
    """System operational state."""
    ACTIVE = "active"
    DEGRADED = "degraded"  # Some limits breached, reduce exposure
    HALTED = "halted"  # Trading stopped, active positions managed only
    EMERGENCY = "emergency"  # Flatten all positions immediately


class InvariantMonitor:
    """
    Central invariant enforcement.
    
    CRITICAL: This is the single source of truth for system health.
    All trading operations MUST check this before proceeding.
    """
    
    def __init__(
        self, 
        invariants: SystemInvariants,
        kill_switch: KillSwitch,
        config: Config,
    ):
        self.invariants = invariants
        self.kill_switch = kill_switch
        self.config = config
        self.state = SystemState.ACTIVE
        self.last_check = datetime.min.replace(tzinfo=timezone.utc)
        self.violations: List[InvariantViolation] = []
        
        # Counters
        self.rejected_orders_this_cycle = 0
        self.api_errors_this_minute: List[datetime] = []
        self.peak_equity: Optional[Decimal] = None
        
    async def check_all(
        self,
        current_equity: Decimal,
        open_positions: List[Position],
        margin_utilization: Decimal,
        available_margin: Decimal,
    ) -> SystemState:
        """
        Check all invariants and update system state.
        
        Returns current system state after checks.
        """
        violations = []
        
        # 1. Equity drawdown check
        if self.peak_equity is None:
            self.peak_equity = current_equity
        else:
            self.peak_equity = max(self.peak_equity, current_equity)
        
        drawdown_pct = (self.peak_equity - current_equity) / self.peak_equity if self.peak_equity > 0 else Decimal("0")
        if drawdown_pct > self.invariants.max_equity_drawdown_pct:
            violations.append(InvariantViolation(
                invariant="max_equity_drawdown_pct",
                threshold=str(self.invariants.max_equity_drawdown_pct),
                actual=str(drawdown_pct),
                severity="CRITICAL",
            ))
        
        # 2. Max notional check
        total_notional = sum(p.size_notional or Decimal("0") for p in open_positions)
        if total_notional > self.invariants.max_open_notional_usd:
            violations.append(InvariantViolation(
                invariant="max_open_notional_usd",
                threshold=str(self.invariants.max_open_notional_usd),
                actual=str(total_notional),
                severity="CRITICAL",
            ))
        
        # 3. Max concurrent positions
        if len(open_positions) > self.invariants.max_concurrent_positions:
            violations.append(InvariantViolation(
                invariant="max_concurrent_positions",
                threshold=str(self.invariants.max_concurrent_positions),
                actual=str(len(open_positions)),
                severity="WARNING",
            ))
        
        # 4. Margin utilization
        if margin_utilization > self.invariants.max_margin_utilization_pct:
            violations.append(InvariantViolation(
                invariant="max_margin_utilization_pct",
                threshold=str(self.invariants.max_margin_utilization_pct),
                actual=str(margin_utilization),
                severity="WARNING",
            ))
        
        # 5. Rejected orders check
        if self.rejected_orders_this_cycle > self.invariants.max_rejected_orders_per_cycle:
            violations.append(InvariantViolation(
                invariant="max_rejected_orders_per_cycle",
                threshold=str(self.invariants.max_rejected_orders_per_cycle),
                actual=str(self.rejected_orders_this_cycle),
                severity="WARNING",
            ))
        
        # 6. API errors check
        now = datetime.now(timezone.utc)
        self.api_errors_this_minute = [
            t for t in self.api_errors_this_minute
            if (now - t).total_seconds() < 60
        ]
        if len(self.api_errors_this_minute) > self.invariants.max_api_errors_per_minute:
            violations.append(InvariantViolation(
                invariant="max_api_errors_per_minute",
                threshold=str(self.invariants.max_api_errors_per_minute),
                actual=str(len(self.api_errors_this_minute)),
                severity="CRITICAL",
            ))
        
        # Determine new state based on violations
        self.violations = violations
        critical_count = sum(1 for v in violations if v.severity == "CRITICAL")
        warning_count = sum(1 for v in violations if v.severity == "WARNING")
        
        if critical_count > 0:
            self.state = SystemState.HALTED
            await self.kill_switch.activate(
                KillSwitchReason.MARGIN_CRITICAL,
                emergency=(critical_count >= 2)
            )
            logger.critical(
                "SYSTEM_HALTED",
                violations=[v.__dict__ for v in violations],
                critical_count=critical_count,
            )
        elif warning_count >= 2:
            self.state = SystemState.DEGRADED
            logger.warning(
                "SYSTEM_DEGRADED",
                violations=[v.__dict__ for v in violations],
                warning_count=warning_count,
            )
        else:
            self.state = SystemState.ACTIVE
        
        return self.state
    
    def record_order_rejection(self):
        """Record an order rejection for rate limiting."""
        self.rejected_orders_this_cycle += 1
        
    def record_api_error(self):
        """Record an API error for rate limiting."""
        self.api_errors_this_minute.append(datetime.now(timezone.utc))
        
    def reset_cycle_counters(self):
        """Reset per-cycle counters (call at end of each tick)."""
        self.rejected_orders_this_cycle = 0
    
    def is_trading_allowed(self) -> bool:
        """Check if new entries are allowed."""
        return self.state == SystemState.ACTIVE
    
    def is_management_allowed(self) -> bool:
        """Check if position management is allowed."""
        return self.state in (SystemState.ACTIVE, SystemState.DEGRADED)
```

---

## Issue 3: Environment Configuration Risk

### Current State
- No schema validation at startup
- No fail-fast if critical vars missing
- Risk of running in half-configured state

### Proposed Fix: Startup Configuration Validator

**Modify:** `src/config/config.py` - Add strict validation

```python
@dataclass
class StartupRequirement:
    """A required configuration item for startup."""
    name: str
    env_var: str
    required_in: List[str]  # ["prod", "paper", "dev"]
    validator: Optional[Callable[[str], Tuple[bool, str]]] = None
    
    
STARTUP_REQUIREMENTS = [
    StartupRequirement(
        name="Database URL",
        env_var="DATABASE_URL",
        required_in=["prod", "paper"],
        validator=lambda v: (v.startswith("postgres"), "Must be PostgreSQL URL"),
    ),
    StartupRequirement(
        name="Futures API Key",
        env_var="KRAKEN_FUTURES_API_KEY",
        required_in=["prod"],
        validator=lambda v: (len(v) > 20, "API key too short"),
    ),
    StartupRequirement(
        name="Futures API Secret",
        env_var="KRAKEN_FUTURES_API_SECRET",
        required_in=["prod"],
        validator=lambda v: (len(v) > 30, "API secret too short"),
    ),
    StartupRequirement(
        name="Environment",
        env_var="ENVIRONMENT",
        required_in=["prod", "paper", "dev"],
        validator=lambda v: (v in ("prod", "paper", "dev", "local"), "Must be prod/paper/dev/local"),
    ),
]


def validate_startup_requirements(environment: str) -> Tuple[bool, List[str]]:
    """
    Validate all startup requirements for the given environment.
    
    Returns:
        (success, list of error messages)
    """
    errors = []
    
    for req in STARTUP_REQUIREMENTS:
        if environment not in req.required_in:
            continue
            
        value = os.getenv(req.env_var)
        
        if value is None or value == "":
            errors.append(f"❌ {req.name} ({req.env_var}): MISSING - Required in {environment}")
            continue
            
        if req.validator:
            is_valid, error_msg = req.validator(value)
            if not is_valid:
                errors.append(f"❌ {req.name} ({req.env_var}): INVALID - {error_msg}")
    
    return (len(errors) == 0, errors)


def fail_fast_startup(environment: str):
    """
    Validate configuration and FAIL FAST if requirements not met.
    
    This should be called at the very start of the application.
    In production, missing configuration = immediate exit.
    """
    success, errors = validate_startup_requirements(environment)
    
    if not success:
        error_block = "\n".join(errors)
        logger.critical(
            "STARTUP_FAILED",
            environment=environment,
            error_count=len(errors),
            errors=errors,
        )
        
        if environment == "prod":
            # HARD FAILURE in production
            raise SystemExit(f"""
╔══════════════════════════════════════════════════════════════╗
║  FATAL: PRODUCTION STARTUP FAILED - MISSING CONFIGURATION   ║
╚══════════════════════════════════════════════════════════════╝

{error_block}

The system cannot start in production mode without these values.
Please configure the required environment variables and restart.

Environment: {environment}
""")
        else:
            # Warning in non-production
            logger.warning(
                "Configuration validation failed (non-production)",
                errors=errors
            )
```

---

## Issue 4: Rebalance/Timing Assumptions are Fragile

### Current State
- Assumes clean 5-minute cycles
- No clock skew handling
- No duplicate run detection

### Proposed Fix: Cycle Guard

**New Module:** `src/runtime/cycle_guard.py`

```python
@dataclass
class CycleState:
    """State of a trading cycle."""
    cycle_id: str
    started_at: datetime
    expected_end: datetime
    coins_processed: int
    signals_generated: int
    orders_placed: int
    is_complete: bool
    overlapped_previous: bool


class CycleGuard:
    """
    Guards against timing issues in the trading loop.
    
    Prevents:
    - Duplicate runs
    - Overlapping cycles
    - Partial data windows
    - Clock skew issues
    """
    
    def __init__(
        self,
        min_cycle_interval_seconds: int = 60,
        max_cycle_duration_seconds: int = 300,
        max_clock_skew_seconds: int = 30,
    ):
        self.min_interval = timedelta(seconds=min_cycle_interval_seconds)
        self.max_duration = timedelta(seconds=max_cycle_duration_seconds)
        self.max_clock_skew = timedelta(seconds=max_clock_skew_seconds)
        
        self.current_cycle: Optional[CycleState] = None
        self.last_completed_cycle: Optional[CycleState] = None
        self.cycle_history: List[CycleState] = []
        
        # Deduplication
        self._processed_candle_timestamps: Dict[str, datetime] = {}
        
    def start_cycle(self) -> Tuple[bool, Optional[str]]:
        """
        Attempt to start a new trading cycle.
        
        Returns:
            (success, error_reason)
        """
        now = datetime.now(timezone.utc)
        
        # Check for overlapping cycle
        if self.current_cycle and not self.current_cycle.is_complete:
            elapsed = now - self.current_cycle.started_at
            if elapsed < self.max_duration:
                return (False, f"OVERLAPPING_CYCLE: Previous cycle still running ({elapsed.total_seconds():.1f}s)")
            else:
                # Force-complete stale cycle
                logger.warning(
                    "Force-completing stale cycle",
                    cycle_id=self.current_cycle.cycle_id,
                    elapsed_seconds=elapsed.total_seconds(),
                )
                self.current_cycle.is_complete = True
                self.last_completed_cycle = self.current_cycle
        
        # Check minimum interval
        if self.last_completed_cycle:
            since_last = now - self.last_completed_cycle.started_at
            if since_last < self.min_interval:
                return (False, f"TOO_SOON: Only {since_last.total_seconds():.1f}s since last cycle")
        
        # Start new cycle
        cycle_id = f"cycle_{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        self.current_cycle = CycleState(
            cycle_id=cycle_id,
            started_at=now,
            expected_end=now + self.max_duration,
            coins_processed=0,
            signals_generated=0,
            orders_placed=0,
            is_complete=False,
            overlapped_previous=False,
        )
        
        logger.info("CYCLE_START", cycle_id=cycle_id)
        return (True, None)
    
    def end_cycle(self) -> CycleState:
        """
        End the current trading cycle.
        
        Returns:
            The completed cycle state
        """
        if not self.current_cycle:
            raise RuntimeError("No cycle to end")
        
        self.current_cycle.is_complete = True
        self.last_completed_cycle = self.current_cycle
        self.cycle_history.append(self.current_cycle)
        
        # Keep only last 100 cycles
        if len(self.cycle_history) > 100:
            self.cycle_history = self.cycle_history[-100:]
        
        elapsed = datetime.now(timezone.utc) - self.current_cycle.started_at
        logger.info(
            "CYCLE_END",
            cycle_id=self.current_cycle.cycle_id,
            duration_seconds=elapsed.total_seconds(),
            coins_processed=self.current_cycle.coins_processed,
            signals_generated=self.current_cycle.signals_generated,
            orders_placed=self.current_cycle.orders_placed,
        )
        
        return self.current_cycle
    
    def is_candle_fresh(
        self,
        symbol: str,
        candle_timestamp: datetime,
        max_age_seconds: int = 120,
    ) -> bool:
        """
        Check if a candle is fresh enough for decision making.
        
        Guards against:
        - Stale candles from API lag
        - Revised candles
        - Clock skew
        """
        now = datetime.now(timezone.utc)
        age = now - candle_timestamp
        
        if age > timedelta(seconds=max_age_seconds):
            logger.warning(
                "STALE_CANDLE",
                symbol=symbol,
                candle_timestamp=candle_timestamp.isoformat(),
                age_seconds=age.total_seconds(),
                max_age=max_age_seconds,
            )
            return False
        
        # Check for duplicate processing
        last_processed = self._processed_candle_timestamps.get(symbol)
        if last_processed and candle_timestamp <= last_processed:
            logger.debug(
                "DUPLICATE_CANDLE",
                symbol=symbol,
                candle_timestamp=candle_timestamp.isoformat(),
                last_processed=last_processed.isoformat(),
            )
            return False
        
        self._processed_candle_timestamps[symbol] = candle_timestamp
        return True
    
    def record_coin_processed(self):
        """Record that a coin was processed this cycle."""
        if self.current_cycle:
            self.current_cycle.coins_processed += 1
    
    def record_signal_generated(self):
        """Record that a signal was generated this cycle."""
        if self.current_cycle:
            self.current_cycle.signals_generated += 1
    
    def record_order_placed(self):
        """Record that an order was placed this cycle."""
        if self.current_cycle:
            self.current_cycle.orders_placed += 1
```

---

## Issue 5: Logging is Good but Not Decision-Complete

### Current State
- Logs events but not always decisions
- Can't always answer "Why did the system do this trade?"
- No signal snapshot, thresholds, or rejected alternatives

### Proposed Fix: Decision Audit Logger

**New Module:** `src/monitoring/decision_audit.py`

```python
@dataclass
class DecisionAudit:
    """Complete record of a trading decision."""
    
    timestamp: datetime
    symbol: str
    cycle_id: str
    
    # Signal snapshot
    signal_type: str
    signal_score: float
    signal_regime: str
    signal_reasoning: str
    
    # Thresholds applied
    thresholds: Dict[str, Any]
    
    # Alternatives considered
    alternatives_rejected: List[Dict[str, Any]]
    
    # Final decision
    decision: str  # "TRADE", "REJECT", "SKIP"
    decision_reason: str
    
    # Execution outcome (if TRADE)
    execution_result: Optional[str] = None
    order_id: Optional[str] = None
    fill_price: Optional[Decimal] = None
    
    # Position context
    existing_positions: List[str] = field(default_factory=list)
    equity_at_decision: Optional[Decimal] = None
    margin_available: Optional[Decimal] = None


class DecisionAuditLogger:
    """
    Logs complete decision context for post-mortems.
    
    Every trade (and non-trade) should be fully explainable.
    """
    
    def __init__(self, repository: Optional[Any] = None):
        self.repository = repository
        self._buffer: List[DecisionAudit] = []
        self._max_buffer_size = 100
        
    def record_decision(
        self,
        symbol: str,
        cycle_id: str,
        signal: Signal,
        thresholds: Dict[str, Any],
        alternatives: List[Dict[str, Any]],
        decision: str,
        reason: str,
        equity: Optional[Decimal] = None,
        margin: Optional[Decimal] = None,
        positions: Optional[List[str]] = None,
    ) -> DecisionAudit:
        """
        Record a complete trading decision.
        
        This should be called for EVERY signal, whether traded or not.
        """
        audit = DecisionAudit(
            timestamp=datetime.now(timezone.utc),
            symbol=symbol,
            cycle_id=cycle_id,
            signal_type=signal.signal_type.value if signal else "NO_SIGNAL",
            signal_score=float(sum(signal.score_breakdown.values())) if signal and signal.score_breakdown else 0.0,
            signal_regime=signal.regime if signal else "unknown",
            signal_reasoning=signal.reasoning if signal else "",
            thresholds=thresholds,
            alternatives_rejected=alternatives,
            decision=decision,
            decision_reason=reason,
            existing_positions=positions or [],
            equity_at_decision=equity,
            margin_available=margin,
        )
        
        self._buffer.append(audit)
        
        # Log structured decision
        logger.info(
            "DECISION_AUDIT",
            symbol=symbol,
            decision=decision,
            reason=reason,
            signal_type=audit.signal_type,
            signal_score=audit.signal_score,
            regime=audit.signal_regime,
            thresholds=thresholds,
            alternatives_count=len(alternatives),
            equity=str(equity) if equity else None,
            margin=str(margin) if margin else None,
            positions=positions,
        )
        
        # Flush if buffer is full
        if len(self._buffer) >= self._max_buffer_size:
            self._flush_buffer()
        
        return audit
    
    def update_execution_result(
        self,
        symbol: str,
        result: str,
        order_id: Optional[str] = None,
        fill_price: Optional[Decimal] = None,
    ):
        """Update the most recent decision with execution result."""
        for audit in reversed(self._buffer):
            if audit.symbol == symbol and audit.execution_result is None:
                audit.execution_result = result
                audit.order_id = order_id
                audit.fill_price = fill_price
                
                logger.info(
                    "DECISION_AUDIT_EXECUTION",
                    symbol=symbol,
                    result=result,
                    order_id=order_id,
                    fill_price=str(fill_price) if fill_price else None,
                )
                break
    
    def _flush_buffer(self):
        """Persist buffered audits to storage."""
        if self.repository and self._buffer:
            try:
                for audit in self._buffer:
                    record_event(
                        event_type="DECISION_AUDIT",
                        symbol=audit.symbol,
                        details=asdict(audit),
                        timestamp=audit.timestamp,
                    )
                self._buffer.clear()
            except Exception as e:
                logger.error("Failed to flush decision audits", error=str(e))
    
    def get_audit_summary(self, hours: int = 24) -> Dict[str, Any]:
        """Get summary of recent decisions for debugging."""
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        recent = [a for a in self._buffer if a.timestamp > since]
        
        return {
            "total_decisions": len(recent),
            "trades_executed": sum(1 for a in recent if a.decision == "TRADE"),
            "rejects": sum(1 for a in recent if a.decision == "REJECT"),
            "skips": sum(1 for a in recent if a.decision == "SKIP"),
            "by_symbol": {
                symbol: len([a for a in recent if a.symbol == symbol])
                for symbol in set(a.symbol for a in recent)
            },
            "common_reject_reasons": self._get_common_reasons(recent, "REJECT"),
        }
    
    def _get_common_reasons(self, audits: List[DecisionAudit], decision: str) -> Dict[str, int]:
        """Get common reasons for a decision type."""
        reasons: Dict[str, int] = {}
        for a in audits:
            if a.decision == decision:
                reasons[a.decision_reason] = reasons.get(a.decision_reason, 0) + 1
        return dict(sorted(reasons.items(), key=lambda x: -x[1])[:10])
```

---

## Implementation Phases

### Phase 1: Critical Safety (Week 1)
1. ✅ Implement `InvariantMonitor` with hard limits
2. ✅ Add `fail_fast_startup()` configuration validation
3. ✅ Integrate into main trading loop

### Phase 2: Execution Integrity (Week 2)
1. ✅ Implement `PositionDeltaReconciler`
2. ✅ Modify execution to act only on reconciled deltas
3. ✅ Add tests for position drift scenarios

### Phase 3: Operational Safety (Week 3)
1. ✅ Implement `CycleGuard` for timing protection
2. ✅ Add candle freshness checks
3. ✅ Guard against duplicate runs

### Phase 4: Observability (Week 4)
1. ✅ Implement `DecisionAuditLogger`
2. ✅ Add structured logging for all decisions
3. ✅ Create audit summary endpoints

---

## Testing Strategy

### Unit Tests
- Test each invariant violation triggers correct state
- Test delta calculation for all edge cases
- Test cycle guard overlap detection
- Test decision audit completeness

### Integration Tests
- Simulate partial fills and verify position drift handling
- Simulate API errors and verify invariant halting
- Simulate clock skew and verify cycle protection

### Production Validation
- Shadow mode: run new checks without enforcement for 7 days
- Compare intended vs actual positions
- Verify no false positives on invariant violations

---

## Files Created/Modified

### New Files
| File | Purpose |
|------|---------|
| `src/safety/__init__.py` | Safety module exports |
| `src/safety/invariant_monitor.py` | Hard safety limits enforcement |
| `src/safety/integration.py` | Unified integration layer |
| `src/runtime/cycle_guard.py` | Timing protection & cycle management |
| `src/reconciliation/position_delta.py` | Strategy-execution decoupling |
| `src/monitoring/decision_audit.py` | Decision-complete logging |
| `src/config/safety.yaml` | Safety thresholds configuration |
| `src/config/safety_config.py` | Configuration loader |

### Test Files
| File | Purpose |
|------|---------|
| `tests/test_invariant_monitor.py` | InvariantMonitor tests |
| `tests/test_cycle_guard.py` | CycleGuard tests |
| `tests/test_position_delta.py` | PositionDeltaReconciler tests |
| `tests/test_decision_audit.py` | DecisionAuditLogger tests |

### Modified Files
| File | Changes |
|------|---------|
| `src/live/live_trading.py` | Added ProductionHardeningLayer initialization, pre-tick checks, and post-tick cleanup |
| `src/config/config.py` | Added `fail_fast_startup()` function |
| `src/runtime/__init__.py` | Added CycleGuard exports |

---

## Rollout Checklist

- [x] All new modules implemented
- [x] InvariantMonitor with hard limits
- [x] CycleGuard for timing protection  
- [x] PositionDeltaReconciler for strategy-execution decoupling
- [x] DecisionAuditLogger for complete decision logging
- [x] ProductionHardeningLayer integration
- [x] Configuration file (`safety.yaml`) created
- [x] Comprehensive tests created
- [ ] Smoke test passes with new modules
- [ ] Shadow mode validation complete (7 days)
- [ ] Alert thresholds tuned based on shadow data
- [ ] Documentation updated
- [ ] Runbook created for invariant violations
- [ ] Team trained on new logging structure

---

## Usage

### Quick Start
```python
from src.safety.integration import init_hardening_layer

# In LiveTrading.__init__():
self.hardening = init_hardening_layer(config, kill_switch)

# In _tick():
if not await self.hardening.pre_tick_check(equity, positions, margin_util, margin):
    return  # System halted

# At end of _tick():
self.hardening.post_tick_cleanup()
```

### Checking System State
```python
# Check if new entries allowed
if self.hardening.is_trading_allowed():
    # Proceed with entry
    ...

# Check if position management allowed
if self.hardening.is_management_allowed():
    # Proceed with management
    ...

# Get status for debugging
status = self.hardening.get_status()
```

### Signal Reconciliation
```python
# Before executing any signal:
delta = self.hardening.reconcile_signal(signal, actual_position, size_notional, size_base)

if delta and delta.allowed and not delta.is_reconciled:
    # Execute the delta
    ...
    
    # Record the decision
    self.hardening.record_decision(symbol, signal, delta, "TRADE", "score_above_threshold")
    
    # After execution, record result
    self.hardening.record_execution_result(symbol, "FILLED", order_id, fill_price)
```
