# Historical Data Backfill Solution

## Problem

The dashboard only shows 2 regimes ("consolidation" and "wide_structure") because:

1. **Root Cause**: All coins are being rejected with `"❌ EMA 200 not available on 1D"`
2. **Why**: EMA 200 requires 200+ daily candles, but coins don't have enough historical data
3. **Result**: Coins are rejected BEFORE structure detection, so they use ADX-based fallback regime classification

## Solution: Backfill Historical Data

Download 250 days of historical candle data for all coins to ensure:
- EMA 200 calculations work properly
- Coins can pass the bias determination step
- Structure detection happens
- "tight_smc" regime appears when Order Blocks/FVGs are detected

## Implementation

### 1. Created Backfill Script

**File**: `scripts/backfill_historical_data.py`

Features:
- Fetches 250 days of historical data for all coins
- Downloads 4 timeframes: 1d, 4h, 1h, 15m
- Stores candles in database
- Handles rate limiting and errors gracefully
- Shows progress and summary

### 2. Added Makefile Command

```bash
make backfill
```

This command:
- Loads environment variables from `.env.local`
- Runs the backfill script
- Downloads ~250 days × 4 timeframes × 311 coins = ~310,000 candles

### 3. Usage

#### Local Development:
```bash
# One-time setup
make backfill
```

#### Production (DigitalOcean):
The backfill script needs to be run once on the production database. Options:

**Option A: Run from local machine against production DB**
```bash
# Set production DATABASE_URL in .env.local temporarily
DATABASE_URL=postgresql://... make backfill
```

**Option B: Run in production worker pod**
```bash
# SSH into worker pod
kubectl exec -it worker-pod-name -- bash

# Run backfill
python scripts/backfill_historical_data.py
```

**Option C: Add to worker startup** (Recommended)
Update `.do/app.yaml` (or your app spec) to run backfill on first deployment:
```yaml
run_command: python scripts/backfill_historical_data.py && python migrate_schema.py && python -m src.entrypoints.prod_live
```

## Expected Results

After backfilling historical data:

1. **Coins pass bias determination** - EMA 200 calculations work
2. **Structure detection happens** - Order Blocks, FVGs, BOS are detected
3. **Regime classification is accurate**:
   - **"tight_smc"** - When OB/FVG detected
   - **"wide_structure"** - When BOS detected or trending
   - **"consolidation"** - Genuinely ranging markets (ADX < 25)
   - **"no_data"** - Only when truly insufficient data

4. **Dashboard shows diverse regime distribution** - 3-4 different regimes instead of just 2

## Maintenance

### Daily Data Retention

The database already has a maintenance task that prunes old candles:

```python
# From src/storage/maintenance.py
# Prunes 15m candles older than 14 days
# Keeps 1h, 4h, 1d candles indefinitely
```

This is perfect because:
- Daily candles are kept forever (needed for EMA 200)
- Hourly candles are kept forever (needed for analysis)
- 15m candles are pruned after 14 days (saves space)

### Continuous Updates

The system already fetches new candles continuously via `DataAcquisition`. The backfill is only needed once to populate historical data.

### Periodic backfill (cron)

To backfill **new** symbols (e.g. after market discovery adds pairs) or **gaps** (e.g. missing days), run the script periodically. With `skip_existing=True` (default), it only fetches for symbols with &lt;200 daily candles.

**Cron-friendly wrapper**: `scripts/run_backfill_cron.sh`

```bash
# Make executable once: chmod +x scripts/run_backfill_cron.sh
# Example: Sundays at 03:00
0 3 * * 0 cd /path/to/TradingSystem && ./scripts/run_backfill_cron.sh
```

The script sources `.env.local`, uses `.venv`, and runs `backfill_historical_data.py` with default options (`skip_existing=True`, 250 days). Or run the backfill script directly:

```bash
cd /path/to/TradingSystem && set -a && [ -f .env.local ] && source .env.local; set +a && .venv/bin/python scripts/backfill_historical_data.py
```

Use the same `DATABASE_URL` as the worker so the live system hydrates from the updated DB.

## Timeline

1. **Now**: Backfill script created, ready to run
2. **After backfill** (~30-60 minutes): Historical data populated
3. **Next analysis cycle** (~5-10 minutes): Coins analyzed with full data
4. **Dashboard update** (~15-20 minutes): New regime distribution visible

## Files Modified

- `scripts/backfill_historical_data.py` - New backfill script
- `Makefile` - Added `make backfill` command
- `docs/HISTORICAL_DATA_BACKFILL.md` - This documentation

## Next Steps

1. Run `make backfill` locally or in production
2. Wait for backfill to complete (~30-60 minutes)
3. Wait for next analysis cycle (~10 minutes)
4. Check dashboard - should see diverse regime distribution
