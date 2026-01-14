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
    import concurrent.futures
    from src.test_system import run_all_tests
    
    try:
        # Run async tests in thread pool (FastAPI already has event loop)
        with concurrent.futures.ThreadPoolExecutor() as executor:
            future = executor.submit(asyncio.run, run_all_tests())
            result = future.result(timeout=120)  # 2 minute timeout
        
        return JSONResponse(content={
            "status": "success" if result else "failed",
            "message": "System tests completed",
            "all_tests_passed": result
        })
    except concurrent.futures.TimeoutError:
        return JSONResponse(
            content={"status": "timeout", "message": "Tests took too long"},
            status_code=504
        )
    except Exception as e:
        return JSONResponse(
            content={"status": "error", "message": str(e)},
            status_code=500
        )


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
