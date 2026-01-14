"""
Health check endpoint for App Platform.
Simple HTTP server to respond to health checks.
"""
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import os

app = FastAPI(title="Trading System Health Check")


@app.get("/")
async def root():
    """Root endpoint."""
    return {"status": "ok", "service": "trading-system"}


@app.get("/health")
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


@app.get("/ready")
async def ready():
    """Readiness probe endpoint."""
    return {"status": "ready"}


@app.get("/test")
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
            text=True,
            timeout=120
        )
        
        stdout, stderr = process.communicate()
        
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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
