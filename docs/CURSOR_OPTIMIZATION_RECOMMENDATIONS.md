# Cursor Optimization Recommendations

Recommendations to get better AI assistance in this repo. All listed items are implemented ✅.

---

## 1. Project context (always on) ✅

**Rule:** `.cursor/rules/trading-system-context.mdc`  
**Purpose:** So the AI always knows this is a **live trading system** (real capital), the production path (`prod_live`), key docs (FORAI, ARCHITECTURE), and to run tests before suggesting deploy.  
**Status:** Added; `alwaysApply: true`.

---

## 2. Risk & execution safety (when editing those files) ✅

**Rule:** `.cursor/rules/risk-execution-safety.mdc`  
**Globs:** `src/risk/**/*.py`, `src/execution/**/*.py`, `src/live/protection_ops.py`  
**Purpose:** When touching risk/execution/protection: consider invariants, run relevant tests, preserve binding/margin/venue semantics; use invariant-first-review for PRs.  
**Status:** Added.

---

## 3. Analyze recent trading ✅

**Rule:** `.cursor/rules/analyze-recent-trading.mdc`  
**Routine:** `docs/ROUTINE_ANALYZE_RECENT_TRADING.md`  
**Script:** `scripts/fetch_recent_trading_logs.sh`  
**Purpose:** User says “analyze recent trading” → fetch production logs, parse sections, produce a one-page report.  
**Status:** Implemented.

---

## 4. Pre-deploy gate ✅

**Rule:** `.cursor/rules/pre-deploy-gate.mdc`  
**Purpose:** When the user says “deploy” or “push to main”, run tests first (make smoke or unit tests for changed areas) and remind to run `make smoke` / `make pre-deploy` before deploy. Do not suggest `--skip-tests` unless the user explicitly asked for a quick deploy.  
**Status:** Added.

---

## 5. Align root project rules with this repo ✅

**File:** `cursor-project-rules.md`  
**Change:** “Project Context” updated to describe the **Python live trading system**, FORAI, ARCHITECTURE, and to run tests / consider invariants for risk/execution.  
**Status:** Done.

---

## 6. Invariant-first-review on risk/execution changes ✅

**Rule:** `.cursor/rules/invariant-review-on-risk-changes.mdc`  
**Globs:** `src/risk/**/*.py`, `src/execution/**/*.py`, `src/live/protection_ops.py`, `src/live/auction_runner.py`  
**Purpose:** When reviewing a PR or diff that touches execution, risk, or position/TP logic, use the **invariant-first-review** skill (`~/.cursor/skills/invariant-first-review/SKILL.md`): identify invariants, check enforcement, enumerate loss modes, summarise safety.  
**Status:** Added.

---

## 7. File-specific test reminders ✅

**Rule:** `.cursor/rules/run-tests-for-changed-paths.mdc`  
**Globs:** `src/risk/**/*.py`, `src/execution/**/*.py`, `src/live/protection_ops.py`, `src/config/config.py`, `src/config/config.yaml`  
**Purpose:** After editing these paths, run the corresponding test modules (table in rule: risk → test_risk_manager; execution → test_runner_capital_fixes, test_tp_placement_invariant, etc.). Single command for multi-file: `pytest tests/unit/test_risk_manager.py tests/unit/test_runner_capital_fixes.py tests/unit/test_tp_placement_invariant.py -v`.  
**Status:** Added.

---

## 8. Config and binding constraints ✅

**Rule:** `.cursor/rules/config-risk-auction-binding.mdc`  
**Globs:** `src/config/config.py`, `src/config/config.yaml`  
**Purpose:** When editing risk/auction config: respect binding constraints (risk_per_trade_pct, max_single_*, min notional, auction_max_positions); utilisation boost only when leverage_based; margin caps always on (no use_margin_caps); keep config.py and config.yaml in sync.  
**Status:** Added.

---

## 9. Summary: all rules

| Rule file | When it applies | Purpose |
|-----------|-----------------|---------|
| `trading-system-context.mdc` | Always | Repo = live trading; FORAI/ARCHITECTURE; run tests before deploy |
| `risk-execution-safety.mdc` | Editing risk/execution/protection_ops | Invariants, tests, binding/margin/venue semantics |
| `analyze-recent-trading.mdc` | User says “analyze recent trading” | Fetch logs, run routine, one-page report |
| `pre-deploy-gate.mdc` | User says “deploy” / “push to main” | Run tests first, remind make smoke |
| `invariant-review-on-risk-changes.mdc` | Reviewing PR/diff on risk/execution | Use invariant-first-review skill |
| `run-tests-for-changed-paths.mdc` | After editing risk/execution/config | Run mapped test modules |
| `config-risk-auction-binding.mdc` | Editing config.py / config.yaml | Binding constraints, utilisation boost, margin caps |

All recommendations are implemented. Use “analyze recent trading” for logs; say “deploy” to trigger the pre-deploy gate; open risk/execution files to get safety and test reminders; review risk/execution diffs to get invariant-first review.
