# CHOP Canary Rollout Checklist

## Scope

- Canary symbols: `BTC/USD`, `ETH/USD`, `SOL/USD`, `XRP/USD`, `ADA/USD`
- Enforcement enabled:
  - `risk.auction_chop_guard_enabled: true`
  - `risk.auction_chop_telemetry_only: false`
  - `risk.auction_anti_flip_lock_enabled: true`
  - `risk.auction_anti_flip_lock_telemetry_only: false`
- Safety: canary scoping must remain active for CHOP and anti-flip checks.

## Promotion KPIs (minimum 24h, prefer 48h)

- **Trade quality**
  - `quick_loss_close / quick_profit_close` improves vs prior baseline.
  - `opposite_reentry_fast` is reduced by at least 25%.
- **Cost efficiency**
  - `fee_drag_pct_on_winners` does not worsen by more than 3 percentage points.
  - `REJECT_EDGE_BELOW_FEES*` does not collapse flow (no starvation).
- **Execution stability**
  - No increase in protection/order failure classes.
  - No restart loops or allocator/type errors in logs.
- **Flow health**
  - `auction_opens_executed` on canary symbols remains non-zero.
  - `signals_after_cooldown -> risk_approved -> opens_executed` funnel conversion remains within expected range.

## Rollback Triggers (immediate)

- Net PnL on canary is materially worse than non-canary for 12h+ with similar regime.
- `would_block_flip` is high but `quick_loss_close` not improving (lock blocking good exits).
- Significant rise in `auction_opens_failed` or safety/protection errors.
- Trade starvation signs for canary symbols (signals present, opens consistently zero).

## Monitoring Queries / Log Events

- `ENTRY_FUNNEL_SUMMARY`:
  - `global_chop`, `chop_canary_mode`, `active_chop_symbols`
  - `would_block_replace`, `would_block_close_no_signal`, `would_block_flip`
  - `quick_reversal`, `opposite_reentry_fast`, `quick_profit_close`, `quick_loss_close`
- `AUCTION_CHOP_SUMMARY`:
  - regime diagnostics and canary coverage
- `AUCTION_CLOSE_REJECTED` with `REJECT_ANTI_FLIP_LOCK`

## Rollout Steps

1. Run with current canary settings for 24h.
2. Compare canary vs non-canary on the KPI set above.
3. If KPIs pass for 24-48h, expand canary set by 3-5 symbols.
4. If KPIs fail, set `auction_chop_telemetry_only: true` and `auction_anti_flip_lock_telemetry_only: true`, then reassess thresholds.
