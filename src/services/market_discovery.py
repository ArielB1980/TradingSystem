"""
Market Discovery Service.

Responsible for discovering active/tradable markets from the exchange
and updating the system's trading universe.
"""
import asyncio
from typing import List, Set, Dict, Optional
from datetime import datetime, timezone
import json
from pathlib import Path

from src.monitoring.logger import get_logger
from src.data.kraken_client import KrakenClient

logger = get_logger(__name__)

# Persistence path
DATA_DIR = Path(__file__).parent.parent.parent / "data"
MARKETS_FILE = DATA_DIR / "discovered_markets.json"

class MarketDiscoveryService:
    """
    Service to discover and manage the list of tradable markets.
    """
    
    def __init__(self, client: KrakenClient):
        self.client = client
        self._cache_valid_seconds = 3600 * 24  # 24 hours
        
    async def discover_markets(self, filter_volume: bool = True) -> Dict[str, str]:
        """
        Fetch all active futures markets from Kraken.
        
        Args:
            filter_volume: Whether to apply additional volume filters (Not fully impl yet)
            
        Returns:
            Dict mapping Spot Symbol -> Futures Symbol
            Example: {"BTC/USD": "PF_XBTUSD", "ETH/USD": "PF_ETHUSD"}
        """
        try:
            logger.info("Starting market discovery...")
            
            # Ensure futures exchange connection is ready
            if not self.client.futures_exchange:
                await self.client.initialize()
                
            # Fetch all markets
            markets = await self.client.futures_exchange.fetch_markets()
            
            # Map: Spot -> Futures
            mapping: Dict[str, str] = {}
            active_count = 0
            
            for m in markets:
                # Filter for Active Perpetual Swaps
                if m.get('type') == 'swap' and m.get('active', False):
                    active_count += 1
                    futures_symbol = m.get('symbol', '')
                    
                    # Determine Spot Symbol (Base/Quote)
                    # Kraken Futures symbols often "PF_ETHUSD" or "ETH/USD:USD" or "PI_XBTUSD"
                    
                    # Logic 1: Check info 'base' 'quote' if available in CCXT
                    base = m.get('base')
                    quote = m.get('quote')
                    
                    spot_symbol = ""
                    if base and quote:
                        spot_symbol = f"{base}/{quote}"
                    elif ':' in futures_symbol:
                        spot_symbol = futures_symbol.split(':')[0]
                    else:
                        # Fallback for "PF_ETHUSD" -> try to parse? 
                        # This is risky without standard format.
                        # CCXT usually normalizes 'symbol' to 'ETH/USD:USD'.
                        # If 'symbol' is 'PF_ETHUSD', we might need 'id'.
                        # Let's rely on 'symbol' being standard CCXT format (Base/Quote:Settle)
                        if '/' in futures_symbol:
                             spot_symbol = futures_symbol.split(':')[0]
                        else:
                             # Skip weird symbols if handled poorly
                             continue

                    # Handle XBT -> BTC normalization if needed
                    # Config uses BTC/USD. Kraken often uses XBT.
                    if "XBT" in spot_symbol:
                         spot_symbol = spot_symbol.replace("XBT", "BTC")
                    
                    mapping[spot_symbol] = futures_symbol
                        
            sorted_spots = sorted(list(mapping.keys()))
            
            logger.info(
                "Market discovery complete",
                raw_markets=len(markets),
                active_swaps=active_count,
                mapped_pairs=len(mapping),
                sample=sorted_spots[:5]
            )
            
            # Persist for dashboard/debug (File)
            self._save_to_disk(sorted_spots)
            
            # Persist to DB for Dashboard (Container-safe)
            try:
                from src.storage.repository import async_record_event
                await async_record_event(
                    event_type="DISCOVERY_UPDATE",
                    symbol="SYSTEM",
                    details={
                        "count": len(sorted_spots),
                        "markets": sorted_spots,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    },
                    timestamp=datetime.now(timezone.utc)
                )
            except Exception as e:
                logger.error("Failed to record discovery event", error=str(e))
            
            return mapping
            
        except Exception as e:
            logger.error("Failed to discover markets", error=str(e))
            # Fallback to loading from disk (only returns spot list usually, so might be partial)
            # Todo: persist full mapping? For now just re-raise or return empty
            raise

    def _save_to_disk(self, symbols: List[str]):
        """Save discovered list to disk."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                "count": len(symbols),
                "markets": symbols
            }
            with open(MARKETS_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error("Failed to save markets to disk", error=str(e))

    def _load_from_disk(self) -> Optional[List[str]]:
        """Load from disk."""
        if not MARKETS_FILE.exists():
            return None
        try:
            with open(MARKETS_FILE, 'r') as f:
                data = json.load(f)
                return data.get('markets', [])
        except Exception:
            return None
