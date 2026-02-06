"""
Market Discovery Service.

Thin wrapper over MarketRegistry—single source of truth for discoverable markets.
Returns spot -> futures symbol mapping for live trading.
"""
from typing import Dict, List, Optional
from datetime import datetime, timezone
import json
from pathlib import Path

from src.monitoring.logger import get_logger
from src.data.kraken_client import KrakenClient
from src.data.market_registry import MarketRegistry, MarketPair

logger = get_logger(__name__)

# Persistence path (shared with dashboard / discovered_markets_loader)
DATA_DIR = Path(__file__).parent.parent.parent / "data"
MARKETS_FILE = DATA_DIR / "discovered_markets.json"
DISCOVERY_GAP_FILE = DATA_DIR / "discovery_gap_report.json"


class MarketDiscoveryService:
    """
    Thin wrapper over MarketRegistry.
    Discovers tradable markets via MarketRegistry and returns spot -> futures mapping.
    """

    def __init__(self, client: KrakenClient, config: object):
        self.client = client
        self.config = config
        self._registry = MarketRegistry(client, config)
        self._cache_valid_seconds = 3600 * 24  # 24 hours

    async def discover_markets(self, filter_volume: bool = True) -> Dict[str, str]:
        """
        Discover markets via MarketRegistry (single source of truth).
        Returns Dict mapping Spot Symbol -> Futures Symbol.
        filter_volume is ignored; Registry applies its own liquidity filters.
        """
        try:
            logger.info("Starting market discovery (MarketRegistry)...")
            # Registry uses client.get_spot_markets() / get_futures_markets(); they initialize as needed.
            pairs = await self._registry.discover_markets()
            mapping = {spot: pair.futures_symbol for spot, pair in pairs.items()}
            sorted_spots = sorted(mapping.keys())
            discovery_report = self._registry.get_last_discovery_report()

            logger.info(
                "Market discovery complete",
                eligible_pairs=len(mapping),
                sample=sorted_spots[:5],
            )

            self._save_to_disk(sorted_spots, mapping, discovery_report)
            try:
                from src.storage.repository import async_record_event
                await async_record_event(
                    event_type="DISCOVERY_UPDATE",
                    symbol="SYSTEM",
                    details={
                        "count": len(sorted_spots),
                        "markets": sorted_spots,
                        "mapping": mapping,
                        "gap_summary": (discovery_report or {}).get("totals", {}),
                        "gap_status_counts": (discovery_report or {}).get("status_counts", {}),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                    timestamp=datetime.now(timezone.utc),
                )
            except Exception as e:
                logger.error("Failed to record discovery event", error=str(e))

            return mapping
        except Exception as e:
            logger.error("Failed to discover markets", error=str(e))
            raise

    def _save_to_disk(
        self,
        symbols: List[str],
        mapping: Optional[Dict[str, str]] = None,
        discovery_report: Optional[Dict[str, object]] = None,
    ):
        """Save discovered list, spot->futures mapping, and discovery gap diagnostics to disk."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "discovered_at": datetime.now(timezone.utc).isoformat(),
                "count": len(symbols),
                "markets": symbols,
            }
            if mapping:
                data["mapping"] = mapping
            with open(MARKETS_FILE, "w") as f:
                json.dump(data, f, indent=2)
            if discovery_report:
                with open(DISCOVERY_GAP_FILE, "w") as f:
                    json.dump(discovery_report, f, indent=2)
        except Exception as e:
            logger.error("Failed to save markets to disk", error=str(e))

    def _load_from_disk(self) -> Optional[List[str]]:
        """Load from disk."""
        if not MARKETS_FILE.exists():
            return None
        try:
            with open(MARKETS_FILE, "r") as f:
                data = json.load(f)
                return data.get("markets", [])
        except Exception:
            return None
    
    def get_discovered_pairs(self) -> Dict[str, MarketPair]:
        """
        Get the full MarketPair objects from the last discovery.
        Returns Dict[spot_symbol, MarketPair] with tier info.
        """
        return self._registry.discovered_pairs
    
    def get_symbol_tier(self, symbol: str) -> str:
        """
        Get the liquidity tier for a symbol.
        Returns "C" (most conservative) if not found.
        """
        pair = self._registry.discovered_pairs.get(symbol)
        if pair:
            return pair.liquidity_tier
        return "C"

    def get_last_discovery_report(self) -> Dict[str, object]:
        """Return last discovery diagnostics report."""
        return self._registry.get_last_discovery_report()
