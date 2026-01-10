"""
Real-time metrics and alerting.
"""
from decimal import Decimal
from typing import List
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class MetricsCollector:
    """
    Real-time metrics tracking and alerting.
    """
    
    def __init__(self):
        """Initialize metrics collector."""
        self.metrics = {}
        logger.info("Metrics Collector initialized")
    
    def record_signal(self, symbol: str, signal_type: str):
        """Record signal generated."""
        logger.info("Signal recorded", symbol=symbol, signal_type=signal_type)
    
    def record_trade_result(self, symbol: str, pnl: Decimal, exit_reason: str):
        """Record trade result."""
        logger.info(
            "Trade result recorded",
            symbol=symbol,
            pnl=str(pnl),
            exit_reason=exit_reason,
        )
    
    def check_margin_alert(self, margin_usage_pct: Decimal, threshold_pct: Decimal):
        """Check if margin usage exceeds threshold."""
        if margin_usage_pct > threshold_pct:
            logger.warning(
                "ALERT: Margin usage high",
                usage=f"{margin_usage_pct:.1%}",
                threshold=f"{threshold_pct:.1%}",
            )
