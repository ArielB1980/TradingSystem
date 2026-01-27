# Auction Symbol Matching Fix - Multiple Positions Per Symbol

## Problem Identified
The system accumulated a **large position of 6,320 PROMPT** (363.53 USD notional) because the auction opened **5 separate positions** for PROMPT/USD across multiple auction cycles:

1. 14:51:12 - Opened position (~73.54 USD)
2. 15:13:46 - Opened position (~72.57 USD)
3. 15:36:02 - Opened position (~71.17 USD)
4. 16:04:52 - Opened position (~68.46 USD)
5. 16:27:12 - Opened position (~68.77 USD)

**Total**: ~354.51 USD (matches actual position of 363.53 USD with price movement)

## Root Cause

### Symbol Format Mismatch
- **Candidates use spot symbols**: "PROMPT/USD" (from signals)
- **Open positions use futures symbols**: "PF_PROMPTUSD" (from exchange)
- **Auction `max_per_symbol` check**: Compared "PROMPT/USD" vs "PF_PROMPTUSD" directly
- **Result**: No match found, so auction allowed multiple entries

### Why It Happened
The auction allocator's `max_per_symbol` check at line 415 compared:
```python
if symbol_counts.get(contender.symbol, 0) >= self.limits.max_per_symbol:
```

But:
- `contender.symbol` for candidates = "PROMPT/USD" (spot)
- `contender.symbol` for open positions = "PF_PROMPTUSD" (futures)
- These don't match, so the check failed to prevent duplicates

## Solution Implemented

### 1. Symbol Normalization Function
**File**: `src/portfolio/auction_allocator.py`

Added `_normalize_symbol_for_matching()` function that:
- Removes Kraken prefixes (PF_, PI_, FI_)
- Removes CCXT suffixes (:USD)
- Removes separators (/, -, _)
- Converts "PROMPT/USD" and "PF_PROMPTUSD" both to "PROMPTUSD"

### 2. Store Spot Symbol in Metadata
**File**: `src/live/live_trading.py`

- Build reverse mapping from futures → spot symbols
- Store `spot_symbol` in `OpenPositionMetadata`
- Use spot symbol when creating contenders from open positions

### 3. Normalized Symbol Matching
**File**: `src/portfolio/auction_allocator.py`

- Use normalized symbols in `max_per_symbol` check
- Compare normalized versions: "PROMPTUSD" == "PROMPTUSD" ✓
- Track winners by normalized symbol

## Changes Made

### `src/portfolio/auction_allocator.py`
1. Added `_normalize_symbol_for_matching()` function
2. Added `spot_symbol` field to `OpenPositionMetadata` dataclass
3. Modified `_build_contender_list()` to use spot symbol for open positions
4. Modified `_select_winners()` to use normalized symbols for matching

### `src/live/live_trading.py`
1. Build spot-to-futures mapping before processing positions
2. Store spot_symbol in OpenPositionMetadata
3. Derive spot symbol from futures symbol if not found in signals

## Expected Behavior

### Before Fix
- Auction sees "PROMPT/USD" candidate
- Checks if "PROMPT/USD" exists in winners → No
- Checks if "PF_PROMPTUSD" exists → No (different symbol)
- Allows duplicate entry ❌

### After Fix
- Auction sees "PROMPT/USD" candidate
- Normalizes to "PROMPTUSD"
- Checks if "PROMPTUSD" exists in winners → Yes (from existing "PF_PROMPTUSD" position)
- Rejects duplicate entry ✅

## Impact

### ✅ Prevents Multiple Positions
- `auction_max_per_symbol: 1` now properly enforced
- No more pyramiding through auction cycles
- Position size stays within intended limits

### ✅ Position Size Analysis
The 6,320 PROMPT position is actually **5 separate positions** that should have been prevented:
- Each ~$70 notional (correct for equity × leverage × risk%)
- Total ~$354 USD (matches actual position)
- This is a **bug**, not intended behavior

## Status

✅ **Committed to GitHub** (commit `511c42f`)
✅ **Deployed to production**
✅ **Service restarted** (16:35:49 UTC)

## Verification

Monitor next auction cycles for:
- ✅ No duplicate positions for same symbol
- ✅ `max_per_symbol` properly enforced
- ✅ Position sizes stay within single-entry limits

## Recommendation

**Review existing PROMPT position**: The current 6,320 PROMPT position represents 5 separate entries that should have been prevented. Consider:
1. Manually closing excess size if desired
2. Letting it run (it's within risk limits, just larger than intended)
3. Monitoring to ensure no more duplicates occur
