# Operations Runbook

## Promoted Tools (`src/tools/`)

All tools default to **dry-run**. Pass `--execute` to take real action.
If live API keys are detected, set `I_UNDERSTAND_LIVE=1` to proceed.

| Tool | Purpose | Modifies State? |
|------|---------|----------------|
| `python -m src.tools.sync_positions` | Sync exchange positions to local registry | Yes (with --execute) |
| `python -m src.tools.recover_sl_order_ids` | Fix positions with missing stop-loss order IDs | Yes (with --execute) |
| `python -m src.tools.check_tp_coverage` | Report TP order coverage for all positions | No (read-only) |
| `python -m src.tools.check_live_readiness` | Validate API connection and system readiness | No (read-only) |
| `python -m src.tools.pre_flight_check` | Full pre-flight check before live trading | No (read-only) |
| `python -m src.tools.check_db_health` | Database integrity and health report | No (read-only) |
| `python -m src.tools.monitor_trade_execution` | Execution quality metrics (slippage, fills) | No (read-only) |
| `python -m src.tools.backfill_historical_data` | Backfill OHLCV gaps in candle database | Yes (with --execute) |

## Deploy

```bash
make deploy          # commit, push, SSH pull, restart service
# or manually:
./scripts/deploy.sh
```

## Kill Switch Recovery

1. **Check status**: `ssh <droplet> systemctl status trading-bot`
2. **View logs**: `ssh <droplet> journalctl -u trading-bot -n 200`
3. **Check halt reason**: Search logs for `KILL_SWITCH_ACTIVATED`
4. **If margin_critical (most common)**:
   - System auto-recovers if margin drops below threshold
   - Manual: restart service (`systemctl restart trading-bot`)
5. **If invariant_violation**:
   - Review FORAI.md for known patterns
   - Fix root cause before restart
   - Run `make smoke` locally to verify

## Position Import

When the system discovers exchange positions it doesn't track:

```bash
python -m src.tools.sync_positions --execute
```

This queries Kraken, creates registry entries, and places protective stops.

## TP Backfill

If positions are missing take-profit orders:

```bash
python -m src.tools.check_tp_coverage  # identify gaps
# then use the execution gateway's TP placement logic
```

## Telegram Commands

| Command | Action |
|---------|--------|
| `/status` | System status, positions, equity |
| `/positions` | Detailed position list |
| `/help` | Available commands |

## Log Patterns

| Pattern | Meaning |
|---------|---------|
| `CYCLE_SUMMARY` | Per-tick metrics (duration, positions, state) |
| `DECISION_TRACE` | Per-coin signal analysis result |
| `KILL_SWITCH_ACTIVATED` | Emergency halt triggered |
| `INVARIANT_VIOLATION` | Safety limit breached |
| `ORDER_REJECTED_BY_VENUE` | Exchange rejected order |
| `API circuit breaker OPENED` | Circuit breaker tripped (API outage) |
| `THRESHOLD_MISMATCH` | Config/safety limit inconsistency |
| `REGISTRY_AUDIT_REPORT` | Startup/runtime lifecycle integrity report |
| `RECONCILIATION_REPORT` | Reconciliation issue classes and convergence counters |
| `RECOVERY_MERGE_COLLISION` | Canonical symbol-key collision resolved during startup merge |

## Registry and Reconciliation Runbook

These two logs are the primary health signals for orphan prevention and replay stability:

- `REGISTRY_AUDIT_REPORT`
- `RECONCILIATION_REPORT`

### REGISTRY_AUDIT_REPORT fields and actions

| Field | Expected | Action if non-zero |
|------|----------|--------------------|
| `violations_total` | `0` | Treat as **critical**. Startup should fail-fast; do not override. |
| `duplicate_active_symbol_keys` | `0` | Investigate duplicate lifecycle rows by symbol key. Run sync tools only after root cause is identified. |
| `non_terminal_in_closed_history` | `0` | Indicates lifecycle corruption. Review recent takeover/import/reconcile logs. |
| `remaining_qty_violations` | `0` | Potential fill-ledger corruption. Inspect fill IDs and position snapshots before restart. |

### RECONCILIATION_REPORT fields and thresholds

| Field | Target | Warning threshold | Critical threshold |
|------|--------|-------------------|--------------------|
| `orphaned` | `0` | `>= 1` in a cycle | `>= 3` in 15 minutes |
| `phantom` | `0` | `>= 1` in a cycle | `>= 3` in 15 minutes |
| `qty_mismatch` | `0` | `>= 1` in a cycle | persistent for 3+ cycles |
| `qty_synced` | low/occasional | `>= 3` in a cycle | sustained increase over 30 minutes |
| `pending_adopted` | rare | `>= 1` in a cycle | repeated every restart |
| `state_adjustments_deduped` | low | rising trend | dominates `state_adjustments_logged` for >30 minutes |

### Incident playbook

1. **Audit failure (`violations_total > 0`)**
   - Keep service stopped.
   - Capture latest `REGISTRY_AUDIT_REPORT`, `RECOVERY_MERGE_COLLISION`, and `CORRUPTED_POSITION` logs.
   - Run local smoke (`make smoke`) before any redeploy.

2. **Orphan/phantom spike**
   - Check exchange connectivity and order stream lag (`API circuit breaker OPENED`, websocket health).
   - Run:
     - `python -m src.tools.check_live_readiness`
     - `python -m src.tools.sync_positions --execute`
   - Verify next 3 `RECONCILIATION_REPORT` cycles trend down.

3. **Persistent qty_mismatch**
   - Inspect recent fill IDs and `QTY_SYNCED` issues.
   - Confirm no repeated symbol format drift (`PF_*` vs unified symbols).
   - If unresolved after 3 cycles, halt and investigate replay/persistence state.

4. **High dedupe with low new adjustments**
   - Usually benign after restart/replay.
   - If sustained, review whether same mismatch issue is emitted repeatedly (possible stale exchange snapshot or event feed lag).

### Escalation criteria

Escalate immediately (page + pause trading) when any of the below occur:

- `REGISTRY_AUDIT_REPORT.violations_total > 0`
- `orphaned` or `phantom` hits critical threshold
- `qty_mismatch` persists for 3+ consecutive cycles
- repeated `CORRUPTED_POSITION` alerts

## Common Troubleshooting

### System halted with margin_critical
- Check if positions are over-leveraged
- Review `auction_max_margin_util` vs `max_margin_utilization_pct`
- System auto-recovers once margin usage drops

### Circuit breaker open
- API outage detected; system waits 60s then probes
- If persistent: check Kraken status page
- Force close: restart the service

### Position not tracked
- Run `python -m src.tools.sync_positions` to reconcile
- Check `position_registry.db` for stale entries

### Missing stop-loss orders
- Run `python -m src.tools.recover_sl_order_ids --execute`
- Verify with `python -m src.tools.check_tp_coverage`
