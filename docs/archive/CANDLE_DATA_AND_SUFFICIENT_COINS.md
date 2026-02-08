# Why Only a Few Coins Have "Sufficient" Candles

The worker reports `coins_with_sufficient_candles` (symbols with ≥50 × 15m candles). If you see **only 2** (or a small number) despite 300+ symbols, here’s what’s going on.

## How candle data gets into the system

1. **Database (historical)**  
   Candles are stored in the DB. At startup, **hydration** loads 14d of 15m, 60d of 1h, etc. into the in-memory cache for all tracked symbols.

2. **Live fetch (incremental)**  
   Each tick we update candles for symbols we have a **spot** or **futures** ticker for. We **skip** only when neither exists.

3. **Futures OHLCV fallback**  
   When **spot** OHLCV is unavailable (e.g. Kraken `BadSymbol`, no spot market), we **fall back to futures OHLCV** for that coin if `use_futures_ohlcv_fallback` is enabled (default: true). Signals then use futures-sourced candles, stored under the spot symbol. This lets the 60+ “no spot” coins still be analysed and traded.

4. **“Sufficient”**  
   We need **≥50 × 15m** candles for SMC. That comes from **hydration (DB)** and/or **live fetches** (spot or futures fallback). If both are missing for a symbol, it stays insufficient.

## Likely causes of “only 2 (or few) sufficient”

### 1. Production DB never backfilled

- Hydration loads from the **production** `DATABASE_URL`.
- If **backfill** was only run against a **local** DB, production has no (or very little) history.
- Result: **0 candles from DB** for almost all symbols. We rely only on live fetches.

**Fix:** Run backfill **against the production DB**, using the **same universe** as live (e.g. discovery list):

```bash
# Point at production DB, then backfill
DATABASE_URL='postgresql://...prod...' python scripts/backfill_historical_data.py
```

See [HISTORICAL_DATA_BACKFILL.md](HISTORICAL_DATA_BACKFILL.md).

### 2. Ticker coverage: many symbols skipped

- We process symbols that have **either** a **spot** ticker (`get_spot_tickers_bulk`) **or** a **futures** ticker. We **skip** only when **neither** exists.
- If Kraken returns neither spot nor futures tickers for many symbols, we skip them → no `update_candles` → no live data.
- `symbols_with_ticker` counts **spot** only; symbols with **futures-only** are still processed (and may use futures OHLCV fallback).

**Check logs for:**

- `"Ticker coverage: some symbols skipped"` → `with_ticker` vs `without_ticker` (spot).
- `"Coin processing status summary"` → `symbols_with_ticker` / `symbols_without_ticker`, `coins_futures_fallback_used` (if present).

If `symbols_without_ticker` is large but you have futures tickers, many coins may still be processed via futures-only path.

### 3. Universe mismatch (backfill vs live)

- **Backfill** uses `_get_monitored_symbols` (config, DB discovery, etc.).
- **Live** uses **market discovery** (e.g. 309 symbols).
- If backfill ran for a **different** set (e.g. 50 from config), only those ~50 have DB history. The rest rely on live fetch → again, subject to ticker coverage.

**Fix:** Backfill the **same** universe live uses (e.g. run discovery first, then backfill that list, or backfill against the DB once the worker has written `DISCOVERY_UPDATE`).

## Startup and periodic logs to check

- **`Hydration complete`**  
  - `with_sufficient_15m` / `with_zero_15m`  
  - Tells you how many symbols got ≥50 vs 0 candles **from DB** at startup.

- **`Coin processing status summary`** (every 5 min)  
  - `coins_with_sufficient_candles`, `coins_waiting_for_candles`  
  - `symbols_with_ticker` / `symbols_without_ticker` (spot), `coins_futures_fallback_used` (if any)  
  - Tells you ticker coverage and how many symbols are “ready” for SMC.

- **`Ticker coverage: some symbols skipped`** (when any missing, throttled)  
  - `with_ticker` / `without_ticker`  
  - Confirms that many symbols are skipped and never updated.

## Summary

| Observation | Likely cause |
|------------|----------------|
| `with_zero_15m` ≈ total at hydration | DB has no history → **run backfill against prod DB** |
| `symbols_without_ticker` large | Many lack spot ticker; **futures-only** coins still processed if they have futures ticker |
| `with_sufficient_15m` small but `symbols_with_ticker` large | DB empty + live fetch still ramping or failing for most → **backfill + inspect fetch errors** |

Running **backfill** against the **production** DB for the **live** universe, and then checking **ticker coverage** and **hydration** logs, usually resolves “only 2 coins with sufficient data.”
