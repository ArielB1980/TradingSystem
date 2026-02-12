# Session Knowledge: Lessons Learned & System Memory

**Last Updated:** 2026-02-11
**Covers Sessions:** 2026-02-06 through 2026-02-11

This file captures everything learned about this trading system across debugging, deployment, and operational sessions. It serves as institutional memory for any future AI agent or developer working on this codebase.

---

## 1. Production Infrastructure

### SSH Access
- **Server:** DigitalOcean droplet at `207.154.193.121`
- **SSH Key:** `~/.ssh/trading_droplet`
- **SSH User:** `root` (for SSH login), then `sudo -u trading` for all commands
- **Service:** `trading-bot.service` managed via `systemctl`
- **Code location on server:** `/home/trading/TradingSystem`
- **Logs on server:** `/home/trading/TradingSystem/logs/run.log`
- **Persisted state directory:** `/home/trading/.trading_system/` (halt_state.json lives here)
- **Kill switch state:** `/home/trading/TradingSystem/data/kill_switch_state.json`

### Deployment Workflow
```bash
# Full deploy sequence:
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  "cd /home/trading/TradingSystem && sudo -u trading git pull && systemctl restart trading-bot.service"

# Check logs after deploy:
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  "tail -100 /home/trading/TradingSystem/logs/run.log"
```

### Common Operational Commands
```bash
# Check service status
systemctl status trading-bot.service

# Restart the bot
systemctl restart trading-bot.service

# View recent logs
tail -500 /home/trading/TradingSystem/logs/run.log

# Check for errors
grep -iE '(error|exception|traceback|HALT|kill_switch)' /home/trading/TradingSystem/logs/run.log | tail -20

# Check active positions
grep 'Active Portfolio' /home/trading/TradingSystem/logs/run.log | tail -5

# Check cycle summaries (new format)
journalctl -u trading-bot.service --no-pager | grep CYCLE_SUMMARY | tail -5

# Check for recent alerts sent
journalctl -u trading-bot.service --no-pager | grep 'Alert\|send_alert' | tail -10
```

---

## 2. Architecture Overview

### Core Components & Data Flow
```
MarketRegistry (discovery) -> SMC Engine (signals) -> Auction Allocator (selection)
    -> Risk Manager (sizing/validation) -> Execution Gateway (orders)
    -> Position Manager V2 (state machine) -> Invariant Monitor (safety)
```

### Key Files
| File | Purpose |
|------|---------|
| `src/data/market_registry.py` | Market discovery, liquidity filtering, tier classification |
| `src/data/market_discovery.py` | Market discovery service (spot/futures pairing) |
| `src/data/symbol_utils.py` | **Single source of truth** for symbol normalization (6 functions) |
| `src/strategy/smc_engine.py` | SMC (Smart Money Concepts) signal generation |
| `src/live/live_trading.py` | Main loop + tick orchestration (1,640 lines, down from 4,367) |
| `src/live/protection_ops.py` | SL reconciliation, TP backfill, orphan cleanup |
| `src/live/signal_handler.py` | Signal processing (v1/v2 paths) |
| `src/live/auction_runner.py` | Auction-based allocation execution |
| `src/live/exchange_sync.py` | Position sync, account state, trade history |
| `src/live/health_monitor.py` | Order polling, protection checks, daily summary, auto-recovery |
| `src/live/coin_processor.py` | Market symbol filtering, universe discovery |
| `src/domain/protocols.py` | EventRecorder protocol for dependency inversion |
| `src/portfolio/auction_allocator.py` | Trade candidate selection and capital allocation |
| `src/risk/risk_manager.py` | Position sizing, risk validation, equity caps |
| `src/execution/execution_gateway.py` | Order submission, tracking, event handling |
| `src/execution/position_manager_v2.py` | Position state machine, reconciliation |
| `src/safety/invariant_monitor.py` | System safety invariants, halt/degraded triggers |
| `src/config/safety.yaml` | Safety thresholds configuration |
| `src/config/config.yaml` | Main system configuration |
| `src/monitoring/alerting.py` | Telegram/Discord webhook alerting module |
| `src/utils/kill_switch.py` | Kill switch with SL-preserving order cancellation |

### live_trading.py Decomposition (Session 3)
The monolithic `live_trading.py` was decomposed into 7 focused modules:
```
src/live/
  live_trading.py      -- Core loop + tick orchestration (1,640 lines)
  protection_ops.py    -- SL/TP reconciliation, orphan cleanup (841 lines)
  health_monitor.py    -- Monitoring: order polling, protection checks,
                          daily summary, auto-recovery (480 lines)
  auction_runner.py    -- Auction allocation execution (414 lines)
  exchange_sync.py     -- Position sync, account state, trade history (331 lines)
  signal_handler.py    -- Signal processing v1/v2 (249 lines)
  coin_processor.py    -- Symbol filtering, universe discovery (191 lines)
```
All extracted modules use a **delegate pattern**: functions receive the `LiveTrading` instance as their first argument (`lt`) to access shared state. The methods on `LiveTrading` remain as 2-3 line thin delegates for backward compatibility.

### MarketRegistry: Single Source of Truth
- `MarketRegistry` is THE authority for what can be traded and at what tier.
- **Pinned Tier A coins:** `{"BTC", "ETH", "SOL", "DOGE", "BNB"}` — these always bypass liquidity filters.
- Other coins must qualify dynamically via futures volume and spread metrics.
- Tiers: A (best liquidity), B (good), C (marginal).
- The old `CoinClassifier` in `src/data/coin_universe.py` was deleted; everything goes through `MarketRegistry` now.
- `CoinUniverseConfig` uses `candidate_symbols` (flat list). The old `liquidity_tiers` dict was removed from config.yaml on Feb 8.

