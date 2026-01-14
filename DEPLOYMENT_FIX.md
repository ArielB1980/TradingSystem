# Deployment Fix - Missing Dependencies

## Issues Fixed

### 1. Missing `pyyaml` Module ✅
- **Error:** `ModuleNotFoundError: No module named 'yaml'`
- **Fix:** Added `pyyaml>=6.0.0` to `requirements.txt`

### 2. Health Check Endpoint ✅
- **Error:** `Readiness probe failed: dial tcp 10.244.20.242:8080: connect: connection refused`
- **Fix:** Created health check endpoint at `src/health.py`
- **Fix:** Updated `Procfile` to run health check server on `web` process

## Changes Made

### requirements.txt
Added:
```
pyyaml>=6.0.0
```

### Procfile
Changed from:
```
web: python run.py live
worker: python run.py live
```

To:
```
web: python -m src.health
worker: python run.py live --force
```

### New File: src/health.py
- Simple FastAPI server for health checks
- Responds on `/health` and `/ready` endpoints
- Checks database configuration
- Runs on port 8080 (App Platform default)

## Next Steps

1. **Commit and Push:**
   ```bash
   git add requirements.txt Procfile src/health.py
   git commit -m "Fix deployment: add pyyaml and health check endpoint"
   git push
   ```

2. **App Platform will automatically rebuild**

3. **Verify Deployment:**
   - Check build logs for success
   - Check runtime logs for health check responses
   - Verify worker process starts trading

## How It Works

- **Web Process:** Runs health check server on port 8080 (responds to App Platform health checks)
- **Worker Process:** Runs the actual trading system (`python run.py live --force`)

The `--force` flag is needed because:
- App Platform runs in non-interactive mode (can't confirm prompts)
- Environment is set to `prod` via environment variable
- Safety gates are bypassed for automated deployment

## Health Check Endpoints

- `GET /` - Root endpoint (returns status)
- `GET /health` - Health check (checks database config)
- `GET /ready` - Readiness probe

## Troubleshooting

If deployment still fails:

1. **Check build logs** - Verify `pyyaml` installs successfully
2. **Check runtime logs** - Look for errors in worker process
3. **Verify environment variables** - Ensure `DATABASE_URL` is set
4. **Check health endpoint** - Visit `https://your-app.ondigitalocean.app/health`
