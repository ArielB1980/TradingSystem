# Production Runtime

## Canonical live path (production)

**Production live trading uses only:**

- **Entrypoint:** `run.py live` (or `python -m src.cli live`)
- **Engine:** `LiveTrading` in `src/live/live_trading.py`
- **Procfile worker:** `python migrate_schema.py && python run.py live --force` (or `... live --force --with-health` if the worker serves HTTP health)

**Production = `run.py live` → `LiveTrading`. Not `main.py` or `main_with_health`.**

Data acquisition, strategy (SMC), risk, and execution all run inside the `LiveTrading` loop. The dashboard and health service run as separate App Platform components (or, for worker-only apps, the worker runs `run.py live --with-health` and serves `/`, `/health`, `/api/metrics`, etc.).

**DigitalOcean / App Platform:** Set the worker `run_command` to  
`python migrate_schema.py && python run.py live --force --with-health`  
so the worker satisfies HTTP health checks. Use `python -m src.health` for a dedicated web component. Do **not** use `main_with_health` for the worker.

## Deprecated / non-production

### `main.py` and `main_with_health.py`

- **Entrypoint:** `python -m src.main` or `python -m src.main_with_health`
- **Architecture:** Single-process async, `DataService` → queue → `TradingService`

These paths use the same strategy, risk, and execution logic but a different data loop (polling + hydration). They are **not** used in production and are **deprecated** for live deployment. Use only for local experimentation.  
`main_with_health` exits with code 1 if `ENVIRONMENT=prod` to prevent accidental production use.

### Summary

| Component     | Production                          | Deprecated (do not deploy)        |
|--------------|-------------------------------------|-----------------------------------|
| Web / health | `python -m src.health`              | -                                 |
| Worker       | `run.py live` → `LiveTrading`       | `main.py` / `main_with_health`    |
| Dashboard    | Streamlit app                       | -                                 |
