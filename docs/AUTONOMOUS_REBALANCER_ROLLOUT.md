# Autonomous Rebalancer Rollout

This rollout controls the auction-path concentration rebalancer that trims oversized positions before new opens.

## Safety Invariants

- Trims are submitted only as `reduceOnly` partial-close actions via `ExecutionGateway`.
- No direct exchange bypass and no new order types.
- New opens remain blocked unless hardening gate is open and pre-open reconciliation is clean.

## Config Keys

Under `risk` in `config.yaml`:

- `auction_rebalancer_enabled`
- `auction_rebalancer_shadow_mode`
- `auction_rebalancer_trigger_pct_equity`
- `auction_rebalancer_clear_pct_equity`
- `auction_rebalancer_per_symbol_trim_cooldown_cycles`
- `auction_rebalancer_max_reductions_per_cycle`
- `auction_rebalancer_max_total_margin_reduced_per_cycle`

## Rollout Stages

1. **Deploy defaults** (`enabled=false`, `shadow_mode=true`).
2. **Shadow validation** (`enabled=true`, `shadow_mode=true`) and verify logs:
   - planned reductions,
   - cooldown skips,
   - per-cycle cap behavior,
   - no open suppression due to stale snapshot/reconcile failures.
3. **Active conservative** (`enabled=true`, `shadow_mode=false`) with:
   - `max_reductions_per_cycle=1`,
   - conservative `max_total_margin_reduced_per_cycle`.
4. **Observe** for multiple cycles:
   - degraded recurrence,
   - entry throughput after trims,
   - trim churn on same symbols.
5. **Tune slowly** only after stable behavior.

## Operational Checks

- Confirm `CYCLE_SUMMARY` no longer sticks in degraded due to concentration overflow.
- Confirm `Auction rebalancer trim executed` appears only for oversized symbols.
- Confirm `Auction opens suppressed by pre-open gate` is absent in healthy conditions.
