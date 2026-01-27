# Operations Health: Unmanaged Positions, Reconciliation, Candle Health

This document explains how the system stays survivable in production: unmanaged-position handling, reconciliation behavior, and candle-health thresholds that can pause new entries.

---

## What “UNMANAGED POSITION” Means

**UNMANAGED POSITION** (or “ghost”) means the exchange has an open position that our internal state (DB + in-memory tracking) does **not** know about. Typical causes:

- Position was opened outside this app (manual trade, another bot, or a previous run that didn’t persist state).
- DB was reset or migrated while the exchange still had the position.
- Restart or crash before we synced that position into our DB.

Until we reconcile it, we do **not** manage that position: no stop-loss, no take-profit, no risk or sizing logic. So it is both a risk and an inconsistency we must fix.

---

## How Reconciliation Works

The **Position Reconciler** runs:

1. **At startup** (before any trading), so we fix ghosts/zombies before opening new trades.
2. **Periodically** (default every 2 minutes; `reconciliation.periodic_interval_seconds`).
3. **After order bursts** (e.g. after auction opens/closes), so we quickly sync state.

For each run it:

- Fetches open futures positions from the exchange.
- Compares them to our DB (and, indirectly, to in-memory tracking).

### Unmanaged (exchange has it, we don’t)

Configurable via `reconciliation.unmanaged_position_policy`:

- **`adopt` (default)**  
  - Create/insert a `Position` row (symbol, side, size, entry, etc.).  
  - Mark it as adopted (e.g. `protection_reason="ADOPTED_UNMANAGED"`).  
  - Optionally attempt to place SL/TP when `unmanaged_position_adopt_place_protection` is true; otherwise protection is added on the next tick by normal TP/SL reconciliation.  
  - The position is then managed like any other.

- **`force_close`**  
  - Place a reduce-only market order to close the position.  
  - Log at CRITICAL with reason `unmanaged_position_policy=force_close`.  
  - Use only when you want no “adopted” positions and prefer to flat the book.

### Zombies (we track it, exchange doesn’t)

- Removed from our DB and in-memory view.
- Logged as “RECONCILE_ZOMBIE_CLEANED” / “zombie_removed”.

### Config

| Key | Default | Description |
|-----|---------|-------------|
| `reconciliation.reconcile_enabled` | `true` | Run reconciliation at startup and on the interval. |
| `reconciliation.periodic_interval_seconds` | `120` | Seconds between periodic reconciliation runs. |
| `reconciliation.unmanaged_position_policy` | `adopt` | `adopt` or `force_close` for exchange positions we don’t track. |
| `reconciliation.unmanaged_position_adopt_place_protection` | `true` | When adopting, try to place SL/TP immediately (otherwise next tick). |

Logs to look for:

- **RECONCILE_START / RECONCILE_END**
- **RECONCILE_SUMMARY** with `on_exchange`, `tracked`, `adopted`, `force_closed`, `zombies_cleaned`
- **RECONCILE_ADOPTED** / **RECONCILE_FORCE_CLOSED** / **RECONCILE_ZOMBIE_CLEANED** per symbol

---

## Candle Health Thresholds and Why Trading Pauses

We require **enough** coins to have usable candle data before allowing **new** entries. This avoids:

- Opening on symbols with no or stale candles.
- Blowing up strategy logic when most of the universe has no data.

### Rules

- **Healthy**  
  - `coins_with_sufficient_candles >= min_healthy_coins` **and**  
  - `(coins_with_sufficient_candles / total_coins) >= min_health_ratio`

- **Unhealthy**  
  - If either condition fails → **new entries are paused** (`trade_paused = True`).  
  - Reconciler, position management, and existing-order handling keep running.

“Sufficient” means at least 50 candles for the 15m series (used for signal generation).

### Config

| Key | Default | Description |
|-----|---------|-------------|
| `data.min_healthy_coins` | `30` | Minimum number of coins with sufficient candles to allow new entries. |
| `data.min_health_ratio` | `0.25` | Minimum fraction of the universe that must have sufficient candles. |

When unhealthy, logs look like:

- **TRADING PAUSED: candle health insufficient** with `coins_with_sufficient_candles`, `total`, `min_healthy_coins`, `min_health_ratio`.

### Rationale

- **min_healthy_coins**: Avoid opening when only a tiny set of symbols has data (e.g. 4 coins with candles, 305 waiting).
- **min_health_ratio**: Avoid opening when the majority of the universe has no candles (e.g. 10 out of 309), even if you hit the absolute count.

Once candle pipeline and/or universe are fixed and health goes back above both thresholds, `trade_paused` is cleared and new entries are allowed again.

---

## Instrument Specs and Order Rejection Reasons

The **InstrumentSpecRegistry** (`src/execution/instrument_specs.py`) loads futures contract specs from Kraken (min size, step, leverage mode) and caches to `data/instrument_specs_cache.json`. The auction only plans opens for symbols that have a spec; the executor validates size (min, step) and leverage (flexible vs fixed) before placing orders.

- **AUCTION_OPEN_REJECTED**: Planned open dropped before sending (reason: NO_SPEC, SIZE_BELOW_MIN, SIZE_STEP_ROUND_TO_ZERO, or leverage).
- **ORDER_REJECTED_BY_VENUE**: Kraken rejected set_leverage or create_order (e.g. CONTRACT_NOT_FLEXIBLE_FUTURES). Check `venue_error_code`, `venue_error_message`, `payload_summary`.

Common reasons: **NO_SPEC** = symbol not in instruments or format mismatch; **SIZE_STEP_ROUND_TO_ZERO** = notional too small; **SIZE_BELOW_MIN** = rounded size below min; **CONTRACT_NOT_FLEXIBLE_FUTURES** = contract supports only fixed leverage tiers (registry adjusts to nearest allowed).

---

## Related

- **OHLCV resilience**: Retries, per-symbol cooldown, and rate limits are in `src.data.ohlcv_fetcher` and configured via `data.ohlcv_*`.
- **Symbol trimming**: Only Kraken-supported symbols are traded; see “SYMBOL REMOVED (unsupported on Kraken)” in logs when the universe is trimmed.
- **Log markers**: `AUCTION_START` / `AUCTION_PLAN` / `AUCTION_END`, `AUCTION_OPEN_REJECTED`, `ORDER_REJECTED_BY_VENUE`, `PYRAMIDING_GUARD_SKIP`, `RECONCILE_SUMMARY`, `STARTUP_BANNER` for ops visibility.
- **Instrument specs**: `src/execution/instrument_specs.py` – registry for min_size, size_step, leverage_mode (flexible/fixed/unknown). Rejections: NO_SPEC, SIZE_STEP_ROUND_TO_ZERO, SIZE_BELOW_MIN; venue CONTRACT_NOT_FLEXIBLE_FUTURES.
