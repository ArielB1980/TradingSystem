# Routine: Analyze Recent Trading

When the user asks to **"analyze recent trading"**, **"run trading analysis"**, or **"review recent trading"**, execute this routine.

## Purpose

Produce a short, structured report on recent production trading: system health, positions, cycle activity, risk/sizing (including binding constraints), auction behaviour, errors, and any recommendations.

## Steps

### 1. Fetch recent production logs

From the repo root, run:

```bash
./scripts/fetch_recent_trading_logs.sh 3000
```

(Or `5000` for more history. Uses SSH from `.env.local` or defaults: `DEPLOY_SERVER`, `DEPLOY_SSH_KEY`, `TRADING_DIR`.)

If SSH fails (e.g. no key or no network), ask the user to run the script locally or paste the last ~3000 lines of `logs/run.log` from the server.

### 2. Parse and summarize the output

From the fetched sections, extract and report:

| Section | What to summarize |
|--------|---------------------|
| **CYCLE_SUMMARY** | Last few cycles: `positions`, `system_state` (NORMAL/DEGRADED/HALT), `duration_ms`, `universe`, `cooldowns_active`. Trend: stable or degrading? |
| **Auction** | Last auction results: `opens_executed`, `opens_planned`, `opens_failed`, `closes`, `rejection_counts`. Any repeated rejections? |
| **Risk sizing binding constraint** | Last few lines: `final_binding_constraint` (risk_sizing / single_margin / aggregate_margin / min_notional_reject / available_margin), `equity`, `final_notional`, `computed_notional_from_risk`. Explains “why is stake this size?” |
| **Trade approved/rejected** | Counts and any repeated rejection reasons. |
| **Utilisation boost applied** | Whether boost is firing; `before`/`after` notional when present. |
| **Errors** | Last few errors: message and symbol/context. Distinguish transient (e.g. API 503) vs logic/state. |
| **Critical** | Anything concerning (exclude benign PROD_INVARIANT_REPORT, PROD_LIVE_LOCK, DATABASE_CONNECTION). |
| **INVARIANT / HALT / KILL_SWITCH** | Any violations or pauses; immediate follow-up if present. |
| **Positions** | Latest `positions=` or registry/Active Portfolio mentions; confirm matches expectations. |

### 3. Produce a short report

Structure the reply as:

1. **Health** – System state (e.g. NORMAL), any HALT/kill_switch/invariant issues.
2. **Positions** – Current position count and symbols if visible.
3. **Last N cycles** – Brief line per cycle (duration, positions, state) or a single summary sentence.
4. **Auction** – Opens/closes last run; notable rejection reasons if any.
5. **Risk / sizing** – Dominant `final_binding_constraint`; whether stakes are mostly risk_sizing, single_margin, min_notional_reject, etc.; utilisation boost usage.
6. **Errors** – Last few; one-line each; note if transient vs needs fix.
7. **Recommendations** – Only if something actionable: e.g. raise a cap, check a symbol, investigate an error.

Keep the report to one short page unless the user asks for more detail.

### 4. Optional: deeper dives

- If the user wants **only binding constraints**: grep the fetched output for `Risk sizing binding constraint` and list `final_binding_constraint` + `final_notional` (and optionally `equity`).
- If the user wants **only errors**: summarize the Errors and Critical sections with context (time, symbol, message).
- For **local** analysis with DB access: `python scripts/check_trading_activity.py` (or `scripts/analyze_server_logs.py` with pasted logs) can supplement the above.

## Trigger phrases

- “Analyze recent trading”
- “Run the trading analysis routine”
- “Review recent trading”
- “How’s the bot doing?” (interpret as request for this routine when context is trading)

## Files involved

- **Routine (this file):** `docs/ROUTINE_ANALYZE_RECENT_TRADING.md`
- **Fetch script:** `scripts/fetch_recent_trading_logs.sh`
- **Optional:** `scripts/review_server_logs.sh` (go-live gates); `scripts/check_trading_activity.py` (DB); `scripts/analyze_server_logs.py` (signals/errors from pasted logs)
