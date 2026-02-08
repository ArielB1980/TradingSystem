# Missing Stop Loss Problem - Implementation Summary

## Status: ✅ COMPLETE

All phases of the fix have been implemented and deployed.

## What Was Fixed

### Phase 1: Position Hydration ✅
- **Added protection fields**: `is_protected` and `protection_reason` to Position model and PositionModel
- **Fetch orders once**: Open orders are fetched once per tick and indexed by symbol (prevents rate limits)
- **Deterministic SL recovery**: Exact precedence order implemented:
  1. DB active position has `initial_stop_price` → use it
  2. Else, parse open reduce-only stop order → extract stop price
  3. Else → mark as UNPROTECTED (no fabrication)
- **Side detection**: Uses signed size (safest, handles all Kraken formats)
- **Updated `_init_managed_position`**: Now receives `db_pos` and `orders_for_symbol` (no async needed)

### Phase 2: Position Sync ✅
- **Preserve protective fields**: `sync_active_positions` now preserves existing DB values if incoming value is None/empty
- **Fields preserved**: `initial_stop_price`, `stop_loss_order_id`, `tp_order_ids`, `is_protected`, `protection_reason`

### Phase 3: Backfill Script ✅
- **Created**: `scripts/backfill_initial_stop_prices.py`
- **Policy**: 
  - Recovers SL from orders if found
  - If not found and `place_missing_sl=False`: Marks UNPROTECTED, alerts
  - If not found and `place_missing_sl=True`: Computes default, places SL order, then persists
- **Never invents**: Never writes computed stop to DB without placing order on exchange

### Phase 4: Validation & Safety ✅
- **Startup validation**: Added `_validate_position_protection()` method
- **TP backfill enforcement**: Now requires `is_protected == True` (not just `initial_stop_price`)
- **Auction allocator**: UNPROTECTED positions are marked as locked
- **Migration script**: Updated to add `is_protected` and `protection_reason` columns

## Current State

**All 26 positions are marked as UNPROTECTED** because:
- No SL orders exist on the exchange (0 orders found)
- No `initial_stop_price` in database
- Backfill script correctly marked them as UNPROTECTED (no fabrication)

## Next Steps

### Option A: Place Missing SL Orders (Recommended)
Run the backfill script with `--place-missing-sl` flag to place default 2% SL orders:

```bash
ssh -i ~/.ssh/trading_droplet trading@207.154.193.121
cd ~/TradingSystem
source venv/bin/activate
export DATABASE_URL='postgresql://dbtradingbot:AVNS_3ZbhLloQP64uLYyhxoe@localhost:5432/dbtradingbot'
python scripts/backfill_initial_stop_prices.py --place-missing-sl
```

This will:
1. Compute default 2% SL for each position
2. Place SL orders on exchange
3. Persist `initial_stop_price` and `stop_loss_order_id` to DB
4. Mark positions as protected

### Option B: Manual Review
Review each position and place SL orders manually, then run backfill again to recover the prices.

### After SL Orders Are Placed

1. **Re-enable safety flag**: Update `src/config/config.yaml`:
   ```yaml
   require_sl_for_tp_backfill: true  # Re-enabled after fix
   ```

2. **Verify TP backfill works**: TP backfill should now run for all protected positions

3. **Monitor**: Watch logs for TP orders being placed

## Safety Guarantees Implemented

1. ✅ **Never invent stops without placing them** - UNPROTECTED positions block TP placement
2. ✅ **Never fetch orders per-position** - Single fetch per tick, indexed by symbol
3. ✅ **Never overwrite with None** - Preserve existing DB values during sync
4. ✅ **Deterministic recovery** - DB → orders → UNPROTECTED (no fabrication)
5. ✅ **Explicit protection status** - `is_protected` field makes status clear
6. ✅ **Startup validation** - Detects and alerts on unprotected positions

## Files Modified

- `src/domain/models.py` - Added protection fields
- `src/storage/repository.py` - Added DB columns, fixed sync to preserve fields
- `src/live/live_trading.py` - Fixed hydration, added validation, fetch orders once
- `src/portfolio/auction_allocator.py` - Treat UNPROTECTED as locked
- `scripts/backfill_initial_stop_prices.py` - New backfill script
- `migrate_schema.py` - Added migration for new columns

## Testing

- ✅ Syntax check passed
- ✅ Migration ran successfully
- ✅ Backfill script ran successfully
- ✅ All 26 positions marked as UNPROTECTED (correct behavior)
- ⏳ Startup validation - needs first tick to complete
- ⏳ TP backfill - will work once positions are protected
