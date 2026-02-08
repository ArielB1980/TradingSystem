# GitHub Push Instructions

## Status
✅ **Fix is deployed to production** (file copied directly)  
❌ **GitHub not updated** (2 commits pending push)

## Commits Ready to Push

### Commit 1: `d0356e6`
**Fix: Convert CCXT unified symbols to PF_* format for instrument specs lookup**

**Files Changed:**
- `src/execution/futures_adapter.py` - Added symbol conversion logic

**What it fixes:**
- Auction opens were failing with "Instrument specs not found"
- Converts CCXT unified format (ONE/USD:USD) → PF_* format (PF_ONEUSD) for instrument lookup
- Ensures instrument specs can be found regardless of symbol format

### Commit 2: `a2979dd`
**Test: Add symbol conversion tests for CCXT unified format**

**Files Changed:**
- `tests/test_futures_adapter_symbol_conversion_simple.py` - New test file

**What it adds:**
- Unit tests for symbol conversion logic
- Tests CCXT unified → PF_* conversion
- Tests edge cases

## How to Push via Cursor

1. **Open Cursor's Source Control panel** (Ctrl/Cmd + Shift + G)
2. **You should see 2 commits** ready to push
3. **Click the "Sync Changes" button** or use the push icon
4. **If authentication is needed**, Cursor will prompt you

## Alternative: Manual Push

If Cursor's Git doesn't work, you can:

1. **Check current status:**
   ```bash
   git status
   git log origin/main..HEAD
   ```

2. **Push via terminal** (after fixing token):
   ```bash
   git push origin main
   ```

## Current Branch Status

- **Branch:** `main`
- **Ahead of origin/main:** 2 commits
- **Working tree:** Clean (no uncommitted changes)

## Production Status

✅ Fix is live on production server  
✅ Service restarted with fix at 10:57:36 UTC  
✅ Monitoring active for next auction cycle

The fix will work in production regardless of GitHub status, but pushing to GitHub ensures:
- Code is backed up
- Server can pull updates in future
- Version history is maintained
