# Plan: Add TP Orders to Existing Positions

## Problem Statement

The current TP backfill system (`_reconcile_protective_orders`) is blocked because:
1. All 26 positions are UNPROTECTED (no SL orders exist)
2. `_should_skip_tp_backfill` checks `if not db_pos.is_protected` and skips TP placement
3. TP backfill only runs for protected positions in the main trading loop

We need a standalone script that can add TP orders to existing positions, even if they're UNPROTECTED.

## Solution Strategy

Create a new script `scripts/add_tp_to_positions.py` that:
1. Loads all active positions from database
2. Fetches current positions and orders from exchange
3. Computes TP prices using the same logic as `_compute_tp_plan`
4. Places TP orders using `executor.update_protective_orders`
5. Updates database with TP prices and order IDs
6. Works independently of protection status (or with option to require SL first)

## Implementation Plan

### Phase 1: Create Standalone TP Addition Script

**File**: `scripts/add_tp_to_positions.py`

**Key Features**:
- Load positions from DB
- Fetch exchange state (positions + orders)
- Compute TP plan for each position
- Place TP orders via executor
- Update DB with TP prices and order IDs
- Option to require SL first (safety flag)

**Script Structure**:
```python
async def add_tp_to_positions(
    require_sl: bool = True,  # Require SL before placing TP
    dry_run: bool = False,    # Preview without placing orders
    min_tp_count: int = 2     # Minimum TP orders to place
):
    """
    Add TP orders to existing positions.
    
    Args:
        require_sl: If True, only add TP to positions with SL. If False, add TP even without SL.
        dry_run: If True, compute and log TP plan without placing orders.
        min_tp_count: Minimum number of TP orders to place (default: 2).
    """
```

### Phase 2: TP Computation Logic

**Reuse existing logic from `_compute_tp_plan`**:
- Prefer stored TP plan (tp1_price, tp2_price, final_target_price)
- If no stored plan, compute using R-multiples:
  - Risk = abs(entry - sl)
  - TP1 = entry + side_sign * 1.0 * risk
  - TP2 = entry + side_sign * 2.0 * risk
  - TP3 = entry + side_sign * 3.0 * risk
- Apply distance guards (min_tp_distance_pct, max_tp_distance_pct)

