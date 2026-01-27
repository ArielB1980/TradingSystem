# Instrument Lookup Fix V2

## Problem

Orders were still failing with "Instrument specs not found" errors even after the initial fix. The issue was that the lookup logic wasn't trying enough symbol format variants.

## Root Cause

The instruments API returns symbols in `PF_SUNUSD` format, but the code was receiving symbols in `SUN/USD:USD` (CCXT unified) format. The initial fix tried to convert and match, but:

1. The conversion logic wasn't robust enough
2. It didn't try all possible format variants
3. Case sensitivity and whitespace weren't handled

## Solution

Improved the lookup logic to:

1. **Extract base currency more robustly** - Handles multiple input formats:
   - `SUN/USD:USD` → extracts `SUN`
   - `PF_SUNUSD` → extracts `SUN`
   - `SUNUSD` → extracts `SUN`

2. **Try multiple format variants**:
   - `PF_SUNUSD` (Kraken format)
   - `SUNUSD` (without prefix)
   - `SUN/USD:USD` (CCXT unified)
   - `SUN/USD` (spot format)
   - Original symbol (as-is)

3. **Case-insensitive matching with whitespace handling**:
   - Strips whitespace from instrument symbols
   - Case-insensitive comparison

## Changes Made

**File**: `src/execution/futures_adapter.py` (lines 265-308)

**Before**: Simple conversion to PF_* format with limited fallback
**After**: Robust base extraction + multiple format variant attempts

## Deployment

- ✅ Committed to GitHub (commit `9dbc74f`)
- ✅ Deployed to production server
- ✅ Service restarted

## Expected Result

Orders should now successfully find instrument specs for symbols like:
- `SUN/USD:USD` → finds `PF_SUNUSD`
- `ONE/USD:USD` → finds `PF_ONEUSD`
- `PAXG/USD:USD` → finds `PF_PAXGUSD`
- `DYM/USD:USD` → finds `PF_DYMUSD`
- `API3/USD:USD` → finds `PF_API3USD`

## Monitoring

Watch for:
- ✅ "Entry order submitted" messages (success)
- ❌ No more "Instrument specs not found" errors
- ✅ "Auction: Opened position" followed by successful orders
