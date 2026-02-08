# Critical Deployment Fixes - 2026-01-20

## Issues Identified from Log Analysis

### 1. Database Schema Error (CRITICAL)
**Problem:** `no such column: positions.trade_type`
- The Position model in code has fields that don't exist in the database
- This blocks ALL position tracking and management
- System cannot persist position state to database

**Root Cause:**
- ORM model `PositionModel` in `src/storage/repository.py` was missing 8 columns
- Migration was never run after Position domain model was extended

**Fix Applied:**
- Updated `src/storage/repository.py` PositionModel to include:
  - `trade_type` (VARCHAR)
  - `partial_close_pct` (NUMERIC)
  - `original_size` (NUMERIC)
  - `tp_order_ids` (TEXT/JSON)
  - `basis_at_entry`, `basis_current` (NUMERIC)
  - `funding_rate`, `cumulative_funding` (NUMERIC)

- Created `scripts/fix_position_schema.py` migration script
- This will run automatically on next deployment (SQLAlchemy creates missing columns)

### 2. Unmanaged Positions (CRITICAL)
**Problem:** 14 active positions NOT being managed by the system
- Positions: CHZ, CTSI, DOG, DOGE, JASMY, KAS, PENGU, PEPE, PNUT, POL, SAGA, TIA, W, ETC
- These exist on Kraken but not in position manager
- No TP/SL management, no risk monitoring

**Root Cause:**
- Positions were opened before the position manager was properly initialized
- OR database errors prevented them from being saved

**Fix Applied:**
- Created `scripts/import_unmanaged_positions.py` to import existing positions
- This script will need to be run manually on the server (instructions below)

### 3. Recent Code Changes Not Deployed
**Problem:** Latest changes (leverage fix, position sizing, order timeout) not live
- Config still shows old settings
- Leverage fix (7x target) not applied
- Order timeout/price invalidation not active

**Fix Applied:**
- This commit includes all previous fixes
- Deployment will automatically apply the new config

## Deployment Instructions

### Automatic (on git push)
```bash
git add -A
git commit -m "Fix database schema and add position import tools"
git push origin main
```

This will:
1. Deploy updated code to DigitalOcean
2. SQLAlchemy will create missing database columns automatically
3. New config will be applied (7x leverage, 120s timeout, 3% price invalidation)

### Manual Steps Required (SSH into DO App)

After deployment, run these commands on the server:

```bash
# 1. Fix database schema (if auto-migration doesn't work)
python3 scripts/fix_position_schema.py

# 2. Import unmanaged positions
python3 scripts/import_unmanaged_positions.py
```

## Expected Outcomes

### After Schema Fix:
- ✓ No more "no such column: positions.trade_type" errors
- ✓ Position sync to DB will work
- ✓ Position management loop will function properly

### After Position Import:
- ✓ All 14 positions tracked in position manager
- ✓ TP/SL management active for existing positions
- ✓ Risk monitoring for all positions

### After Config Deployment:
- ✓ New trades use 7x leverage (not 1x)
- ✓ Position sizes 1-3% (Kelly+Volatility adaptive)
- ✓ Orders cancelled after 120s OR 3% price move
- ✓ Multi-TP system active (TP1: 40%, TP2: 40%, Runner: 20%)

## Monitoring

After deployment, check logs for:
1. No more database schema errors
2. "Imported X positions" message
3. Position management loop running without errors
4. New trades opening with correct leverage (7x not 1x)

## Rollback Plan

If issues occur:
```bash
git revert HEAD
git push origin main
```

This will revert to previous version. The database columns won't cause issues as they're nullable.
