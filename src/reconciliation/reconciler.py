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

    def _normalize_symbol_for_comparison(self, symbol: str) -> str:
        """
        Normalize symbol for comparison between exchange and system formats.
        
        Handles: PF_EURUSD, EUR/USD:USD, EURUSD, EUR/USD -> EURUSD (base only)
        """
        if not symbol:
            return ""
        s = str(symbol).upper()
        # Remove Kraken prefixes
        s = s.replace('PF_', '').replace('PI_', '').replace('FI_', '')
        # Remove CCXT suffixes
        s = s.split(':')[0]
        # Remove separators
        s = s.replace('/', '').replace('-', '').replace('_', '')
        # Remove USD suffix for comparison (PF_EURUSD vs EUR/USD:USD both become EUR)
        if s.endswith('USD'):
            s = s[:-3]
        return s

    async def _reconcile_positions(self, exchange_pos: Dict[str, Dict], system_pos: List[Position]):
        """Compare and alert on discrepancies. Alerts on ghosts; deletes zombies from DB."""
        from src.monitoring.alerts import get_alert_system

        # Normalize symbols for comparison (handle format differences)
        # Exchange might return PF_EURUSD, system might store EUR/USD:USD
        exchange_symbols_normalized = {
            self._normalize_symbol_for_comparison(sym): (sym, pos_data)
            for sym, pos_data in exchange_pos.items()
        }
        system_symbols_normalized = {
            self._normalize_symbol_for_comparison(p.symbol): (p.symbol, p)
            for p in system_pos
        }

        exchange_normalized_set = set(exchange_symbols_normalized.keys())
        system_normalized_set = set(system_symbols_normalized.keys())

        # Ghost Positions (Exchange has it, we don't) -> alert only
        ghosts_normalized = exchange_normalized_set - system_normalized_set
        if ghosts_normalized:
            # Get original exchange symbols for reporting
            ghost_symbols = [exchange_symbols_normalized[g][0] for g in ghosts_normalized]
            logger.critical("GHOST POSITIONS DETECTED", symbols=ghost_symbols)
            try:
                get_alert_system().send_alert(
                    "critical",
                    "Reconciliation: Ghost Positions",
                    f"Exchange has positions we do not track: {sorted(ghost_symbols)}. Review and sync or close manually.",
                    metadata={"symbols": ghost_symbols},
                )
            except Exception as e:
                logger.warning("Failed to send ghost-position alert", error=str(e))

        # Zombie Positions (We have it, exchange doesn't) -> delete from DB + alert
        zombies_normalized = system_normalized_set - exchange_normalized_set
        if zombies_normalized:
            # Get original system symbols for deletion
            zombie_symbols = [system_symbols_normalized[z][0] for z in zombies_normalized]
            logger.critical("ZOMBIE POSITIONS DETECTED", symbols=zombie_symbols)
            for z in zombie_symbols:
                try:
                    delete_position(z)
                    logger.info("Removed zombie position from DB", symbol=z)
                except Exception as e:
                    logger.warning("Failed to delete zombie position", symbol=z, error=str(e))
            try:
                get_alert_system().send_alert(
                    "critical",
                    "Reconciliation: Zombie Positions Closed",
                    f"Positions removed from system (missing on exchange): {sorted(zombie_symbols)}.",
                    metadata={"symbols": zombie_symbols},
                )
            except Exception as e:
                logger.warning("Failed to send zombie-position alert", error=str(e))

    async def verify_order_alignment(self, position: Position):
        """Verify that a specific position has the correct orders open."""
        # Logic to check if TP/SL orders exist on exchange
        pass
