# OHLCV Timeouts/BadSymbol and TP-Backfill/Protection Warnings – Detail

This document describes two **pre-existing** operational issues seen in production logs. Neither is caused by the duplicate-position (pyramiding-guard) fix.

---

## 1. OHLCV timeouts and BadSymbol

### What appears in logs

- **`"Failed to fetch spot OHLCV"`** with `"level": "error"`.
- **`error_type`**: often `"TimeoutError"` or `"BadSymbol"`.
- **Examples**:
  - `TimeoutError`: XLM/USD, UNI/USD, SAND/USD, ATOM/USD, LRC/USD, XMR/USD, GMT/USD, GRT/USD, ETHW/USD, XTZ/USD, ALGO/USD, BAND/USD, ALICE/USD, ICX/USD, FET/USD, SUI/USD, etc.
  - `BadSymbol`: LUNA2/USD (`"kraken does not have market symbol LUNA2/USD"`), THETA/USD (`"kraken does not have market symbol THETA/USD"`).
- Sometimes followed by:
  - **`"No candles for {symbol} {tf} (spot failed, futures fallback skipped or empty)"`** when both spot and futures fallback yield no data.

### Where it comes from

1. **Spot OHLCV fetch**  
   `KrakenClient.get_spot_ohlcv()` in `src/data/kraken_client.py`:
   - Calls `exchange.fetch_ohlcv(symbol, timeframe, ...)` via CCXT.
   - Wraps the call in **`asyncio.wait_for(..., timeout=10.0)`**.
   - On **any** exception (including `asyncio.TimeoutError` and CCXT `BadSymbol`), it:
     - Logs **`"Failed to fetch spot OHLCV"`** with `symbol`, `error`, and `error_type`.
     - Re-raises.

2. **Callers**  
   Spot OHLCV is used by:
   - **`CandleManager.update_candles()`** (live loop: `_update_candles(spot_symbol)` per symbol).
   - **`DataService`** (polling 15m/1h/4h/1d).
   - **`DataAcquisition.fetch_spot_historical()`** (backfill).
   - Paper/backtest paths.

3. **CandleManager behavior** (`src/data/candle_manager.py`):
   - For each timeframe (15m, 1h, 4h, 1d), it:
     - Tries **`client.get_spot_ohlcv(symbol, tf, ...)`**.
     - On exception: sets `candles = []` (exception is **not** propagated; the “Failed to fetch spot OHLCV” log is emitted inside `get_spot_ohlcv` before it raises, so the log is from the Kraken client).
     - If `candles` is empty and **`use_futures_ohlcv_fallback`** is true: tries **futures OHLCV** via `spot_to_futures(symbol)` and `get_futures_ohlcv(fsym, ...)`.
     - If still no candles: logs **`"No candles for {symbol} {tf} (spot failed, futures fallback skipped or empty)"`** and returns (no update for that symbol/tf).

So the **errors** you see are from the Kraken client when spot fetch fails; the **warnings** are from CandleManager when both spot and fallback fail.

### TimeoutError

- **Meaning**: The spot OHLCV request did not complete within **10 seconds**.
- **Typical causes**:
  - Kraken API latency or overload.
  - Network congestion or packet loss.
  - Rate limiting (e.g. many symbols in parallel).
- **Effect**: For that symbol/timeframe, the live loop gets no new candles from spot; CandleManager then tries futures fallback (if enabled). If fallback works, SMC can still run; if not, you get the “No candles / spot failed, futures fallback skipped or empty” warning and **“SMC Analysis X: NO_SIGNAL -> ERROR: Missing 1h Data”** (or similar for the missing tf).

### BadSymbol

- **Meaning**: Kraken (via CCXT) reports that the **spot** symbol does not exist or is not tradeable.
- **Examples**:
  - **LUNA2/USD**: Kraken may list “LUNA” or another ticker, not “LUNA2/USD”.
  - **THETA/USD**: Spot pair may be delisted or renamed.
