# Trading System Audit Report

**Date:** 2026-01-23  
**Scope:** Kraken Futures SMC Trading System – purpose, correctness, issues, and efficiency.

**Status (2026-01-25):** Many items have been addressed. Production runtime: `run.py live` → `LiveTrading`. App spec: `.do/app.yaml`. See [PRODUCTION_RUNTIME](docs/PRODUCTION_RUNTIME.md), [CLEANUP_PROPOSAL](docs/CLEANUP_PROPOSAL.md).

---

## 1. System Purpose & Architecture

### 1.1 Purpose
The system performs **algorithmic futures trading** on Kraken Futures using Smart Money Concepts (SMC):
- **Data:** Spot OHLCV (15m, 1h, 4h, 1d) for bias and execution.
- **Strategy:** SMC (order blocks, FVGs, break of structure) with regime-aware scoring (tight_smc vs wide_structure).
- **Execution:** Kraken Futures perpetuals (PF_XBTUSD, etc.) with leverage-based sizing, multi-TP, SL, break-even, trailing.
- **Risk:** Position limits, daily loss limit, loss-streak cooldown, basis guards, liquidation buffer.

### 1.2 Deployed Runtime
- **Procfile:** `web` = health server (`src.health`), `worker` = `run.py live --force`, `dashboard` = Streamlit.
- **Live path:** `cli live` → `LiveTrading` in `src/live/live_trading.py`. **Not** `main.py` (DataService + TradingService).

### 1.3 Alternative Architecture (Not Deployed)
- `main.py`: single-process async, DataService → queue → TradingService. Uses same strategy/risk/execution but different data loop (polling + hydration). Not used in production.

---

## 2. Issues Found

### 2.1 Critical (Correctness / Crashes)

| Issue | Location | Impact |
|-------|----------|--------|
| **`self.markets.keys()` on list** | `live_trading.py` | `self.markets` is a **list** (spot symbols). Calling `.keys()` raises `AttributeError`. Used at: `candle_manager.initialize(...)`, `get_spot_tickers_bulk(...)`. **Crashes on startup / first tick.** |
| **Abandon-ship & time-based exit never applied** | `live_trading.py` `_check_dynamic_exits` | Config checks use `self.config.abandon_ship_enabled` and `self.config.time_based_exit_bars`. These live under `config.strategy`. **Dynamic exits are effectively disabled.** |
| **Startup validator never run** | `startup_validator.py` | `ensure_all_coins_have_traces` exists but is **never called** at LiveTrading startup. Missing coins never get initial DECISION_TRACE; dashboard can show gaps. |
| **Periodic maintenance never run** | `maintenance.py` | `periodic_data_maintenance` is **never scheduled**. Stale/missing trace recovery does not run. |

### 2.2 Config & Schema

| Issue | Location | Impact |
|-------|----------|--------|
| **`max_position_size_usd` defined twice** | `config.py` `RiskConfig` | Same field at lines 53 and 69; second overwrites first. Redundant and confusing. |
| **`multi_tp` in YAML unused** | `config.yaml` vs `ExecutionConfig` | `multi_tp` (tp1_r_multiple, tp1_close_pct, etc.) is in YAML but **not** in `ExecutionConfig`. Execution uses `execution.tp_splits` and `rr_fallback_multiples` only. |

### 2.3 Minor / Cleanup

| Issue | Location | Impact |
|-------|----------|--------|
| **Duplicate `candle_count` in trace_details** | `live_trading.py` | Same key twice in dict; harmless but redundant. |
| **`get_latest_traces` vs trace structure** | `startup_validator` | Uses `trace.get('symbol')`; `get_latest_traces` returns `{symbol, timestamp, details}`. Correct. |
| **FuturesAdapter legacy symbol** | `futures_adapter.py` | Comment says "e.g. BTCUSD-PERP" but Kraken uses `PF_XBTUSD`. Mapping is correct; comment is outdated. |

### 2.4 Efficiency & Robustness

| Area | Finding |
|------|---------|
| **DataService** | Semaphore(8) for 250 symbols; 15m every loop, 1h/4h/1d throttled. Reasonable. Gap-fill every 10 min. |
| **LiveTrading** | Batch tickers + bulk futures tickers + per-coin candle update. Semaphore(20). Ok. |
| **Queue backpressure** | `market_data_queue` maxsize=100. DataService can block on `put` if TradingService lags. |
| **GC in trading loop** | `gc.collect()` in TradingService when `processed > 0`. Helps small instances but adds latency. |
| **DB** | Sync `record_event`; `async_record_event` offloads to thread. Good. |
| **Duplicate equity logic** | `_calculate_effective_equity` duplicated in `TradingService` and `LiveTrading`. Should be shared. |

