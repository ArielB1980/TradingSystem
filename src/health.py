"""
Health check endpoint for App Platform.
Simple HTTP server to respond to health checks.

worker_health_app: run.py live --with-health (worker container).
Serves /, /health, /api, /api/health, /api/debug/signals, /debug/signals.
Default app URL (tradingbot-*.ondigitalocean.app) routes to worker, so these
debug endpoints live on the worker health server.
"""
import html as html_module
from datetime import datetime, timezone
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
import os
import time
from typing import Optional, Tuple

app = FastAPI(title="Trading System Health Check")

_worker_start = time.time()


def _metrics_json() -> Tuple[dict, int]:
    """Shared logic for /api/metrics. Returns (content, status_code)."""
    try:
        from src.storage.repository import get_latest_metrics_snapshot
        snap = get_latest_metrics_snapshot()
        out = snap if snap is not None else {}
        return ({"source": "worker_snapshot", "metrics": out}, 200)
    except Exception as e:
        return ({"source": "worker_snapshot", "metrics": {}, "error": str(e)[:100]}, 200)


def _metrics_prometheus() -> Tuple[str, int]:
    """Shared logic for /metrics (Prometheus-style). Returns (body, status_code)."""
    try:
        from src.storage.repository import get_latest_metrics_snapshot
        snap = get_latest_metrics_snapshot()
        if not snap:
            return ("", 200)
        lines = ["# Worker metrics (latest snapshot)"]
        for k, v in sorted(snap.items()):
            if v is None:
                continue
            if isinstance(v, (int, float)):
                lines.append(f"trading_{k} {v}")
            elif isinstance(v, str) and k == "last_tick_at":
                lines.append(f'trading_{k}_iso "{v}"')
            else:
                continue
        return ("\n".join(lines) + "\n", 200)
    except Exception as e:
        return (f"# error: {e}\n", 200)


def get_worker_health_app() -> FastAPI:
    """
    Health app for worker (run.py live --with-health).
    Serves /, /health. Also /api, /api/health, /api/debug/signals, /debug/signals
    so the default app URL (which routes to worker) can serve debug endpoints.
    """

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

    @w.get("/api")
    async def api_root():
        return {"status": "ok", "service": "trading-worker"}

    @w.get("/api/health")
    async def api_health():
        """Quick health: DB ping, environment."""
        out = {"status": "healthy", "database": "unknown", "environment": os.getenv("ENVIRONMENT", "unknown")}
        if os.getenv("DATABASE_URL"):
            out["database"] = "configured"
            try:
                from src.storage.db import get_db
                from sqlalchemy import text
                db = get_db()
                with db.get_session() as session:
                    session.execute(text("SELECT 1;"))
                out["database"] = "connected"
            except Exception as e:
                out["database"] = f"error: {str(e)[:80]}"
                out["status"] = "unhealthy"
        else:
            out["status"] = "unhealthy"
        return JSONResponse(content=out, status_code=200 if out["status"] == "healthy" else 503)

    @w.get("/api/debug/signals")
    async def api_debug_signals(request: Request, symbol: Optional[str] = None, format: Optional[str] = None):
        data = _debug_signals_impl(symbol_filter=symbol)
        return _debug_signals_respond(data, request, format_param=format)

    @w.get("/debug/signals")
    async def debug_signals_route(request: Request, symbol: Optional[str] = None, format: Optional[str] = None):
        data = _debug_signals_impl(symbol_filter=symbol)
        return _debug_signals_respond(data, request, format_param=format)

    @w.get("/api/metrics")
    async def api_metrics():
        content, status = _metrics_json()
        return JSONResponse(content=content, status_code=status)

    @w.get("/metrics")
    async def metrics_prometheus():
        body, status = _metrics_prometheus()
        return PlainTextResponse(body, status_code=status)

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
    """Observability: latest metrics snapshot from worker (DB). Includes last_tick_at, signals_last_min, api_fetch_latency_ms, markets_count, coins_futures_fallback_used."""
    content, status = _metrics_json()
    return JSONResponse(content=content, status_code=status)


@app.get("/metrics")
async def metrics_prometheus():
    """Prometheus-style plain text metrics from latest worker snapshot."""
    body, status = _metrics_prometheus()
    return PlainTextResponse(body, status_code=status)


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


