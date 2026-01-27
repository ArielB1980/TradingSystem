"""
Market Registry for automatic discovery and validation of tradable pairs.

Discovers spot markets and futures perpetuals, builds validated mappings,
and applies liquidity filters to determine eligible trading pairs.
"""
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List, Optional, Set
from datetime import datetime, timezone

from src.data.kraken_client import KrakenClient
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class MarketPair:
    """Validated spot→futures market pair."""
    spot_symbol: str           # e.g., "ETH/USD"
    futures_symbol: str        # e.g., "ETHUSD-PERP"
    spot_volume_24h: Decimal
    futures_open_interest: Optional[Decimal]
    spread_pct: Decimal
    is_eligible: bool
    rejection_reason: Optional[str] = None
    last_updated: datetime = None


class MarketRegistry:
    """
    Discovers and validates tradable market pairs.
    
    Responsibilities:
    - Fetch all Kraken Spot markets
    - Fetch all Kraken Futures perpetuals
    - Build spot→futures mappings
    - Apply liquidity and spread filters
    - Return only eligible pairs
    """
    
    def __init__(self, client: KrakenClient, config):
        self.client = client
        self.config = config
        self.discovered_pairs: Dict[str, MarketPair] = {}
        self.last_discovery: Optional[datetime] = None
    
    async def discover_markets(self) -> Dict[str, MarketPair]:
        """
        Discover all eligible market pairs via client.get_spot_markets / get_futures_markets.
        Returns Dict[spot_symbol, MarketPair]. If spot fails but futures succeed and
        config allows, builds futures-only universe.
        """
        logger.info("Starting market discovery...")

        # 1. Fetch via KrakenClient interface (no spot_exchange / futures_exchange)
        spot_markets = await self._fetch_spot_markets()
        logger.info("Found %s spot markets", len(spot_markets))

        futures_markets = await self._fetch_futures_markets()
        logger.info("Found %s futures perpetuals", len(futures_markets))

        # 2. Build mappings (spot×futures or, if allowed, futures-only)
        allow_futures_only = getattr(
            getattr(self.config, "exchange", None), "allow_futures_only_universe", False
        )
        if spot_markets and futures_markets:
            pairs = self._build_mappings(spot_markets, futures_markets)
        elif not spot_markets and futures_markets and allow_futures_only:
            pairs = self._build_futures_only_mappings(futures_markets)
        else:
            pairs = self._build_mappings(spot_markets, futures_markets)

        logger.info("Built %s spot→futures mappings", len(pairs))

        # 3. Apply filters
        eligible_pairs = await self._apply_filters(pairs)
        logger.info("%s pairs passed filters", len(eligible_pairs))

        self.discovered_pairs = eligible_pairs
        self.last_discovery = datetime.now(timezone.utc)

        return eligible_pairs

    async def _fetch_spot_markets(self) -> Dict[str, dict]:
        """Fetch Kraken spot markets via client.get_spot_markets()."""
        try:
            return await self.client.get_spot_markets()
        except Exception as e:
            logger.error("Failed to fetch spot markets", error=str(e))
            return {}

    async def _fetch_futures_markets(self) -> Dict[str, dict]:
        """Fetch Kraken futures perpetuals via client.get_futures_markets()."""
        try:
            return await self.client.get_futures_markets()
        except Exception as e:
            logger.error("Failed to fetch futures markets", error=str(e))
            return {}

    def _build_futures_only_mappings(self, futures_markets: Dict[str, dict]) -> Dict[str, MarketPair]:
        """Build spot_symbol -> MarketPair when only futures available (base_quote used as spot_symbol)."""
        pairs = {}
        for base_quote, info in futures_markets.items():
            pair = MarketPair(
                spot_symbol=base_quote,
                futures_symbol=info["symbol"],
                spot_volume_24h=Decimal("0"),
                futures_open_interest=None,
                spread_pct=Decimal("0"),
                is_eligible=False,
                last_updated=datetime.now(timezone.utc),
            )
            pairs[base_quote] = pair
        return pairs
    
    def _build_mappings(
        self, 
        spot_markets: Dict[str, dict], 
        futures_markets: Dict[str, dict]
    ) -> Dict[str, MarketPair]:
        """Build spot→futures mappings."""
        pairs = {}
        
        for spot_symbol, spot_info in spot_markets.items():
            # Check if futures perp exists for this base
            if spot_symbol in futures_markets:
                futures_info = futures_markets[spot_symbol]
                
                pair = MarketPair(
                    spot_symbol=spot_symbol,
                    futures_symbol=futures_info['symbol'],
                    spot_volume_24h=Decimal("0"),  # To be filled by filters
                    futures_open_interest=None,
                    spread_pct=Decimal("0"),
                    is_eligible=False,  # Will be set by filters
                    last_updated=datetime.now(timezone.utc)
                )
                pairs[spot_symbol] = pair
            else:
                logger.debug(f"No futures perp for {spot_symbol}")
        
        return pairs
    
    async def _apply_filters(self, pairs: Dict[str, MarketPair]) -> Dict[str, MarketPair]:
        """Apply liquidity and spread filters."""
        eligible = {}
        filters = self.config.liquidity_filters
        
        for symbol, pair in pairs.items():
            try:
                # Fetch 24h volume
                ticker = await self.client.get_spot_ticker(symbol)
                volume_24h = Decimal(str(ticker.get('quoteVolume', 0)))
                pair.spot_volume_24h = volume_24h
                
                # Check minimum volume
                if volume_24h < filters.min_spot_volume_usd_24h:
                    pair.is_eligible = False
                    pair.rejection_reason = f"Volume ${volume_24h:,.0f} < ${filters.min_spot_volume_usd_24h:,.0f}"
                    continue
                
                # Check spread (if available)
                bid = Decimal(str(ticker.get('bid', 0)))
                ask = Decimal(str(ticker.get('ask', 0)))
                if bid > 0 and ask > 0:
                    spread_pct = (ask - bid) / bid
                    pair.spread_pct = spread_pct
                    
                    if spread_pct > filters.max_spread_pct:
                        pair.is_eligible = False
                        pair.rejection_reason = f"Spread {spread_pct:.2%} > {filters.max_spread_pct:.2%}"
                        continue
                
                # Check minimum price
                if hasattr(filters, 'min_price_usd'):
                    last_price = Decimal(str(ticker.get('last', 0)))
                    if last_price < filters.min_price_usd:
                        pair.is_eligible = False
                        pair.rejection_reason = f"Price ${last_price} < ${filters.min_price_usd}"
                        continue
                
                # Passed all filters
                pair.is_eligible = True
                eligible[symbol] = pair
                
            except Exception as e:
                logger.warning(f"Failed to filter {symbol}", error=str(e))
                pair.is_eligible = False
                pair.rejection_reason = f"Filter error: {str(e)}"
        
        return eligible
    
    def get_eligible_markets(
        self, 
        mode: str, 
        whitelist: List[str], 
        blacklist: List[str]
    ) -> List[MarketPair]:
        """
        Apply mode-based filtering to eligible markets.
        
        Args:
            mode: "auto", "whitelist", or "blacklist"
            whitelist: List of symbols to include (if mode=whitelist)
            blacklist: List of symbols to exclude (if mode=blacklist)
        
        Returns:
            List of eligible MarketPairs
        """
        if mode == "whitelist":
            return [
                pair for symbol, pair in self.discovered_pairs.items()
                if symbol in whitelist and pair.is_eligible
            ]
        
        elif mode == "blacklist":
            return [
                pair for symbol, pair in self.discovered_pairs.items()
                if symbol not in blacklist and pair.is_eligible
            ]
        
        else:  # auto
            return [
                pair for pair in self.discovered_pairs.values()
                if pair.is_eligible
            ]
    
    def needs_refresh(self, refresh_hours: int = 24) -> bool:
        """Check if discovery needs refresh."""
        if not self.last_discovery:
            return True
        
        hours_since = (datetime.now(timezone.utc) - self.last_discovery).total_seconds() / 3600
        return hours_since >= refresh_hours
