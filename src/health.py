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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