---

## 3. Fixes Applied (This Session)

1. **LiveTrading markets:** Add `_market_symbols()` returning `list` of spot symbols whether `self.markets` is list or dict; use it for `initialize` and `get_spot_tickers_bulk`.
2. **Abandon-ship / time-based exit:** Use `self.config.strategy.abandon_ship_enabled` and `self.config.strategy.time_based_exit_bars`.
3. **RiskConfig:** Remove duplicate `max_position_size_usd` (keep the richer definition).
4. **Startup:** Call `ensure_all_coins_have_traces(self._market_symbols())` at LiveTrading startup (after markets resolved).
5. **Maintenance:** Run `periodic_data_maintenance(self._market_symbols())` periodically in the main loop (e.g. every 1–2 hours).
6. **Trace details:** Remove duplicate `candle_count` key in trace_details dict.

---

## 4. Recommendations (Not Implemented)

### 4.1 High priority
- **Unify runtimes:** Either deploy `main.py` (DataService + TradingService) or retire it. Avoid two parallel “live” architectures.
- **Config:** Add `multi_tp` to `ExecutionConfig` (or a dedicated section) if you want YAML-driven TP splits; otherwise remove from YAML.
- **Shared equity:** Extract `_calculate_effective_equity` to a shared module (e.g. `src/execution/equity.py`) and use in both TradingService and LiveTrading.

### 4.2 Medium priority
- **Health check:** Extend `src.health` to optionally ping DB, check worker liveness, and report last successful tick.
- **Observability:** Add metrics (e.g. signals/min, queue depth, API latency) and optionally export to Prometheus/Datadog.
- **Tests:** Add integration tests for LiveTrading tick (with mocked Kraken + DB) to guard against regressions like the `markets.keys()` bug.

### 4.3 Lower priority
- **FuturesAdapter:** Expand or discover `TICKER_MAP` for full coin universe instead of relying only on `PF_{base}USD`.
- **Documentation:** Document which path is production (LiveTrading vs main), and the role of `main_with_health.py`.

---

## 5. Verification

After applying fixes:
- Run `python run.py live --force` with `MAX_LOOPS=2` or `RUN_SECONDS=120` (smoke mode) and confirm no `AttributeError` or similar.
- Confirm `abandon_ship_enabled` / `time_based_exit_bars` in `config.strategy` change behavior when toggled.
- Check that startup creates initial traces for missing symbols and that periodic maintenance runs as scheduled.

**Smoke test (2026-01-23):** Ran `ENVIRONMENT=dev DATABASE_URL=sqlite:///./test_audit.db MAX_LOOPS=1 python3 run.py live --force`. Startup completed, `ensure_all_coins_have_traces` ran (107 created), `candle_manager.initialize(_market_symbols())` and `get_spot_tickers_bulk(_market_symbols())` succeeded with 309 symbols (dict from market discovery). No `markets.keys()` crash.

---

## 6. Summary

| Category | Count |
|----------|-------|
| Critical bugs | 4 |
| Config/schema issues | 2 |
| Minor cleanup | 2 |
| Efficiency notes | 5 |
| Fixes applied | 6 |
| Recommendations | 8 |

The system’s **purpose** (SMC-based Kraken Futures trading with risk controls) is clear and largely implemented. The main **issues** were a startup/tick crash from `markets.keys()`, disabled dynamic exits due to wrong config access, and unused startup/maintenance logic. Addressing these ensures the deployed LiveTrading path runs correctly and uses the intended safeguards.

---

## 7. Post-Audit Improvements (2026-01-23)

All "what else to improve" items were implemented:

| Area | Change |
|------|--------|
| **Reconciler** | `_fetch_exchange_positions` uses `get_all_futures_positions()`. Ghosts → alert only; zombies → `delete_position` + alert. |
| **Alerts** | Slack and Discord webhooks via config / env. `send_alert` POSTs when configured. |
| **Dashboard / Health** | Portfolio uses account state, positions, kill switch. Health reports `kill_switch_active`, `worker_last_tick_at`, `worker_stale`. |
| **Tests** | `conftest` mocks `record_event` for unit tests. asyncio mark registered. |
| **FuturesAdapter** | `TICKER_MAP` extended; `spot_to_futures_override` from discovery. |
| **main.py** | Docstrings and startup warnings note non-production path. |
| **Metrics** | `api_fetch_latency_ms` in metrics snapshot. |
