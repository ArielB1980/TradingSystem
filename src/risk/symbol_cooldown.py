"""
Symbol-level loss tracking and cooldown management.

Tracks recent losses per symbol and applies trading cooldowns
to symbols that have exceeded the loss threshold.
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple
from decimal import Decimal

from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# In-memory cache for cooldowns (symbol -> cooldown_until timestamp)
_symbol_cooldowns: Dict[str, datetime] = {}


def normalize_symbol(symbol: str) -> str:
    """Normalize symbol to base format for matching."""
    # Handle both WIF/USD and PF_WIFUSD formats
    base = symbol.replace("PF_", "").replace("USD", "").replace("/", "")
    return base.upper()


def get_symbol_loss_stats(symbol: str, lookback_hours: int = 24) -> Tuple[int, float]:
    """
    Query database for recent losses on a symbol.
    
    Args:
        symbol: Trading symbol (e.g., 'WIF/USD' or 'PF_WIFUSD')
        lookback_hours: Hours to look back for losses
        
    Returns:
        Tuple of (loss_count, total_pnl_pct)
    """
    try:
        import psycopg2
        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            return 0, 0.0
            
        conn = psycopg2.connect(database_url)
        cur = conn.cursor()
        
        # Normalize symbol for matching
        base_symbol = normalize_symbol(symbol)
        
        # Query trades table for losses
        cur.execute("""
            SELECT COUNT(*), COALESCE(SUM(net_pnl), 0), COALESCE(SUM(size_notional), 0)
            FROM trades 
            WHERE (
                UPPER(REPLACE(REPLACE(symbol, 'PF_', ''), '/', '')) LIKE %s
                OR UPPER(REPLACE(REPLACE(symbol, 'USD', ''), '/', '')) LIKE %s
            )
            AND net_pnl < 0
            AND exited_at >= NOW() - INTERVAL '%s hours'
        """, (f"%{base_symbol}%", f"%{base_symbol}%", lookback_hours))
        
        result = cur.fetchone()
        loss_count = result[0] if result and result[0] else 0
        total_pnl = float(result[1]) if result and result[1] else 0.0
        total_notional = float(result[2]) if result and result[2] else 1.0
        
        # Calculate PnL as percentage of notional
        pnl_pct = (total_pnl / total_notional * 100) if total_notional > 0 else 0.0
        
        cur.close()
        conn.close()
        
        return loss_count, pnl_pct
        
    except Exception as e:
        logger.warning("Failed to query symbol losses", symbol=symbol, error=str(e))
        return 0, 0.0


def check_symbol_cooldown(
    symbol: str,
    lookback_hours: int = 24,
    loss_threshold: int = 3,
    cooldown_hours: int = 12,
    min_pnl_pct: float = -0.5,
) -> Tuple[bool, Optional[str]]:
    """
    Check if a symbol should be on cooldown due to repeated losses.
    
    Args:
        symbol: Trading symbol
        lookback_hours: Hours to look back for losses
        loss_threshold: Number of losses to trigger cooldown
        cooldown_hours: Hours to pause trading after threshold
        min_pnl_pct: Minimum loss percentage to count (e.g., -0.5 = -0.5%)
        
    Returns:
        Tuple of (is_on_cooldown, reason)
    """
    normalized = normalize_symbol(symbol)
    
    # Check in-memory cooldown cache first
    if normalized in _symbol_cooldowns:
        cooldown_until = _symbol_cooldowns[normalized]
        if datetime.now(timezone.utc) < cooldown_until:
            remaining = (cooldown_until - datetime.now(timezone.utc)).total_seconds() / 3600
            return True, f"Symbol on cooldown for {remaining:.1f}h more (repeated losses)"
        else:
            # Cooldown expired, remove from cache
            del _symbol_cooldowns[normalized]
    
    # Query recent losses
    loss_count, pnl_pct = get_symbol_loss_stats(symbol, lookback_hours)
    
    if loss_count >= loss_threshold:
        # Apply cooldown
        cooldown_until = datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)
        _symbol_cooldowns[normalized] = cooldown_until
        
        logger.warning(
            "Symbol cooldown applied due to repeated losses",
            symbol=symbol,
            loss_count=loss_count,
            total_pnl_pct=f"{pnl_pct:.2f}%",
            cooldown_hours=cooldown_hours,
            cooldown_until=cooldown_until.isoformat()
        )
        
        return True, f"Symbol paused: {loss_count} losses in {lookback_hours}h (cooldown {cooldown_hours}h)"
    
    return False, None


def clear_symbol_cooldown(symbol: str) -> bool:
    """
    Manually clear a symbol's cooldown.
    
    Args:
        symbol: Trading symbol
        
    Returns:
        True if cooldown was cleared, False if none existed
    """
    normalized = normalize_symbol(symbol)
    if normalized in _symbol_cooldowns:
        del _symbol_cooldowns[normalized]
        logger.info("Symbol cooldown manually cleared", symbol=symbol)
        return True
    return False


def get_all_cooldowns() -> Dict[str, str]:
    """
    Get all active symbol cooldowns.
    
    Returns:
        Dict of symbol -> cooldown_until timestamp
    """
    now = datetime.now(timezone.utc)
    active = {}
    expired = []
    
    for symbol, cooldown_until in _symbol_cooldowns.items():
        if cooldown_until > now:
            remaining = (cooldown_until - now).total_seconds() / 3600
            active[symbol] = f"{remaining:.1f}h remaining"
        else:
            expired.append(symbol)
    
    # Clean up expired
    for symbol in expired:
        del _symbol_cooldowns[symbol]
    
    return active
