# System Cleanup Proposal

**Purpose:** Identify areas of cleanup to reduce duplication, clarify ownership, and simplify maintenance.  
**Scope:** Codebase review (no runtime debugging). Prioritized by impact and effort.

---

## 1. Symbol helpers (PF_ ↔ unified, position↔order matching)

**Issue:** `_pf_to_unified` and `_position_symbol_matches_order` are duplicated across:

- `src/tools/audit_open_orders.py`
- `src/tools/place_missing_stops.py`
- `src/services/trading_service.py`

Dashboard `utils.py` also has a `PF_{base}USD`-style pattern. XBT→BTC is handled in `services/market_discovery` but not in `_pf_to_unified`, which caused the single place_missing_stops failure for Bitcoin.

**Proposal:**

- Add `src/data/symbol_utils.py` (or `src/utils/symbols.py`) with:
  - `pf_to_unified(s: str) -> str` (include XBT→BTC)
  - `position_symbol_matches_order(position_symbol: str, order_symbol: str) -> bool`
- Replace duplicates in audit, place_missing_stops, and trading_service with imports.
- Optionally use the same helpers in dashboard utils if they need spot↔futures mapping.

**Impact:** Single place for symbol rules; consistent XBT→BTC handling; less drift.

---

## 2. Market discovery: two modules, similar names

**Issue:**

- `src/services/market_discovery.py`: async, Kraken API, used by LiveTrading. Writes `data/discovered_markets.json`.
- `src/utils/market_discovery.py`: sync, file-based loader from `data/discovered_markets.json`. Used by dashboard (server, utils).

Different roles but overlapping names and same data file. Confusing when grepping or onboarding.

**Proposal:**

- Rename `utils/market_discovery` → `discovered_markets_loader` (or `market_discovery_file`) and keep it as “load from JSON.”
- Document: “Live path uses `MarketDiscoveryService`; dashboard uses file loader.”
- Optionally have the service write, loader read, and keep a single `discovered_markets.json` path (already shared).

**Impact:** Clear separation; less confusion.

---

## 3. Dashboard servers: combined vs unified vs plain

**Issue:**

- `combined_server.py`: FastAPI + Streamlit (thread). Not used in `.do/app.yaml`.
- `unified_server.py`: FastAPI + Streamlit subprocess, reverse proxy. Not used in `.do/app.yaml`.
- `server.py`, `streamlit_app.py`: used (dashboard component runs Streamlit only; health is separate).

Production uses Streamlit alone and a separate health service. Combined/unified exist but aren’t in the deploy spec.

**Proposal:**

- Decide canonical “dashboard + health” pattern:
  - If Streamlit-only + separate health is permanent: **document** that and mark `combined_server` / `unified_server` as **optional / dev-only** (or move to `scripts/` / `archive/`).
  - If you want a single process: **pick one** (e.g. unified), use it in app spec, deprecate the other.
- Update `docs/` and any runbooks to point at the chosen setup.

**Impact:** No ambiguity about what’s prod vs dev; less dead code.

---

## 4. Execution dual path (Legacy vs V2 + Gateway)

**Issue:**

- **Production:** `python -m src.entrypoints.prod_live` → `LiveTrading` uses **V2** (`PositionManagerV2`, `ExecutionGateway`) for entries/stops, plus **Executor** (e.g. `sync_open_orders`, `check_order_timeouts`) and **legacy** `PositionManager` for `evaluate` on `managed_pos`.
- **Deprecated:** `main.py` / `main_with_health` → `TradingService` use **Executor** + **PositionManager** only.

So we have legacy PM + Executor still in the live path alongside V2 + Gateway. `SYSTEM_AUDIT.md` already suggests retiring or unifying the non-production path.

**Proposal:**

- **Phase 1:** Keep `main` / `main_with_health` but clearly **deprecated** (as in `PRODUCTION_RUNTIME`). Add a short “Migration: main → prod_live entrypoint” note if useful.
- **Phase 2:** In `LiveTrading`, audit every use of `PositionManager` and `Executor`. Where V2 + Gateway already cover the behavior, **remove** legacy usage and rely only on V2 + Gateway.
- **Phase 3:** If nothing else uses `TradingService` for live execution, consider moving it to `src/legacy/` or folding any still-needed bits into `LiveTrading` and retiring the rest.

**Impact:** Single execution model in production; simpler mental model and fewer bugs.

---

## 5. Root-level clutter (scripts, logs, one-offs)

**Issue:**

- One-off scripts at repo root: `analyze_events.py`, `check_account_state(s).py`, `debug_*.py`, `fetch_*.py`, `find_*.py`, `get_trace.py`, `verify_events.py`, etc.
- Ad-hoc output: `full_logs.txt`, `raw_logs.txt`, `server_logs*.txt`, `curl_logs.txt`, `recent_signals.json`, `signal_output.json`, `trades_today.txt`, `trace_output.txt`.
- `scripts/` already has many orchestration and ops scripts.

**Proposal:**

