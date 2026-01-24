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
from src.storage.repository import get_active_positions, delete_position

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
        """Fetch all open positions from exchange via Kraken Futures API."""
        try:
            if not self.client.has_valid_futures_credentials():
                return {}
            raw = await self.client.get_all_futures_positions()
            out: Dict[str, Dict] = {}
            for p in raw:
                sym = p.get("symbol")
                if sym and (p.get("size") or 0) != 0:
                    out[str(sym)] = p
            return out
        except Exception as e:
            logger.warning("Failed to fetch exchange positions for reconciliation", error=str(e))
            return {}

    async def _reconcile_positions(self, exchange_pos: Dict[str, Dict], system_pos: List[Position]):
        """Compare and alert on discrepancies. Alerts on ghosts; deletes zombies from DB."""
        from src.monitoring.alerts import get_alert_system

        exchange_symbols = set(exchange_pos.keys())
        system_symbols = {p.symbol for p in system_pos}

        # Ghost Positions (Exchange has it, we don't) -> alert only
        ghosts = exchange_symbols - system_symbols
        if ghosts:
            logger.critical("GHOST POSITIONS DETECTED", symbols=list(ghosts))
            try:
                get_alert_system().send_alert(
                    "critical",
                    "Reconciliation: Ghost Positions",
                    f"Exchange has positions we do not track: {sorted(ghosts)}. Review and sync or close manually.",
                    metadata={"symbols": list(ghosts)},
                )
            except Exception as e:
                logger.warning("Failed to send ghost-position alert", error=str(e))

        # Zombie Positions (We have it, exchange doesn't) -> delete from DB + alert
        zombies = system_symbols - exchange_symbols
        if zombies:
            logger.critical("ZOMBIE POSITIONS DETECTED", symbols=list(zombies))
            for z in zombies:
                try:
                    delete_position(z)
                    logger.info("Removed zombie position from DB", symbol=z)
                except Exception as e:
                    logger.warning("Failed to delete zombie position", symbol=z, error=str(e))
            try:
                get_alert_system().send_alert(
                    "critical",
                    "Reconciliation: Zombie Positions Closed",
                    f"Positions removed from system (missing on exchange): {sorted(zombies)}.",
                    metadata={"symbols": list(zombies)},
                )
            except Exception as e:
                logger.warning("Failed to send zombie-position alert", error=str(e))

    async def verify_order_alignment(self, position: Position):
        """Verify that a specific position has the correct orders open."""
        # Logic to check if TP/SL orders exist on exchange
        pass
