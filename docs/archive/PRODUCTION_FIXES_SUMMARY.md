# Production Fixes Summary

## Date: 2026-01-26

This document summarizes the three production fixes implemented to address critical issues in the trading system.

---

## Issue 1: Auction Execution Blocked by Stale Margin

### Problem
Auction mode created allocation plans with opens, but execution called `_handle_signal()` which re-ran margin validation with stale `available_margin` from before closes. Opens were rejected even though auction already sized them.

### Solution
1. **Margin refresh after closes**: `_run_auction_allocation()` now refreshes account state after executing closes
2. **Override parameters**: `_handle_signal()` accepts `available_margin_override`, `notional_override`, and `skip_margin_check`
3. **RiskManager support**: `validate_trade()` supports `skip_margin_check` to bypass margin validation when auction already validated
4. **Enhanced logging**: Logs auction signals count, plan summary, refreshed margin, and execution results

### Files Modified
- `src/live/live_trading.py`: Auction execution, `_handle_signal()` signature
- `src/risk/risk_manager.py`: `validate_trade()` with override support
- `src/portfolio/auction_allocator.py`: `CandidateSignal` now stores `position_notional`

### Tests Added
- `tests/test_auction_margin_refresh.py`: Tests for `notional_override` and `skip_margin_check`

---

## Issue 2: Futures Ticker Mapping Failures (LUNA2/THETA)

### Problem
- Signals generated from SPOT candles, execution on FUTURES
- Futures ticker lookup failed for some assets (LUNA2, THETA) because mapping format didn't match Kraken ticker keys
- Discovery might yield CCXT unified symbols while some paths assumed PF_* format

### Solution
1. **Normalized ticker keys**: `get_futures_tickers_bulk()` now returns tickers keyed by multiple formats:
   - Original raw: `PI_THETAUSD`
   - PF_ format: `PF_THETAUSD`
   - CCXT unified: `THETA/USD:USD`
   - BASE/USD: `THETA/USD`
2. **Improved mapping**: `FuturesAdapter.map_spot_to_futures()` now:
   - Checks discovery override first
   - Looks up in futures tickers for best executable symbol
   - Prefers CCXT unified, then PF_, then raw
3. **Candle source logging**: Rate-limited logs indicate "spot" vs "futures_fallback" per symbol

### Files Modified
- `src/data/kraken_client.py`: `get_futures_tickers_bulk()` normalization
- `src/execution/futures_adapter.py`: `map_spot_to_futures()` with ticker lookup
- `src/data/candle_manager.py`: Added data source logging
- `src/live/live_trading.py`: Pass tickers to `map_spot_to_futures()`

### Tests Added
- `tests/test_futures_ticker_normalization.py`: Tests for normalization and mapping

---

## Issue 3: ShockGuard - Wick/Flash Move Protection

### Problem
System vulnerable to crypto wicks/flash moves that could liquidate positions or cause large losses. No explicit mechanism to pause entries and reduce exposure during extreme volatility.

### Solution
1. **ShockGuard class**: New `src/risk/shock_guard.py` with:
   - Detection: 1-minute move, range spike, basis spike, market-wide shock
   - Response: Entry pausing, exposure reduction (CLOSE/TRIM)
   - State tracking: Cooldown management, price history
2. **Integration**: Integrated into main trading loop:
   - Updates mark prices after ticker fetch
   - Evaluates shock conditions
   - Pauses entries if shock active
   - Runs exposure reduction once per tick
3. **Config**: Added 9 new parameters to `RiskConfig`

### Files Created
- `src/risk/shock_guard.py`: ShockGuard implementation

### Files Modified
- `src/config/config.py`: Added ShockGuard config to `RiskConfig`
- `src/live/live_trading.py`: ShockGuard initialization and integration

### Tests Added
- `tests/test_shock_guard.py`: Tests for detection thresholds and exposure actions

---

## Configuration Changes

### New Config Keys (RiskConfig)

```yaml
risk:
  shock_guard_enabled: true
  shock_move_pct: 0.025
  shock_range_pct: 0.04
  basis_shock_pct: 0.015
  shock_cooldown_minutes: 30
  emergency_buffer_pct: 0.10
  trim_buffer_pct: 0.18
  shock_marketwide_count: 3
  shock_marketwide_window_sec: 60
```

**No environment variables required** - all config via YAML.

---

## Files Changed

### Modified
1. `src/live/live_trading.py` - Auction execution, ShockGuard integration, candle logging
2. `src/risk/risk_manager.py` - `skip_margin_check` parameter
3. `src/portfolio/auction_allocator.py` - `CandidateSignal.position_notional` field
4. `src/data/kraken_client.py` - Ticker normalization
5. `src/execution/futures_adapter.py` - Improved symbol mapping
6. `src/data/candle_manager.py` - Data source logging
7. `src/config/config.py` - ShockGuard config

### Created
1. `src/risk/shock_guard.py` - ShockGuard implementation
2. `tests/test_auction_margin_refresh.py` - Issue 1 tests
3. `tests/test_futures_ticker_normalization.py` - Issue 2 tests
4. `tests/test_shock_guard.py` - Issue 3 tests
5. `RISK_SHOCK_GUARD.md` - Documentation
6. `PRODUCTION_FIXES_SUMMARY.md` - This file

---

## How to Run Tests

```bash
# Run all new tests
python -m pytest tests/test_auction_margin_refresh.py tests/test_futures_ticker_normalization.py tests/test_shock_guard.py -v

# Run specific test file
python -m pytest tests/test_shock_guard.py -v

# Run with coverage
python -m pytest tests/test_auction_margin_refresh.py tests/test_futures_ticker_normalization.py tests/test_shock_guard.py --cov=src --cov-report=term-missing
```

---

## Verification Checklist

### Issue 1: Auction Execution
- [x] Margin refreshed after closes
- [x] `_handle_signal()` accepts overrides
- [x] `RiskManager.validate_trade()` supports `skip_margin_check`
- [x] Auction opens use refreshed margin and overrides
- [x] Enhanced logging added
- [x] Tests added

### Issue 2: Futures Mapping
- [x] `get_futures_tickers_bulk()` normalizes to multiple formats
- [x] `map_spot_to_futures()` uses ticker lookup
- [x] Candle source logging added
- [x] Tests added

### Issue 3: ShockGuard
- [x] ShockGuard class created
- [x] Detection mechanisms implemented
- [x] Exposure reduction actions implemented
- [x] Integrated into live loop
- [x] Config added
- [x] Tests added

---

## Production Deployment

### No Breaking Changes
- Production start command unchanged: `python run.py live --force`
- No new environment variables required
- All config via YAML (existing pattern)

### Monitoring
After deployment, monitor logs for:
- Auction execution: "Auction: Margin refreshed after closes"
- Futures mapping: "Futures symbol not found" should decrease
- ShockGuard: "SHOCK_MODE ACTIVATED" events

---

## Next Steps

1. Deploy to production using deployment workflow
2. Monitor logs for auction execution success
3. Verify LUNA2/THETA are now tradable
4. Watch for ShockGuard activation during volatile periods
