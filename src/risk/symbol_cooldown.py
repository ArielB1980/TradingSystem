"""
Symbol-level loss tracking and cooldown management.

Tracks recent losses per symbol and applies trading cooldowns
to symbols that have exceeded the loss threshold.
"""
import os
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional, Tuple
from decimal import Decimal

from src.data.symbol_utils import normalize_to_base as normalize_symbol
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# In-memory cache for cooldowns (symbol -> cooldown_until timestamp)
_symbol_cooldowns: Dict[str, datetime] = {}


_loss_stats_cache: Dict[str, tuple] = {}  # key: (symbol, lookback_hours) -> ((count, pct), expires_at)
_LOSS_STATS_CACHE_TTL = 300  # 5 minutes


def get_symbol_loss_stats(symbol: str, lookback_hours: int = 24) -> Tuple[int, float]:
    """
    Query database for recent losses on a symbol.
    Uses SQLAlchemy connection pool (not raw psycopg2) and a 5-minute TTL cache.
    
    Args:
        symbol: Trading symbol (e.g., 'WIF/USD' or 'PF_WIFUSD')
        lookback_hours: Hours to look back for losses
        
    Returns:
        Tuple of (loss_count, total_pnl_pct)
    """
    import time as _time

    cache_key = f"{symbol}:{lookback_hours}"
    cached = _loss_stats_cache.get(cache_key)
    if cached and _time.monotonic() < cached[1]:
        return cached[0]

    try:
        from sqlalchemy import text
        from src.storage.db import get_db

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            return 0, 0.0
        
        db = get_db()
        base_symbol = normalize_symbol(symbol)
        
        with db.get_session() as session:
            result = session.execute(
                text("""
                    SELECT COUNT(*), COALESCE(SUM(net_pnl), 0), COALESCE(SUM(size_notional), 0)
                    FROM trades 
                    WHERE (
                        UPPER(REPLACE(REPLACE(symbol, 'PF_', ''), '/', '')) LIKE :base1
                        OR UPPER(REPLACE(REPLACE(symbol, 'USD', ''), '/', '')) LIKE :base2
                    )
                    AND net_pnl < 0
                    AND exited_at >= NOW() - INTERVAL :hours
                """),
                {"base1": f"%{base_symbol}%", "base2": f"%{base_symbol}%", "hours": f"{lookback_hours} hours"},
            )
            row = result.fetchone()
        
        loss_count = row[0] if row and row[0] else 0
        total_pnl = float(row[1]) if row and row[1] else 0.0
        total_notional = float(row[2]) if row and row[2] else 1.0
        
        pnl_pct = (total_pnl / total_notional * 100) if total_notional > 0 else 0.0
        
        stats = (loss_count, pnl_pct)
        _loss_stats_cache[cache_key] = (stats, _time.monotonic() + _LOSS_STATS_CACHE_TTL)
        
        return stats
        
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
