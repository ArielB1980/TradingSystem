"""
Health check endpoint for App Platform.
Simple HTTP server to respond to health checks.

worker_health_app: minimal app for run.py live --with-health (worker container).
Serves GET / and GET /health only; no DB. Use for readiness when LiveTrading is the worker.
"""
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import os
import time
from typing import Optional

app = FastAPI(title="Trading System Health Check")

_worker_start = time.time()


def get_worker_health_app() -> FastAPI:
    """Minimal health app for worker running run.py live --with-health. GET / and /health only."""
    w = FastAPI(title="Worker Health")

    @w.get("/")
    async def root():
        return {"status": "ok", "service": "trading-worker"}

    @w.get("/health")
    async def health():
        return JSONResponse(
            content={
                "status": "healthy",
                "uptime_seconds": int(time.time() - _worker_start),
                "environment": os.getenv("ENVIRONMENT", "unknown"),
            },
            status_code=200,
        )

    return w


worker_health_app = get_worker_health_app()


@app.get("/api")
async def root():
    """Root endpoint."""
    return {"status": "ok", "service": "trading-system"}


@app.get("/api/health")
async def health():
    """Health check. Pings DB; reports kill switch and worker liveness from metrics."""
    checks = {
        "status": "healthy",
        "database": "unknown",
        "environment": os.getenv("ENVIRONMENT", "unknown"),
        "kill_switch_active": False,
        "worker_last_tick_at": None,
        "worker_stale": None,
    }
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        checks["database"] = "missing"
        checks["status"] = "unhealthy"
    else:
        checks["database"] = "configured"
        try:
            from src.storage.db import get_db
            from sqlalchemy import text
            db = get_db()
            with db.get_session() as session:
                session.execute(text("SELECT 1;"))
            checks["database"] = "connected"
        except Exception as e:
            checks["database"] = f"error: {str(e)[:80]}"
            checks["status"] = "unhealthy"

    try:
        from src.utils.kill_switch import read_kill_switch_state
        ks = read_kill_switch_state()
        checks["kill_switch_active"] = bool(ks.get("active"))
    except Exception:
        pass

    try:
        from src.storage.repository import get_latest_metrics_snapshot
        snap = get_latest_metrics_snapshot()
        if snap and snap.get("last_tick_at"):
            checks["worker_last_tick_at"] = snap["last_tick_at"]
            try:
                ts = datetime.fromisoformat(snap["last_tick_at"].replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_sec = (datetime.now(timezone.utc) - ts).total_seconds()
                checks["last_tick_age_seconds"] = round(age_sec, 1)
                checks["worker_stale"] = age_sec > 300
            except Exception:
                checks["worker_stale"] = None
    except Exception:
        pass

    status_code = 200 if checks["status"] == "healthy" else 503
    return JSONResponse(content=checks, status_code=status_code)


@app.get("/api/ready")
async def ready():
    """Readiness probe endpoint."""
    return {"status": "ready"}


@app.get("/api/metrics")
async def metrics():
    """Observability: latest metrics snapshot from worker (DB). Includes last_tick_at, signals_last_min, api_fetch_latency_ms, markets_count. Returns {} if none."""
    try:
        from src.storage.repository import get_latest_metrics_snapshot
        snap = get_latest_metrics_snapshot()
        out = snap if snap is not None else {}
        return JSONResponse(content={"source": "worker_snapshot", "metrics": out})
    except Exception as e:
        return JSONResponse(
            content={"source": "worker_snapshot", "metrics": {}, "error": str(e)[:100]},
            status_code=200,
        )


@app.get("/api/dashboard")
async def dashboard_routing_debug():
    """Debug endpoint to identify routing issues."""
    return JSONResponse(
        status_code=404,
        content={
            "error": "Routing Error",
            "message": "You have reached the WEB service (on /api/dashboard), not the DASHBOARD service.",
            "detail": "If you see this, DigitalOcean App Platform is correctly routing /api to this container, but /dashboard should go to the dashboard service.",
            "service": "web"
        }
    )


@app.get("/api/quick-test")
async def quick_test():
    """Quick system connectivity test."""
    results = {
        "database": "unknown",
        "api_keys": "unknown",
        "environment": os.getenv("ENVIRONMENT", "unknown")
    }
    
    # Check database URL
    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        results["database"] = "configured"
        # Try to connect
        try:
            from src.storage.db import get_db
            from sqlalchemy import text
            db = get_db()
            with db.get_session() as session:
                session.execute(text("SELECT 1;"))
            results["database"] = "connected"
        except Exception as e:
            results["database"] = f"error: {str(e)[:50]}"
    else:
        results["database"] = "not_configured"
    
    # Check API keys
    has_spot_key = bool(os.getenv("KRAKEN_API_KEY"))
    has_spot_secret = bool(os.getenv("KRAKEN_API_SECRET"))
    has_futures_key = bool(os.getenv("KRAKEN_FUTURES_API_KEY"))
    has_futures_secret = bool(os.getenv("KRAKEN_FUTURES_API_SECRET"))
    
    if has_spot_key and has_spot_secret:
        results["api_keys"] = "spot_configured"
    if has_futures_key and has_futures_secret:
        results["api_keys"] = "futures_configured" if results["api_keys"] == "spot_configured" else "futures_only"
    if not has_spot_key and not has_futures_key:
        results["api_keys"] = "not_configured"
    
    results["status"] = "ok" if results["database"] == "connected" else "issues"
    
    return JSONResponse(content=results)


@app.get("/api/test")
async def test_system():
    """Run system tests (API, data, signals)."""
    import asyncio
    import subprocess
    import sys
    import os
    
    results = {
        "status": "running",
        "tests": {}
    }
    
    try:
        # Run test script as subprocess to avoid event loop conflicts
        test_script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "src", "test_system.py")
        
        # Use subprocess to run tests in separate process
        process = subprocess.Popen(
            [sys.executable, test_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        
        try:
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            raise
        
        # Parse results (simple check for pass/fail indicators)
        results["status"] = "completed"
        results["output"] = stdout[:1000]  # Limit output
        results["exit_code"] = process.returncode
        results["all_passed"] = process.returncode == 0
        
        if stderr:
            results["errors"] = stderr[:500]
        
        return JSONResponse(content=results)
        
    except subprocess.TimeoutExpired:
        return JSONResponse(
            content={"status": "timeout", "message": "Tests took too long (120s timeout)"},
            status_code=504
        )
    except Exception as e:
        return JSONResponse(
            content={
                "status": "error",
                "message": str(e),
                "type": type(e).__name__
            },
            status_code=500
        )



def _debug_signals_impl(symbol_filter: Optional[str] = None) -> dict:
    """Shared logic for /api/debug/signals and /debug/signals."""
    import json

    try:
        from src.storage.repository import get_recent_events

        limit = 20 if symbol_filter else 50
        events = get_recent_events(
            limit=limit,
            event_type="DECISION_TRACE",
            symbol=symbol_filter,
        )
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
            "type": type(e).__name__,
            "last_signal": None,
            "checked_events": 0,
            "recent_decisions": [],
        }

    results: dict = {
        "status": "success",
        "last_signal": None,
        "checked_events": 0,
        "recent_decisions": [],
    }

    for ev in events:
        results["checked_events"] += 1
        ts = ev.get("timestamp", "")
        sym = ev.get("symbol", "")
        data = ev.get("details") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {"error": "failed to parse details"}
        signal = data.get("signal", "NONE")
        quality = data.get("setup_quality", 0)
        reasoning = data.get("reasoning", [])
        reasoning_tip = reasoning[-1] if isinstance(reasoning, list) and reasoning else "No reasoning logged"

        results["recent_decisions"].append({
            "time": ts,
            "symbol": sym,
            "signal": signal,
            "quality": quality,
            "reasoning": reasoning_tip,
        })

        if signal and str(signal).upper() in ("LONG", "SHORT") and results["last_signal"] is None:
            results["last_signal"] = {
                "timestamp": ts,
                "symbol": sym,
                "signal": signal,
                "quality": quality,
                "details": data,
                "reasoning": reasoning,
            }

    return results


@app.get("/api/debug/signals")
async def debug_signals(symbol: Optional[str] = None):
    """
    Debug endpoint to find the last generated signal.
    Uses system_events DECISION_TRACE. Optional ?symbol=... to filter.
    """
    data = _debug_signals_impl(symbol_filter=symbol)
    return JSONResponse(content=data, status_code=200)


@app.get("/debug/signals")
async def debug_signals_no_api(symbol: Optional[str] = None):
    """Same as /api/debug/signals, for deployments that strip /api prefix."""
    data = _debug_signals_impl(symbol_filter=symbol)
    return JSONResponse(content=data, status_code=200)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
