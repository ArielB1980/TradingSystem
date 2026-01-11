"""
Coin universe discovery and classification for multi-asset trading.

V2 Feature: Expands from BTC/ETH to full Kraken liquid pair universe.
"""
from typing import List, Dict, Optional
from decimal import Decimal
from dataclasses import dataclass
from datetime import datetime, timedelta
import requests

from src.monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class CoinInfo:
    """Information about a tradeable coin."""
    symbol: str  # e.g., "BTC/USD"
    futures_symbol: str  # e.g., "BTCUSD-PERP"
    tier: str  # "A", "B", or "C"
    max_leverage: float
    spot_volume_24h: Decimal
    last_checked: datetime
    enabled: bool


class CoinClassifier:
    """
    Discovers and classifies tradeable coin pairs from Kraken.
    
    Responsibilities:
    - Query Kraken for available spot pairs
    - Verify futures perp availability
    - Classify by liquidity tier
    - Assign tier-specific max leverage
    """
    
    def __init__(self, config):
        """
        Initialize coin classifier.
        
        Args:
            config: CoinUniverseConfig from main config
        """
        self.config = config
        self.coin_cache: Dict[str, CoinInfo] = {}
        self.last_discovery = None
        
        logger.info("CoinClassifier initialized", config=config.model_dump() if hasattr(config, 'model_dump') else str(config))
    
    def discover_tradeable_pairs(self, force_refresh: bool = False) -> List[CoinInfo]:
        """
        Discover all tradeable pairs from Kraken.
        
        Args:
            force_refresh: If True, bypass cache and re-query Kraken
        
        Returns:
            List of CoinInfo objects for tradeable pairs
        """
        # Use cache if recent (< 1 hour old)
        if not force_refresh and self.last_discovery:
            age = datetime.now() - self.last_discovery
            if age < timedelta(hours=1):
                logger.debug("Using cached coin universe", age_minutes=age.total_seconds()/60)
                return list(self.coin_cache.values())
        
        logger.info("Discovering tradeable pairs from Kraken")
        
        tradeable_coins = []
        
        # Check each configured tier
        for tier, symbols in self.config.liquidity_tiers.items():
            max_leverage = self.config.tier_max_leverage.get(tier, 5.0)
            
            for symbol in symbols:
                # Derive futures symbol (simple mapping for now)
                futures_symbol = self._get_futures_symbol(symbol)
                
                # Get spot volume
                volume = self._get_spot_volume(symbol)
                
                # Check if meets minimum threshold
                if volume and volume >= Decimal(str(self.config.min_spot_volume_24h)):
                    coin_info = CoinInfo(
                        symbol=symbol,
                        futures_symbol=futures_symbol,
                        tier=tier,
                        max_leverage=max_leverage,
                        spot_volume_24h=volume,
                        last_checked=datetime.now(),
                        enabled=True
                    )
                    tradeable_coins.append(coin_info)
                    self.coin_cache[symbol] = coin_info
                    
                    logger.info(
                        "Coin qualified",
                        symbol=symbol,
                        tier=tier,
                        volume_usd=str(volume),
                        max_leverage=max_leverage
                    )
                else:
                    logger.warning(
                        "Coin below volume threshold",
                        symbol=symbol,
                        volume=str(volume) if volume else "N/A",
                        threshold=self.config.min_spot_volume_24h
                    )
        
        self.last_discovery = datetime.now()
        logger.info("Discovery complete", total_coins=len(tradeable_coins))
        
        return tradeable_coins
    
    def get_coin_info(self, symbol: str) -> Optional[CoinInfo]:
        """Get cached info for a specific coin."""
        return self.coin_cache.get(symbol)
    
    def classify_liquidity(self, symbol: str, volume_24h: Decimal) -> str:
        """
        Classify coin into liquidity tier based on 24h volume.
        
        Args:
            symbol: Coin symbol (e.g., "BTC/USD")
            volume_24h: 24h spot volume in USD
        
        Returns:
            Tier: "A", "B", or "C"
        """
        # Tier thresholds (configurable in future)
        if volume_24h >= Decimal("50000000"):  # $50M+
            return "A"
        elif volume_24h >= Decimal("10000000"):  # $10M+
            return "B"
        else:
            return "C"
    
    def get_max_leverage(self, symbol: str) -> float:
        """
        Get tier-specific max leverage for a coin.
        
        Args:
            symbol: Coin symbol
        
        Returns:
            Max leverage allowed (respects global 10x cap)
        """
        coin_info = self.coin_cache.get(symbol)
        if coin_info:
            return min(coin_info.max_leverage, 10.0)  # Hard cap at 10x
        
        # Default conservative if not found
        return 5.0
    
    def _get_futures_symbol(self, spot_symbol: str) -> str:
        """
        Convert spot symbol to futures perp symbol.
        
        Args:
            spot_symbol: Spot pair (e.g., "BTC/USD")
        
        Returns:
            Futures symbol (e.g., "BTCUSD-PERP")
        """
        # Simple mapping for common pairs
        mappings = {
            "BTC/USD": "BTCUSD-PERP",
            "ETH/USD": "ETHUSD-PERP",
            "SOL/USD": "SOLUSD-PERP",
            "LINK/USD": "LINKUSD-PERP",
            "AVAX/USD": "AVAXUSD-PERP",
            "MATIC/USD": "MATICUSD-PERP",
        }
        
        if spot_symbol in mappings:
            return mappings[spot_symbol]
        
        # Generic conversion (may need refinement)
        base = spot_symbol.split("/")[0]
        return f"{base}USD-PERP"
    
    def _get_spot_volume(self, symbol: str) -> Optional[Decimal]:
        """
        Get 24h spot volume from Kraken (or mock for now).
        
        Args:
            symbol: Spot pair
        
        Returns:
            24h volume in USD, or None if unavailable
        """
        # TODO: Integrate with real Kraken API
        # For now, return mock data for configured symbols
        
        mock_volumes = {
            "BTC/USD": Decimal("500000000"),  # $500M
            "ETH/USD": Decimal("200000000"),  # $200M
            "SOL/USD": Decimal("80000000"),   # $80M
            "LINK/USD": Decimal("30000000"),  # $30M
            "AVAX/USD": Decimal("25000000"),  # $25M
            "MATIC/USD": Decimal("15000000"), # $15M
        }
        
        volume = mock_volumes.get(symbol)
        
        if volume:
            logger.debug("Spot volume retrieved", symbol=symbol, volume_usd=str(volume))
        else:
            logger.warning("No volume data available", symbol=symbol)
        
        return volume
    
    def disable_coin(self, symbol: str, reason: str):
        """
        Disable a coin from trading (e.g., due to liquidity degradation).
        
        Args:
            symbol: Coin to disable
            reason: Why it was disabled
        """
        if symbol in self.coin_cache:
            self.coin_cache[symbol].enabled = False
            logger.warning("Coin disabled", symbol=symbol, reason=reason)
    
    def enable_coin(self, symbol: str):
        """Re-enable a previously disabled coin."""
        if symbol in self.coin_cache:
            self.coin_cache[symbol].enabled = True
            logger.info("Coin re-enabled", symbol=symbol)
