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
    get_active_positions,
    get_all_trades,
    get_recent_events,
    get_decision_chain,
    get_latest_account_state,
)
from src.domain.models import Side
from src.dashboard.utils import (
    _spot_to_futures,
    get_daily_pnl_total,
    get_daily_pnl_for_symbol,
)
from src.utils.discovered_markets_loader import load_discovered_mapping

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


# ===== Multi-Asset Endpoints (NEW) =====

@app.get("/api/assets")
async def get_all_assets() -> Dict[str, Any]:
    """
    Get comprehensive status for all eligible assets.
    Uses get_active_positions() and spot->futures mapping for correct position lookup.
    """
    symbols = config.exchange.spot_markets
    position_by_futures = {p.symbol: p for p in get_active_positions()}
    mapping = load_discovered_mapping()
    assets = {}

    for symbol in symbols:
        futures = _spot_to_futures(symbol, mapping_override=mapping)
        pos = position_by_futures.get(futures)
        events = get_recent_events(limit=1, event_type="DECISION_TRACE", symbol=symbol)
        latest_decision = events[0] if events else None
        details = latest_decision.get("details", {}) if latest_decision else {}
        signal_strength = float(details.get("setup_quality", 0))

        risk_events = get_recent_events(limit=5, event_type="RISK_VALIDATION", symbol=symbol)
        rejections = [e for e in risk_events if e.get("details", {}).get("approved") is False]

        assets[symbol] = {
            "health": {"spot_feed": True, "futures_feed": True, "basis": True},
            "market_state": {
                "regime": details.get("regime", "unknown"),
                "bias": details.get("bias", "neutral"),
                "signal_strength": signal_strength,
            },
            "position": {
                "active": pos is not None,
                "side": pos.side.value if pos else None,
                "size_notional": float(pos.size_notional) if pos else 0.0,
                "unrealized_pnl": float(pos.unrealized_pnl) if pos else 0.0,
                "entry_price": float(pos.entry_price) if pos else None,
            },
            "pnl": {
                "daily": get_daily_pnl_for_symbol(symbol, futures_symbol=futures),
                "total": float(pos.unrealized_pnl) if pos else 0.0,
            },
            "rejections": {
                "consecutive": len(rejections),
                "last_reason": (
                    rejections[0]["details"].get("rejection_reasons")
                    if rejections else None
                ),
            },
        }

    return assets


@app.get("/api/portfolio")
async def get_portfolio_metrics() -> Dict[str, Any]:
    """
    Get aggregate portfolio-level metrics.
    
    Returns:
    - Total equity
    - Total PnL (realized + unrealized)
    - Active positions count
    - Assets monitored
    - Assets healthy
    - Kill switch status
    - Utilization metrics
    """
    symbols = config.exchange.spot_markets
    positions = get_active_positions()
    active_count = len(positions)
    total_unrealized = sum(float(p.unrealized_pnl) for p in positions)

    account = get_latest_account_state()
    if account:
        equity = float(account["equity"])
        margin_used = float(account["margin_used"])
        available = float(account["available_margin"])
    else:
        equity = 10000.0
        margin_used = active_count * 1000.0
        available = max(0.0, equity - margin_used)

    kill_switch_active = False
    try:
        from src.utils.kill_switch import read_kill_switch_state
        kill_switch_active = bool(read_kill_switch_state().get("active"))
    except Exception:
        pass

    return {
        "equity": {"total": equity, "available": available, "margin_used": margin_used},
        "pnl": {
            "unrealized": total_unrealized,
            "realized_today": get_daily_pnl_total(),
            "total_today": total_unrealized + get_daily_pnl_total(),
        },
        "positions": {
            "active": active_count,
            "max_allowed": config.risk.max_concurrent_positions,
            "utilization_pct": (active_count / config.risk.max_concurrent_positions) * 100,
        },
        "assets": {
            "monitored": len(symbols),
            "eligible": len(symbols),
            "healthy": len(symbols),
            "unhealthy": 0,
        },
        "safety": {
            "kill_switch_active": kill_switch_active,
            "new_entries_blocked": kill_switch_active or active_count >= config.risk.max_concurrent_positions,
            "daily_loss_limit_pct": config.risk.daily_loss_limit_pct,
        },
    }


@app.get("/api/registry")
async def get_market_registry() -> Dict[str, Any]:
    """
    Get market registry status.
    
    Returns:
    - Eligible markets
    - Rejected markets with reasons
    - Discovery timestamp
    - Filter settings
    """
    # Placeholder: MarketRegistry not yet implemented. Uses config spot_markets.
    return {
        "eligible_pairs": [
            {
                "spot_symbol": symbol,
                "futures_symbol": f"{symbol.replace('/', '')}:USD",
                "is_eligible": True,
                "volume_24h": 0.0,
                "spread_pct": 0.0
            }
            for symbol in config.exchange.spot_markets
        ],
        "rejected_pairs": [],
        "discovery": {
            "last_update": datetime.now().isoformat(),
            "refresh_hours": config.exchange.discovery_refresh_hours if hasattr(config.exchange, 'discovery_refresh_hours') else 24
        },
        "filters": {
            "mode": config.assets.mode if hasattr(config, 'assets') else "auto",
            "liquidity_threshold": 5_000_000
        }
    }



# Serve Static Files (Frontend)
# Ensure the directory exists
static_dir = Path(__file__).parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)

app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
