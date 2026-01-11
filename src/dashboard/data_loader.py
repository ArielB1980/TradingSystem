"""
Dashboard data loader.

Fetches and structures data for the single-page coin monitor.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Optional, Dict
from src.storage.repository import get_latest_traces
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


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
        # Get latest DECISION_TRACE for each symbol
        traces = get_latest_traces(limit=300)
        
        snapshots = []
        now = datetime.now(timezone.utc)
        
        for trace in traces:
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
                symbol=trace.get('symbol', 'UNKNOWN'),
                price=details.get('spot_price', 0.0),
                change_24h=0.0,  # TODO: Calculate from historical data
                regime=details.get('regime', 'unknown'),
                bias=details.get('bias', 'neutral'),
                signal=details.get('signal', 'NO_SIGNAL'),
                quality=details.get('setup_quality', 0.0),
                adx=details.get('adx', 0.0),
                atr=details.get('atr', 0.0),
                ema200_slope=details.get('ema200_slope', 'flat'),
                score_breakdown=score_breakdown,
                last_update=last_update or now,
                last_signal=None,  # TODO: Query for last non-NO_SIGNAL
                status=status
            )
            
            snapshots.append(snapshot)
        
        # Sort alphabetically by symbol
        snapshots.sort(key=lambda x: x.symbol)
        
        logger.info(f"Loaded {len(snapshots)} coin snapshots")
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
                'timestamp': latest.get('timestamp')
            },
            'recent_signals': [
                {
                    'timestamp': e.get('timestamp'),
                    'signal': e.get('details', {}).get('signal'),
                    'quality': e.get('details', {}).get('setup_quality', 0)
                }
                for e in events
            ]
        }
        
    except Exception as e:
        logger.error(f"Failed to get detail for {symbol}", error=str(e))
        return None
