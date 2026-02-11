# What Needs Fixing

Prioritized list from the audit, live DO logs, and codebase scan.

**Updates (2026-01-25):** Superseded in part by applied fixes and [CLEANUP_PROPOSAL](docs/CLEANUP_PROPOSAL.md). See [SYSTEM_AUDIT](SYSTEM_AUDIT.md), [PRODUCTION_RUNTIME](docs/PRODUCTION_RUNTIME.md). App spec: `.do/app.yaml`.

---

## 1. Critical

### 1.1 Worker running wrong runtime (deployment)

**Issue:** DigitalOcean **tradingbot** worker logs show:
- `"main_with_health is NOT the production runtime"`
- `"Initializing Trading Bot with Health Endpoints"`
- `"Data Service Task Starting"` / `"Trading Service Task Starting"`

So the worker is running **`main_with_health`** (DataService + TradingService + embedded health), not **`run.py live`** → `LiveTrading`. Production should use `LiveTrading` per [docs/PRODUCTION_RUNTIME.md](docs/PRODUCTION_RUNTIME.md).

**Fix:** Update the DO app **worker** `run_command` to  
`python migrate_schema.py && python run.py live --force`  
(and remove any `python -m src.main_with_health` or similar). Ensure the **deployed** spec (DO console or `app.yaml` / `.do/app.yaml`) matches. `app.yaml` and `.do/app.yaml` in repo already use `run.py live --force`; the live **tradingbot** app may be using a different spec.

---

### 1.2 Many unprotected / unmanaged positions (operational)

**Issue:** Live logs show 51 active positions. Many are:
- **"Adopting unmanaged position"** (on exchange but not tracked)
- **"UNPROTECTED POSITION: … has NO STOP LOSS! Placing emergency stop..."**

The system **does** place emergency stops and adopt them, but the volume suggests:
- Positions existed on exchange before this deploy (e.g. from another runtime or manual trading).
- Stops were never set or were lost (e.g. crash, different process).

**Fix:**
- Confirm worker entrypoint (1.1) so **one** process (LiveTrading) owns all positions.
- Consider a **one-time reconciliation + protection pass** at startup: fetch exchange positions, adopt missing ones, place stops where absent, then run the normal loop.
- Keep alerts for unprotected/unmanaged; ensure Slack/Discord are configured if you use them.

---

## 2. High priority

### 2.1 Stale data & failed OHLCV (data quality)

**Issue:** Logs show:
- **Stale 1d data:** 2Z/USD, ALGO/USD, ALICE/USD, ALT/USD, etc.
- **Failed to fetch spot OHLCV:** ANIME/USD, ANKR/USD, AAVE/USD, 2Z/USD (empty `error` in logs).
- **"Skipping analysis for X: Stale data detected"** → no signals for those pairs.

**Fix:**
- **Exclude or demote** known-bad pairs: delisted, rename, or consistently failing (e.g. 2Z, ANIME if not supported). Use config or discovery allow/block list.
- **Improve Kraken error logging:** log full API error body when `Failed to fetch spot OHLCV` so you can tell rate-limit vs missing symbol vs transient.
- Optionally **relax or tune** staleness thresholds for 1d (e.g. allow slightly older 1d if 15m/1h are fresh) only where it’s safe for strategy.

---

### 2.2 Audit items still open ([SYSTEM_AUDIT.md](SYSTEM_AUDIT.md))

| Item | Action |
|------|--------|
| **Unify runtimes** | Either deploy `main` (DataService + TradingService) or retire it. Avoid two parallel “live” architectures. Right now production should be **LiveTrading only**; main/main_with_health are non-production. |
| **`multi_tp` config** | Add `multi_tp` to `ExecutionConfig` (or dedicated section) and use it from YAML, or remove `multi_tp` from YAML and rely on existing `tp_splits` / `rr_fallback_multiples`. |
| **Shared equity** | Extract `_calculate_effective_equity` to e.g. `src/execution/equity.py` and use in both TradingService and LiveTrading. |

---

## 3. Medium priority

### 3.1 Backtest cleanup

**Issue:** `run_quick_backtest.py` (and similar backtest scripts) never close the Kraken client. Logs:  
`kraken requires to release all resources with an explicit call to the .close() coroutine` and unclosed aiohttp session.

**Fix:** Use a single `KrakenClient` (or BacktestEngine’s client), run backtests, then `await client.close()` in a `finally` block.

