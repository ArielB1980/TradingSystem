"""
Dashboard data utilities.

Helper functions for fetching and formatting data for Streamlit dashboard.
"""
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import asyncio

from src.config.config import load_config
from src.storage.repository import (
    get_active_position,
    get_all_trades,
    get_recent_events,
    get_latest_account_state,
    get_event_stats,
)
from src.domain.models import Position, Side
from src.domain.events import CoinStateSnapshot, REASON_CODES


def _get_monitored_symbols(config) -> List[str]:
    """
    Helper to get full list of monitored symbols.
    
    Priority:
    1. DB Discovery Events (Truth from live worker)
    2. Coin Universe (if enabled)
    3. Config markets (fallback)
    """
    # 1. Check for discovered markets in DB (Most reliable in production)
    try:
        from src.storage.repository import get_recent_events
        discovery_events = get_recent_events(limit=1, event_type="DISCOVERY_UPDATE")
        if discovery_events:
            markets = discovery_events[0].get('details', {}).get('markets', [])
            if markets:
                return markets
    except Exception as e:
        logger.debug(f"Failed to fetch discovery from DB: {e}")

    # 2. Fallback to Local discovery file (if exists, e.g. local dev)
    try:
        from src.utils.market_discovery import load_discovered_markets
        discovered_markets = load_discovered_markets()
        if discovered_markets:
            return discovered_markets
    except Exception:
        pass
    
    # Fallback to Coin Universe if enabled
    if hasattr(config, "coin_universe") and config.coin_universe.enabled:
        expanded = []
        for tier, coins in config.coin_universe.liquidity_tiers.items():
            expanded.extend(coins)
        return list(set(expanded))
    
    # Final fallback to config markets
    return config.exchange.spot_markets

def get_portfolio_metrics() -> Dict[str, Any]:
    """Get portfolio-level metrics."""
    config = load_config()
    
    # Get all configured symbols dynamically
    symbols = _get_monitored_symbols(config)
    
    # Calculate metrics
    active_positions = 0
    total_unrealized_pnl = 0.0
    total_margin_used = 0.0
    
    for symbol in symbols:
        pos = get_active_position(symbol)
        if pos:
            active_positions += 1
            total_unrealized_pnl += float(pos.unrealized_pnl)
            total_margin_used += float(pos.margin_used)
    
    # Get actual equity from account state
    account_state = get_latest_account_state()
    if account_state:
        equity = float(account_state['equity'])
        balance = float(account_state['balance'])
    else:
        # Fallback if no sync yet
        equity = 10000.0
        balance = 10000.0
    
    # Calculate daily PnL (realized today + unrealized)
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    trades_today = get_trades_since(today_start)
    realized_pnl_today = sum(trade.net_pnl for trade in trades_today)
    
    daily_pnl = realized_pnl_today + total_unrealized_pnl
    
    return {
        "equity": equity,
        "balance": balance,
        "margin_used": total_margin_used, 
        "margin_available": equity - total_margin_used,
        "unrealized_pnl": total_unrealized_pnl,
        "daily_pnl": daily_pnl,
        "active_positions": active_positions,
        "max_positions": config.risk.max_concurrent_positions,
        "effective_leverage": total_margin_used / equity if equity > 0 else 0.0,
    }


def get_all_positions() -> List[Dict[str, Any]]:
    """Get all active positions with risk metrics."""
    config = load_config()
    symbols = _get_monitored_symbols(config)
    
    positions = []
    for symbol in symbols:
        pos = get_active_position(symbol)
        if pos:
            # Calculate risk flags
            flags = []
            if pos.liquidation_price and pos.current_mark_price:
                liq_dist = abs(pos.current_mark_price - pos.liquidation_price) / pos.current_mark_price
                if liq_dist < 0.15:
                    flags.append("NEAR_LIQ")
            
            # Get TP fill status
            tp_status = "0/0/0"  # TODO: Parse from tp_order_ids
            
            positions.append({
                "symbol": symbol,
                "side": pos.side.value,
                "notional": float(pos.size_notional),
                "entry_price": float(pos.entry_price),
                "current_price": float(pos.current_mark_price),
                "unrealized_pnl": float(pos.unrealized_pnl),
                "liq_price": float(pos.liquidation_price) if pos.liquidation_price else 0.0,
                
                # V3 Active Management Fields - using correct field names for dashboard
                "initial_stop_price": float(pos.initial_stop_price) if pos.initial_stop_price else None,
                "tp1_price": float(pos.tp1_price) if pos.tp1_price else None,
                "tp2_price": float(pos.tp2_price) if pos.tp2_price else None,
                "final_target_price": float(pos.final_target_price) if pos.final_target_price else None,
                "stop_loss": float(pos.initial_stop_price) if pos.initial_stop_price else 0.0,  # Keep for backward compat
                "tp1": float(pos.tp1_price) if pos.tp1_price else 0.0,  # Keep for backward compat
                "status": "CONFIRMED" if pos.intent_confirmed else ("TP1 HIT" if pos.tp1_hit else "OPEN"),
                "liq_distance_pct": liq_dist * 100 if pos.liquidation_price else 0.0,
                "stop_price": 0.0,  # TODO: Parse from stop_loss_order_id
                "stop_distance_pct": 0.0,
                "tp_status": tp_status,
                "trailing_active": pos.trailing_active,
                "basis_at_entry": 0.0,  # TODO: Store at entry
                "basis_current": 0.0,  # TODO: Calculate current
                "risk_flags": flags,
            })
    
    return positions


