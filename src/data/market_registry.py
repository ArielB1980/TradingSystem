"""
Market Registry for automatic discovery and validation of tradable pairs.

Discovers spot markets and futures perpetuals, builds validated mappings,
and applies liquidity filters to determine eligible trading pairs.
"""
import asyncio
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Dict, List, Optional, Set, Tuple
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
    source: str = "spot_mapped"  # "spot_mapped" or "futures_only"
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
        self.last_discovery_report: Dict[str, Any] = {}
        self._last_seen_futures_symbols: Set[str] = set()

    # Permanent Tier-A universe (root-cause guard against dynamic-tier drift for core majors).
    _PINNED_TIER_A_BASES: Set[str] = {"BTC", "ETH", "SOL", "BNB"}
    _SYMBOL_PREFIXES_TO_STRIP: Tuple[str, ...] = ("PF_", "PI_", "FI_")
    _SYMBOL_SUFFIXES_TO_STRIP: Tuple[str, ...] = ("-PERP", "USD")
    _BASE_ALIASES: Dict[str, str] = {"XBT": "BTC"}

    @classmethod
    def _normalize_base_symbol(cls, symbol: Optional[str]) -> str:
        """Normalize spot/futures symbol into canonical base code (e.g. XBT -> BTC)."""
        raw = str(symbol or "").strip().upper()
        if not raw:
            return ""

        # CCXT-style futures symbol suffix (e.g. ETH/USD:USD)
        if ":" in raw:
            raw = raw.split(":", 1)[0]

        # Spot/unified form (e.g. BTC/USD)
        if "/" in raw:
            base = raw.split("/", 1)[0].strip()
            return cls._BASE_ALIASES.get(base, base)

        # Kraken futures IDs / internal forms (e.g. PF_XBTUSD, BTCUSD-PERP)
        for prefix in cls._SYMBOL_PREFIXES_TO_STRIP:
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
                break

        for suffix in cls._SYMBOL_SUFFIXES_TO_STRIP:
            if raw.endswith(suffix):
                raw = raw[: -len(suffix)]

        return cls._BASE_ALIASES.get(raw, raw)

    @classmethod
    def is_pinned_tier_a_symbol(
        cls,
        spot_symbol: Optional[str] = None,
        futures_symbol: Optional[str] = None,
    ) -> bool:
        """Return True when either symbol resolves to a permanently Tier-A base."""
        spot_base = cls._normalize_base_symbol(spot_symbol)
        futures_base = cls._normalize_base_symbol(futures_symbol)
        return spot_base in cls._PINNED_TIER_A_BASES or futures_base in cls._PINNED_TIER_A_BASES
    
    def _get_config_tier(self, symbol: str) -> Optional[str]:
        """
        Get the configured tier for a symbol from config.yaml.
        
        Returns 'A', 'B', 'C', or None if not found.
        """
        try:
            coin_universe = getattr(self.config, 'coin_universe', None)
            if not coin_universe:
                return None
            liquidity_tiers = getattr(coin_universe, 'liquidity_tiers', {})
            for tier in ['A', 'B', 'C']:
                if symbol in liquidity_tiers.get(tier, []):
                    return tier
            return None
        except Exception:
            return None
    
    def _is_config_tier_a(self, symbol: str) -> bool:
        """Check if symbol is in the config's Tier A list."""
        return self._get_config_tier(symbol) == 'A'
    
    def _is_config_tier_b(self, symbol: str) -> bool:
        """Check if symbol is in the config's Tier B list."""
        return self._get_config_tier(symbol) == 'B'
    
    def _get_tier_volume_threshold(self, tier: str) -> Decimal:
        """
        Get minimum futures volume threshold for a tier.
        
        Tier A: Bypasses all filters (trusted majors)
        Tier B: $500k minimum
        Tier C: $250k minimum
        """
        tier_thresholds = {
            'A': Decimal("0"),  # Tier A bypasses
            'B': Decimal("500000"),
            'C': Decimal("250000"),
        }
        return tier_thresholds.get(tier, Decimal("250000"))
    
    def _get_tier_spread_threshold(self, tier: str) -> Decimal:
        """
        Get maximum spread threshold for a tier.
        
        Tier A: Bypasses all filters (trusted majors)
        Tier B: 0.25%
        Tier C: 0.50%
        """
        tier_thresholds = {
            'A': Decimal("1.0"),  # Tier A bypasses (100% = no limit)
            'B': Decimal("0.0025"),  # 0.25%
            'C': Decimal("0.0050"),  # 0.50%
        }
        return tier_thresholds.get(tier, Decimal("0.0050"))
    
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

        # 2. Build mappings (spot×futures and, optionally, futures-only pairs)
        exchange_cfg = getattr(self.config, "exchange", None)
        allow_futures_only_universe = bool(
            getattr(exchange_cfg, "allow_futures_only_universe", False)
        )
        allow_futures_only_pairs = bool(
            getattr(exchange_cfg, "allow_futures_only_pairs", False)
        )
        previous_futures_symbols = set(self._last_seen_futures_symbols)
        if spot_markets and futures_markets:
            pairs = self._build_mappings(
                spot_markets,
                futures_markets,
                include_futures_only=allow_futures_only_pairs,
            )
        elif not spot_markets and futures_markets and allow_futures_only_universe:
            pairs = self._build_futures_only_mappings(futures_markets)
        else:
            pairs = self._build_mappings(spot_markets, futures_markets)

        logger.info("Built %s spot→futures mappings", len(pairs))

        # 3. Apply filters
        eligible_pairs, rejected_reasons = await self._apply_filters(pairs)
        logger.info("%s pairs passed filters", len(eligible_pairs))

        self.discovered_pairs = eligible_pairs
        self.last_discovery = datetime.now(timezone.utc)
        self._last_seen_futures_symbols = {
            str(info.get("symbol"))
            for info in futures_markets.values()
            if info.get("symbol")
        }
        self.last_discovery_report = self._build_discovery_gap_report(
            spot_markets=spot_markets,
            futures_markets=futures_markets,
            candidate_pairs=pairs,
            eligible_pairs=eligible_pairs,
            rejected_reasons=rejected_reasons,
            allow_futures_only_pairs=allow_futures_only_pairs,
            allow_futures_only_universe=allow_futures_only_universe,
            previous_futures_symbols=previous_futures_symbols,
        )
        self._log_discovery_gap_summary(self.last_discovery_report)

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
            pair = self._new_pair(
                spot_symbol=base_quote,
                futures_symbol=info["symbol"],
                source="futures_only",
            )
            pairs[base_quote] = pair
        return pairs
    
    def _build_mappings(
        self, 
        spot_markets: Dict[str, dict], 
        futures_markets: Dict[str, dict],
        include_futures_only: bool = False,
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
                
                pair = self._new_pair(
                    spot_symbol=spot_symbol,
                    futures_symbol=futures_info['symbol'],
                    source="spot_mapped",
                )
                pairs[spot_symbol] = pair
            else:
                logger.debug(f"No futures perp for {spot_symbol}")
        
        # Optional: include futures contracts even when a matching spot market is absent.
        if include_futures_only:
            for base_quote, futures_info in futures_markets.items():
                if base_quote in pairs:
                    continue
                if has_disallowed_base(base_quote) or has_disallowed_base(futures_info.get("symbol")):
                    continue
                pairs[base_quote] = self._new_pair(
                    spot_symbol=base_quote,
                    futures_symbol=futures_info["symbol"],
                    source="futures_only",
                )
        
        return pairs
    
    async def _apply_filters(self, pairs: Dict[str, MarketPair]) -> Tuple[Dict[str, MarketPair], Dict[str, str]]:
        """
        Apply liquidity and spread filters using both spot and futures data.
        
        Filter modes:
        - "futures_primary": Futures filters required, spot filters optional (recommended for perp trading)
        - "spot_and_futures": Both spot and futures filters must pass
        """
        eligible = {}
        rejected: Dict[str, str] = {}
        filters = self.config.liquidity_filters
        filter_mode = getattr(filters, "filter_mode", "futures_primary")
        
        symbols = list(pairs.keys())
        if not symbols:
            return eligible, rejected

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
                    rejected[symbol] = pair.rejection_reason
                    continue
                
                # Populate futures fields
                pair.futures_open_interest = fticker.open_interest
                pair.futures_volume_24h = fticker.volume_24h
                pair.futures_spread_pct = fticker.spread_pct
                pair.funding_rate = fticker.funding_rate
                
                # ============================================================
                # TIER-AWARE FILTERING (V2 REDESIGN)
                # ============================================================
                # Key principle: OI and Funding are UNRELIABLE on Kraken.
                # - OI: Removed as gate (logged only for observability)
                # - Funding: Removed as gate (logged only for observability)
                # - Volume + Spread: PRIMARY gates with tier-specific thresholds
                # - Price: SECONDARY sanity check ($0.01 minimum)
                # ============================================================
                
                # Determine configured tier for this symbol
                config_tier = self._get_config_tier(symbol)
                is_pinned = self.is_pinned_tier_a_symbol(pair.spot_symbol, pair.futures_symbol)
                is_tier_a = config_tier == 'A' or is_pinned
                is_tier_b = config_tier == 'B'
                
                # Use configured tier for thresholds, or default to 'C' (most restrictive)
                effective_tier = 'A' if is_tier_a else ('B' if is_tier_b else 'C')
                
                # --- OI: LOG ONLY (removed as gate - Kraken misreports) ---
                min_oi = getattr(filters, "min_futures_open_interest", Decimal("0")) or Decimal("0")
                if fticker.open_interest < min_oi:
                    logger.debug(
                        "OI below threshold (logged only, not a gate)",
                        symbol=symbol,
                        tier=effective_tier,
                        reported_oi=f"${fticker.open_interest:,.0f}",
                        threshold=f"${min_oi:,.0f}",
                    )
                
                # --- FUNDING: LOG ONLY (removed as gate - Kraken misreports) ---
                max_funding = getattr(filters, "max_funding_rate_abs", None)
                if max_funding and fticker.funding_rate is not None:
                    if abs(fticker.funding_rate) > max_funding:
                        logger.debug(
                            "Funding rate above threshold (logged only, not a gate)",
                            symbol=symbol,
                            tier=effective_tier,
                            reported_funding=f"{fticker.funding_rate:.4%}",
                            threshold=f"{max_funding:.4%}",
                        )
                
                # --- VOLUME: PRIMARY GATE (tier-specific thresholds) ---
                # Tier A bypasses all filters (trusted majors)
                if not is_tier_a:
                    min_vol_tier = self._get_tier_volume_threshold(effective_tier)
                    if fticker.volume_24h < min_vol_tier:
                        pair.is_eligible = False
                        pair.rejection_reason = f"Futures vol ${fticker.volume_24h:,.0f} < ${min_vol_tier:,.0f} (Tier {effective_tier})"
                        rejected[symbol] = pair.rejection_reason
                        continue
                
                # --- SPREAD: PRIMARY GATE (tier-specific thresholds) ---
                # Tier A bypasses all filters (trusted majors)
                if not is_tier_a:
                    max_spread_tier = self._get_tier_spread_threshold(effective_tier)
                    if fticker.spread_pct > max_spread_tier:
                        pair.is_eligible = False
                        pair.rejection_reason = f"Futures spread {fticker.spread_pct:.2%} > {max_spread_tier:.2%} (Tier {effective_tier})"
                        rejected[symbol] = pair.rejection_reason
                        continue
                
                # Log Tier A bypasses for visibility
                if is_tier_a:
                    issues = []
                    if fticker.open_interest < min_oi:
                        issues.append(f"OI=${fticker.open_interest:,.0f}")
                    if fticker.spread_pct > Decimal("0.003"):
                        issues.append(f"spread={fticker.spread_pct:.2%}")
                    if max_funding and fticker.funding_rate and abs(fticker.funding_rate) > max_funding:
                        issues.append(f"funding={fticker.funding_rate:.4%}")
                    if fticker.volume_24h < Decimal("500000"):
                        issues.append(f"vol=${fticker.volume_24h:,.0f}")
                    if issues:
                        logger.warning(
                            "Tier A coin bypassing filters (trusted major)",
                            symbol=symbol,
                            issues=", ".join(issues),
                        )
                
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
                    rejected[symbol] = pair.rejection_reason or "Spot filters failed"
                    continue
                
                # Passed all required filters - classify tier and mark eligible
                pair.liquidity_tier = self._classify_tier(pair)
                pair.is_eligible = True
                eligible[symbol] = pair
                
            except Exception as e:
                logger.warning(f"Failed to filter {symbol}", error=str(e))
                pair.is_eligible = False
                pair.rejection_reason = f"Filter error: {str(e)}"
                rejected[symbol] = pair.rejection_reason
        
        # Log tier distribution
        tier_counts = {"A": 0, "B": 0, "C": 0}
        for p in eligible.values():
            tier_counts[p.liquidity_tier] = tier_counts.get(p.liquidity_tier, 0) + 1
        logger.info("Filtering complete", eligible=len(eligible), tier_distribution=tier_counts)
        
        return eligible, rejected

    def _new_pair(self, spot_symbol: str, futures_symbol: str, source: str) -> MarketPair:
        """Create a default MarketPair placeholder before filters populate metrics."""
        return MarketPair(
            spot_symbol=spot_symbol,
            futures_symbol=futures_symbol,
            spot_volume_24h=Decimal("0"),
            futures_open_interest=Decimal("0"),
            spot_spread_pct=Decimal("0"),
            futures_spread_pct=Decimal("0"),
            futures_volume_24h=Decimal("0"),
            funding_rate=None,
            is_eligible=False,
            source=source,
            last_updated=datetime.now(timezone.utc),
        )

    def _build_discovery_gap_report(
        self,
        *,
        spot_markets: Dict[str, dict],
        futures_markets: Dict[str, dict],
        candidate_pairs: Dict[str, MarketPair],
        eligible_pairs: Dict[str, MarketPair],
        rejected_reasons: Dict[str, str],
        allow_futures_only_pairs: bool,
        allow_futures_only_universe: bool,
        previous_futures_symbols: Set[str],
    ) -> Dict[str, Any]:
        """
        Build per-futures-symbol discovery diagnostics.

        Each futures market is classified as:
        - eligible
        - rejected_by_filters
        - unmapped_no_spot
        - excluded_disallowed_base
        """
        entries: List[Dict[str, Any]] = []
        status_counts = {
            "eligible": 0,
            "rejected_by_filters": 0,
            "unmapped_no_spot": 0,
            "excluded_disallowed_base": 0,
        }
        rejection_reason_counts: Dict[str, int] = {}

        for base_quote in sorted(futures_markets.keys()):
            info = futures_markets.get(base_quote) or {}
            futures_symbol = str(info.get("symbol") or "")
            spot_available = base_quote in spot_markets
            candidate = candidate_pairs.get(base_quote)
            eligible = base_quote in eligible_pairs
            disallowed = has_disallowed_base(base_quote) or has_disallowed_base(futures_symbol)

            if disallowed:
                status = "excluded_disallowed_base"
                reason = "Excluded by disallowed base policy (fiat/stablecoin)."
            elif eligible:
                status = "eligible"
                reason = "Passed all discovery filters."
            elif candidate is not None:
                status = "rejected_by_filters"
                reason = (
                    rejected_reasons.get(base_quote)
                    or candidate.rejection_reason
                    or "Rejected by filters with unknown reason."
                )
                rejection_reason_counts[reason] = rejection_reason_counts.get(reason, 0) + 1
            else:
                status = "unmapped_no_spot"
                reason = (
                    "No matching spot market symbol. Enable exchange.allow_futures_only_pairs "
                    "to evaluate this futures contract directly."
                )

            status_counts[status] = status_counts.get(status, 0) + 1
            is_new = bool(futures_symbol and futures_symbol not in previous_futures_symbols)
            
            # Include tier and metrics for eligible pairs
            eligible_pair = eligible_pairs.get(base_quote)
            tier = eligible_pair.liquidity_tier if eligible_pair else None
            
            entry_data = {
                "spot_symbol": base_quote,
                "futures_symbol": futures_symbol,
                "status": status,
                "reason": reason,
                "is_new": is_new,
                "spot_market_available": spot_available,
                "candidate_considered": candidate is not None,
                "candidate_source": candidate.source if candidate else None,
            }
            
            # Add tier and metrics for eligible pairs
            if eligible_pair:
                entry_data["liquidity_tier"] = tier
                entry_data["futures_volume_24h"] = str(eligible_pair.futures_volume_24h)
                entry_data["futures_spread_pct"] = str(eligible_pair.futures_spread_pct)
                entry_data["futures_open_interest"] = str(eligible_pair.futures_open_interest)
                entry_data["funding_rate"] = str(eligible_pair.funding_rate) if eligible_pair.funding_rate else None
            
            entries.append(entry_data)

        gaps = [e for e in entries if e["status"] != "eligible"]
        new_entries = [e for e in entries if e["is_new"]]
        new_gaps = [e for e in new_entries if e["status"] != "eligible"]
        new_eligible = [e for e in new_entries if e["status"] == "eligible"]
        top_rejection_reasons = sorted(
            rejection_reason_counts.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:20]
        
        # Calculate tier distribution for eligible pairs
        tier_distribution = {"A": 0, "B": 0, "C": 0}
        for pair in eligible_pairs.values():
            tier = pair.liquidity_tier
            tier_distribution[tier] = tier_distribution.get(tier, 0) + 1

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": {
                "allow_futures_only_pairs": allow_futures_only_pairs,
                "allow_futures_only_universe": allow_futures_only_universe,
            },
            "totals": {
                "spot_markets": len(spot_markets),
                "futures_markets": len(futures_markets),
                "candidate_pairs": len(candidate_pairs),
                "eligible_pairs": len(eligible_pairs),
                "gap_count": len(gaps),
            },
            "status_counts": status_counts,
            "tier_distribution": tier_distribution,  # V2: Add tier distribution
            "new_futures_summary": {
                "total": len(new_entries),
                "eligible": len(new_eligible),
                "gaps": len(new_gaps),
            },
            "top_rejection_reasons": [
                {"reason": reason, "count": count} for reason, count in top_rejection_reasons
            ],
            "entries": entries,
            "gaps": gaps,
            "new_futures": new_entries,
            "new_futures_gaps": new_gaps,
        }

    def _log_discovery_gap_summary(self, report: Dict[str, Any]) -> None:
        """Emit concise summary logs for discovery coverage diagnostics."""
        totals = report.get("totals", {})
        status_counts = report.get("status_counts", {})
        new_summary = report.get("new_futures_summary", {})
        logger.info(
            "Discovery gap report generated",
            futures_markets=totals.get("futures_markets", 0),
            candidate_pairs=totals.get("candidate_pairs", 0),
            eligible_pairs=totals.get("eligible_pairs", 0),
            rejected_by_filters=status_counts.get("rejected_by_filters", 0),
            unmapped_no_spot=status_counts.get("unmapped_no_spot", 0),
            excluded_disallowed_base=status_counts.get("excluded_disallowed_base", 0),
            new_futures=new_summary.get("total", 0),
            new_futures_eligible=new_summary.get("eligible", 0),
            new_futures_gaps=new_summary.get("gaps", 0),
        )

    def get_last_discovery_report(self) -> Dict[str, Any]:
        """Return last generated discovery diagnostics report."""
        return self.last_discovery_report
    
    def _classify_tier(self, pair: MarketPair) -> str:
        """
        Classify market pair into liquidity tier.
        
        TIER CLASSIFICATION LOGIC (V2 - No OI, No Funding):
        =====================================================
        Kraken misreports OI and funding for many coins, so we use ONLY:
        - Volume (24h futures volume)
        - Spread (bid-ask spread %)
        
        Classification priority:
        1. Pinned Tier A (BTC, ETH, SOL, BNB) - always Tier A
        2. Config-defined tier (from config.yaml liquidity_tiers)
        3. Dynamic classification based on volume + spread
        
        Dynamic thresholds:
        - Tier A: vol >= $5M, spread <= 0.10%
        - Tier B: vol >= $500k, spread <= 0.25%
        - Tier C: Everything else that passes filters
        
        Risk controls are applied per-tier in execution:
        - Tier A: 10x leverage, $100k max
        - Tier B: 3-5x leverage, $50k max
        - Tier C: 1-2x leverage, $25k max
        """
        # 1. Pinned Tier A (hardcoded majors)
        if self.is_pinned_tier_a_symbol(pair.spot_symbol, pair.futures_symbol):
            return "A"
        
        # 2. Config-defined tier takes precedence
        config_tier = self._get_config_tier(pair.spot_symbol)
        if config_tier:
            return config_tier
        
        # 3. Dynamic classification based on volume + spread (no OI)
        vol = pair.futures_volume_24h or Decimal("0")
        spread = pair.futures_spread_pct or Decimal("1")
        
        # Tier A: High liquidity - vol >= $5M, spread <= 0.10%
        if vol >= Decimal("5000000") and spread <= Decimal("0.0010"):
            return "A"
        # Tier B: Medium liquidity - vol >= $500k, spread <= 0.25%
        elif vol >= Decimal("500000") and spread <= Decimal("0.0025"):
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
