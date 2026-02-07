# Session Knowledge: Lessons Learned & System Memory

**Last Updated:** 2026-02-07
**Covers Sessions:** 2026-02-06 through 2026-02-07

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

# Check cycle summaries
grep 'CYCLE_END' /home/trading/TradingSystem/logs/run.log | tail -5
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
| `src/strategy/smc_engine.py` | SMC (Smart Money Concepts) signal generation |
| `src/live/live_trading.py` | Main trading loop, TP backfill, cycle orchestration |
| `src/portfolio/auction_allocator.py` | Trade candidate selection and capital allocation |
| `src/risk/risk_manager.py` | Position sizing, risk validation, equity caps |
| `src/execution/execution_gateway.py` | Order submission, tracking, event handling |
| `src/execution/position_manager_v2.py` | Position state machine, reconciliation |
| `src/safety/invariant_monitor.py` | System safety invariants, halt/degraded triggers |
| `src/config/safety.yaml` | Safety thresholds configuration |
| `src/config/config.yaml` | Main system configuration |

### MarketRegistry: Single Source of Truth
- `MarketRegistry` is THE authority for what can be traded and at what tier.
- **Pinned Tier A coins:** `{"BTC", "ETH", "SOL", "DOGE", "BNB"}` — these always bypass liquidity filters.
- Other coins must qualify dynamically via futures volume and spread metrics.
- Tiers: A (best liquidity), B (good), C (marginal).
- The old `CoinClassifier` in `src/data/coin_universe.py` was deleted; everything goes through `MarketRegistry` now.
- `CoinUniverseConfig` supports both `candidate_symbols` (new) and `liquidity_tiers` (deprecated, auto-normalized).

---

## 3. Critical Bugs Fixed & Root Causes

### Bug 1: Kill Switch Cancelling SL/TP Orders
**Symptom:** User saw SL and TP orders appear on Kraken, then disappear seconds later.
**Root Cause:** Margin threshold mismatch. The auction allocator was allowed to use up to 90% margin (`auction_max_margin_util`), but the invariant monitor would trigger a CRITICAL halt at 85% (`max_margin_utilization_pct`). So any time the auction used its full budget, the invariant monitor immediately activated the kill switch, which cancels ALL open orders — including the SL/TP orders that were just placed.
**Fix:** Aligned thresholds:
- `max_margin_utilization_pct`: 0.85 -> **0.92** (above auction's 0.90)
- `degraded_margin_utilization_pct`: 0.70 -> **0.85** (warning at auction's target)

**Lesson:** Safety thresholds must be coordinated with operational thresholds. A safety gate set tighter than the operational limit guarantees false trips.

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
```
- **DEGRADED:** No new entries, existing positions managed normally
- **HALTED:** Kill switch activates, all open orders cancelled, no trading
- **Persisted halt state:** `/home/trading/.trading_system/halt_state.json` — must be manually deleted to recover
- **Kill switch state:** `/home/trading/TradingSystem/data/kill_switch_state.json` — must be manually deleted to recover

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

## 6. Account State (as of 2026-02-07)

- **Equity:** ~$345
- **Available Margin:** ~$296
- **Open Positions:** 1 (PF_APTUSD / APT)
- **System State:** NORMAL (no degraded, no halt)
- **Coins Scanned:** 56 per cycle
- **Cycle Interval:** ~60-130 seconds

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
5. If the kill switch fires, ALL orders (including SL/TP) are cancelled — this is the most dangerous scenario for open positions

### Log Patterns to Watch For
| Pattern | Meaning |
|---------|---------|
| `Kill switch active` | System halted, all orders cancelled |
| `DEGRADED` | System in warning state, no new entries |
| `PHANTOM` | Exchange has a position the bot doesn't know about |
| `ORPHAN` | Bot thinks it has a position that doesn't exist on exchange |
| `4H_STRUCTURE_REQUIRED` | Normal — strategy filtering, not an error |
| `entry not in OTE/Key Fib` | Normal — strategy filtering, not an error |
| `Execution failed` | Actual error — investigate immediately |
| `flatten_orphan` | Was dangerous (Bug 3), now deferred safely |
| `Tier A coin bypassing filters` | Normal — pinned majors skipping liquidity checks |

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
- **Makefile targets:** `make venv`, `make install`, `make run`, `make smoke`, `make logs`, `make smoke-logs`
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
