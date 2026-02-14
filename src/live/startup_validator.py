"""
Startup validator for live trading.

Ensures all monitored coins have initial DECISION_TRACE events before starting trading.
Prevents missing coin data issues.
"""
from datetime import datetime, timezone
from typing import List
from src.exceptions import OperationalError, DataError
from src.storage.repository import get_latest_traces, async_record_event
from src.monitoring.logger import get_logger
import asyncio

logger = get_logger(__name__)


async def ensure_all_coins_have_traces(monitored_symbols: List[str]) -> dict:
    """
    Ensure all monitored coins have at least one DECISION_TRACE event.
    
    This prevents coins from being invisible in the dashboard.
    
    Args:
        monitored_symbols: List of all symbols being monitored
        
    Returns:
        Dict with validation results:
        - total: Total coins checked
        - existing: Coins that already had traces
        - created: Coins that got initial traces created
        - errors: Coins that failed to create traces
    """
    logger.info("Validating coin data coverage", total_coins=len(monitored_symbols))
    
    # Get existing traces
    traces = get_latest_traces(limit=1000)
    traces_by_symbol = {trace.get('symbol'): trace for trace in traces}
    
    # Find missing coins
    missing_symbols = [s for s in monitored_symbols if s not in traces_by_symbol]
    
    existing_count = len(monitored_symbols) - len(missing_symbols)
    
    if not missing_symbols:
        logger.info("All coins have trace data", total=len(monitored_symbols))
        return {
            'total': len(monitored_symbols),
            'existing': existing_count,
            'created': 0,
            'errors': []
        }
    
    logger.info(
        "Found coins without trace data",
        missing_count=len(missing_symbols),
        existing_count=existing_count
    )
    
    # Create initial traces for missing coins
    now = datetime.now(timezone.utc)
    created_count = 0
    errors = []
    
    for symbol in missing_symbols:
        try:
            trace_details = {
                "signal": "NO_SIGNAL",
                "regime": "unknown",
                "bias": "neutral",
                "adx": 0.0,
                "atr": 0.0,
                "ema200_slope": "flat",
                "spot_price": 0.0,
                "setup_quality": 0.0,
                "score_breakdown": {},
                "status": "initializing",
                "reason": "startup_initialization"
            }
            
            await async_record_event(
                event_type="DECISION_TRACE",
                symbol=symbol,
                details=trace_details,
                timestamp=now
            )
            
            created_count += 1
            
            if created_count % 10 == 0:
                logger.debug(f"Created {created_count}/{len(missing_symbols)} initial traces")
                
        except (OperationalError, DataError, ValueError) as e:
            logger.error("Failed to create initial trace for symbol", symbol=symbol, error=str(e), error_type=type(e).__name__)
            errors.append({'symbol': symbol, 'error': str(e)})
    
    logger.info(
        "Startup validation complete",
        total=len(monitored_symbols),
        existing=existing_count,
        created=created_count,
        errors=len(errors)
    )
    
    if errors:
        logger.warning("Some coins failed initialization", error_count=len(errors))
    
    return {
        'total': len(monitored_symbols),
        'existing': existing_count,
        'created': created_count,
        'errors': errors
    }


async def validate_market_coverage(monitored_symbols: List[str], min_coverage_pct: float = 95.0) -> bool:
    """
    Validate that we have data coverage for at least min_coverage_pct of monitored coins.
    
    Args:
        monitored_symbols: List of all symbols being monitored
        min_coverage_pct: Minimum percentage of coins that must have data (default 95%)
        
    Returns:
        True if coverage is sufficient, False otherwise
    """
    traces = get_latest_traces(limit=1000)
    traces_by_symbol = {trace.get('symbol'): trace for trace in traces}
    
    covered_count = sum(1 for s in monitored_symbols if s in traces_by_symbol)
    coverage_pct = (covered_count / len(monitored_symbols)) * 100 if monitored_symbols else 0.0
    
    is_valid = coverage_pct >= min_coverage_pct
    
    logger.info(
        "Market coverage validation",
        total=len(monitored_symbols),
        covered=covered_count,
        coverage_pct=f"{coverage_pct:.1f}%",
        min_required=f"{min_coverage_pct:.1f}%",
        valid=is_valid
    )
    
    return is_valid
