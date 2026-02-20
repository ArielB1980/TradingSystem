# Decision Logic and Data Sources

This doc audits **runtime decision logic** that depends on "recent" or historical data. The goal is to avoid discrepancies when **bounded in-memory or log-based data** underrepresents the true window (e.g. log tail scrolls past, cycle history keeps only last N cycles).

**Lesson from production:** Relying only on a log tail or in-memory window for "what traded recently" can make analysis (or monitors) miss real activity. Use **DB or exchange as source of truth** where the decision must be correct over a fixed time window.

---

## 1. Trade starvation monitor

| What | Decides whether to fire "TRADE STARVATION" alert (signals ≥ 10, orders = 0 in window). |
|------|----------------------------------------------------------------------------------------|
| **Primary data** | CycleGuard cycle history: `signals_generated`, `orders_placed` per cycle (max 100 cycles ≈ ~1.7h at 1 min/cycle). |
| **Window** | Configurable (default 6h); effective window is **capped by cycle history size**. |
| **Risk** | Opens that occurred in cycles that have scrolled out look like "0 orders" → false starvation alarm. |
| **Mitigation** | **DB backstop (two queries) before alarming:** (1) `count_trades_opened_since(cutoff)` — closed trades entered in window; (2) `count_open_positions_opened_since(cutoff)` — still-open positions entered in window. If either ≥ 1, do not alarm. Together they cover both closed and still-live positions, fully closing the bounded-history gap. See `src/live/health_monitor.py` and `src/storage/repository.py`. |

---

## 2. Symbol cooldown (repeated losses)

| What | Pauses trading a symbol after N losses in lookback hours. |
|------|------------------------------------------------------------|
| **Data source** | **DB:** `get_symbol_loss_stats(symbol, lookback_hours)` queries `trades` (net_pnl < 0, exited_at in window). |
| **Risk** | None from bounded memory; DB has full history. |

---

## 3. Loss streak cooldown (risk manager)

| What | Global pause after N consecutive losses (time-based cooldown). |
|------|-----------------------------------------------------------------|
| **Data source** | **In-memory:** `consecutive_losses` and `cooldown_until` updated in `record_trade_result()` on every close. |
| **Risk** | If `record_trade_result()` is never called for a close (e.g. code path bug), streak can be wrong. All closes should go through `_save_trade_history` → `save_trade_history` which calls `record_trade_result`. |

---

## 4. Recent stop-outs (SMC engine)

| What | Stop widening / behaviour when symbol has recent stop-outs. |
|------|-------------------------------------------------------------|
| **Data source** | **DB:** `get_recent_stopouts(symbol, lookback_hours)` with 5‑min TTL cache. |
| **Risk** | None from bounded memory. |

---

## 5. Intent hash deduplication (executor)

| What | Prevents duplicate order intents after restart. |
|------|---------------------------------------------------|
| **Data source** | **DB:** `load_recent_intent_hashes(lookback_hours=24)`. |
| **Risk** | None from bounded memory. |

---

## 6. Analysis / reporting ("analyze recent trading")

| What | Human or AI summarizes recent trading (opens, closes, errors). |
|------|-----------------------------------------------------------------|
| **Primary data** | Log tail (last N lines) + **Kraken Futures fills** (last 48h) fetched on server. |
| **Mitigation** | **Kraken fills are source of truth** for executed trades. Routine and fetch script use fills so analysis does not miss trades that scrolled out of the log. See `docs/ROUTINE_ANALYZE_RECENT_TRADING.md` and `scripts/fetch_recent_trading_logs.sh`. |

---

## 7. Other cooldowns (in-memory, not time-window critical)

- **Signal cooldown** (`_signal_cooldown`): Same symbol cannot re-signal within N hours. Per-signal, not "did we trade in last 6h".
- **TP backfill cooldown** (`tp_backfill_cooldowns`): Per-symbol time since last backfill. Bounded memory is acceptable (prevents spam, not correctness over 6h).
- **Partial-close cooldown** (auction): Skips new opens for N seconds after a partial close. Short window, in-memory is fine.

---

## Adding new decision logic

When adding logic that depends on "recent" trades, opens, or fills:

**Rule of thumb:**
- **Window ≥ 1 hour** → **must** have a DB or exchange backstop. In-memory / log-tail alone is not acceptable.
- **Window < 1 hour** → in-memory is fine if the decision is purely advisory (cooldown, rate-limit).

Steps:
1. **Prefer DB or exchange** for any window ≥ 1 hour (e.g. "opens in last 6h").
2. If using **in-memory or log tail**, add a **source-of-truth backstop** (DB query or exchange fetch) when the decision affects alerts or trading.
3. Document the data source and risk in this file.
