"""
Dashboard data loader.

Fetches and structures data for the single-page coin monitor.
"""
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Optional, Dict, Any
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
        24h change percentage (0.0 if cannot calculate)
    """
    try:
        from datetime import datetime, timezone, timedelta
        
        if current_price <= 0:
            return 0.0
        
        # Try multiple timeframes to find 24h price
        # Priority: 1h candles (most accurate), then 15m, then 4h
        for timeframe, limit in [("1h", 25), ("15m", 100), ("4h", 7)]:
            try:
                candles = get_candles(symbol, timeframe, limit=limit)
                
                if not candles or len(candles) < 2:
                    continue
                
                # Find candle closest to 24h ago
                now = datetime.now(timezone.utc)
                target_time = now - timedelta(hours=24)
                
                # Find closest candle to 24h ago
                closest_candle = None
                min_diff = timedelta.max
                
                for candle in candles:
                    diff = abs(candle.timestamp - target_time)
                    if diff < min_diff:
                        min_diff = diff
                        closest_candle = candle
                
                # Use closest candle if within 2 hours of target
                if closest_candle and min_diff < timedelta(hours=2):
                    price_24h_ago = float(closest_candle.close)
                    
                    if price_24h_ago > 0:
                        change_pct = ((current_price - price_24h_ago) / price_24h_ago) * 100
                        return change_pct
                
                # Fallback: use oldest candle if we have enough history
                if len(candles) >= 24:  # At least 24 periods
                    oldest_candle = candles[0]
                    price_24h_ago = float(oldest_candle.close)
                    
                    if price_24h_ago > 0:
                        change_pct = ((current_price - price_24h_ago) / price_24h_ago) * 100
                        return change_pct
                        
            except Exception as e:
                logger.debug(f"Failed to get {timeframe} candles for 24h change", symbol=symbol, error=str(e))
                continue
        
        # If all methods fail, return 0.0 (dashboard will show 0.00%)
        logger.debug(f"Could not calculate 24h change for {symbol} - no suitable candles")
        return 0.0
        
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
    candle_count: int  # Number of 15m candles available (data depth)
    structure: Dict[str, float]
    meta: Dict[str, Any]
    
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


def load_all_coins() -> tuple[List[CoinSnapshot], Dict[str, int]]:
    """
    Load latest analysis state for all tracked coins.
    
    Returns:
        Tuple of (snapshots list, metadata dict with source counts).
    """
    try:
        from src.config.config import load_config
        from src.dashboard.utils import _get_monitored_symbols
        from src.storage.repository import get_recent_events, count_candles
        
        # Get all configured symbols (should be 250 coins)
        config = load_config()
        all_symbols = _get_monitored_symbols(config)
        
        # Get latest DECISION_TRACE for each symbol (creates a dict for quick lookup)
        traces = get_latest_traces(limit=1000) # Increased to cover full universe
        traces_by_symbol = {trace.get('symbol'): trace for trace in traces if trace.get('symbol')}
        
        # Get latest DISCOVERY_UPDATE (Robust source)
        discovery_list = []
        try:
            discovery_events = get_recent_events(limit=1, event_type="DISCOVERY_UPDATE")
            if discovery_events:
                 discovery_list = discovery_events[0].get('details', {}).get('markets', [])
        except Exception as e:
            logger.error("Failed to load discovery events", error=str(e))
        
        # Merge all sources
        # This ensures we see discovered markets even if file sharing fails or config is stale
        monitored_set = set(all_symbols)
        trace_set = set(traces_by_symbol.keys())
        discovery_set = set(discovery_list)
        
        merged_symbols = list(monitored_set | trace_set | discovery_set)
        
        snapshots = []
        now = datetime.now(timezone.utc)
        
        # Process all configured symbols, not just ones with traces
        for symbol in merged_symbols:
            trace = traces_by_symbol.get(symbol)
            
            if trace:
                # Parse details JSON
                details = trace.get('details', {})
                
                # Calculate status based on last update
                last_update = trace.get('timestamp')
                if last_update:
                    # Ensure timezone-aware datetime
                    if isinstance(last_update, str):
                        try:
                            last_update = datetime.fromisoformat(last_update.replace('Z', '+00:00'))
                        except:
                            last_update = datetime.now(timezone.utc)
                    elif last_update.tzinfo is None:
                        # Make timezone-aware if naive
                        last_update = last_update.replace(tzinfo=timezone.utc)
                    
                    age_seconds = (now - last_update).total_seconds()
                    # More lenient thresholds: active = 1h, stale = 6h, dead = >6h
                    # Accounts for batch processing of 250 coins with throttling
                    if age_seconds < 3600:  # < 1 hour (was 10 minutes)
                        status = "active"
                    elif age_seconds < 21600:  # < 6 hours (was 1 hour)
                        status = "stale"
                    else:
                        status = "dead"
                else:
                    status = "dead"
                
                # Extract score breakdown
                score_breakdown = details.get('score_breakdown', {})
                
                # Extract price - ensure it's a valid float
                spot_price = details.get('spot_price', 0.0)
                try:
                    spot_price = float(spot_price) if spot_price else 0.0
                except (TypeError, ValueError):
                    spot_price = 0.0
                
                # Get candle count for data depth (Robust fallback)
                candle_count = details.get('candle_count', 0)
                if candle_count == 0:
                     candle_count = count_candles(symbol, "15m")
                
                # Create snapshot
                snapshot = CoinSnapshot(
                    symbol=symbol,
                    price=spot_price,
                    change_24h=calculate_24h_change(symbol, spot_price) if spot_price > 0 else 0.0,
                    regime=details.get('regime', 'unknown'),
                    bias=details.get('bias', 'neutral'),
                    signal=details.get('signal', 'NO_SIGNAL'),
                    quality=calculate_signal_strength(details),
                    adx=float(details.get('adx', 0.0)) if details.get('adx') is not None else 0.0,
                    atr=float(details.get('atr', 0.0)) if details.get('atr') is not None else 0.0,
                    ema200_slope=details.get('ema200_slope', 'flat'),
                    score_breakdown=score_breakdown if isinstance(score_breakdown, dict) else {},
                    last_update=last_update or now,
                    last_signal=None,  # TODO: Query for last non-NO_SIGNAL
                    status=status,
                    candle_count=candle_count,
                    structure=details.get('structure', {}),
                    meta=details.get('meta', {})
                )
            else:
                # No trace yet - create default snapshot
                # Use a very old timestamp to indicate no data
                no_data_timestamp = datetime.min.replace(tzinfo=timezone.utc)
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
                    last_update=no_data_timestamp,
                    last_signal=None,
                    status='dead',  # No data yet
                    candle_count=count_candles(symbol, "15m"), # Check DB just in case
                    structure={},
                    meta={}
                )
            
            snapshots.append(snapshot)
        
        # Sort alphabetically by symbol
        snapshots.sort(key=lambda x: x.symbol)
        
        # Build metadata for debug/dashboard status
        metadata = {
            "config_count": len(monitored_set),
            "trace_count": len(trace_set),
            "discovery_count": len(discovery_set)
        }
        
        logger.info(f"Loaded {len(snapshots)} coin snapshots ({len([s for s in snapshots if s.status != 'dead'])} with data)")
        return snapshots, metadata
        
    except Exception as e:
        logger.error("Failed to load coin snapshots", error=str(e))
        return [], {}


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
                'structure': details.get('structure', {}),
                'meta': details.get('meta', {}),
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
