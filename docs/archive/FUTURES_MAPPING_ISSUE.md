# Futures Mapping Issue - Signals Not Trading

## Problem

Signals are being generated (e.g., ATOM/USD, OGN/USD, TON/USD) but trades are not being opened because:

1. **Signals are generated** ✅ - System is creating signals correctly
2. **Futures mapping happens** ✅ - Maps to PF_ATOMUSD, PF_OGNUSD, etc.
3. **Futures contract check fails** ❌ - The mapped futures symbol doesn't exist in `map_futures_tickers`
4. **Signal is skipped** ❌ - `is_tradable = False`, so signal is not added to auction

## Root Cause

The system checks if a futures contract exists:
```python
futures_symbol = self.futures_adapter.map_spot_to_futures(spot_symbol)  # e.g., "PF_ATOMUSD"
has_futures = futures_symbol in map_futures_tickers  # Check if exists
is_tradable = bool(has_futures)  # Only tradable if futures exists
```

If `is_tradable = False`, the signal is skipped and logged:
```
"Signal skipped (no futures ticker)"
```

## Why This Happens

1. **Symbol doesn't have futures on Kraken** - Some coins don't have perpetual futures contracts
2. **Mapping mismatch** - The mapping might be wrong (e.g., PF_ATOMUSD vs actual symbol format)
3. **Market discovery not loaded** - The `spot_to_futures_override` might not be populated

## Solution

### Option 1: Use Market Discovery (Recommended)

The system has market discovery that finds actual available futures contracts. This should populate `spot_to_futures_override` in the FuturesAdapter.

**Check if market discovery is running:**
```bash
tail -f logs/run.log | grep -E "market discovery|spot_to_futures_override"
```

### Option 2: Verify Futures Contracts Exist

Check which futures contracts are actually available on Kraken and update the mapping accordingly.

### Option 3: Filter Signals Before Generation

Only generate signals for symbols that have confirmed futures contracts available.

## Debugging

Added debug logging to show:
- Which futures symbol was mapped
- Similar futures symbols that exist
- Total futures available

Check logs for:
```bash
tail -f logs/run.log | grep "Futures symbol not found for signal"
```

## Next Steps

1. **Check market discovery** - Ensure it's running and populating the override mapping
2. **Verify futures availability** - Check which futures contracts actually exist on Kraken
3. **Update mapping** - Fix any incorrect mappings in `FuturesAdapter.TICKER_MAP`
4. **Filter coin universe** - Only monitor coins that have futures contracts