---

## 3. Critical Bugs Fixed & Root Causes

### Bug 1: Kill Switch Cancelling SL/TP Orders
**Symptom:** User saw SL and TP orders appear on Kraken, then disappear seconds later.
**Root Cause:** Margin threshold mismatch. The auction allocator was allowed to use up to 90% margin (`auction_max_margin_util`), but the invariant monitor would trigger a CRITICAL halt at 85% (`max_margin_utilization_pct`). So any time the auction used its full budget, the invariant monitor immediately activated the kill switch, which cancels ALL open orders — including the SL/TP orders that were just placed.
**Fix:** Aligned thresholds:
- `max_margin_utilization_pct`: 0.85 -> **0.92** (above auction's 0.90)
- `degraded_margin_utilization_pct`: 0.70 -> **0.85** (warning at auction's target)

**Lesson:** Safety thresholds must be coordinated with operational thresholds. A safety gate set tighter than the operational limit guarantees false trips.

### Runner Logic & Capital Utilisation Fixes (2026-02-11)
**Scope:** ExecutionEngine, ManagedPosition, PositionManagerV2, RiskManager, AuctionAllocator.

**Changes:**
- **Decimal quantize**: Replaced `round(qty, 4)` with `Decimal.quantize(step_size, ROUND_DOWN)` in `_split_quantities` to avoid ConversionSyntax and TP backfill issues.
- **Snapshot targets**: Added `entry_size_initial`, `tp1_qty_target`, `tp2_qty_target` on ManagedPosition; set once on first entry fill; TP1/TP2 hit uses `min(target, remaining_qty)` instead of `remaining_qty * pct` to prevent partial sizing drift.
- **Trailing guard**: `trailing_active` set only when ATR >= `trailing_activation_atr_min` at TP1; RULE 9 relaxed to allow trailing when `(break_even_triggered or trailing_active)`.
- **Margin caps**: Replaced notional caps with margin caps (25% single, 200% aggregate vs 7x leverage); config `max_single_position_margin_pct_equity`, `max_aggregate_margin_pct_equity`.
- **Capital reallocation**: `on_partial_close` callback in ExecutionGateway; `auction_partial_close_cooldown_seconds` to skip new opens for N seconds after TP1/TP2 partial (default 0 = disabled).

**Invariant Review:** See `docs/RUNNER_CAPITAL_FIXES_INVARIANT_REVIEW.md`.

### Bug 2: Position Over-Sizing (Max Single Position % of Equity)
**Symptom:** Even after fixing margin thresholds, the system immediately entered DEGRADED then HALTED. Individual positions were 70-120% of equity (notional).
**Root Cause:** `max_single_position_pct_equity` (25%) was only a **post-trade warning** in the invariant monitor, NOT a **pre-trade gate** in the risk manager. The auction allocator would assign large `position_notional` values via `notional_override`, and the risk manager had no cap to prevent it.
**Fix:** Added pre-trade enforcement in `src/risk/risk_manager.py:validate_trade()`:
- Hard cap: `position_notional` clamped to 25% of account equity (notional basis)
- Applies to ALL sizing paths including auction `notional_override`
- Added `min_notional_viable` check ($10) after the cap to reject dust trades

**Lesson:** Every post-trade safety check should have a corresponding pre-trade gate. Otherwise the system will open positions it immediately considers violations.

### Bug 3: Phantom Position Auto-Flattening on Startup
**Symptom:** After deploying fixes, the bot was market-closing real open positions (ETH, ZEC, RENDER, ENS) on startup.
**Root Cause:** `ExecutionGateway.sync_with_exchange()` runs BEFORE `production_takeover`. If the persisted position registry was stale or incomplete, real exchange positions were classified as "PHANTOM" by `position_manager_v2.reconcile()`, which generated `FLATTEN_ORPHAN` actions. The gateway immediately executed these, destroying real positions.
**Fix:** Changed `reconcile()` in `position_manager_v2.py` to emit `NO_ACTION` (with a warning log) instead of `FLATTEN_ORPHAN` for PHANTOM positions. Phantoms are now deferred to `_import_phantom_positions()` and `production_takeover` for safe import.

**Lesson:** Reconciliation must never take destructive action on positions it doesn't fully understand. "I see something unexpected" should default to "log and defer," not "destroy it." This is especially critical during startup when internal state may be incomplete.

### Bug 4: `flatten_orphan` Crash (NoneType.value)
**Symptom:** Repeated "Execution failed" errors for `action_type=flatten_orphan` with `AttributeError: 'NoneType' object has no attribute 'value'`.
**Root Cause:** `_wal_record_intent` in `execution_gateway.py` accessed `action.side.value`, but `FLATTEN_ORPHAN` actions don't have a `side` (it's None).
**Fix:** Added null check: `action.side.value if action.side else "unknown"`.

**Lesson:** Any field access on an action/event should handle None gracefully. Different action types populate different fields.

### Bug 5: TP Backfill Rejecting All TPs If One Is Invalid
**Symptom:** Take-profit orders not being placed even when some TP levels were valid.
**Root Cause:** `_compute_tp_plan` would reject the ENTIRE TP plan if TP1 was "too close" to current price. In fast-moving markets, TP1 might already be passed while TP2/TP3 are still valid.
**Fix:** Changed to individually filter each TP level. Invalid TPs are skipped with a warning; remaining valid TPs are still placed. Only returns None if ALL TPs are invalid.

**Lesson:** Don't let one bad element invalidate an entire batch. Filter individually when items are independent.

### Bug 6: PENDING Position Race Condition (Compounding Entries)
**Symptom:** SPX/USD accumulated 5257 units (434% of equity) from a single ~$52 intended trade. Kill switch fired, cancelling ALL orders including other positions' SL/TP.
**Root Cause:** Race condition between market order fills and `sync_with_exchange`. When a market order fills instantly, the position is still PENDING (remaining_qty=0) in the registry. `reconcile_with_exchange` sees `registry qty=0, exchange qty=174` and classifies it as `STALE_ZERO_QTY`, archiving the position. The registry loses track. Next cycle, a new signal fires, `can_open_position` says "no existing position" -> approved -> another entry. This repeats every 2 minutes, compounding exposure until the invariant monitor halts.
**Fix (two layers):**
- Layer 1: PENDING positions with zero remaining_qty are no longer archived as STALE_ZERO_QTY. Instead, the exchange quantity is adopted via a synthetic fill, transitioning the position to OPEN.
- Layer 2: Defense-in-depth `_known_exchange_symbols` set in `can_open_position()`. Even if the registry loses a position, the guard checks exchange positions (updated each reconciliation) and blocks new entries for symbols with live exposure.

**Lesson:** Reconciliation logic must be state-aware. A position in PENDING state with zero qty is expected (awaiting fill), not stale. Never make destructive decisions about in-flight positions.

### Bug 7: SL Cancel NotFound Aborting New SL Placement
**Symptom:** After a kill switch recovery, positions had no stop loss on the exchange. The protection monitor (Invariant K) detected "naked positions" and fired another emergency kill switch, closing all positions.
**Root Cause:** In `executor.py:update_protective_orders()`, the old SL cancel and new SL placement were in the same try block. When the old SL cancel threw `notFound` (because the kill switch had already cancelled it), the entire block aborted. The new SL was never placed, leaving the position naked.
**Fix (two layers):**
- Layer 1: Separated SL cancel and SL place into independent try blocks. If the cancel fails (e.g., old order already gone), it's logged as a warning and the new SL placement proceeds anyway.
- Layer 2: Protection monitor now has a 90-second startup grace period and requires 2 consecutive naked detections before triggering emergency kill switch. This gives the tick loop time to place missing stops after restart.

**Lesson:** Never put "cancel old" and "place new" in the same try block. A cancel failure for a stale order is informational, not fatal. The placement must always proceed.

### Bug 8: PositionState.PENDING_PROTECTION Doesn't Exist
**Symptom:** Phantom position import crashed on startup with `AttributeError: type object 'PositionState' has no attribute 'PENDING_PROTECTION'`.
**Root Cause:** The phantom import code referenced a state enum value that was never added.
**Fix:** Changed to `PositionState.OPEN` (position exists but may not have stop confirmed yet — the protection monitor handles this).

**Lesson:** Never reference enum values without verifying they exist. This is a compile-time check in typed languages but a runtime crash in Python.

### Bug 9: Aggregate Notional Cap Using Wrong Attribute Name
**Symptom:** `'Position' object has no attribute 'mark_price'` in auction candidate creation (logged as "Failed to create candidate signal for auction").
**Root Cause:** The aggregate notional cap code (added in the same session) referenced `p.mark_price` but the Position dataclass field is `current_mark_price`.
**Fix:** Changed `p.mark_price` to `p.current_mark_price` in `src/risk/risk_manager.py`.

**Lesson:** Always verify attribute names against the dataclass/model definition. Python won't catch typos until runtime. The `Position` class uses `current_mark_price`, not `mark_price`.

### Bug 10: CYCLE_SUMMARY Silent Failures (Three Successive Errors)
**Symptom:** CYCLE_SUMMARY never appeared in logs after deployment.
**Root Cause (3 cascading issues):**
1. `self.hardening._current_state` doesn't exist — the state is on `self.hardening.invariant_monitor.state`
2. `self.execution_gateway.position_state_machine` doesn't exist — the attribute is `self.execution_gateway.registry`
3. `PositionRegistry` doesn't have `get_all_positions()` — the method is `get_all_active()`

All three were silently swallowed by `except Exception: pass`. Changed to `except Exception as e: logger.warning(...)` to make future failures visible.

**Fix:** Corrected all three attribute/method names. Changed silent exception swallowing to logged warnings.

**Lesson:** 
- Never use bare `except: pass` — always log the error. Silent failures are the hardest bugs to find.
- When referencing attributes across module boundaries (e.g., `gateway.registry.get_all_active()`), verify each link in the chain against the actual class definition.
- The `PositionRegistry` uses `get_all_active()` (active positions only) and `get_all()` (all including terminal).

---

## 3b. Safety & Observability Features Added (2026-02-08 Session)

### Feature 1: Kill Switch Preserves SL Orders
**File:** `src/utils/kill_switch.py`
**Change:** The `activate()` method no longer calls `cancel_all_orders()`. Instead, it fetches all open orders, inspects each one, and only cancels non-SL orders. Stop-loss orders (identified by type containing "stop" + `reduceOnly=True`) are preserved.
**Why:** Cancelling SL orders was the #1 cause of losses after kill switch events — positions were left naked and could not recover.
**Log patterns:** `Kill switch: PRESERVING stop loss order` (good), `Kill switch: Order cleanup complete, preserved_stop_losses=N` (good).

### Feature 2: Signal Cooldown (4h per symbol)
**File:** `src/live/live_trading.py`
**Change:** `self._signal_cooldown: Dict[str, datetime]` tracks when each symbol last signalled. After a signal fires, the same symbol is suppressed for 4 hours (`self._signal_cooldown_hours = 4`).
**Why:** The SPX compounding bug (Bug 6) was caused partly by the same signal firing every 2 minutes. Even with the state machine fix, this is belt-and-suspenders.
**CYCLE_SUMMARY shows:** `cooldowns_active=N` — number of symbols currently in cooldown.

### Feature 3: Aggregate Notional Cap (200% of equity)
**File:** `src/risk/risk_manager.py`
**Change:** Before the per-trade margin check, the total existing notional across all positions is computed. If adding the new position would exceed 200% of equity, the position is either capped to the remaining headroom or rejected entirely.
**Why:** Even with individual position caps (25% each), you could still end up with 8 positions * 25% = 200% total exposure. The aggregate cap provides a hard ceiling.
**Important:** Uses `p.current_mark_price` (NOT `p.mark_price` — see Bug 9).

### Feature 4: Telegram/Discord Alerting
**File:** `src/monitoring/alerting.py` (new)
**Config:** `ALERT_WEBHOOK_URL` and `ALERT_CHAT_ID` environment variables.
**Events alerting on:**
- `KILL_SWITCH` — kill switch activated (urgent)
- `SYSTEM_HALTED` — invariant monitor halted the system (urgent)
- `NEW_POSITION` — new position opened
- `POSITION_CLOSED` — position closed with P&L, exit reason, duration
- `AUTO_RECOVERY` — system auto-recovered from margin halt (urgent)
- `DAILY_SUMMARY` — sent at midnight UTC with equity, P&L, trades, positions
**Rate limiting:** 1 alert per event type per 5 minutes (bypass with `urgent=True`).
**Telegram bot:** `@My_KBot_bot`, token in server `.env` file.
**Chat ID:** `14159355` (user's Telegram numeric ID).

### Feature 5: Cycle Summary Log Line
**File:** `src/live/live_trading.py`
**Change:** After each tick (post-reconciliation, pre-sleep), emits a single `CYCLE_SUMMARY` log line with:
- `cycle`: loop iteration count
- `duration_ms`: tick duration in milliseconds
- `positions`: number of active positions (via `registry.get_all_active()`)
- `universe`: number of coins in the market universe
- `system_state`: NORMAL / DEGRADED / KILL_SWITCH
- `cooldowns_active`: number of signal cooldowns active
**Log pattern:** `CYCLE_SUMMARY cycle=5 duration_ms=32000 positions=1 system_state=NORMAL universe=49`

### Feature 6: Auto Halt Recovery
**File:** `src/live/live_trading.py` (method `_try_auto_recovery`)
**Rules (ALL must be true):**
1. Kill switch reason is `MARGIN_CRITICAL` (the most common false-positive)
2. At least 5 minutes since the halt was activated
3. Current margin utilization is below 85% (well below the 92% trigger)
4. Fewer than 2 auto-recoveries in the last 24 hours
**Why:** Before this, `margin_critical` halts required SSH access to manually delete state files. With SL orders now preserved during halts, margin naturally recovers as positions hit SL or price moves. The system can safely resume without human intervention.
**Safety:** The 2/day limit means if it keeps halting, something is genuinely wrong and human intervention is needed.

### Feature 7: Concurrency Increase (Semaphore 20 → 50)
**File:** `src/live/live_trading.py`
**Change:** `asyncio.Semaphore(20)` → `asyncio.Semaphore(50)` for the parallel coin analysis loop.
**Why:** Most time in the tick is I/O-bound (waiting on Kraken API responses for candle data). Higher concurrency means more coins fetched in parallel, reducing cycle time from ~66s toward ~20-30s.
**Risk:** Minimal — Kraken's rate limits are per-API-key, not per-connection. 50 concurrent I/O tasks is well within normal async Python capacity.

### Feature 8: Daily P&L Summary (Telegram, Midnight UTC)
**File:** `src/live/live_trading.py` (method `_run_daily_summary`)
**Change:** Background task that sleeps until midnight UTC, then sends a Telegram summary with: equity, margin used %, trades in last 24h, win/loss count, win rate, daily P&L, and list of open positions with unrealized P&L.
**Why:** Gives the user a daily health check without needing to SSH into the server.

### Feature 9: Position Close Alerts
**File:** `src/live/live_trading.py` (in `_save_trade_history`)
**Change:** After saving a closed trade to the database, sends a Telegram alert with: symbol, side, entry/exit prices, net P&L, exit reason, and holding duration.
**Why:** Full trade lifecycle visibility on phone — know immediately when a position closes and whether it was profitable.

---

## 4. Safety System Architecture

### Invariant Thresholds (from safety.yaml)
| Parameter | Value | Effect |
|-----------|-------|--------|
| `max_margin_utilization_pct` | 0.92 | HALT (kill switch) |
| `degraded_margin_utilization_pct` | 0.85 | DEGRADED (no new entries) |
| `max_single_position_pct_equity` | 0.25 | Warning (also pre-trade gated in risk_manager) |
| `max_equity_drawdown_pct` | 0.15 | HALT |
| `degraded_equity_drawdown_pct` | 0.10 | DEGRADED |
| `max_concurrent_positions` | 10 | HALT |
| `auction_max_margin_util` (in RiskConfig) | 0.90 | Auction budget ceiling |

### Threshold Relationship (CRITICAL to maintain)
```
auction_max_margin_util (0.90) < max_margin_utilization_pct (0.92)
                                < 1.0
degraded_margin_utilization_pct (0.85) <= auction_max_margin_util (0.90)
```
If `max_margin_utilization_pct` is ever set BELOW `auction_max_margin_util`, the kill switch will fire every time the auction fills its budget. This was the original bug.

### State Transitions
```
NORMAL -> DEGRADED -> HALTED
  |                      |
  +--- kill switch ------+
                         |
  +--- auto-recovery ----+ (margin_critical only, max 2/day)
```
- **DEGRADED:** No new entries, existing positions managed normally
- **HALTED:** Kill switch activates, non-SL orders cancelled, no trading
- **Kill switch now PRESERVES stop-loss orders** (only cancels entries and TPs)
- **Persisted halt state:** `/home/trading/.trading_system/halt_state.json`
- **Kill switch state:** `/home/trading/TradingSystem/data/kill_switch_state.json`
- **Auto-recovery:** For `margin_critical` halts only — clears automatically when margin drops below 85%, after 5min cooldown, max 2x/day. All other halt reasons still require manual intervention.

---

## 5. SMC Strategy Behavior

### Signal Pipeline (why trades are rare)
The SMC engine is intentionally very selective. A trade must pass ALL gates:

1. **Daily Bias:** Price must be above/below 200 EMA on the 1D timeframe (directional filter)
2. **4H Decision Structure:** Must have a valid break of structure (BOS) or change of character (CHoCH) on the 4H timeframe. **This is the #1 rejection reason** (`4H_STRUCTURE_REQUIRED`).
3. **Market Regime Classification:** Structure must classify into a tradeable regime (e.g., `tight_smc`)
4. **OTE/Key Fibonacci Gate:** Entry price must be within the Optimal Trade Entry zone (0.618-0.786 Fibonacci retracement). **This is the #2 rejection reason** (`entry not in OTE/Key Fib`).
5. **Risk Validation:** Position must pass sizing, margin, and concentration checks.

### What "No Trades Today" Looks Like
In a strong bearish trend (as of 2026-02-07):
- All 56 coins show bearish bias (correct)
- Most coins lack 4H structure — they're either ranging or haven't formed a clear BOS/CHoCH
- A few coins form structure but price hasn't pulled back to OTE — the strategy won't chase
- **Result:** 0 signals generated across hundreds of cycles. This is by design.

### If the User Wants More Trades
Options to discuss (in order of impact):
1. Relax the OTE/Fibonacci gate (allow entries slightly outside optimal zone)
2. Accept 1H structures in addition to 4H
3. Add a momentum/breakdown strategy alongside SMC
4. Widen tight_smc regime criteria

---

## 6. Account State (as of 2026-02-08 session 2)

- **Equity:** ~$345.75
- **Open Positions:** 2 (PF_PAXGUSD LONG, PF_BNBUSD SHORT) — both protected with SL orders
- **System State:** NORMAL
- **Coins Scanned:** 45 per cycle (universe dynamically filtered from 246 candidates)
- **Cycle Interval:** ~62s normal, ~116s reconciliation tick (every ~120s)
- **Telegram Bot:** `@My_KBot_bot` connected — push alerts + interactive commands (`/status`, `/positions`, `/trades`, `/help`)
- **Discovery refresh:** Every 4 hours (was 24h)
- **Safety additions:** Kill switch preserves SLs, signal cooldown (4h), aggregate notional cap (200%), auto halt recovery (margin_critical only, 2x/day max), pre-entry spread check (fail-open), universe shrink protection, daily loss enforcement
- **Database:** PostgreSQL for trades/events/candles, SQLite for position state machine
- **Python venv on server:** `/home/trading/TradingSystem/venv/bin/python3`

---

## 7. Operational Gotchas

### Startup Order Matters
`sync_with_exchange()` runs BEFORE `production_takeover`. If the position registry is stale, this can misclassify real positions. The fix (Bug 3 above) prevents destructive action, but be aware of this ordering.

### Persisted State Files Can Block Startup
If the bot previously halted, `halt_state.json` and/or `kill_switch_state.json` must be manually removed before the bot will trade again. These don't auto-clear.

### Config Changes Need Coordinated Updates
Some config values are defined in BOTH `safety.yaml` AND as defaults in `invariant_monitor.py`. When changing thresholds, update BOTH to avoid confusion (the yaml is loaded at runtime, but the Python defaults serve as fallback documentation).

### The Auction Allocator Respects Margin Budget
The auction won't open trades beyond `auction_max_margin_util` (90%). But the risk manager now also caps individual positions at 25% of equity. Both gates must be satisfied.

### TP/SL Lifecycle
1. Entry order filled -> `PositionManagerV2.handle_order_event()` triggers `PLACE_STOP` and `PLACE_TP` actions
2. `ExecutionGateway` executes these actions
3. TP orders are stored as `tp1_order_id`, `tp2_order_id` on the position record
4. TP backfill runs periodically to catch any missed TPs
5. If the kill switch fires, SL orders are **PRESERVED** (only entries and TPs cancelled). This is a critical safety improvement from the 2026-02-08 session.
6. After restart: `_place_missing_stops_for_unprotected` auto-places new stops, but needs ~60-90s to run through a full tick cycle. The protection monitor has a 90s grace period to allow this.
7. For `margin_critical` halts: auto-recovery clears the halt when margin drops below 85% (max 2x/day, 5min cooldown). Positions stay protected by their SL orders throughout.

### After a Kill Switch Recovery
When recovering from a kill switch:
1. All SL/TP order IDs in the registry are now stale (the kill switch cancelled them)
2. On restart, the SL update will try to cancel old IDs -> `notFound` -> this is normal and handled gracefully
3. New SL/TP orders are placed automatically by the tick cycle and TP backfill
4. The protection monitor waits 90s before enforcing, giving time for new stops to be placed
5. If recovery fails, manually run `make place-missing-stops-live` to protect all positions

### Log Patterns to Watch For
| Pattern | Meaning |
|---------|---------|
| `Kill switch active` | System halted, all orders cancelled |
| `DEGRADED` | System in warning state, no new entries |
| `PHANTOM` | Exchange has a position the bot doesn't know about |
| `ORPHAN` | Bot thinks it has a position that doesn't exist on exchange |
| `PENDING_ADOPTED` | Race condition fixed: PENDING position adopted exchange qty (Bug 6 fix) |
| `Exchange has live exposure...Blocking` | Defense guard prevented duplicate entry (Bug 6 fix) |
| `Old SL cancel failed (proceeding to place new SL)` | Normal after kill switch recovery (Bug 7 fix) |
| `NAKED_POSITIONS_DETECTED (first occurrence)` | Warning, monitor giving time to self-heal |
| `NAKED_POSITIONS_DETECTED (persistent)` | CRITICAL — will trigger emergency kill switch |
| `4H_STRUCTURE_REQUIRED` | Normal — strategy filtering, not an error |
| `entry not in OTE/Key Fib` | Normal — strategy filtering, not an error |
| `Execution failed` | Actual error — investigate immediately |
| `flatten_orphan` | Was dangerous (Bug 3), now deferred safely |
| `Tier A coin bypassing filters` | Normal — pinned majors skipping liquidity checks |
| `CYCLE_SUMMARY` | Per-tick health line — positions, state, duration, cooldowns |
| `CYCLE_SUMMARY_FAILED` | Summary computation error — check the logged error message |
| `Kill switch: PRESERVING stop loss order` | Good — SL kept alive during kill switch |
| `Kill switch: Order cleanup complete` | Shows cancelled vs preserved SL counts |
| `AUTO_RECOVERY: Clearing kill switch` | Auto-recovery succeeded (margin dropped below 85%) |
| `Auto-recovery: daily limit reached` | System needs manual intervention now |
| `Capping position notional by aggregate exposure limit` | Aggregate 200% cap engaged |
| `Alert (no webhook configured)` | ALERT_WEBHOOK_URL env var not set on server |
| `UNIVERSE_SHRINK_REJECTED` | Discovery returned <50% of last discovery — kept old universe |
| `SYMBOL_ADDED` / `SYMBOL_REMOVED` | Coin entered/left the discovered universe |
| `SIGNAL_REJECTED_SPREAD` | Live spread too wide for entry (>1.0%) — signal skipped |
| `Daily loss tracking initialized` | Starting equity recorded for daily P&L tracking |
| `DAILY_LOSS_WARNING` | Daily loss approaching or exceeding limit |

---

## 8. Git & Branch Conventions

- **Production branch:** `main`
- **Deploy:** Push to `main`, then SSH pull + systemctl restart
- **Never force push to main**
- **Secrets are in `.env.local` (never committed)**
- `.gitignore` covers: `.env.local`, `.local/`, `logs/`, `.venv/`

---

## 9. Development Environment

- **Python entrypoint:** `run.py`
- **Makefile targets:** `make venv`, `make install`, `make run`, `make smoke`, `make logs`, `make smoke-logs`, `make lint`, `make format`
- **Linter/Formatter:** `ruff` (installed in venv)
- **Config loading:** `python-dotenv` loads `.env.local` when `ENV=local` or `.env.local` exists
- **Template:** `.env.local.example` has all required env vars with placeholders
- **Local DB default:** `sqlite:///./.local/app.db` when `DATABASE_URL` is missing in local mode

---

## 10. Things That Look Like Bugs But Aren't

1. **"0 signals generated" for hours** — This is the SMC strategy being selective. Check that cycles are running and coins are being scanned. If `coins_processed` > 0 and you see `4H_STRUCTURE_REQUIRED`, the system is working correctly.

2. **"Tier A coin bypassing filters (trusted major)"** — This warning is expected for BTC, ETH, SOL, DOGE, BNB. They skip volume/spread checks by design.

3. **"OHLCV timeout" for a single coin** — Transient API timeouts are harmless if they're isolated. Only investigate if the same coin fails repeatedly or multiple coins timeout simultaneously.

4. **"coins_processed=0" in CYCLE_END** — This can happen when the cycle only does portfolio management (sync, reconciliation) without processing new signals. It's normal during quiet periods.

5. **Auction selecting "winners_selected=1"** with only 1 contender — When there's only one open position and no new signals, the auction just reaffirms the existing position. Not an error.

6. **`cooldowns_active=5` in CYCLE_SUMMARY** — This means 5 symbols fired signals in the last 4 hours and are in cooldown. It does NOT mean the system is blocked from trading — only those specific symbols are temporarily suppressed.

7. **No Telegram alerts for a while** — Rate limiting suppresses duplicate event types for 5 minutes. If the system is in a stable state, you won't get alerts (that's good). The daily summary at midnight UTC is always sent regardless.

8. **`Kill switch: PRESERVING stop loss order`** during a kill switch event — This is intentional and correct. SL orders are protective and should never be cancelled by the kill switch.

---

## 11. Trading Performance (as of Feb 8 2026)

**Closed trades: 4** (all LTC/USD SHORT, all stopped out on Feb 2)
- Win rate: 0/4 (0%)
- Total P&L: -$1.80
- Average loss: -$0.45 per trade

**Signal distribution (last 24h):**
- 12,115 NO_SIGNAL decisions (vast majority — strategy is very selective)
- 137 LONG signals generated
- 60 SHORT signals generated
- Most rejections are `4H_STRUCTURE_REQUIRED` — no valid order block on the 4H decision timeframe

**Currently open:**
- PF_PAXGUSD LONG (entry $4979.60, SL $4880.00) — opened Feb 7
- PF_BNBUSD SHORT (entry $647.35, SL $660.30) — opened Feb 8

**Key insight:** The SMC 4H decision authority is extremely selective. With 45 coins and ~22 cycles/hour, the system evaluates ~990 coin-analyses/hour but generates only ~8 signals/hour. Most coins don't have valid 4H structure. This is by design (78.9% backtest win rate) but means the system needs time to prove itself.

**Database:** Trade history in PostgreSQL (`kraken_futures_trading`), position state machine in SQLite (`data/positions.db`).

---

## 12. Telegram Bot Commands

The system runs an interactive Telegram bot alongside the alert system. It polls for messages every 5 seconds from the authorized `ALERT_CHAT_ID`.

| Command | Aliases | Description |
|---------|---------|-------------|
| `/status` | `/s` | Equity, margin %, unrealized P&L, system state, positions count, universe size, cycle count, cooldowns |
| `/positions` | `/pos`, `/p` | Detailed list of open positions with entry/mark price, size, leverage, P&L ($, %) |
| `/trades` | `/t` | Last 5 closed trades with P&L, duration, exit reason, win summary |
| `/help` | `/start` | List available commands |

**Implementation:** `src/monitoring/telegram_bot.py` → `TelegramCommandHandler`
- Extracts bot token from `ALERT_WEBHOOK_URL` env var
- Only responds to messages from `ALERT_CHAT_ID` (security)
- Data fetched live from exchange via `_get_system_status()` in `live_trading.py`
- Crash-safe: errors are logged, never propagated to trading loop

---

## 13. Daily Loss Enforcement

The daily loss limit (`daily_loss_limit_pct` in risk config, default 5%) is now actively enforced end-to-end:

1. **Initialization:** At startup, `_sync_account_state()` calls `risk_manager.reset_daily_metrics(equity)` to set the day's starting equity.
2. **Tracking:** On every trade close, `_save_trade_history()` calls `risk_manager.record_trade_result(net_pnl, equity, setup_type)` to update `daily_pnl`.
3. **Enforcement:** `risk_manager.validate_trade()` rejects new entries when `|daily_pnl| / daily_start_equity > daily_loss_limit_pct`.
4. **Early warning:** At 70% of the limit, a `DAILY_LOSS_WARNING` Telegram alert is sent. At 100%, the alert is marked `urgent`.
5. **Reset:** At midnight UTC, `_run_daily_summary()` resets the daily metrics.

### Bug History
Previously (before this fix), `daily_pnl` and `daily_start_equity` were always 0 because `record_trade_result()` and `reset_daily_metrics()` were never called from `live_trading.py`. The limit existed in config but was never enforced.

---

## 14. Cycle Time Analysis

As of Feb 2026, cycle times are:
- **~62 seconds** (normal tick): 45 coins with `Semaphore(50)` concurrency and smart candle-boundary caching.
- **~124 seconds** (reconciliation tick): Every ~120s, a full `sync_with_exchange` adds position/order reconciliation, doubling the cycle.

**Candle caching (Feb 8 fix):** Instead of fetching all timeframes every cycle, the candle manager now checks if a new bar boundary has crossed since the last fetch. For 15m candles, this means ~15 of 45 coins actually fetch per cycle (those whose 15m bar just closed), not all 45. Higher timeframes (1h, 4h, 1d) are skipped even more aggressively.

## 15. Universe Hardening (Feb 8)

Five improvements to coin universe management:

1. **Discovery refresh: 24h → 4h** (`config.yaml`). Catches liquidity changes, new listings, and delistings faster.

2. **Universe shrink protection** (`live_trading.py → _update_market_universe`). If a new discovery returns <50% of the LAST discovered universe, it's rejected as a likely API issue. Sends `UNIVERSE_SHRINK` Telegram alert. Compares against last discovery, not the initial config list (which has 243 symbols vs ~45 eligible).

3. **Pre-entry spread check** (`live_trading.py → process_coin`). Before a signal leads to entry:
   - Uses already-fetched spot ticker bid/ask (zero new API calls)
   - Rejects signals when live spread > 1.0% (extreme spreads only)
   - **Fail-open**: any error in the check → allow trade
   - Logs `SIGNAL_REJECTED_SPREAD` when triggered

4. **Pinned Tier A comments fixed**. All comments now correctly say "BTC, ETH, SOL, DOGE, BNB" instead of "BTC, ETH only".

5. **Smart candle-boundary caching** (`candle_manager.py`). 
   - 30s hard floor on all refetches
   - For 15m/1h: only refetch when a new bar boundary has crossed since last fetch
   - Eliminates ~2/3 of redundant API calls per cycle

---

## 16. Config Migration: liquidity_tiers → candidate_symbols (Feb 8)

The `coin_universe` section in `config.yaml` was migrated from the deprecated `liquidity_tiers` (nested A/B/C dict) to `candidate_symbols` (flat list of 246 symbols). This eliminates the `DEPRECATION: coin_universe.liquidity_tiers is deprecated` warning that fired on every startup.

**Before:**
```yaml
coin_universe:
  liquidity_tiers:
    A: ["BTC/USD", "ETH/USD", ...]
    B: ["ARB/USD", ...]
    C: ["2Z/USD", ...]
```

**After:**
```yaml
coin_universe:
  candidate_symbols:
    - "BTC/USD"
    - "ETH/USD"
    - ...  # 246 total
```

The tier classification is still done dynamically by `MarketRegistry` based on live futures volume and spread — it was never actually using the A/B/C grouping in the config for classification (only for discovery candidates).

**Backward compatibility:** The `CoinUniverseConfig` model in `config.py` still supports `liquidity_tiers` and auto-flattens it into `candidate_symbols` via `normalize_candidates()`. But since config.yaml no longer uses it, the deprecation code path is never hit.

---

## 17. Log Noise Reduction (Feb 8)

**Problem:** The `SMC Analysis: NO_SIGNAL` log line fired for nearly every coin on every cycle (~12,000 lines/day). Each reasoning string contained newlines, so lines like `❌ No valid order block found` appeared as separate journalctl entries with no module tag, making logs hard to grep.

**Fix:**
1. Downgraded from `logger.info()` to `logger.debug()` — NO_SIGNAL decisions are normal operation, not noteworthy events. The reasoning is still captured in DECISION_TRACE events in PostgreSQL.
2. Changed reasoning separator from `\n` to ` | ` so the entire reasoning appears on one line.
3. Used structured logging format (`logger.debug("SMC Analysis: NO_SIGNAL", symbol=..., reasoning=...)`) instead of f-string interpolation.

**Result:** Zero `❌` lines in journalctl after deployment. Logs are now dominated by actionable info: cycle summaries, ticker fetches, signal events, and position management.

---

## 18. Session 3: Production Refactor (Feb 8, 2026)

### Overview
Session 3 was a comprehensive code cleanup and architecture improvement spanning 3 PRs:
- **PR #2** (`refactor/cleanup-v1`): Dead code removal, architecture improvements, DB optimizations
- **PR #3** (`refactor/lt-extractions`): Further `live_trading.py` decomposition
- **PR #4** (`refactor/symbol-normalizers`): Symbol normalizer consolidation

### Impact Summary
| Metric | Before | After | Change |
|--------|--------|-------|--------|
| `live_trading.py` lines | 4,367 | 1,640 | -62% |
| Total codebase lines removed | - | ~6,220 | Net deletion |
| Files deleted | - | ~40 | Dead code, debug scripts |
| Test regressions | - | 0 | All 330 tests passing |

### What Was Removed (Phase 2)
- **V1 Position Manager** (`position_manager.py`, 233 lines) + all legacy V1 branches in `live_trading.py`
- **Paper trading module** (`src/paper/`, 451 lines) -- never used in production
- **IPC module** (`src/ipc/`, 26 lines), service abstractions (`src/services/`, 1,116 lines)
- **Legacy main files** (`src/main.py`, `src/main_with_health.py`, 452 lines)
- **Debug scripts** (`scripts/debug/`, 15 files + 4 standalone, ~835 lines)
- **App Platform scripts** (16 files, ~2,232 lines) -- system runs on Droplet only
- **Streamlit references** from `Procfile`, `health.py`, `cli.py`

### Architecture Improvements (Phase 3)

**EventRecorder Protocol** (`src/domain/protocols.py`):
- `SMCEngine` and `RiskManager` accept `event_recorder` via constructor injection
- Eliminates direct `from src.storage.repository import record_event` coupling
- Unit tests use the default no-op recorder instead of module-level mocking

**Database Optimizations** (`src/storage/repository.py`, `smc_engine.py`, `symbol_cooldown.py`):
- Fixed N+1 query in `sync_active_positions` (batch fetch + dict lookup)
- SQL `GROUP BY` in `get_last_signal_per_symbol` (was Python-side aggregation)
- `SELECT ... FOR UPDATE` on `save_position` to prevent race conditions
- Composite index `idx_trade_symbol_exited` on `TradeModel`
- Raw `psycopg2` migrated to SQLAlchemy pool with 5-minute TTL caching

**KrakenClient Performance** (`src/data/kraken_client.py`):
- Persistent `aiohttp.ClientSession` with connection pooling (limit=30, DNS cache 300s)
- Replaced 5 per-request session creations in futures API methods

**Symbol Normalizer Consolidation** (`src/data/symbol_utils.py`):
- `symbol_utils.py` is now the single source of truth for ALL symbol normalization
- Added `normalize_to_base()` (strips to base asset: "BTC/USD" -> "BTC")
- Added `exchange_position_side()` (position side from exchange dict)
- Replaced 6 duplicate normalizers across `executor.py`, `auction_allocator.py`, `reconciler.py`, `symbol_cooldown.py`, `live_trading.py`, `protection_ops.py`

### Bugs Fixed During Refactor
- **`db_positions` NameError**: `_validate_position_protection` referenced undefined variable in else branch (leftover from V1 removal). Would crash at runtime when all positions were protected. Fixed during health_monitor extraction.
- **Simplified `_exchange_position_side` in `protection_ops.py`**: Used `float()` instead of `Decimal()`, losing precision. Replaced with canonical version from `symbol_utils`.

### Key Decisions
1. **Delegate pattern over pass-through args**: Extracted functions receive `lt: "LiveTrading"` to access shared state. This was preferred over passing 10+ individual dependencies, balancing architecture clarity with practical regression risk.
2. **No trading logic changes**: Zero modifications to signal generation, risk limits, sizing, order routing, or config defaults. All changes are structural.
3. **Droplet-only target**: Archived App Platform config, deleted 16 related scripts. The system is deployed exclusively via `systemd` on a DigitalOcean Droplet.

### Makefile Additions
```
make lint      -- Run ruff linter with auto-fix
make format    -- Format code with ruff
```

---

## 19. Current System Status & What Comes Next

**As of Feb 8, 2026 (end of session 3):**

The system is **stable, fully operational, and significantly cleaner**. The codebase has been reduced by ~6,200 lines with zero behavior changes. All 330 tests pass.

**Remaining deferred items (lower priority, separate PRs):**
- Move ~80 archival markdown files to `docs/archive/`
- Create `ARCHITECTURE.md` with Droplet deployment guide and database layout
- DB observability: connection pool logging, candle pruning, reconciliation monitor

**When to intervene:**
- If both positions get stopped out -> investigate whether 4H ATR stop multipliers (0.15-0.30x) are too tight
- If daily loss limit fires -> check if the limit (5%) is appropriate for the account size
- If universe shrink protection fires -> check Kraken API health
- If kill switch fires -> check logs for the specific invariant that triggered it

**What NOT to change without data:**
- Strategy parameters (score thresholds, ATR multipliers, Fibonacci gates)
- Tier classification thresholds ($5M/$500K/$250K)
- Signal cooldown duration (4h)
- Any safety thresholds in `safety.yaml`

These were set based on backtests showing 78.9% win rate. Changing them without new forward-test data would be premature optimization.
