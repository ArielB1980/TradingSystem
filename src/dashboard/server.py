"""
FastAPI backend for the Trading Board dashboard.
"""
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Dict, Any, Optional
from datetime import datetime
import os
from pathlib import Path

from src.config.config import load_config
from src.storage.repository import (
    get_active_position, 
    get_all_trades, 
    get_recent_events, 
    get_decision_chain
)
from src.domain.models import Side

app = FastAPI(title="Trading Board")

# CORS for local dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load config
config = load_config()

# API Endpoints
@app.get("/api/status")
async def get_status() -> Dict[str, Any]:
    """Get current system status (Legacy/Simple)."""
    pos = get_active_position()
    
    return {
        "environment": config.environment,
        "active_position": {
            "symbol": pos.symbol,
            "side": pos.side.value,
            "size": float(pos.size_notional),
            "entry_price": float(pos.entry_price),
            "current_price": float(pos.current_mark_price),
            "unrealized_pnl": float(pos.unrealized_pnl),
            "leverage": float(pos.leverage),
            "liquidation_price": float(pos.liquidation_price),
            "opened_at": pos.opened_at.isoformat()
        } if pos else None
    }

@app.get("/api/fleet")
async def get_fleet() -> List[Dict[str, Any]]:
    """Get fleet overview (all coins)."""
    # Use configured markets
    symbols = config.exchange.spot_markets
    fleet = []
    
    for symbol in symbols:
        pos = get_active_position(symbol)
        # Get latest decision trace SPECIFIC to this symbol
        events = get_recent_events(limit=1, event_type="DECISION_TRACE", symbol=symbol)
        latest_decision = events[0] if events else None
        
        fleet.append({
            "symbol": symbol,
            "position": "LONG" if pos and pos.side == Side.LONG else "SHORT" if pos else "NONE",
            "bias": latest_decision['details'].get('bias', 'unknown') if latest_decision else 'unknown',
            "pnl": float(pos.unrealized_pnl) if pos else 0.0,
            "last_updated": latest_decision['timestamp'] if latest_decision else None
        })
    return fleet

@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    """Get relevant config info."""
    return {
        "markets": config.exchange.spot_markets,
        "max_leverage": config.risk.max_leverage,
        "risk_per_trade": config.risk.risk_per_trade_pct,
        "strategy": {
            "bias_timeframes": config.strategy.bias_timeframes,
            "execution_timeframes": config.strategy.execution_timeframes
        }
    }

@app.get("/api/events")
async def get_events(limit: int = 50, type: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get system event stream."""
    return get_recent_events(limit=limit, event_type=type)

@app.get("/api/coin/{symbol:path}")
async def get_coin_details(symbol: str):
    """Get deep drilldown for a coin."""
    # Handle slash in URL path
    symbol = symbol.replace("_", "/") # fallback if client sends underscore
    
    pos = get_active_position(symbol)
    
    # Get recent decision traces (last 5)
    traces = get_recent_events(limit=5, event_type="DECISION_TRACE", symbol=symbol)
    
    # Get recent signals
    signals = get_recent_events(limit=5, event_type="SIGNAL_GENERATED", symbol=symbol)
    
    # Get risk validations
    risk = get_recent_events(limit=5, event_type="RISK_VALIDATION", symbol=symbol)
    
    return {
        "symbol": symbol,
        "position": {
            "side": pos.side.value,
            "size": float(pos.size_notional),
            "pnl": float(pos.unrealized_pnl),
            "entry": float(pos.entry_price)
        } if pos else None,
        "latest_trace": traces[0] if traces else None,
        "history": {
            "traces": traces,
            "signals": signals,
            "risk": risk
        }
    }

@app.get("/api/trades")
async def get_trades() -> List[Dict[str, Any]]:
    """Get recent trades."""
    trades = get_all_trades()
    return [
        {
            "id": t.trade_id,
            "symbol": t.symbol,
            "side": t.side.value,
            "entry_price": float(t.entry_price),
            "exit_price": float(t.exit_price),
            "pnl": float(t.net_pnl),
            "timestamp": t.exited_at.isoformat(),
            "status": "WIN" if t.net_pnl > 0 else "LOSS"
        }
        for t in trades[:50] # Limit to 50
    ]



# Serve Static Files (Frontend)
# Ensure the directory exists
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)

app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
