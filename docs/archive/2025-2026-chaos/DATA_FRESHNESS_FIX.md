# Data Freshness Analysis & Fixes

## Current Status

**Analysis Date**: 2026-01-14 11:12 UTC

### Summary
- **Total Monitored Coins**: 310
- **🟢 Active (< 1h)**: 251 coins (81.0%) ✅
- **🟡 Stale (1-6h)**: 0 coins (0.0%) ✅
- **🔴 Dead (> 6h)**: 0 coins (0.0%) ✅
- **⚪ Missing (no data)**: 59 coins (19.0%) ❌

### Issue: 59 Coins Have No Data

These coins have **never** had a DECISION_TRACE event logged, meaning they don't appear in the dashboard at all.

**Missing Coins** (sample):
- 1MBABYDOGE/USD, ACE/USD, AI/USD, BANANA/USD, BB/USD, BEL/USD, BOME/USD
- BRETT/USD, CATI/USD, CETUS/USD, CFX/USD, CGPT/USD, CHF/USD
- ... and 46 more

## Root Causes

### 1. **Coins Not Yet Processed** (Most Likely)
- These are likely new coins added to the monitored list
- Live trading hasn't processed them yet (or was stopped before processing them)
- They'll get data once live trading runs and processes them

### 2. **Silent Failures** (Possible)
- Coins that fail before reaching trace logging code
- API errors that don't trigger our error handlers
- Invalid symbols that are filtered out early

### 3. **Live Trading Not Running**
- Current status: **Live trading is NOT running**
- Traces are fresh (1.1 min old), meaning it was running recently but stopped
- Missing coins won't get data until live trading restarts

## Fixes

### Fix 1: Ensure All Coins Get Initial Trace Events

**Problem**: New coins don't have any trace events until they're processed.

**Solution**: Log initial "initializing" trace events for all missing coins.

**Run**:
```bash
python3 scripts/fix_missing_coins.py
```

This will:
- Identify all coins without DECISION_TRACE events
- Log initial trace events with `status: "initializing"`
- Make them visible in dashboard (with "dead" status until processed)

### Fix 2: Start Live Trading

**Problem**: Live trading is not running, so coins won't get fresh data.

**Solution**: Start live trading to process all coins.

**Run**:
```bash
python3 run.py live --force
```

This will:
- Process all 310 monitored coins
- Log DECISION_TRACE events every 3 minutes per coin
- Update dashboard with fresh data

### Fix 3: Verify Coin Validity

**Problem**: Some coins might not exist on the exchange, causing silent failures.

**Solution**: Check if missing coins are valid symbols.

**Check**:
```bash
# Check if a specific coin exists
python3 -c "
from src.data.kraken_client import KrakenClient
from src.config.config import load_config
import asyncio

async def check():
    config = load_config()
    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        futures_api_key=config.exchange.futures_api_key,
        futures_api_secret=config.exchange.futures_api_secret
    )
    
    # Test a missing coin
    try:
        ticker = await client.get_spot_ticker('ACE/USD')
        print(f'ACE/USD exists: {ticker}')
    except Exception as e:
        print(f'ACE/USD error: {e}')
    
    await client.close()

asyncio.run(check())
"
```

### Fix 4: Monitor Processing Coverage

**Problem**: We don't know if all coins are being processed in each tick.

**Solution**: Add logging to track which coins are processed vs skipped.

**Already Implemented**: The code now logs traces for:
- ✅ Circuit breaker skips
- ✅ No price errors
- ✅ Fetch errors
- ✅ Processing errors

**Check logs**:
```bash
# Check which coins are being skipped
grep "skipping\|Skipping" logs/live_trading_stdout.log | tail -50
```

## Immediate Actions

### Step 1: Log Initial Traces (Quick Fix)
```bash
python3 scripts/fix_missing_coins.py
```

This makes missing coins visible in dashboard with "initializing" status.

### Step 2: Start Live Trading
```bash
python3 run.py live --force
```

This processes all coins and generates fresh data.

### Step 3: Monitor Progress
```bash
# Watch dashboard or check freshness
python3 scripts/check_data_freshness.py
```

## Expected Results

After running fixes:

1. **All 310 coins visible in dashboard**
   - Missing coins show as "initializing" or "dead" initially
   - Status updates to "active" once processed

2. **Fresh data for all coins**
   - DECISION_TRACE events logged every 3 minutes
   - Dashboard shows real-time status

3. **No silent failures**
   - All errors logged with trace events
   - Dashboard shows error status for problematic coins

## Prevention

### Ensure Live Trading Runs Continuously

**Option 1: Systemd Service** (Recommended for production)
```bash
sudo systemctl start trading-system.service
sudo systemctl enable trading-system.service
```

**Option 2: Watchdog** (Auto-restart on failure)
```bash
python3 system_watchdog.py
```

**Option 3: Screen/Tmux** (Development)
```bash
screen -S trading
python3 run.py live --force
# Ctrl+A, D to detach
```

### Monitor Freshness Regularly

**Daily Check**:
```bash
python3 scripts/check_data_freshness.py
```

**Automated Alert** (if freshness drops):
```bash
# Add to crontab
0 */6 * * * python3 /path/to/scripts/check_data_freshness.py | grep "Dead\|Missing" && echo "Alert: Stale data detected" | mail -s "Trading System Alert" admin@example.com
```

## Troubleshooting

### If coins still missing after fixes:

1. **Check if symbols are valid**:
   ```bash
   python3 scripts/check_data_freshness.py | grep "Missing"
   ```

2. **Check live trading logs**:
   ```bash
   tail -100 logs/live_trading_stdout.log | grep -i "error\|skip\|fail"
   ```

3. **Check API connectivity**:
   ```bash
   python3 -c "from src.data.kraken_client import KrakenClient; import asyncio; client = KrakenClient(...); print(asyncio.run(client.test_connection()))"
   ```

4. **Check database**:
   ```bash
   sqlite3 trading.db "SELECT COUNT(*) FROM system_events WHERE event_type='DECISION_TRACE' AND timestamp > datetime('now', '-1 hour');"
   ```

## Summary

**Current Issue**: 59 coins (19%) have no data because:
1. Live trading is not running
2. These coins haven't been processed yet
3. They may be new additions to the monitored list

**Quick Fix**: 
1. Run `scripts/fix_missing_coins.py` to log initial traces
2. Start live trading with `python3 run.py live --force`
3. Monitor with `scripts/check_data_freshness.py`

**Long-term**: Ensure live trading runs continuously via systemd or watchdog.
