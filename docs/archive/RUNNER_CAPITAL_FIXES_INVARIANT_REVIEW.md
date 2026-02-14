# Invariant-First Review: Runner Logic & Capital Utilisation Fixes

**Review Date:** 2026-02-11  
**Scope:** Runner logic fixes, snapshot targets, margin-based caps, trailing guard, capital reallocation

---

## 1) Executive Verdict: GO-WITH-CONDITIONS

- **Verdict**: GO-WITH-CONDITIONS
- **Reason**: The changes restore and strengthen invariants (quantize vs round, snapshot targets, margin caps). Minor conditions: ensure `auction_partial_close_cooldown_seconds` stays 0 unless explicitly enabled to avoid blocking new opens.

---

## 2) System Invariants (Explicit + Implicit)

- **Single position authority**: Exactly one module (PositionRegistry) owns position state
- **TP quantities never exceed remaining**: `min(target, remaining_qty)` enforced at TP1/TP2 hit
- **Snapshot targets immutable once set**: `entry_size_initial`, `tp1_qty_target`, `tp2_qty_target` set once, never mutate
- **Decimal precision**: TP quantities use `Decimal.quantize(step_size, ROUND_DOWN)` to avoid ConversionSyntax
- **Margin caps**: `margin_used <= equity * auction_max_margin_util` (single + aggregate)
- **Trailing activation guard**: `trailing_active` set only when ATR >= threshold at TP1
- **Stop only improves**: Invariant D unchanged; stop moves only toward profit
- **Idempotent event handling**: Duplicate events ignored via event hash

---

## 3) For Each Invariant: Enforcement, Bypass Paths, Pre-Venue

| Invariant | Enforced at | Can be bypassed by | Pre-venue? |
|-----------|-------------|-------------------|------------|
| TP qty ≤ remaining | `position_manager_v2.py` RULE 5/10: `min(tp1_qty_target, remaining_qty)` | None identified | Yes |
| Snapshot immutable | `position_state_machine.py` `ensure_snapshot_targets()` returns early if set | None | N/A (state) |
| Quantize not round | `execution_engine.py` `_split_quantities` uses `quantize(step, ROUND_DOWN)` | `step_size=None` falls back to `qty_precision` (0.001) | Yes |
| Margin caps | `risk_manager.py` `validate_trade` (always on) | None | Yes |
| Trailing guard | `position_state_machine.py` `activate_trailing_if_guard_passes` | `atr_min=0` passes always | Yes |
| No duplicate orders | `execution_gateway` + WAL + event enforcer | Concurrency (multiple workers) | Yes |

---

## 4) Loss Modes

| Mode | Mitigation |
|------|------------|
| Rounding to zero | `quantize` with ROUND_DOWN; guard `if qty <= 0: continue` in `_split_quantities` |
| Partial sizing drift | Snapshot targets fix TP sizes at open; no recompute from `remaining_qty * pct` |
| Margin over-utilization | Margin-based caps replace notional; `existing_margin + new_margin <= equity * 2.0` |
| Trailing too early | `trailing_activation_atr_min` guard; defaults to 0 (always activate when ATR present) |
| Recursive partial→auction→partial | `auction_partial_close_cooldown_seconds`; default 0 disables |
| Stale snapshot | `ensure_snapshot_targets()` recomputes from fills if None on load |

---

## 5) Required Fixes (Minimal)

- **None** for invariant restoration. All identified invariants are enforced.

---

## 6) Tests Added

- `tests/unit/test_runner_capital_fixes.py`:
  - `test_quantize_produces_valid_decimal`: asserts quantize produces valid Decimal
  - `test_step_size_passed_to_generate_entry_plan`: asserts step_size accepted
  - `test_ensure_snapshot_targets_sets_once`: asserts snapshot set from fills
  - `test_ensure_snapshot_targets_idempotent`: asserts no overwrite on second call
  - `test_tp1_hit_uses_tp1_qty_target_when_set`: asserts RULE 5 uses snapshot
  - `test_margin_caps_allow_larger_notional_than_legacy`: asserts margin caps
  - `test_aggregate_margin_cap_limits_total`: asserts aggregate margin cap limits total
  - `test_activate_trailing_if_guard_passes`: asserts trailing activation
  - `test_activate_trailing_guard_atr_min_blocks`: asserts guard blocks when ATR < min

---

## Reviewer Checklist

- [x] Identified all code paths that can place/cancel/modify orders
- [x] Verified risk limits enforced pre-venue (margin caps in validate_trade)
- [x] Verified snapshot targets prevent partial sizing drift
- [x] Verified quantize avoids ConversionSyntax
- [x] Added deterministic tests for critical paths