- **Effect**: Spot OHLCV for that symbol always fails. Fallback uses **futures** (`spot_to_futures(symbol)`). If futures exists (e.g. `PF_LUNA2USD`), you get candles from futures and analysis continues; if not, that symbol has no candles and yields “Missing X Data” / no signal.

### Config that affects this

- **`use_futures_ohlcv_fallback`** (default `true`): when spot fails, use futures OHLCV for the same asset so SMC can still run.
- **`spot_ohlcv_blocklist`**: symbols to skip for OHLCV (e.g. `["2Z/USD", "ANIME/USD"]`). Blocklisting avoids repeated BadSymbol/timeout noise for known-bad pairs; adding LUNA2/USD or THETA/USD there would silence those errors for spot (they’d still need a valid futures mapping if you want signals).

### Summary (OHLCV)

| Log / symptom | Cause | Mitigation |
|---------------|--------|------------|
| `"Failed to fetch spot OHLCV"` + `TimeoutError` | Spot request > 10s | Retries, backoff, or longer timeout; reduce concurrency; ensure Kraken/network health. |
| `"Failed to fetch spot OHLCV"` + `BadSymbol` | Spot symbol not on Kraken | Use `spot_ohlcv_blocklist` or fix market discovery so we don’t request that symbol on spot. |
| `"No candles for X 1h (spot failed, futures fallback skipped or empty)"` | Spot failed and (fallback off or futures failed/empty) | Rely on futures fallback where possible; backfill or fix symbol mapping. |
| `"SMC Analysis X: NO_SIGNAL -> ERROR: Missing 1h Data"` | No candles for that tf (often after the above) | Same as above; ensure at least one of spot or futures supplies 1h (and other required tf) for that asset. |

---

## 2. TP-backfill and “position not protected” warnings

### What appears in logs

- **`"TP backfill skipped: position not protected"`** with `symbol`, `reason`, `has_sl_price`, `has_sl_order`.
- **`"Positions needing protection (TP backfill skipped)"`** with a list of symbols and:
  - `action="Run 'make place-missing-stops' (dry-run) then 'make place-missing-stops-live' to protect."`

### What TP backfill is

**TP backfill** is a **reconciliation** step that runs after position sync. Its job is to **repair missing take-profit (TP) orders** on positions that are supposed to have a TP ladder but don’t (e.g. SL was placed but TPs were never placed, or TP orders were lost/cancelled).

- Implemented in **`LiveTrading._reconcile_protective_orders()`** which calls **`_should_skip_tp_backfill()`** and **`_needs_tp_backfill()`** / **`_place_tp_backfill()`**.
- Enabled when **`tp_backfill_enabled`** is true (default in `ExecutionConfig`).

For each open position it:

1. Loads the DB position (`get_active_position(symbol)`).
2. Checks **safety/skip** conditions in **`_should_skip_tp_backfill()`**.
3. If not skipped, checks **`_needs_tp_backfill()`** (TP plan exists but TP orders are missing or insufficient).
4. If needed, computes a TP plan and calls **`_place_tp_backfill()`** to place or repair TP orders.

### When TP backfill is skipped: “position not protected”

One of the **required** conditions to run TP backfill is that the position is **protected**:

- **`db_pos.is_protected`** must be **True**.
- Protection is set when the DB has both:
  - **`initial_stop_price`** (the intended SL level), and  
  - **`stop_loss_order_id`** (a live SL order on the exchange, and not a placeholder like `"unknown_..."`).

If **`db_pos.is_protected`** is False, **`_should_skip_tp_backfill()`** skips that position and logs:

- **`"TP backfill skipped: position not protected"`**  
  with `reason=db_pos.protection_reason`, `has_sl_price=…`, `has_sl_order=…`.

So “position not protected” means: we are **not** willing to add or fix TP orders until a **stop-loss** is in place and recorded. That avoids adding TPs on “naked” positions.

### Where “Positions needing protection” comes from

Inside **`_reconcile_protective_orders()`**:

