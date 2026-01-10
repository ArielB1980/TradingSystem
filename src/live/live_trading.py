"""
Live trading runtime (stub).

TODO: Full implementation with:
- Real Kraken Futures orders
- Safety gates enforcement
- Kill switch integration
- Real-time reconciliation
"""
from src.config.config import Config
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class LiveTrading:
    """Live trading with real capital (REAL RISK)."""
    
    def __init__(self, config: Config):
        """Initialize live trading."""
        self.config = config
        logger.info("Live Trading initialized (stub)")
    
    async def run(self):
        """Run live trading loop."""
        logger.critical("LIVE TRADING MODE - REAL CAPITAL AT RISK (stub)")
        
        # TODO: Implement full live trading logic with safety gates
        logger.warning("Live trading implementation pending")
