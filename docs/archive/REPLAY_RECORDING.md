# Record & Replay Harness

Validate the Data Sanity Gate and DataQualityTracker using real Kraken Futures
market data -- without making live API calls during backtests.

## Overview

The harness has two phases:

1. **Record** -- capture live market snapshots at regular intervals (production-safe, read-only).
2. **Replay** -- deterministically replay those snapshots through the sanity gate and compare gate-enabled vs gate-disabled behavior.

---

## Running the Recorder

### Prerequisites

- `DATABASE_URL` environment variable pointing to your PostgreSQL instance.
- `data/discovered_markets.json` (or custom symbol list file).
- Python 3.10+ with project dependencies installed.

### Command

```bash
# Record every 5 minutes (default), running indefinitely
python -m src.recording.kraken_futures_recorder \
    --symbols-file data/discovered_markets.json \
    --interval-seconds 300

# Record for a fixed number of cycles (testing / CI)
python -m src.recording.kraken_futures_recorder \
    --symbols-file data/discovered_markets.json \
    --interval-seconds 300 \
    --max-cycles 12    # 1 hour of data
```

### What it records

Per symbol, per interval:

| Field                    | Source                      | Notes                              |
|--------------------------|-----------------------------|------------------------------------|
| `ts_utc`                 | Wall clock                  | UTC timestamp of the snapshot      |
| `symbol`                 | Symbol list                 | e.g. `BTC/USD`                     |
| `futures_bid`            | Kraken Futures API          | Best bid price                     |
| `futures_ask`            | Kraken Futures API          | Best ask price                     |
| `futures_spread_pct`     | Computed                    | `(ask - bid) / bid`                |
| `futures_volume_usd_24h` | Kraken Futures API         | 24h volume in USD                  |
| `open_interest_usd`      | Kraken Futures API          | Recorded but NOT used for gating   |
| `funding_rate`           | Kraken Futures API          | Recorded but NOT used for gating   |
| `last_candle_ts_json`    | Candle DB                   | JSON: latest candle ts per TF      |
| `candle_count_json`      | Candle DB                   | JSON: candle count per TF          |
| `error_code`             | Error handler               | Non-null if symbol fetch failed    |

### Safety guarantees

- **Read-only**: only calls public ticker endpoints; never places orders.
- **Rate-limited**: uses the existing `KrakenClient` with its 20-token bucket limiter.
- **Resilient**: per-symbol `try/except`; a single symbol failure records an error snapshot and continues.
- **Append-only**: never overwrites or deletes existing snapshots.
- **No secrets stored**: only market data is persisted.

---

## Suggested Interval

**300 seconds (5 minutes)** -- matches the trading system's tick cadence.

At this rate, one day produces ~288 snapshots per symbol, and 30 days produces
~8,640 per symbol.  For 50 symbols, that's ~432,000 rows/month -- trivial for
PostgreSQL.

---

## Retention Policy

Monthly rotation recommended:

```sql
-- Delete snapshots older than 90 days
DELETE FROM market_snapshots
WHERE ts_utc < NOW() - INTERVAL '90 days';

-- Optional: VACUUM to reclaim space
VACUUM market_snapshots;
```

---

## Running the Replay Backtest

### Command

```bash
python -m src.replay.run_replay_backtest \
    --db-url postgresql://user:pass@host:5432/trading \
    --start 2025-11-06 \
    --end 2025-12-06 \
    --tick-seconds 300 \
    --output-dir data/replay_reports
```

### What it does

1. **Loads** all recorded snapshots in the date range.
2. **Pass 1 (gate enabled)**: Runs the full sanity gate + DataQualityTracker state machine over the recorded data. Symbols transition through HEALTHY -> DEGRADED -> SUSPENDED as appropriate.
3. **Pass 2 (gate disabled)**: Control pass -- every symbol analyzed on every tick.
4. **Reports**: Generates coverage + delta reports in JSON + human-readable summary.

### Determinism guarantee

Given the same recording DB, start/end dates, and tick interval, the replay
produces **byte-identical** report JSON on every run.  This is enforced by:

- Injectable `clock` in DataQualityTracker (uses replay time, not wall time).
- Injectable `now` in `check_candle_sanity()`.
- No randomness or external state in the replay path.

---

## Interpreting Reports

### Coverage Report (`coverage_<start>_<end>.json`)

**Healthy recording** looks like:

- Most symbols: `cycles_sanity_pass / cycles_total > 0.95`
- `ticker_missing_rate < 0.05` (few missed API calls)
- Zero or few suspensions
- Top failure reasons: occasional `candles_stale` (expected during market closures)

**Red flags**:

- `ticker_missing_rate > 0.20` -- recorder connectivity issues
- Many symbols in SUSPENDED state -- possible API changes or symbol delistings
- `candles_count` failures growing -- candle DB backfill needed

### Delta Report (`delta_<start>_<end>.json`)

Key metric: **`wasted_work_pct`**

- `10-30%`: Gate is providing moderate savings -- good.
- `>50%`: Many symbols have data quality issues -- investigate root causes.
- `0%`: All symbols are healthy -- gate isn't needed (unlikely in practice).

---

## Troubleshooting

### Empty recordings

```
ERROR: no_symbols_in_recording
```

- Verify the recorder has been running: `SELECT COUNT(*) FROM market_snapshots;`
- Check the date range matches your recording period.

### Missing symbols

A symbol appears in the recording but has `error_code = 'no_futures_ticker'` on
every snapshot:

- The symbol may not have a futures market.  Check `data/discovered_markets.json`.
- The Kraken futures ticker key may have changed.  Check recorder logs for the
  actual keys returned.

### DB connection issues

```
ERROR: recording_batch_insert_failed
```

- Verify `DATABASE_URL` is correct and the DB is reachable.
- Check PostgreSQL logs for connection limits or disk space.

### Recorder crash recovery

The recorder is designed to be restarted safely at any time.  Snapshots are
append-only, so there's no risk of data corruption.  Simply restart the process.

---

## Database Schema

The `market_snapshots` table is auto-created on first recorder run:

```sql
CREATE TABLE market_snapshots (
    id SERIAL PRIMARY KEY,
    ts_utc TIMESTAMP WITH TIME ZONE NOT NULL,
    symbol VARCHAR(64) NOT NULL,
    futures_bid NUMERIC(20, 8),
    futures_ask NUMERIC(20, 8),
    futures_spread_pct NUMERIC(20, 8),
    futures_volume_usd_24h NUMERIC(20, 8),
    open_interest_usd NUMERIC(20, 8),
    funding_rate NUMERIC(20, 10),
    last_candle_ts_json TEXT,
    candle_count_json TEXT,
    error_code VARCHAR(128)
);

CREATE INDEX idx_snapshot_sym_ts ON market_snapshots (symbol, ts_utc);
```
