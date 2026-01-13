"""
Dashboard data loader.

Fetches and structures data for the single-page coin monitor.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Optional, Dict
from src.storage.repository import get_latest_traces, get_candles
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def calculate_signal_strength(details: dict) -> float:
    """
    Calculate signal strength from score breakdown.
    
    Args:
        details: Trace details containing score_breakdown
        
    Returns:
        Signal strength (0.0 to 1.0)
    """
    score_breakdown = details.get('score_breakdown', {})
    if not score_breakdown:
        return 0.0
    
    # Sum all score components
    total_score = sum(float(v) for v in score_breakdown.values())
    
    # Normalize to 0-1 range (assuming max score is ~5)
    return min(total_score / 5.0, 1.0)


def calculate_24h_change(symbol: str, current_price: float) -> float:
    """
    Calculate 24h price change percentage.
    
    Args:
        symbol: Trading symbol
        current_price: Current price
        
    Returns:
        24h change percentage
    """
    try:
        from datetime import datetime, timezone, timedelta
        
        # Get candles from 24h ago
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        candles = get_candles(symbol, "1h", limit=25)
        
        if not candles or len(candles) < 2:
            return 0.0
        
        # Get price from ~24h ago (oldest candle)
        price_24h_ago = float(candles[0].close)
        
        if price_24h_ago == 0:
            return 0.0
        
        change_pct = ((current_price - price_24h_ago) / price_24h_ago) * 100
        return change_pct
        
    except Exception as e:
        logger.debug(f"Failed to calculate 24h change for {symbol}", error=str(e))
        return 0.0


@dataclass
class CoinSnapshot:
    """Snapshot of a coin's current analysis state."""
    symbol: str
    price: float
    change_24h: float
    regime: str
    bias: str
    signal: str
    quality: float
    adx: float
    atr: float
    ema200_slope: str
    score_breakdown: Dict[str, float]
    last_update: datetime
    last_signal: Optional[datetime]
    status: str  # "active" | "stale" | "dead"
    
    @property
    def status_emoji(self) -> str:
        """Get status indicator emoji."""
        if self.status == "active":
            return "🟢"
        elif self.status == "stale":
            return "🟡"
        else:
            return "🔴"
    
    @property
    def signal_emoji(self) -> str:
        """Get signal indicator emoji."""
        if self.signal == "LONG":
            return "🟢"
        elif self.signal == "SHORT":
            return "🔴"
        else:
            return "⚪"
    
    @property
    def quality_color(self) -> str:
        """Get quality color class."""
        if self.quality >= 71:
            return "quality-high"
        elif self.quality >= 41:
            return "quality-mid"
        else:
            return "quality-low"


def load_all_coins() -> List[CoinSnapshot]:
    """
    Load latest analysis state for all tracked coins.
    
    Returns:
        List of CoinSnapshot objects, sorted alphabetically by symbol.
    """
    try:
        from src.config.config import load_config
        from src.dashboard.utils import _get_monitored_symbols
        
        # Get all configured symbols (should be 250 coins)
        config = load_config()
        all_symbols = _get_monitored_symbols(config)
        
        # Get latest DECISION_TRACE for each symbol (creates a dict for quick lookup)
        traces = get_latest_traces(limit=500)
        traces_by_symbol = {trace.get('symbol'): trace for trace in traces}
        
        snapshots = []
        now = datetime.now(timezone.utc)
        
        # Process all configured symbols, not just ones with traces
        for symbol in all_symbols:
            trace = traces_by_symbol.get(symbol)
            
            if trace:
                # Parse details JSON
                details = trace.get('details', {})
                
                # Calculate status based on last update
                last_update = trace.get('timestamp')
                if last_update:
                    age_seconds = (now - last_update).total_seconds()
                    if age_seconds < 600:  # < 10 minutes
                        status = "active"
                    elif age_seconds < 3600:  # < 1 hour
                        status = "stale"
                    else:
                        status = "dead"
                else:
                    status = "dead"
                
                # Extract score breakdown
                score_breakdown = details.get('score_breakdown', {})
                
                # Create snapshot
                snapshot = CoinSnapshot(
                    symbol=symbol,
                    price=details.get('spot_price', 0.0),
                    change_24h=calculate_24h_change(symbol, details.get('spot_price', 0.0)),
                    regime=details.get('regime', 'unknown'),
                    bias=details.get('bias', 'neutral'),
                    signal=details.get('signal', 'NO_SIGNAL'),
                    quality=calculate_signal_strength(details),
                    adx=details.get('adx', 0.0),
                    atr=details.get('atr', 0.0),
                    ema200_slope=details.get('ema200_slope', 'flat'),
                    score_breakdown=score_breakdown,
                    last_update=last_update or now,
                    last_signal=None,  # TODO: Query for last non-NO_SIGNAL
                    status=status
                )
            else:
                # No trace yet - create default snapshot
                snapshot = CoinSnapshot(
                    symbol=symbol,
                    price=0.0,
                    change_24h=0.0,
                    regime='unknown',
                    bias='neutral',
                    signal='NO_SIGNAL',
                    quality=0.0,
                    adx=0.0,
                    atr=0.0,
                    ema200_slope='flat',
                    score_breakdown={},
                    last_update=now,
                    last_signal=None,
                    status='dead'  # No data yet
                )
            
            snapshots.append(snapshot)
        
        # Sort alphabetically by symbol
        snapshots.sort(key=lambda x: x.symbol)
        
        logger.info(f"Loaded {len(snapshots)} coin snapshots ({len([s for s in snapshots if s.status != 'dead'])} with data)")
        return snapshots
        
    except Exception as e:
        logger.error("Failed to load coin snapshots", error=str(e))
        return []


def get_coin_detail(symbol: str) -> Optional[Dict]:
    """
    Get detailed analysis for a specific coin.
    
    Args:
        symbol: Coin symbol (e.g., "BTC/USD")
        
    Returns:
        Dict with detailed analysis data, or None if not found.
    """
    try:
        from src.storage.repository import get_recent_events
        
        # Get recent events for this symbol
        events = get_recent_events(limit=10, symbol=symbol, event_type="DECISION_TRACE")
        
        if not events:
            return None
        
        # Helper to ensure datetime
        def to_datetime(ts):
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(ts.replace('Z', '+00:00'))
                except:
                    return datetime.now(timezone.utc)
            return ts or datetime.now(timezone.utc)
        
        # Get latest event
        latest = events[0]
        details = latest.get('details', {})
        
        # Build detail view
        return {
            'symbol': symbol,
            'latest_analysis': {
                'regime': details.get('regime'),
                'bias': details.get('bias'),
                'signal': details.get('signal'),
                'quality': details.get('setup_quality'),
                'reasoning': details.get('reasoning', 'No reasoning available'),
                'timestamp': to_datetime(latest.get('timestamp'))
            },
            'recent_signals': [
                {
                    'timestamp': to_datetime(e.get('timestamp')),
                    'signal': e.get('details', {}).get('signal'),
                    'quality': e.get('details', {}).get('setup_quality', 0)
                }
                for e in events
            ]
        }
        
    except Exception as e:
        logger.error(f"Failed to get detail for {symbol}", error=str(e))
        return None
