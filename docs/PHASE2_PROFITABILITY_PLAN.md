# Phase 2 Trading Plan

## Objective
Turn the successful Phase 1 deployment into a repeatable optimization process that improves profitability while keeping capital safety invariant.

---

## 1) Lock and Stabilize Current Winner (Days 0-2)

- Freeze config at current deployed values (no further strategy/risk edits during stabilization window).
- Document baseline snapshot (7d + 30d):
  - trades/day
  - net P&L/day
  - avg P&L/trade
  - win rate
  - profit factor
  - drawdown
  - % no_signal
  - top 3 rejection reasons
  - % unknown exit reasons
- Mark release tag in git for easy rollback reference.

Exit criteria:
- System remains NORMAL
- No malformed trade persistence
- No spike in unknown exit reasons
- No abnormal safety triggers

---

## 2) Make Integrity Controls Permanent (Days 0-3)

- Operational guardrails:
  - Alert on blocked malformed trade persistence (critical)
  - Alert if unknown exit reasons > threshold (e.g. >2% of closes in 24h)
- Runbook hardening:
  - Keep `scripts/cleanup_malformed_trades.py` in ops checklist
  - Define "dry-run first, apply second" SOP
- Data quality check cadence:
  - Daily integrity check (new malformed rows, unknown exits, orphan trade anomalies)

Exit criteria:
- Alerting tested and confirmed firing paths
- 3 consecutive days with no integrity violations

---

## 3) Define the Decision Framework (Before Any New Tuning)

For every experiment, predefine:

- Primary KPI: net P&L/trade and profit factor
- Secondary KPIs: trades/day, win rate, avg loss, drawdown, rejection mix
- Rollback triggers (hard):
  - 2 consecutive days worse than -3% equity daily loss
  - avg loss/trade deterioration >40% vs baseline
  - risk/safety instability or repeated kill/degraded events
- Test horizon: 5-7 trading days minimum per lever
- One-change rule: exactly one strategy lever per cycle

---

## 4) Phase 2 Experiment Queue (One Lever at a Time)

### Experiment A (first): Directional Concentration Control

Rationale: current closed trades are heavily one-sided (short concentration risk).

- Add/enable soft side-balance pressure (not hard bans)
- Keep risk caps unchanged

Success:
- Reduced side concentration
- Similar or improved expectancy and drawdown

### Experiment B: Entry Quality Calibration (post-A)

Rationale: keep the gain in opportunity capture while reducing adverse selection.

- Fine-tune one of:
  - `fib_proximity_bps`
  - `entry_zone_tolerance_pct`
- Do not tune both in same cycle

Success:
- Maintain higher signal throughput
- Improve profit factor and avg P&L/trade

### Experiment C: Execution Cost Accuracy

Rationale: better fee/funding/slippage truth improves optimization quality.

- Improve realized cost attribution (fill-level economics)
- Compare estimated vs realized drift in reporting

Success:
- Reduced model-vs-realized cost error
- Better confidence in expectancy decisions

---

## 5) Scaling Plan (Only After Stable Expectancy)

When 2+ weeks show stable/improving profitability:

- Increase risk gradually (+10-20% step)
- Hold each step for at least 1 week
- Apply same rollback rules at each step

Never increase size during an active strategy experiment.

---

## 6) Operating Cadence

### Daily (15 min)

- Health state (NORMAL/DEGRADED/HALT)
- New closes + P&L
- Unknown exit reason count
- Integrity alerts
- Top rejection reasons trend

### Weekly (60 min)

- Experiment scorecard (baseline vs test)
- Keep / rollback / iterate decision
- Next single-lever assignment

---

## 7) Immediate Action Checklist

- [ ] Freeze current config and mark baseline commit/tag
- [ ] Capture baseline KPI report (7d/30d template)
- [ ] Enable/verify integrity alerts
- [ ] Schedule daily integrity checks
- [ ] Start Experiment A (directional concentration control)
- [ ] Review after 5-7 days with predefined thresholds
