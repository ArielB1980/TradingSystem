# Why Only a Few Coins Have "Sufficient" Candles

The worker reports `coins_with_sufficient_candles` (symbols with ≥50 × 15m candles). If you see **only 2** (or a small number) despite 300+ symbols, here’s what’s going on.

## How candle data gets into the system

1. **Database (historical)**  
   Candles are stored in the DB. At startup, **hydration** loads 14d of 15m, 60d of 1h, etc. into the in-memory cache for all tracked symbols.

2. **Live fetch (incremental)**  
   Each tick, we call Kraken for new candles **only for symbols we have a spot ticker for**. We **skip** symbols without a ticker → they never get `update_candles` → no new candles.

3. **“Sufficient”**  
   We need **≥50 × 15m** candles for SMC. That comes from **hydration (DB)** and/or **live fetches**. If both are missing for a symbol, it stays insufficient.

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

- We **only process** symbols that appear in `get_spot_tickers_bulk(...)`.
- If Kraken returns **tickers for only a few** of our symbols (e.g. discovery uses pairs Kraken spot doesn’t support, or format mismatch), we **skip** the rest.
- Skipped symbols **never** get `update_candles` → no live data → always 0 candles.

**Check logs for:**

- `"Ticker coverage: some symbols skipped"` → `with_ticker` vs `without_ticker`.
- `"Coin processing status summary"` → `symbols_with_ticker` / `symbols_without_ticker` (if present).

If `symbols_without_ticker` is large, that explains why most symbols never accumulate candles.

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
  - `symbols_with_ticker` / `symbols_without_ticker` (if logged)  
  - Tells you ticker coverage and how many symbols are “ready” for SMC.

- **`Ticker coverage: some symbols skipped`** (when any missing, throttled)  
  - `with_ticker` / `without_ticker`  
  - Confirms that many symbols are skipped and never updated.

## Summary

| Observation | Likely cause |
|------------|----------------|
| `with_zero_15m` ≈ total at hydration | DB has no history → **run backfill against prod DB** |
| `symbols_without_ticker` large | Most symbols skipped → **check discovery vs Kraken spot** |
| `with_sufficient_15m` small but `symbols_with_ticker` large | DB empty + live fetch still ramping or failing for most → **backfill + inspect fetch errors** |

Running **backfill** against the **production** DB for the **live** universe, and then checking **ticker coverage** and **hydration** logs, usually resolves “only 2 coins with sufficient data.”