def _debug_signals_html(data: dict) -> str:
    """Render debug signals payload as human-readable HTML."""
    def esc(s: str) -> str:
        if s is None:
            return ""
        return html_module.escape(str(s))

    status = data.get("status", "unknown")
    checked = data.get("checked_events", 0)
    last = data.get("last_signal")
    decisions = data.get("recent_decisions") or []

    status_class = "ok" if status == "success" else "err"
    status_label = "OK" if status == "success" else esc(status)

    html_parts = [
        "<!DOCTYPE html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
        "<title>Debug Signals</title>",
        "<style>"
        "body{font-family:system-ui,-apple-system,sans-serif;max-width:900px;margin:0 auto;padding:1rem;background:#0f0f12;color:#e4e4e7;}",
        "h1{font-size:1.25rem;font-weight:600;margin:0 0 0.5rem;}",
        ".meta{color:#71717a;font-size:0.875rem;margin-bottom:1.5rem;}",
        ".badge{display:inline-block;padding:0.2rem 0.5rem;border-radius:4px;font-size:0.75rem;font-weight:600;}",
        ".badge.ok{background:#166534;color:#bbf7d0;}",
        ".badge.err{background:#7f1d1d;color:#fecaca;}",
        "h2{font-size:1rem;font-weight:600;margin:1.5rem 0 0.5rem;color:#a1a1aa;}",
        ".card{background:#18181b;border:1px solid #27272a;border-radius:8px;padding:1rem;margin-bottom:1rem;}",
        ".card .row{display:flex;flex-wrap:wrap;gap:1rem;margin-bottom:0.5rem;}",
        ".card .k{color:#71717a;font-size:0.8rem;}",
        ".card .v{font-weight:500;}",
        ".signal-long{color:#22c55e;}",
        ".signal-short{color:#ef4444;}",
        ".signal-none{color:#71717a;}",
        "pre{background:#09090b;border:1px solid #27272a;border-radius:6px;padding:0.75rem;overflow-x:auto;font-size:0.8rem;line-height:1.4;white-space:pre-wrap;word-break:break-word;}",
        "table{width:100%;border-collapse:collapse;font-size:0.875rem;}",
        "th,td{padding:0.5rem;text-align:left;border-bottom:1px solid #27272a;}",
        "th{color:#71717a;font-weight:500;}",
        "tr:hover{background:#18181b;}",
        ".reason{max-width:420px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}",
        "a{color:#3b82f6;}",
        "</style></head><body>",
        f"<h1>Debug Signals</h1>",
        f"<div class='meta'>Status: <span class='badge {status_class}'>{status_label}</span> · "
        f"Checked events: {checked} · "
        f"<a href='?format=json'>JSON</a></div>",
    ]

    if data.get("message"):
        html_parts.append(f"<p class='meta'>{esc(data['message'])}</p>")

    if last:
        d = last.get("details") or {}
        reason = d.get("reason") or ""
        sym = last.get("symbol", "—")
        sig = (last.get("signal") or "no_signal").lower()
        sig_cls = "signal-long" if sig == "long" else ("signal-short" if sig == "short" else "signal-none")
        html_parts.append("<h2>Last signal</h2>")
        html_parts.append("<div class='card'>")
        html_parts.append("<div class='row'>")
        html_parts.append(f"<span class='k'>Symbol</span><span class='v'>{esc(sym)}</span>")
        html_parts.append(f"<span class='k'>Signal</span><span class='v {sig_cls}'>{esc(str(last.get('signal', '—')))}</span>")
        html_parts.append(f"<span class='k'>Quality</span><span class='v'>{esc(str(last.get('quality', '—')))}</span>")
        html_parts.append(f"<span class='k'>Time</span><span class='v'>{esc(str(last.get('timestamp', '—')))}</span>")
        html_parts.append("</div>")
        if reason:
            html_parts.append("<pre>" + esc(reason) + "</pre>")
        html_parts.append("</div>")

    html_parts.append("<h2>Recent decisions</h2>")
    html_parts.append("<div class='card'><table><thead><tr><th>Time</th><th>Symbol</th><th>Signal</th><th>Quality</th><th>Reasoning</th></tr></thead><tbody>")
    for dec in decisions[:40]:
        t = dec.get("time", "")
        s = dec.get("symbol", "")
        sig = (dec.get("signal") or "no_signal").lower()
        sig_cls = "signal-long" if sig == "long" else ("signal-short" if sig == "short" else "signal-none")
        q = dec.get("quality", "")
        r = dec.get("reasoning") or "—"
        html_parts.append(
            f"<tr><td>{esc(t)}</td><td>{esc(s)}</td><td class='{sig_cls}'>{esc(str(dec.get('signal', '—')))}</td>"
            f"<td>{esc(str(q))}</td><td class='reason' title='{esc(r)}'>{esc(r)}</td></tr>"
        )
    html_parts.append("</tbody></table></div>")
    html_parts.append("</body></html>")
    return "".join(html_parts)


def _debug_signals_respond(data: dict, request: Request, format_param: Optional[str] = None):
    """Return HTML or JSON based on Accept header or ?format=html|json."""
    use_html = False
    if format_param == "html":
        use_html = True
    elif format_param == "json":
        use_html = False
    else:
        accept = (request.headers.get("accept") or "").lower()
        use_html = "text/html" in accept
    if use_html:
        return HTMLResponse(_debug_signals_html(data))
    return JSONResponse(content=data, status_code=200)


@app.get("/api/debug/signals")
async def debug_signals(request: Request, symbol: Optional[str] = None, format: Optional[str] = None):
    """
    Debug endpoint to find the last generated signal.
    Uses system_events DECISION_TRACE. Optional ?symbol=... to filter.
    Browser (Accept: text/html) or ?format=html → human-readable HTML; ?format=json → raw JSON.
    """
    data = _debug_signals_impl(symbol_filter=symbol)
    return _debug_signals_respond(data, request, format_param=format)


@app.get("/debug/signals")
async def debug_signals_no_api(request: Request, symbol: Optional[str] = None, format: Optional[str] = None):
    """Same as /api/debug/signals, for deployments that strip /api prefix."""
    data = _debug_signals_impl(symbol_filter=symbol)
    return _debug_signals_respond(data, request, format_param=format)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
