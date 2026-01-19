"""
Health check endpoint for App Platform.
Simple HTTP server to respond to health checks.
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import os
from typing import Optional

app = FastAPI(title="Trading System Health Check")


@app.get("/api")
async def root():
    """Root endpoint."""
    return {"status": "ok", "service": "trading-system"}


@app.get("/api/health")
async def health():
    """Health check endpoint for App Platform."""
    # Check if critical environment variables are set
    checks = {
        "status": "healthy",
        "database": "unknown",
        "environment": os.getenv("ENVIRONMENT", "unknown"),
    }
    
    # Check database connection
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        checks["database"] = "configured"
    else:
        checks["database"] = "missing"
        checks["status"] = "unhealthy"
    
    status_code = 200 if checks["status"] == "healthy" else 503
    return JSONResponse(content=checks, status_code=status_code)


@app.get("/api/ready")
async def ready():
    """Readiness probe endpoint."""
    return {"status": "ready"}


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



@app.get("/api/debug/signals")
async def debug_signals(symbol: Optional[str] = None):
    """
    Debug endpoint to find the last generated signal.
    Queries the system_events table directly.
    """
    try:
        from src.storage.db import get_db
        from sqlalchemy import text
        import json
        
        db = get_db()
        results = {
            "status": "success",
            "last_signal": None,
            "checked_events": 0,
            "recent_decisions": []
        }
        
        with db.get_session() as session:
            # Get last 50 decision traces
            if symbol:
                query = text("""
                    SELECT timestamp, details, symbol
                    FROM system_events 
                    WHERE event_type = 'DECISION_TRACE' AND symbol = :symbol
                    ORDER BY timestamp DESC 
                    LIMIT 20
                """)
                params = {"symbol": symbol}
            else:
                query = text("""
                    SELECT timestamp, details, symbol
                    FROM system_events 
                    WHERE event_type = 'DECISION_TRACE' 
                    ORDER BY timestamp DESC 
                    LIMIT 50
                """)
                params = {}
            
            rows = session.execute(query, params)
            
            for row in rows:
                timestamp = row[0]
                details_raw = row[1]
                symbol = row[2]
                
                results["checked_events"] += 1
                
                # Parse details
                try:
                    if isinstance(details_raw, str):
                        data = json.loads(details_raw)
                    else:
                        data = details_raw
                except:
                    data = {"error": "failed to parse details"}
                
                signal = data.get('signal', 'NONE')
                quality = data.get('setup_quality', 0)
                reasoning = data.get('reasoning', [])
                
                # Add to recent list (summary)
                results["recent_decisions"].append({
                    "time": str(timestamp),
                    "symbol": symbol,
                    "signal": signal,
                    "quality": quality,
                    "reasoning": reasoning[-1] if reasoning else "No reasoning logged"
                })
                
                # Check if it's a valid signal
                if signal and signal.upper() in ['LONG', 'SHORT']:
                    if results["last_signal"] is None:
                        results["last_signal"] = {
                            "timestamp": str(timestamp),
                            "symbol": symbol,
                            "signal": signal,
                            "quality": quality,
                            "details": data,
                            "reasoning": reasoning
                        }
                        break
            
            return JSONResponse(content=results)
            
    except Exception as e:
        return JSONResponse(
            content={
                "status": "error",
                "message": str(e),
                "type": type(e).__name__
            },
            status_code=500
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
