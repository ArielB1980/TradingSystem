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

from src.data.kraken_client import KrakenClient, FuturesTicker
from src.monitoring.logger import get_logger
from src.data.fiat_currencies import has_disallowed_base

logger = get_logger(__name__)


@dataclass
class MarketPair:
    """Validated spot→futures market pair with liquidity tier."""
    spot_symbol: str           # e.g., "ETH/USD"
    futures_symbol: str        # e.g., "ETHUSD-PERP"
    spot_volume_24h: Decimal
    futures_open_interest: Decimal  # Now required (was Optional)
    spot_spread_pct: Decimal   # Renamed from spread_pct for clarity
    futures_spread_pct: Decimal  # NEW: Futures bid-ask spread
    futures_volume_24h: Decimal  # NEW: 24h futures volume
    funding_rate: Optional[Decimal]  # NEW: Current funding rate
    is_eligible: bool
    rejection_reason: Optional[str] = None
    liquidity_tier: str = "C"  # NEW: "A", "B", or "C" tier
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
            # Exclude fiat + stablecoin bases from the universe.
            if has_disallowed_base(base_quote) or has_disallowed_base(info.get("symbol")):
                continue
            pair = MarketPair(
                spot_symbol=base_quote,
                futures_symbol=info["symbol"],
                spot_volume_24h=Decimal("0"),
                futures_open_interest=Decimal("0"),
                spot_spread_pct=Decimal("0"),
                futures_spread_pct=Decimal("0"),
                futures_volume_24h=Decimal("0"),
                funding_rate=None,
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
            # Exclude fiat + stablecoin bases from the universe (e.g., GBP/USD, USDT/USD).
            if has_disallowed_base(spot_symbol):
                continue
            # Check if futures perp exists for this base
            if spot_symbol in futures_markets:
                futures_info = futures_markets[spot_symbol]
                if has_disallowed_base(futures_info.get("symbol")):
                    continue
                
                pair = MarketPair(
                    spot_symbol=spot_symbol,
                    futures_symbol=futures_info['symbol'],
                    spot_volume_24h=Decimal("0"),  # To be filled by filters
                    futures_open_interest=Decimal("0"),  # To be filled by filters
                    spot_spread_pct=Decimal("0"),  # To be filled by filters
                    futures_spread_pct=Decimal("0"),  # To be filled by filters
                    futures_volume_24h=Decimal("0"),  # To be filled by filters
                    funding_rate=None,  # To be filled by filters
                    is_eligible=False,  # Will be set by filters
                    last_updated=datetime.now(timezone.utc)
                )
                pairs[spot_symbol] = pair
            else:
                logger.debug(f"No futures perp for {spot_symbol}")
        
        return pairs
    
    async def _apply_filters(self, pairs: Dict[str, MarketPair]) -> Dict[str, MarketPair]:
        """
        Apply liquidity and spread filters using both spot and futures data.
        
        Filter modes:
        - "futures_primary": Futures filters required, spot filters optional (recommended for perp trading)
        - "spot_and_futures": Both spot and futures filters must pass
        """
        eligible = {}
        filters = self.config.liquidity_filters
        filter_mode = getattr(filters, "filter_mode", "futures_primary")
        
        symbols = list(pairs.keys())
        if not symbols:
            return eligible

        # Fetch spot tickers (for spot filters and price reference)
        spot_tickers: Dict[str, dict] = {}
        if hasattr(self.client, "get_spot_tickers_bulk"):
            try:
                spot_tickers = await self.client.get_spot_tickers_bulk(symbols)
                logger.info(
                    "Fetched spot tickers for discovery filters",
                    requested=len(symbols),
                    received=len(spot_tickers),
                )
            except Exception as e:
                logger.error("Bulk spot ticker fetch failed during discovery filters", error=str(e))
                spot_tickers = {}
        
        # Fetch futures tickers (for futures filters - primary for perp trading)
        futures_tickers: Dict[str, FuturesTicker] = {}
        if hasattr(self.client, "get_futures_tickers_bulk_full"):
            try:
                futures_tickers = await self.client.get_futures_tickers_bulk_full()
                logger.info(
                    "Fetched futures tickers for discovery filters",
                    count=len(futures_tickers),
                )
            except Exception as e:
                logger.error("Bulk futures ticker fetch failed during discovery filters", error=str(e))
                futures_tickers = {}

        for symbol, pair in pairs.items():
            try:
                # --- FUTURES FILTERS (primary gate for futures_primary mode) ---
                # Look up futures ticker by multiple formats
                fticker = (
                    futures_tickers.get(pair.futures_symbol) or
                    futures_tickers.get(symbol) or
                    futures_tickers.get(f"{symbol}:USD")
                )
                
                if not fticker:
                    pair.is_eligible = False
                    pair.rejection_reason = f"No futures ticker data for {pair.futures_symbol}"
                    continue
                
                # Populate futures fields
                pair.futures_open_interest = fticker.open_interest
                pair.futures_volume_24h = fticker.volume_24h
                pair.futures_spread_pct = fticker.spread_pct
                pair.funding_rate = fticker.funding_rate
                
                # Check minimum futures open interest
                min_oi = getattr(filters, "min_futures_open_interest", Decimal("0")) or Decimal("0")
                if fticker.open_interest < min_oi:
                    pair.is_eligible = False
                    pair.rejection_reason = f"OI ${fticker.open_interest:,.0f} < ${min_oi:,.0f}"
                    continue
                
                # Check futures spread
                max_futures_spread = getattr(filters, "max_futures_spread_pct", Decimal("0.003")) or Decimal("0.003")
                if fticker.spread_pct > max_futures_spread:
                    pair.is_eligible = False
                    pair.rejection_reason = f"Futures spread {fticker.spread_pct:.2%} > {max_futures_spread:.2%}"
                    continue
                
                # Check futures volume
                min_futures_vol = getattr(filters, "min_futures_volume_usd_24h", Decimal("0")) or Decimal("0")
                if fticker.volume_24h < min_futures_vol:
                    pair.is_eligible = False
                    pair.rejection_reason = f"Futures vol ${fticker.volume_24h:,.0f} < ${min_futures_vol:,.0f}"
                    continue
                
                # Check funding rate (if configured)
                max_funding = getattr(filters, "max_funding_rate_abs", None)
                if max_funding and fticker.funding_rate is not None:
                    if abs(fticker.funding_rate) > max_funding:
                        pair.is_eligible = False
                        pair.rejection_reason = f"Funding {fticker.funding_rate:.4%} > max {max_funding:.4%}"
                        continue
                
                # --- SPOT FILTERS (optional in futures_primary mode) ---
                spot_ticker = spot_tickers.get(symbol)
                spot_filters_passed = True
                spot_rejection_reason = None
                
                if spot_ticker:
                    # Populate spot fields
                    pair.spot_volume_24h = Decimal(str(spot_ticker.get('quoteVolume', 0)))
                    
                    bid = Decimal(str(spot_ticker.get('bid', 0)))
                    ask = Decimal(str(spot_ticker.get('ask', 0)))
                    if bid > 0 and ask > 0:
                        pair.spot_spread_pct = (ask - bid) / bid
                    
                    # Check spot volume
                    min_spot_vol = getattr(filters, "min_spot_volume_usd_24h", Decimal("0")) or Decimal("0")
                    if pair.spot_volume_24h < min_spot_vol:
                        spot_filters_passed = False
                        spot_rejection_reason = f"Spot vol ${pair.spot_volume_24h:,.0f} < ${min_spot_vol:,.0f}"
                    
                    # Check spot spread
                    max_spot_spread = getattr(filters, "max_spread_pct", Decimal("0.002")) or Decimal("0.002")
                    if spot_filters_passed and pair.spot_spread_pct > max_spot_spread:
                        spot_filters_passed = False
                        spot_rejection_reason = f"Spot spread {pair.spot_spread_pct:.2%} > {max_spot_spread:.2%}"
                    
                    # Check minimum price
                    min_price = getattr(filters, "min_price_usd", Decimal("0.01")) or Decimal("0.01")
                    last_price = Decimal(str(spot_ticker.get('last', 0)))
                    if spot_filters_passed and last_price < min_price:
                        spot_filters_passed = False
                        spot_rejection_reason = f"Price ${last_price} < ${min_price}"
                else:
                    # No spot ticker - only fail if mode requires spot
                    if filter_mode == "spot_and_futures":
                        spot_filters_passed = False
                        spot_rejection_reason = "No spot ticker data"
                
                # Apply filter mode logic
                if filter_mode == "spot_and_futures" and not spot_filters_passed:
                    pair.is_eligible = False
                    pair.rejection_reason = spot_rejection_reason
                    continue
                
                # Passed all required filters - classify tier and mark eligible
                pair.liquidity_tier = self._classify_tier(pair)
                pair.is_eligible = True
                eligible[symbol] = pair
                
            except Exception as e:
                logger.warning(f"Failed to filter {symbol}", error=str(e))
                pair.is_eligible = False
                pair.rejection_reason = f"Filter error: {str(e)}"
        
        # Log tier distribution
        tier_counts = {"A": 0, "B": 0, "C": 0}
        for p in eligible.values():
            tier_counts[p.liquidity_tier] = tier_counts.get(p.liquidity_tier, 0) + 1
        logger.info("Filtering complete", eligible=len(eligible), tier_distribution=tier_counts)
        
        return eligible
    
    def _classify_tier(self, pair: MarketPair) -> str:
        """
        Classify market pair into liquidity tier based on futures metrics.
        
        Tier A: High liquidity (BTC/ETH/SOL tier) - full size/leverage
        Tier B: Medium liquidity - reduced size/leverage
        Tier C: Lower liquidity - restricted size/leverage
        """
        oi = pair.futures_open_interest or Decimal("0")
        vol = pair.futures_volume_24h or Decimal("0")
        spread = pair.futures_spread_pct or Decimal("1")
        
        # Tier A: High liquidity - OI >= $10M, vol >= $5M, spread <= 0.10%
        if oi >= Decimal("10000000") and vol >= Decimal("5000000") and spread <= Decimal("0.0010"):
            return "A"
        # Tier B: Medium liquidity - OI >= $1M, vol >= $1M, spread <= 0.25%
        elif oi >= Decimal("1000000") and vol >= Decimal("1000000") and spread <= Decimal("0.0025"):
            return "B"
        # Tier C: Lower liquidity (eligible but restricted)
        else:
            return "C"
    
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