- **Scripts:** Move one-off / debug / analysis scripts into `scripts/` (e.g. `scripts/debug/`, `scripts/analysis/`) or `scripts/archive/` if obsolete. Prefer `make` or `scripts/` as the only entrypoints for such tools.
- **Output files:** Add patterns to `.gitignore` (e.g. `*_logs.txt`, `*_output.json`, `trades_today.txt`, `trace_output.txt`) so they don’t get committed. Keep any that are **intentionally** committed (e.g. fixtures) explicit.
- **Backtests:** Root-level `backtest_*.py`, `run_*_backtest.py` → move to `scripts/backtest/` or a single `scripts/run_backtest.py` with subcommands, and call `load_config("src/config/config.yaml")` from there.

**Impact:** Cleaner root; clear place for ad-hoc tools; less accidental commit of logs/outputs.

---

## 6. Documentation fragmentation

**Issue:**

- 40+ `.md` files at repo root (deployment, dashboard, API, setup, fixes, etc.) and more under `docs/`.
- Overlap (e.g. multiple “deployment” / “dashboard” guides) and some outdated (e.g. `FIXES_NEEDED` / pre-audit state).

**Proposal:**

- **Consolidate:** Group into `docs/` by topic, e.g.:
  - `docs/deployment/` (DigitalOcean, Heroku, etc.)
  - `docs/dashboard/` (setup, data sources, URL)
  - `docs/setup/` (credentials, env, database)
  - `docs/operations/` (runbooks, audits, place_missing_stops, etc.)
- **Root:** Keep `README.md`, maybe `CONTRIBUTING.md`, `CHANGELOG.md`. Link to `docs/` for the rest.
- **Status:** Retire or update `FIXES_NEEDED.md`. In `SYSTEM_AUDIT.md`, add a short “Status” (e.g. “Many items addressed; see PRODUCTION_RUNTIME and CLEANUP_PROPOSAL”) and dates.

**Impact:** Easier to find and maintain docs; less duplication.

---

## 7. Config and app spec

**Issue:**

- `config.yaml.backup` exists; no code references it.
- `app.yaml` at root vs `.do/app.yaml`: both define DO-style app spec; project uses `.do/app.yaml` for deploy.

**Proposal:**

- **Backup:** Remove `config.yaml.backup` or move to `src/config/examples/` and document.
- **App spec:** Treat `.do/app.yaml` as **source of truth** for DigitalOcean. Remove root `app.yaml` or turn it into a pointer/symlink to `.do/app.yaml` (if your tooling supports it). Document in `docs/deployment/`.

**Impact:** Single config backup pattern; single DO app spec.

---

## 8. Equity helper usage

**Issue:**

- `LiveTrading` and `TradingService` each define `_calculate_effective_equity` that only delegates to `calculate_effective_equity` in `src/execution/equity.py`.

**Proposal:**

- Call `calculate_effective_equity` from `equity` directly where needed; delete the thin wrappers. Pass `kraken_client` and `base_currency` as today.

**Impact:** Less duplication; one obvious place for equity logic.

---

## 9. Root-level tests vs `tests/`

**Issue:**

- `test_api.py`, `test_config.py`, `test_backtest.py`, `test_ccxt_*.py`, etc. live at root. The main suite lives under `tests/`.

**Proposal:**

- Move root `test_*` into `tests/` or `tests/integration/` (or `tests/scripts/` if they exercise scripts). Run everything via `pytest` / `make test`. Use a shared config path (e.g. `src/config/config.yaml`) and env handling.

**Impact:** One test layout; consistent discovery and CI.

---

## 10. Quick reference

| Area | Action | Effort |
|------|--------|--------|
| Symbol helpers | Centralize in `symbol_utils` / `utils.symbols`, add XBT→BTC | Small |
| Market discovery | Rename utils module; document roles | Small |
| Dashboard servers | Document or archive combined/unified; choose one if used | Small |
| Execution dual path | Reduce legacy PM/Executor in LiveTrading; deprecate main path | Medium |
| Root clutter | Move scripts; gitignore outputs; group backtests | Small |
| Docs | Consolidate into `docs/`; update FIXES_NEEDED / AUDIT | Medium |
| Config / app spec | Remove or relocate backup; single DO spec | Small |
| Equity | Use `equity.calculate_effective_equity` directly | Small |
| Tests | Move root `test_*` into `tests/` | Small |

---

**Next steps:** Pick 1–2 items (e.g. symbol utils + root clutter) and implement; then iterate. Prefer small, reviewable PRs over a single large cleanup.

---

## Implemented (2026-01-25)

- **Symbol utils:** `src/data/symbol_utils.py` with `pf_to_unified` (XBT→BTC), `position_symbol_matches_order`. Audit, place_missing_stops, trading_service updated.
- **Equity:** LiveTrading and TradingService use `calculate_effective_equity` directly; wrappers removed.
- **Config / app spec:** `config.yaml.backup` → `src/config/examples/`; root `app.yaml` removed. `.do/app.yaml` is source of truth.
- **Market discovery:** `utils/market_discovery` renamed to `discovered_markets_loader`; dashboard imports updated.
- **Root clutter:** Backtest scripts → `scripts/backtest/`; one-offs → `scripts/debug/`. `.gitignore` extended for ad-hoc logs/output. `make backtest-quick`, `make backtest-full` added.
- **Tests:** Root `test_*` moved to `tests/`.
- **Docs:** SYSTEM_AUDIT and FIXES_NEEDED status updated; DIGITALOCEAN_APP_DEPLOYMENT notes `.do/app.yaml`.
- **Dashboard servers:** `combined_server` and `unified_server` docstrings state dev-only.
