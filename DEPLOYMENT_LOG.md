# Deployment Log

## Deployment: Auction Allocator Production Release
**Date:** 2026-01-25  
**Version:** 3.0.0  
**Branch:** main  
**Commit:** Latest

### Changes Deployed

#### Core Features
- ✅ Auction-based portfolio allocator (`src/portfolio/auction_allocator.py`)
- ✅ Deterministic auction algorithm for selecting best 50 positions
- ✅ Hysteresis and cost penalties to prevent churn
- ✅ Hard constraints (margin, cluster, symbol limits)
- ✅ Anti-churn mechanisms (MIN_HOLD, locked positions, per-cycle limits)

#### Bug Fixes
- ✅ Fixed indentation error in `trading_service.py`
- ✅ Fixed sort tie-breaker (OPEN beats NEW on equal value)
- ✅ Fixed locked positions logic
- ✅ Fixed hysteresis portfolio_state consistency
- ✅ Fixed AllocationPlan type definitions
- ✅ Fixed per-cycle limits to preserve "best 50" via paired swaps

#### Integration
- ✅ Wired auction mode into live trading tick loop
- ✅ Signal collection during parallel processing
- ✅ Auction allocation execution after signal generation
- ✅ Allocation plan execution (closes then opens)
- ✅ Backward compatible (disabled by default)

### Configuration

Auction mode is **disabled by default**. To enable:

```yaml
risk:
  auction_mode_enabled: true
  auction_max_positions: 50
  auction_max_margin_util: 0.90
  auction_max_per_cluster: 12
  auction_max_per_symbol: 1
  auction_swap_threshold: 10.0
  auction_min_hold_minutes: 15
  auction_max_trades_per_cycle: 5
  auction_max_new_opens_per_cycle: 5
  auction_max_closes_per_cycle: 5
  auction_entry_cost: 2.0
  auction_exit_cost: 2.0
```

### Pre-Production Validation

✅ All imports successful  
✅ All files compile without errors  
✅ Core functionality tested  
✅ End-to-end auction allocation tested  
✅ Configuration validated  
✅ Integration tested (disabled mode)  
✅ No syntax/indentation errors  

### Deployment Status

- [x] Code committed
- [x] Pushed to main branch
- [x] Critical fix: Auction budget margin logic
- [ ] Production deployment (if applicable)
- [ ] Monitoring enabled
- [ ] Rollback plan prepared

### Latest Update (2026-01-25)

**Critical Fix:** Auction candidate selection now uses auction budget margin instead of current available margin. This ensures the auction sees ALL candidates and can optimize by closing positions to free margin for better opportunities. Without this fix, the auction would only see "best 50 among affordable" instead of "best 50 overall".

### Rollback Plan

If issues occur:
1. Set `auction_mode_enabled: false` in config (immediate)
2. Revert to previous commit if needed: `git revert <commit-hash>`
3. System will fall back to per-symbol immediate entry mode

### Monitoring

Monitor for:
- Auction allocation execution logs
- Position count staying within limits
- Swap decisions and hysteresis application
- Any errors in `_run_auction_allocation()`

### Notes

- Auction mode is opt-in (disabled by default)
- Existing trading behavior unchanged when disabled
- All position metadata fields populated automatically
- Cluster derivation works from signal setup_type + regime
