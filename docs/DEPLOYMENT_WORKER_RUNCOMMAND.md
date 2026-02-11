# Deployment Worker Run Command

For **DigitalOcean App Platform** (and similar), the **worker** component must run the production live path.

## Correct Run Command

```bash
python migrate_schema.py && python run.py live --force
```

**With health server** (for readiness probes on :8080):

```bash
python migrate_schema.py && WITH_HEALTH=1 python -m src.entrypoints.prod_live
```

## Do NOT Use

- `python -m src.main_with_health` — deprecated; exits with code 1 when `ENVIRONMENT=prod`
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
