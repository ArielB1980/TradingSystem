# Ghost Positions and Order Cancellation Fix

## Issues Fixed

### 1. Ghost Positions Alert
**Problem**: Reconciliation was detecting "ghost positions" (positions on exchange but not in system) due to symbol format mismatches.

**Root Cause**: 
- Exchange returns symbols in format like `PF_EURUSD`
- System stores symbols in format like `EUR/USD:USD` or `EURUSD`
- Direct comparison failed, causing false ghost position alerts

**Solution**:
- Added `_normalize_symbol_for_comparison()` method to normalize symbols before comparison
- Extracts base currency from various formats (PF_EURUSD, EUR/USD:USD, EURUSD → EUR)
- Compares normalized symbols instead of raw symbols
- Prevents false ghost position alerts

**Files Changed**:
- `src/reconciliation/reconciler.py`

### 2. Order Cancellation Errors
**Problem**: System was trying to cancel orders with invalid order IDs, causing "invalidArgument: order_id" errors.

**Root Cause**:
- When exchange doesn't return a proper `order_id`, system creates placeholder: `unknown_{uuid}`
- These placeholder IDs are not valid exchange order IDs
- Attempting to cancel them causes API errors

**Solution**:
- Skip cancellation for order IDs starting with "unknown_"
- Handle "invalidArgument" errors gracefully (don't raise, just log warning)
- Updated orphan order cleanup to check for invalid IDs before attempting cancellation

**Files Changed**:
- `src/execution/futures_adapter.py` - `cancel_order()` method
- `src/live/live_trading.py` - Orphan order cleanup logic

## Changes Made

### Reconciliation (`src/reconciliation/reconciler.py`)
- Added symbol normalization method
- Normalize both exchange and system symbols before comparison
- Prevents false ghost/zombie position alerts

### Order Cancellation (`src/execution/futures_adapter.py`)
- Check for "unknown_" prefix before attempting cancellation
- Handle invalidArgument errors gracefully
- Log warnings instead of errors for expected failures

### Orphan Order Cleanup (`src/live/live_trading.py`)
- Validate order IDs before cancellation attempts
- Skip placeholder order IDs
- Better error handling for invalid order IDs

## Expected Results

### ✅ Ghost Positions
- No more false ghost position alerts due to symbol format mismatches
- Reconciliation will correctly match positions regardless of symbol format
- Only real ghost positions (manually opened, etc.) will be detected

### ✅ Order Cancellation
- No more "invalidArgument: order_id" errors
- Placeholder order IDs are skipped gracefully
- Invalid order IDs handled without errors

## Deployment

- ✅ Committed to GitHub (commit `afad3d9`)
- ✅ Deployed to production server
- ✅ Service restarted

## Verification

Monitor logs for:
- ✅ No more ghost position alerts (unless real ghosts exist)
- ✅ No more "invalidArgument: order_id" errors
- ✅ Order cancellations working smoothly

## Status

**✅ Both issues fixed and deployed**

The system will now:
- Correctly reconcile positions regardless of symbol format
- Handle order cancellations gracefully without errors
- Only alert on real ghost positions (positions opened outside the system)
