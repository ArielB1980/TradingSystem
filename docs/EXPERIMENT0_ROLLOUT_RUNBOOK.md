# Experiment 0 Rollout Runbook

This runbook operationalizes `master-hybrid-phase2-plan` rollout gates for:

- hybrid exit mode (`multi_tp.hybrid_exit_mode_enabled`)
- no-signal persistence (`risk.auction_no_signal_persistence_enabled`)
- stricter swap threshold (`risk.auction_swap_threshold=12.0`)

## Scope Guardrails

- Do not enable Phase 2 Experiment A/B/C until this runbook is fully passed.
- Keep one-change-per-cycle attribution discipline.
- Safety invariant: no-signal persistence only applies to allocator strategic closes; it must not affect reduceOnly trims, protective order maintenance, or emergency/risk closes.

## Phase A: Telemetry-Only (minimum 24h)

1. Deploy with both feature flags off:
   - `multi_tp.hybrid_exit_mode_enabled: false`
   - `risk.auction_no_signal_persistence_enabled: false`
2. Confirm new telemetry fields are present in logs:
   - `effective_exit_mode`, `regime`, `fallback_used`
   - swap diagnostics: `close_value`, `new_value`, `gap`, `threshold`
3. Baseline KPIs:
   - net PnL/trade
   - profit factor
   - trades/day
   - fee drag/trade
   - swap churn
   - close-without-open incidents

## Phase B: Canary (5-7 trading days)

1. Select canary symbols (small subset, liquid, representative regimes).
2. Enable only for canaries:
   - `multi_tp.hybrid_exit_mode_enabled: true`
   - `multi_tp.hybrid_exit_canary_symbols: [..]`
   - `risk.auction_no_signal_persistence_enabled: true`
   - `risk.auction_no_signal_persistence_canary_symbols: [..]`
3. Keep non-canary behavior unchanged.
4. Daily gate checks:
   - Primary KPIs non-degrading vs Phase A baseline
   - no spike in close-without-open incidents
   - no safety anomalies (`degraded`, `kill`, emergency instability)

## Phase C: Full Enablement

Enable globally only if Phase B passes:

- `multi_tp.hybrid_exit_canary_symbols: []`
- `risk.auction_no_signal_persistence_canary_symbols: []`

Continue monitoring for at least 7 trading days before considering Phase 2 Experiment A.

## Hard Rollback Triggers

Rollback immediately (disable both feature flags) if any trigger fires:

- 2 consecutive days below `-3%` daily equity change
- average loss/trade deterioration greater than `40%` vs baseline
- safety instability (degraded/kill/emergency anomalies)

## Rollback Procedure

1. Set:
   - `multi_tp.hybrid_exit_mode_enabled: false`
   - `risk.auction_no_signal_persistence_enabled: false`
2. Redeploy and verify runtime config snapshot in logs.
3. Confirm behavior reverts:
   - no `effective_exit_mode=runner|fixed_tp3` overrides from hybrid mode
   - strategic closes no longer persistence-gated
4. Open incident note with:
   - trigger hit
   - timestamp window
   - affected symbols
   - mitigation status

## Exit Criteria

Experiment 0 passes only when:

- no hard rollback trigger fired during canary/full windows
- primary KPIs improved or neutral within acceptable variance
- safety invariants held continuously

Then and only then, begin Phase 2 Experiment A.