def get_coin_snapshots() -> Dict[str, CoinStateSnapshot]:
    """
    Get latest state snapshot for all coins.
    
    TODO: This will be populated by MultiAssetOrchestrator emitting events.
    For now, build from available data.
    """
    config = load_config()
    symbols = _get_monitored_symbols(config)
    
    snapshots = {}
    for symbol in symbols:
        # Get position if exists
        pos = get_active_position(symbol)
        
        # Get latest decision trace
        events = get_recent_events(limit=1, event_type="DECISION_TRACE", symbol=symbol)
        latest = events[0] if events else None
        
        # Get risk validations for rejections
        risk_events = get_recent_events(limit=5, event_type="RISK_VALIDATION", symbol=symbol)
        rejections = [e for e in risk_events if not e.get('details', {}).get('approved', True)]
        
        # Build snapshot
        snapshot = CoinStateSnapshot(
            symbol_spot=symbol,
            symbol_perp=symbol.replace("/", "") + ":USD",
            timestamp=datetime.now(timezone.utc),
            
            # Spot (from decision trace)
            spot_price=Decimal(str(latest['details'].get('spot_price', 0))) if latest else Decimal("0"),
            spot_ohlcv_ts=datetime.now(timezone.utc),
            bias_htf=latest['details'].get('bias', 'neutral') if latest else 'neutral',
            regime=latest['details'].get('regime', 'unknown') if latest else 'unknown',
            adx=Decimal(str(latest['details'].get('adx', 0))) if latest else Decimal("0"),
            atr=Decimal(str(latest['details'].get('atr', 0))) if latest else Decimal("0"),
            ema200_slope=latest['details'].get('ema200_slope', 'flat') if latest else 'flat',
            
            # Position data
            pos_side=pos.side.value if pos else None,
            pos_notional=pos.size_notional if pos else None,
            entry_price=pos.entry_price if pos else None,
            liq_price_exchange=pos.liquidation_price if pos else None,
            
            # Decision
            signal=latest['details'].get('signal', 'HOLD') if latest else 'HOLD',
            setup_quality=float(latest['details'].get('setup_quality', 0)) if latest else 0.0,
            score_breakdown=latest['details'].get('score_breakdown', {}) if latest else {},
            next_action="WAIT",  # TODO: Calculate from state
            block_reason_codes=[r['details'].get('rejection_reasons', ['UNKNOWN'])[0] for r in rejections[:1]],
        )
        
        # Add event stats
        stats = get_event_stats(symbol)
        snapshot.event_count = stats.get('count', 0)
        snapshot.last_event_ts = stats.get('last_event')
        
        snapshots[symbol] = snapshot
    
    return snapshots


def get_system_status() -> Dict[str, Any]:
    """Get system health and status."""
    config = load_config()
    
    # TODO: Get actual status from system state
    return {
        "mode": config.environment.upper(),
        "kill_switch": False,  # TODO: Get from orchestrator
        "trading_status": "RUNNING",
        "last_recon_seconds": 5,  # TODO: Get from reconciliation service
        "spot_feed_health": True,
        "futures_feed_health": True,
        "database_health": True,
        "rate_limit_ok": True,
    }


def get_event_feed(limit: int = 50, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    """Get recent event stream."""
    events = get_recent_events(limit=limit, symbol=symbol)
    
    # Format for display
    formatted = []
    for event in events:
        formatted.append({
            "timestamp": event.get('timestamp', ''),
            "type": event.get('event_type', ''),
            "symbol": event.get('symbol', 'SYSTEM'),
            "message": _format_event_message(event),
            "severity": _get_event_severity(event),
        })
    
    return formatted


def _format_event_message(event: Dict) -> str:
    """Format event for display."""
    event_type = event.get('event_type', '')
    details = event.get('details', {})
    
    if event_type == "SIGNAL_GENERATED":
        signal = details.get('signal_type', 'UNKNOWN')
        return f"Signal: {signal}"
    
    elif event_type == "RISK_VALIDATION":
        approved = details.get('approved', False)
        if approved:
            return "✅ Trade approved"
        else:
            reasons = details.get('rejection_reasons', [])
            main_reason = reasons[0] if reasons else 'Unknown'
            if "Max concurrent positions" in main_reason:
                return "❌ Rejected: Max Positions"
            return f"❌ Rejected: {main_reason}"
    
    elif event_type == "DECISION_TRACE":
        bias = details.get('bias', 'neutral')
        return f"Bias: {bias}"
    
    return str(details)


def _get_event_severity(event: Dict) -> str:
    """Get event severity for color coding."""
    event_type = event.get('event_type', '')
    details = event.get('details', {})
    
    if event_type == "RISK_VALIDATION":
        return "success" if details.get('approved') else "warning"
    
    elif event_type == "SIGNAL_GENERATED":
        signal = details.get('signal_type', '')
        return "info" if signal != "NO_SIGNAL" else "secondary"
    
    return "info"


def format_reason_code(code: str) -> str:
    """Get human-readable reason description."""
    return REASON_CODES.get(code, code)
