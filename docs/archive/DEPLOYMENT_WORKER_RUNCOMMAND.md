# Deployment Worker Run Command

For **DigitalOcean App Platform** (and similar), the **worker** component must run the production live path.

> **Note:** This droplet uses **systemd** (`trading-bot.service`), not DO App Platform. The service runs `python -m src.entrypoints.prod_live`. For DO App Platform workers, use the commands below.

## Correct Run Command

```bash
python migrate_schema.py && python run.py live --force
```

**With health server** (for readiness probes on :8080):

```bash
python migrate_schema.py && WITH_HEALTH=1 python -m src.entrypoints.prod_live
```

## Do NOT Use

- `python -m src.main_with_health` — removed; stub exits with code 1
- `python -m src.main` — legacy TradingService, not LiveTrading

## Verification

1. **DO Console** → Worker → Edit → Run Command: ensure it matches above
2. **Runtime logs** should show: `State Machine V2 running - all orders via gateway`, not `main_with_health` or `Data Service Task Starting`
3. See [PRODUCTION_RUNTIME.md](PRODUCTION_RUNTIME.md) for full architecture

## Quick Checks

```bash
# From DO Console (worker shell)
python scripts/run_server_checks.py

# Or with curl
curl -s http://127.0.0.1:8080/ && curl -s http://127.0.0.1:8080/health
```

---

## Server-side checks (run in worker container)

To run checks **inside the worker** (e.g. DigitalOcean **Console** → open shell on the worker): no `.env.local`, `.venv`, or DO API token. The script uses only the Python stdlib, GETs `http://127.0.0.1:{PORT}/` and `/health`, and reports pass/fail.

- **Logs**: App Platform streams worker stdout/stderr to **Runtime Logs** in the DO dashboard. There is no local `logs/live_trading.log` in the container. To fetch logs remotely, use `do_track_and_logs.py --logs` **from your local machine** (with `DO_API_TOKEN` set).
- **Health / deployment tracking**: Use `do_track_and_logs.py --check-health` and `--logs` **locally**; they call the DO API and the app's public URL, not from inside the worker.
- **Verify signal scanning**: Run `make check-signals` **locally** (with `DO_API_TOKEN` in `.env.local`). This fetches worker RUN logs and reports whether the system appears to be scanning for signals.
