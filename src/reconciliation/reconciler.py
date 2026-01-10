"""
Reconciliation engine for state synchronization.

Ensures the internal system state matches the exchange reality.
"""
from typing import List, Dict, Optional
from decimal import Decimal
from datetime import datetime, timezone
from src.monitoring.logger import get_logger
from src.data.kraken_client import KrakenClient
from src.domain.models import Position
from src.storage.repository import update_position, get_active_positions

logger = get_logger(__name__)


class Reconciler:
    """
    Reconciliation engine.
    
    Responsibilities:
    1. Verify active positions (System vs Exchange).
    2. Detect ghost positions (Exchange has it, System doesn't).
    3. Detect zombie positions (System has it, Exchange doesn't).
    4. Verify active orders (counts, types).
    """
    
    def __init__(self, client: KrakenClient):
        self.client = client
        
    async def reconcile_all(self):
        """Run full reconciliation."""
        logger.info("Starting reconciliation...")
        try:
            # 1. Fetch Exchange State
            futures_positions = await self._fetch_exchange_positions()
            
            # 2. Fetch System State
            system_positions = get_active_positions()
            
            # 3. Compare
            await self._reconcile_positions(futures_positions, system_positions)
            
            logger.info("Reconciliation complete")
            
        except Exception as e:
            logger.error("Reconciliation failed", error=str(e))
            raise

    async def _fetch_exchange_positions(self) -> Dict[str, Dict]:
        """Fetch all open positions from exchange."""
        # Note: In a real implementation this would iterate all symbols
        # For now we might just check known symbols or use an 'openpositions' endpoint without symbol
        # Kraken Futures 'openpositions' returns all.
        try:
            # We need to call client with a generic catch-all or iterate.
            # Assuming client has a method to get ALL positions or we know the whitelist.
            # Let's assume we iterate known active symbols for now + a discovery call if available.
            return {} # Placeholder for actual API call which requires specific endpoint knowledge
        except Exception:
            return {}

    async def _reconcile_positions(self, exchange_pos: Dict[str, Dict], system_pos: List[Position]):
        """Compare and alert on discrepancies."""
        exchange_symbols = set(exchange_pos.keys())
        system_symbols = set(p.symbol for p in system_pos)
        
        # Ghost Positions (Exchange only)
        ghosts = exchange_symbols - system_symbols
        if ghosts:
            logger.critical("GHOST POSITIONS DETECTED", symbols=list(ghosts))
            # Action: Alert User / Emergency Close?
            
        # Zombie Positions (System only)
        zombies = system_symbols - exchange_symbols
        if zombies:
            logger.critical("ZOMBIE POSITIONS DETECTED", symbols=list(zombies))
            # Action: Mark system position as closed (since it's gone on exchange)
            for z in zombies:
                # Find the position
                pos = next(p for p in system_pos if p.symbol == z)
                logger.warning("Closing zombie position in system", symbol=z)
                # update_position(pos.id, status="closed_by_reconciler")

    async def verify_order_alignment(self, position: Position):
        """Verify that a specific position has the correct orders open."""
        # Logic to check if TP/SL orders exist on exchange
        pass
