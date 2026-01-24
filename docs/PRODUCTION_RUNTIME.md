# Production Runtime

## Canonical live path

**Production live trading uses:**

- **Entrypoint:** `run.py live` (or `python -m src.cli live`)
- **Engine:** `LiveTrading` in `src/live/live_trading.py`
- **Procfile worker:** `python migrate_schema.py && python run.py live --force`

Data acquisition, strategy (SMC), risk, and execution all run inside the `LiveTrading` loop. The dashboard and health service run as separate App Platform components.

## Alternative: `main.py` (not used in production)

- **Entrypoint:** `python -m src.main` or `python -m src.main_with_health`
- **Architecture:** Single-process async, `DataService` → queue → `TradingService`

This path uses the same strategy, risk, and execution logic but a different data loop (polling + hydration). It is **not** deployed. Use it only for local experimentation or if you explicitly switch the worker to this runtime.

## `main_with_health.py`

Runs the `main.py` style worker (DataService + TradingService) with an embedded FastAPI health server. Also **not** used in production. The deployed setup uses `src.health` for the web service and `run.py live` for the worker.

## Summary

| Component     | Production                          | Alternative (not deployed)     |
|--------------|-------------------------------------|---------------------------------|
| Web / health | `python -m src.health`              | -                               |
| Worker       | `run.py live` → `LiveTrading`       | `main.py` / `main_with_health`  |
| Dashboard    | Streamlit app                       | -                               |
