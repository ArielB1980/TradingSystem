# Stop Loss Order Tracking Issue

## Problem
The system is alerting about "UNPROTECTED positions" but stop loss orders are actually present on the exchange. This is a **tracking/categorization issue**, not a protection issue.

## User Observation
- Stop loss orders are visible in open orders on exchange (confirmed for PAXG)
- Orders will function as stop losses even if system doesn't recognize them
- System is incorrectly flagging positions as unprotected

## Root Cause
The system checks for protection using three criteria:
1. `pos.is_protected` flag
2. `pos.initial_stop_price` 
3. `pos.stop_loss_order_id` (must exist and not start with "unknown_")

**Issue**: Stop loss orders are being placed on the exchange, but:
- The `stop_loss_order_id` is not being saved to the database after order placement
- OR the order ID is being saved but the `is_protected` flag is not being updated
- OR the order type is not being recognized as a "stop" order

## Code Locations

### Protection Check
- `src/live/live_trading.py:642-643`: Checks `if not pos.is_protected or not pos.initial_stop_price or not pos.stop_loss_order_id:`
- `src/storage/repository.py:623`: Sets `is_protected = (pm.initial_stop_price is not None and pm.stop_loss_order_id is not None and not str(pm.stop_loss_order_id).startswith('unknown'))`

### Stop Loss Order Placement
- `src/execution/executor.py:327-357`: `place_protective_orders()` method places SL orders
- `src/live/live_trading.py:1556`: `update_protective_orders()` called after entry fill

## Impact
- **Low Risk**: Orders are on exchange and will function correctly
- **Medium Impact**: False alerts causing confusion
- **High Priority**: Should fix tracking to ensure system state matches exchange reality

## Recommended Fix
1. **Immediate**: Add reconciliation logic to match exchange stop orders with database positions
2. **Short-term**: Ensure `stop_loss_order_id` is saved immediately after order placement
3. **Long-term**: Add periodic reconciliation to sync order IDs from exchange

## Verification
- Check exchange for open stop orders matching positions
- Compare exchange order IDs with database `stop_loss_order_id` values
- Verify order type classification (ensure "stop" orders are recognized)
