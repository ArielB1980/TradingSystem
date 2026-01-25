"""
Unified server: FastAPI + Streamlit subprocess (reverse proxy).

Dev-only / optional. Production uses separate dashboard and web components.
Not referenced in .do/app.yaml.
"""
import os
import subprocess
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional
import uvicorn
import httpx

# FastAPI app
app = FastAPI(title="Trading System")

# Streamlit process handle
streamlit_process = None


@app.on_event("startup")
async def startup_event():
    """Start Streamlit subprocess."""
    global streamlit_process
    streamlit_process = subprocess.Popen([
        "streamlit", "run",
        "src/dashboard/streamlit_app.py",
        "--server.port", "8501",
        "--server.address", "127.0.0.1",
        "--server.headless", "true",
        "--browser.serverAddress", "0.0.0.0",
        "--server.enableCORS", "false",
        "--server.enableXsrfProtection", "false"
    ])
    # Wait for Streamlit to start
    time.sleep(3)


@app.on_event("shutdown")
async def shutdown_event():
    """Stop Streamlit subprocess."""
    if streamlit_process:
        streamlit_process.terminate()
        streamlit_process.wait()


# Health and API endpoints
@app.get("/api")
async def root():
    """Root endpoint."""
    return {"status": "ok", "service": "trading-system"}


@app.get("/api/health")
async def health():
    """Health check endpoint."""
    checks = {
        "status": "healthy",
        "database": "unknown",
        "environment": os.getenv("ENVIRONMENT", "unknown"),
        "streamlit": "unknown"
    }

    database_url = os.getenv("DATABASE_URL")
    if database_url:
        checks["database"] = "configured"
    else:
        checks["database"] = "missing"
        checks["status"] = "unhealthy"

    # Check if Streamlit is running
    if streamlit_process and streamlit_process.poll() is None:
        checks["streamlit"] = "running"
    else:
        checks["streamlit"] = "stopped"
        checks["status"] = "unhealthy"

    status_code = 200 if checks["status"] == "healthy" else 503
    return JSONResponse(content=checks, status_code=status_code)


@app.get("/api/ready")
async def ready():
    """Readiness probe."""
    return {"status": "ready"}


@app.get("/api/quick-test")
async def quick_test():
    """Quick system connectivity test."""
    results = {
        "database": "unknown",
        "api_keys": "unknown",
        "environment": os.getenv("ENVIRONMENT", "unknown")
    }

    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        results["database"] = "configured"
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


@app.get("/api/debug/signals")
async def debug_signals(symbol: Optional[str] = None):
    """Debug endpoint for signals."""
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

                results["recent_decisions"].append({
                    "time": str(timestamp),
                    "symbol": symbol,
                    "signal": signal,
                    "quality": quality,
                    "reasoning": reasoning[-1] if reasoning else "No reasoning logged"
                })

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


# Proxy all other requests to Streamlit
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"])
async def proxy_to_streamlit(request: Request, path: str):
    """Proxy non-API requests to Streamlit."""
    streamlit_url = f"http://127.0.0.1:8501/{path}"

    # Get query params
    query_params = str(request.url.query)
    if query_params:
        streamlit_url += f"?{query_params}"

    async with httpx.AsyncClient() as client:
        # Forward headers (except host)
        headers = dict(request.headers)
        headers.pop("host", None)

        # Make request to Streamlit
        try:
            if request.method == "GET":
                response = await client.get(streamlit_url, headers=headers, follow_redirects=True)
            elif request.method == "POST":
                body = await request.body()
                response = await client.post(streamlit_url, content=body, headers=headers, follow_redirects=True)
            else:
                # Handle other methods
                body = await request.body()
                response = await client.request(
                    request.method,
                    streamlit_url,
                    content=body,
                    headers=headers,
                    follow_redirects=True
                )

            # Return response
            return StreamingResponse(
                response.aiter_bytes(),
                status_code=response.status_code,
                headers=dict(response.headers)
            )
        except Exception as e:
            return JSONResponse(
                content={"error": f"Failed to proxy to Streamlit: {str(e)}"},
                status_code=502
            )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