**Edge Cases**:
- No SL: If `require_sl=False`, use default 2% stop for TP calculation (but don't place SL)
- No entry price: Skip position (log warning)
- Invalid prices: Skip position (log error)

### Phase 3: Order Placement

**Use `executor.update_protective_orders`**:
- Pass `current_sl_id=None` (don't modify SL)
- Pass `new_sl_price=None` (don't modify SL)
- Pass `current_tp_ids=[]` (cancel existing TPs if any)
- Pass `new_tp_prices=[tp1, tp2, tp3]` (new TP ladder)
- Pass `position_size_notional` (for proper sizing)

**Order Placement Flow**:
1. Cancel existing TP orders (if any)
2. Place new TP ladder (TP1, TP2, TP3)
3. Collect order IDs
4. Update database

### Phase 4: Database Updates

**Update Position in DB**:
- `tp1_price` = first TP price
- `tp2_price` = second TP price (if exists)
- `final_target_price` = third TP price (if exists)
- `tp_order_ids` = JSON array of order IDs
- `updated_at` = current timestamp

**Use `save_position()` to persist changes**

### Phase 5: Safety & Validation

**Pre-flight Checks**:
1. Position exists on exchange (size > 0)
2. Entry price is valid (> 0)
3. If `require_sl=True`: SL exists (initial_stop_price or stop_loss_order_id)
4. Current price is available
5. TP prices are valid (not too close to current price)

**Post-placement Validation**:
1. Verify orders were placed (check order IDs)
2. Verify order prices match plan (within tolerance)
3. Log summary report

### Phase 6: Error Handling

**Graceful Degradation**:
- If one position fails, continue with others
- Log all errors with context
- Return summary: success_count, failed_count, skipped_count

**Common Errors to Handle**:
- Position not found on exchange
- Invalid entry/SL prices
- Order placement failures
- Database update failures
- API rate limits

## Implementation Details

### Key Code Sections

**1. Load Positions**:
```python
from src.storage.repository import get_active_positions
db_positions = await asyncio.to_thread(get_active_positions)
```

**2. Fetch Exchange State**:
```python
exchange_positions = await client.get_all_futures_positions()
exchange_orders = await client.get_futures_open_orders()
```

**3. Compute TP Plan** (reuse logic):
```python
# From _compute_tp_plan
entry = Decimal(str(pos_data.get('entry_price', 0)))
sl = db_pos.initial_stop_price
risk = abs(entry - sl)
side_sign = Decimal("1") if db_pos.side == Side.LONG else Decimal("-1")
tp1 = entry + side_sign * Decimal("1.0") * risk
tp2 = entry + side_sign * Decimal("2.0") * risk
tp3 = entry + side_sign * Decimal("3.0") * risk
```

**4. Place Orders**:
```python
from src.execution.executor import Executor
from src.execution.futures_adapter import FuturesAdapter

executor = Executor(config.execution, futures_adapter)
sl_id, tp_ids = await executor.update_protective_orders(
    symbol=symbol,
    side=db_pos.side,
    current_sl_id=db_pos.stop_loss_order_id,
    new_sl_price=None,  # Don't modify SL
    current_tp_ids=db_pos.tp_order_ids or [],
    new_tp_prices=tp_plan,
    position_size_notional=position_size_notional
)
```

**5. Update Database**:
```python
from src.storage.repository import save_position

db_pos.tp1_price = tp_plan[0] if len(tp_plan) > 0 else None
db_pos.tp2_price = tp_plan[1] if len(tp_plan) > 1 else None
db_pos.final_target_price = tp_plan[2] if len(tp_plan) > 2 else None
db_pos.tp_order_ids = tp_ids
save_position(db_pos)
```

## Testing Strategy

1. **Dry Run Test**: Run with `--dry-run` to preview TP plans without placing orders
2. **Single Position Test**: Test with one position first
3. **Protected Position Test**: Test with position that has SL
4. **Unprotected Position Test**: Test with `--require-sl=false` (if allowed)
5. **Validation Test**: Verify orders were placed correctly

## Rollout Plan

1. Create script locally
2. Test with dry-run mode
3. Test with single position
4. Test with all positions (dry-run)
5. Deploy to server
6. Run with `--dry-run` on server
7. Run for real (start with small subset if needed)
8. Verify orders on exchange
9. Verify database updates

## CLI Arguments

```bash
python scripts/add_tp_to_positions.py \
  [--require-sl] \          # Require SL before placing TP (default: True)
  [--no-require-sl] \       # Allow TP even without SL
  [--dry-run] \             # Preview without placing orders
  [--min-tp-count N] \      # Minimum TP orders to place (default: 2)
  [--symbol SYMBOL]         # Process only specific symbol (optional)
```

## Files to Create/Modify

- `scripts/add_tp_to_positions.py` - New script (main implementation)
- `scripts/check_tp_coverage.py` - Update to show TP order status

## Safety Considerations

1. **Never place TP without SL** (unless explicitly allowed with `--no-require-sl`)
2. **Validate prices** before placing orders
3. **Cancel existing TPs** before placing new ones (avoid duplicates)
4. **Use proper position sizing** (pass `position_size_notional`)
5. **Handle errors gracefully** (don't fail entire batch on one error)
6. **Log everything** (for audit trail)

## Success Criteria

1. ✅ Script runs without errors
2. ✅ TP orders placed on exchange
3. ✅ Database updated with TP prices and order IDs
4. ✅ Orders visible in exchange UI
5. ✅ TP backfill system can now detect TP orders exist
6. ✅ No duplicate TP orders

## Next Steps After Implementation

1. Run script to add TP to all positions
2. Verify TP orders on exchange
3. Re-enable `require_sl_for_tp_backfill: true` in config
4. Monitor TP backfill system (should now detect TPs exist)
5. Consider running script periodically as maintenance
