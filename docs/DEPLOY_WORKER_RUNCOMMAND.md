# Worker run command for App Platform

For **DigitalOcean App Platform** (and similar), the **worker** component must run the production live path **with** the minimal health server (for readiness on :8080):

```bash
python migrate_schema.py && python run.py live --force --with-health
```

- **Do not** use `python -m src.main_with_health` or `python -m src.main` for the worker.
- `--with-health` starts a minimal HTTP server on PORT/8080 so the worker passes readiness probes.
- The **web** component should run `python -m src.health` for health checks and API.

`main_with_health` exits with code 1 when `ENVIRONMENT=prod` to avoid accidental use. Production uses `run.py live` → `LiveTrading`. See [PRODUCTION_RUNTIME.md](PRODUCTION_RUNTIME.md).

---

## Server-side checks (run in worker container)

To run checks **inside the worker** (e.g. DigitalOcean **Console** → open shell on the worker): no `.env.local`, `.venv`, or DO API token. Use:

```bash
python scripts/run_server_checks.py
```

or `python3` if that’s what’s available. The script uses only the Python stdlib, GETs `http://127.0.0.1:{PORT}/` and `/health`, and reports pass/fail.

With **curl** (if present):

```bash
curl -s http://127.0.0.1:8080/ && echo "" && curl -s http://127.0.0.1:8080/health
```

- **Logs**: App Platform streams worker stdout/stderr to **Runtime Logs** in the DO dashboard. There is no local `logs/live_trading.log` in the container. To fetch logs remotely, use `do_track_and_logs.py --logs` **from your local machine** (with `DO_API_TOKEN` set).
- **Health / deployment tracking**: Use `do_track_and_logs.py --check-health` and `--logs` **locally**; they call the DO API and the app’s public URL, not from inside the worker.
- **Verify signal scanning**: Run `make check-signals` **locally** (with `DO_API_TOKEN` in `.env.local`). This fetches worker RUN logs, greps for patterns (e.g. `Coin processing status summary`, `SMC Analysis ... NO_SIGNAL`, `New signal detected`), and reports whether the system appears to be scanning for signals.