- For every position with `size > 0`, the code checks **`db_pos.is_protected`**.
- If **not** protected, that `symbol` is added to **`skipped_not_protected`**.
- After the loop, if **`skipped_not_protected`** is non-empty, it logs:
  - **`"Positions needing protection (TP backfill skipped)"`**  
    with `symbols=…`, `count=…`, and the `action` message about `make place-missing-stops` / `make place-missing-stops-live`.

So that log is a **summary**: “these positions were skipped for TP backfill because they are not protected.”

### Why a position is “not protected”

From **`src/storage/repository.py`** (and domain logic), protection is derived as:

- **`is_protected == True`** only if:
  - **`initial_stop_price`** is set, and  
  - **`stop_loss_order_id`** is set and **not** `"unknown_..."`.
- If not protected, **`protection_reason`** is set to things like:
  - **`"NO_SL_ORDER_OR_PRICE"`** (missing SL price or both price and order),
  - **`"SL_ORDER_MISSING"`** (has price but no real SL order id).

So “position not protected” usually means one or both of:

1. **No SL price stored** for that position (`initial_stop_price` is None).
2. **No live SL order** (or only a placeholder): `stop_loss_order_id` is None or starts with `"unknown_"`.

Typical situations:

- Position was **opened by something that didn’t record SL** (e.g. manual, old flow, or adoption of an exchange position without going through our entry path).
- SL **order was placed but ID was never saved** (e.g. crash before write, bug in execution path).
- SL **order was cancelled or rejected** on the exchange and we didn’t update or re-place.

### Other skip reasons (for completeness)

TP backfill is also skipped when:

- **Cooldown**: Last backfill for that symbol was less than **`tp_backfill_cooldown_minutes`** ago.
- **Zero size**: `pos_data.get('size', 0) <= 0`.
- **Too new**: Position age &lt; **`min_hold_seconds`** (avoid racing with initial placement).

Only the “position not protected” case produces the **“TP backfill skipped: position not protected”** and **“Positions needing protection (TP backfill skipped)”** warnings.

### What to do about it

The log explicitly suggests:

- **`make place-missing-stops`** (dry-run) to **see** which positions would get a stop.
- **`make place-missing-stops-live`** to **place** missing stops (uses `STOP_PCT=2` by default; see Makefile/script).

Those targets drive the **place_missing_stops** tool, which:

- Finds open positions that have **no** or **insufficient** SL order on the exchange.
- Uses a configured `STOP_PCT` (or default) to compute an SL price and place a reduce-only stop.

After SLs are placed and the DB is updated (e.g. via reconciliation or the same tool), **`is_protected`** can become True and TP backfill will stop skipping those symbols for that reason.

### Summary (TP backfill / protection)

| Log / symptom | Meaning | Next step |
|---------------|---------|-----------|
| **`"TP backfill skipped: position not protected"`** | This position has no valid SL (price + order) in our view; we refuse to add TPs until it’s protected. | Fix protection: place SL, then re-run reconciliation or ensure DB has `initial_stop_price` + `stop_loss_order_id`. |
| **`"Positions needing protection (TP backfill skipped)"`** | One or more positions were skipped for TP backfill because they’re not protected. | Run **`make place-missing-stops`** then **`make place-missing-stops-live`** (or equivalent) to put SLs on those positions. |
| **`action="Run 'make place-missing-stops' (dry-run) then 'make place-missing-stops-live' to protect."`** | Same as above; it’s the suggested remediation. | Use those make targets (or the underlying script) to protect the listed symbols. |

---

## 3. Relationship to the duplicate-position fix

- **OHLCV timeouts/BadSymbol**: Purely **data fetching** (spot/futures OHLCV). No change in the pyramiding or “one position per symbol” logic.
- **TP backfill / “position not protected”**: Purely **risk/ops** (SL/TP repair and when we’re allowed to do it). No change in how many positions we open per symbol.

Both are **pre-existing** and **unrelated** to the normalized-symbol pyramiding guard and auction dedupe.
