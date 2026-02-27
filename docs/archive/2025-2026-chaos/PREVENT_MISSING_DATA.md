# Preventing Missing Coin Data

## Problem

Coins can end up without DECISION_TRACE events, making them invisible in the dashboard. This happens when:
1. New coins are added to the monitored list but not processed yet
2. Live trading stops/restarts and misses some coins
3. Coins fail silently before trace logging occurs
4. Race conditions during startup

## Solution: Multi-Layer Prevention

### Layer 1: Startup Validation ✅

**What**: On every startup, ensure all monitored coins have at least one DECISION_TRACE event.

**Where**: `src/live/startup_validator.py`

**When**: Runs automatically when `LiveTrading.run()` starts

**How it works**:
```python
# In live_trading.py run() method:
from src.live.startup_validator import ensure_all_coins_have_traces
validation_result = await ensure_all_coins_have_traces(self.markets)
```

**Result**: 
- Missing coins get initial trace events with `status: "initializing"`
- All coins become visible in dashboard immediately
- Real data arrives once processing starts

### Layer 2: Periodic Maintenance ✅

**What**: Hourly check to catch any coins that become stale or missing.

**Where**: `src/live/maintenance.py`

**When**: Runs every hour during live trading loop

**How it works**:
```python
# In live_trading.py _tick() method:
if (now - self.last_data_maintenance).total_seconds() > 3600:
    await periodic_data_maintenance(self.markets)
```

**Result**:
- Catches coins that become stale (> 6 hours old)
- Creates traces for any coins that lost data
- Prevents accumulation of missing coins

### Layer 3: Error Handling ✅

**What**: All error paths now log DECISION_TRACE events.

**Where**: `src/live/live_trading.py` - `process_coin()` function

**Coverage**:
- ✅ Circuit breaker skips → logs trace with `status: "circuit_breaker_open"`
- ✅ No price errors → logs trace with `status: "no_price"` or `"zero_price"`
- ✅ Fetch errors → logs trace with `status: "fetch_error"`
- ✅ Processing errors → logs trace with `status: "error"`

**Result**: No coin can fail silently without leaving a trace.

### Layer 4: Monitoring & Alerts ✅

**What**: Scripts to check and fix data freshness.

**Tools**:
1. `scripts/check_data_freshness.py` - Analyze current state
2. `scripts/fix_missing_coins.py` - Fix missing coins manually

**Usage**:
```bash
# Check freshness
python3 scripts/check_data_freshness.py

# Fix missing coins (if needed)
python3 scripts/fix_missing_coins.py
```

## Implementation Details

### Startup Validator

**File**: `src/live/startup_validator.py`

**Functions**:
- `ensure_all_coins_have_traces()` - Creates initial traces for missing coins
- `validate_market_coverage()` - Validates coverage percentage

**Integration**: Called automatically in `LiveTrading.run()` before main loop

### Periodic Maintenance

**File**: `src/live/maintenance.py`

**Functions**:
- `periodic_data_maintenance()` - Checks for stale/missing data and fixes

**Integration**: Called automatically in `LiveTrading._tick()` every hour

## Testing

### Test Startup Validation

```python
# Test that startup validation works
from src.live.startup_validator import ensure_all_coins_have_traces
result = await ensure_all_coins_have_traces(['BTC/USD', 'ETH/USD', 'NEWCOIN/USD'])
assert result['total'] == 3
```

### Test Missing Coin Detection

```bash
# Run freshness check
python3 scripts/check_data_freshness.py

# Should show 0 missing coins after fixes
```

## Monitoring

### Dashboard Indicators

- **Status Column**: Shows freshness status (🟢 Active, 🟡 Stale, 🔴 Dead)
- **Status Bar**: Shows active/stale/dead counts
- **Last Update Column**: Shows time since last update

### Log Monitoring

```bash
# Check for initialization logs
grep "startup_initialization\|startup validation" logs/live_trading_stdout.log

# Check for maintenance logs
grep "periodic data maintenance\|Data maintenance" logs/live_trading_stdout.log
```

## Best Practices

### 1. Always Start Live Trading Properly

**Good**:
```bash
python3 run.py live --force
```

**Bad**: Starting without proper initialization (bypasses startup validation)

### 2. Monitor Freshness Regularly

**Daily Check**:
```bash
python3 scripts/check_data_freshness.py
```

**Automated Alert** (if coverage drops):
```bash
# Add to crontab
0 */6 * * * python3 scripts/check_data_freshness.py | grep -q "Missing\|Dead" && echo "Alert: Data freshness issue" | mail admin@example.com
```

### 3. Keep Live Trading Running

**Production**: Use systemd service or watchdog
```bash
sudo systemctl start trading-system.service
```

**Development**: Use screen/tmux
```bash
screen -S trading python3 run.py live --force
```

### 4. Add New Coins Properly

When adding new coins to monitored list:
1. Add to config/discovered markets
2. Restart live trading (startup validation will handle initialization)
3. Verify with `scripts/check_data_freshness.py`

## Troubleshooting

### If coins still missing after fixes:

1. **Check startup logs**:
   ```bash
   grep "startup validation\|Created.*initial traces" logs/live_trading_stdout.log
   ```

2. **Check if live trading is running**:
   ```bash
   ps aux | grep "run.py live"
   ```

3. **Manually fix**:
   ```bash
   python3 scripts/fix_missing_coins.py
   ```

4. **Check database**:
   ```bash
   sqlite3 trading.db "SELECT COUNT(*) FROM system_events WHERE event_type='DECISION_TRACE' AND symbol='NEWCOIN/USD';"
   ```

## Summary

**Prevention Layers**:
1. ✅ **Startup Validation** - Ensures all coins have traces on startup
2. ✅ **Periodic Maintenance** - Hourly check for stale/missing data
3. ✅ **Error Handling** - All error paths log traces
4. ✅ **Monitoring Tools** - Scripts to check and fix issues

**Result**: Missing coin data should never happen again. If it does, the system will auto-fix within 1 hour (periodic maintenance) or immediately on next startup (startup validation).
