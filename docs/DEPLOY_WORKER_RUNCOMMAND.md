# Worker run command for App Platform

For **DigitalOcean App Platform** (and similar), the **worker** component must run the production live path **with** the minimal health server (for readiness on :8080):

```bash
python migrate_schema.py && python run.py live --force --with-health
```

- **Do not** use `python -m src.main_with_health` or `python -m src.main` for the worker.
- `--with-health` starts a minimal HTTP server on PORT/8080 so the worker passes readiness probes.
- The **web** component should run `python -m src.health` for health checks and API.

`main_with_health` exits with code 1 when `ENVIRONMENT=prod` to avoid accidental use. Production uses `run.py live` → `LiveTrading`. See [PRODUCTION_RUNTIME.md](PRODUCTION_RUNTIME.md).