---

### 3.2 Dashboard / data TODOs

- **`dashboard/utils.py`:** `TODO: This will be populated by MultiAssetOrchestrator emitting events` and `next_action="WAIT"  # TODO: Calculate from state`.
- **`dashboard/server.py`:** `TODO: This will be populated by MarketRegistry`.

**Fix:** Either implement the referenced components (orchestrator, state, registry) or replace TODOs with a clear “N/A” / placeholder and document.

---

### 3.3 Observability & health

- **Health:** Already reports kill switch, worker liveness, etc. Optional: add DB ping, last successful tick Age.
- **Metrics:** Consider signals/min, queue depth, API latency exports (e.g. Prometheus/Datadog) if you add that stack.

---

## 4. Lower priority

- **FuturesAdapter:** Expand or discover `TICKER_MAP` for full coin universe; you’ve already added overrides from market discovery.
- **FuturesAdapter comment:** Update “e.g. BTCUSD-PERP” to Kraken’s `PF_XBTUSD` where relevant.
- **`position_manager.py`:** `# (Requires current_stop detection which is TODO)` – implement or document.

---

## 5. Quick checks

1. **Worker entrypoint:** DO app **tradingbot** → worker **run_command** = `python migrate_schema.py && python run.py live --force`. No `main_with_health`.
2. **Smoke:** `ENVIRONMENT=dev DATABASE_URL=sqlite:///./test.db MAX_LOOPS=2 python run.py live --force` — no crashes, use `_market_symbols()`.
3. **Backtest:** `python run_quick_backtest.py` — then fix `client.close()` and re-run to clear warnings.

---

## Summary

| Priority | Item |
|----------|------|
| **Critical** | Worker running main_with_health instead of `run.py live`; many unprotected/unmanaged positions |
| **High** | Stale/failed OHLCV and pair filtering; audit follow-ups (runtimes, multi_tp, shared equity) |
| **Medium** | Backtest client cleanup; dashboard TODOs; optional observability |
| **Low** | FuturesAdapter tweaks; position_manager TODO |

Addressing **1.1** and **1.2** first will align production with the intended architecture and reduce operational noise from unprotected positions.

---

## 6. Fixes Applied (2026-01-24)

| Item | Change |
|------|--------|
| **1.1 Prod guard** | `main_with_health` exits with `sys.exit(1)` when `ENVIRONMENT=prod`. Prevents accidental prod use. |
| **1.2 Startup reconcile** | LiveTrading runs `Reconciler.reconcile_all()` once at startup (after sync, before main loop). Skips if dry-run and no credentials. |
| **2.1 Blocklist** | `exchange.spot_ohlcv_blocklist` (default `["2Z/USD", "ANIME/USD"]`). `_market_symbols()` excludes them. |
| **2.1 Kraken errors** | `get_spot_ohlcv` failure logs `error`, `error_type`, and `response.text` when present. |
| **3.1 Backtest cleanup** | `run_quick_backtest`, `backtest_chz`, `backtest_chz_7days`, `backtest_live_positions`, `run_tier_a_backtest`, `run_full_backtest`: `finally` block calls `await engine.client.close()`. |
| **3.2 Dashboard TODOs** | `get_coin_snapshots` / `next_action` and server `eligible` placeholder comments updated; no functional change. |
| **FuturesAdapter** | Docstring `BTCUSD-PERP` → `PF_XBTUSD`. |
| **position_manager** | `current_stop` TODO replaced with brief note. |

**Local verification:** `make smoke` (✅), `run_quick_backtest` (✅), 57 unit tests passed. One async test fails without `pytest-asyncio` (existing).

---

## 7. Cleanup (2026-02-11)

| Item | Change |
|------|--------|
| **Duplicate import** | Removed duplicate `datetime` import in `position_manager_v2.py` |
| **Deploy docs** | Merged `DEPLOY_WORKER_RUNCOMMAND.md` into `DEPLOYMENT_WORKER_RUNCOMMAND.md`; deleted duplicate |
| **Dead code** | Removed unused `src/services/market_discovery.py` (duplicate of `src/data/market_discovery.py`) |
| **discovered_markets_loader** | Fixed docstring: `src.services.market_discovery` → `src.data.market_discovery` |

See [CLEANUP_PROPOSAL](docs/CLEANUP_PROPOSAL.md) for full cleanup history.
