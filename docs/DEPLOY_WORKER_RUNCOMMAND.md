# Worker run command for App Platform

For **DigitalOcean App Platform** (and similar), the **worker** component must run the production live path:

```bash
python migrate_schema.py && python run.py live --force
```

- **Do not** use `python -m src.main_with_health` or `python -m src.main` for the worker.
- The **web** component should run `python -m src.health` for health checks and API.

`main_with_health` exits with code 1 when `ENVIRONMENT=prod` to avoid accidental use. Production uses `run.py live` → `LiveTrading`. See [PRODUCTION_RUNTIME.md](PRODUCTION_RUNTIME.md).
