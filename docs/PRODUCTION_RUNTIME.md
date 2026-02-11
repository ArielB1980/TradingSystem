# Production Runtime

## Canonical live path (production)

**Production live trading uses only:**

- **Entrypoint:** `python -m src.entrypoints.prod_live`
- **Engine:** `LiveTrading` in `src/live/live_trading.py`
- **Procfile worker:** `python migrate_schema.py && python -m src.entrypoints.prod_live`

**Production = `python -m src.entrypoints.prod_live` → `LiveTrading`. Not `run.py`, `main.py`, or `main_with_health`.**

Data acquisition, strategy (SMC), risk, and execution all run inside the `LiveTrading` loop. The dashboard and health service can run as separate App Platform components. If a platform requires the worker to bind an HTTP port, set `WITH_HEALTH=1` so the worker serves `/`, `/health`, `/api/metrics`, etc.

**DigitalOcean / App Platform:**

- **Worker (no HTTP required):**
  - `python migrate_schema.py && python -m src.entrypoints.prod_live`
- **Worker (HTTP required on :8080):**
  - `python migrate_schema.py && WITH_HEALTH=1 python -m src.entrypoints.prod_live`
- **Dedicated web/health component (recommended):**
  - `python -m src.health`

Do **not** use `main_with_health` for production.

## Production live safety requirements

In production live trading, the runtime enforces these hard gates:

- **Single-runtime + V2-only**:
  - `ENVIRONMENT=prod`
  - `DRY_RUN=0`
  - `USE_STATE_MACHINE_V2=true`
- **Explicit human confirmation**:
  - `CONFIRM_LIVE=YES` (required even when `--force` is used)
- **Single-process guard**:
  - The worker acquires a **Postgres advisory lock** (account-scoped). If a second worker starts against the same account, it exits non-zero.
- **Dotenv safety**:
  - `src/entrypoints/prod_live.py` never loads dotenv files. In `ENVIRONMENT=prod`, `.env` / `.env.local` are **not loaded** (secrets must come from the platform runtime env).
- **Real-exchange tests are disabled**:
  - Keep `RUN_REAL_EXCHANGE_TESTS=0` in prod workers.

## Deprecated / non-production

### `main_with_health.py` (removed 2026-02-11)

Replaced with a deprecation stub that exits with code 1 and prints the correct entrypoints. Use `python -m src.entrypoints.prod_live` or `run.py live --force` for production.

### Summary

| Component     | Production                          | Deprecated (do not deploy)        |
|--------------|-------------------------------------|-----------------------------------|
| Web / health | `python -m src.health`              | -                                 |
| Worker       | `python -m src.entrypoints.prod_live` → `LiveTrading` | `run.py live`, `main.py` (main_with_health removed) |
| Dashboard    | Streamlit app                       | -                                 |
