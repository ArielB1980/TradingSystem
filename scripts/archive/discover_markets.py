#!/usr/bin/env python3
"""
Daily Market Discovery Script

Discovers all coins supported by Kraken Futures API and saves the list.
Should be run daily at midnight to keep the market list up-to-date.

Usage:
    python scripts/discover_markets.py
"""
import asyncio
import json
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Set

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.data.kraken_client import KrakenClient
from src.config.config import load_config
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# Output file for discovered markets
MARKETS_FILE = project_root / "data" / "discovered_markets.json"


async def discover_all_futures_markets() -> List[str]:
    """
    Discover all spot symbols that have Kraken Futures perpetuals.
    
    Returns:
        List of spot symbols (e.g., ["BTC/USD", "ETH/USD", ...])
    """
    config = load_config()
    
    # Initialize Kraken client
    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        futures_api_key=config.exchange.futures_api_key,
        futures_api_secret=config.exchange.futures_api_secret,
        use_testnet=config.exchange.use_testnet
    )
    
    try:
        logger.info("Starting market discovery...")
        
        # Fetch all futures markets
        futures_markets = await client.futures_exchange.fetch_markets()
        
        # Extract spot symbols that have futures perpetuals
        spot_symbols: Set[str] = set()
        
        for market in futures_markets:
            # Only include active perpetual swaps
            if market.get('type') == 'swap' and market.get('active', False):
                symbol = market.get('symbol', '')
                
                # Convert futures symbol to spot symbol format
                # Kraken futures format: "BTC/USD:USD" -> "BTC/USD"
                if ':' in symbol:
                    spot_symbol = symbol.split(':')[0]
                    spot_symbols.add(spot_symbol)
                else:
                    # Some futures symbols might already be in spot format
                    spot_symbols.add(symbol)
        
        # Sort for consistency
        sorted_symbols = sorted(list(spot_symbols))
        
        logger.info(
            "Market discovery complete",
            total_futures_markets=len(futures_markets),
            active_perps=len(sorted_symbols),
            sample_symbols=sorted_symbols[:10]
        )
        
        return sorted_symbols
        
    except Exception as e:
        logger.error("Failed to discover markets", error=str(e))
        raise
    finally:
        # Close async connections
        if client.futures_exchange:
            await client.futures_exchange.close()


def save_discovered_markets(symbols: List[str]) -> None:
    """Save discovered markets to JSON file."""
    # Ensure data directory exists
    MARKETS_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    data = {
        "discovered_at": datetime.now(timezone.utc).isoformat(),
        "total_markets": len(symbols),
        "markets": symbols
    }
    
    # Write to file atomically
    temp_file = MARKETS_FILE.with_suffix('.json.tmp')
    with open(temp_file, 'w') as f:
        json.dump(data, f, indent=2)
    
    # Atomic move
    temp_file.replace(MARKETS_FILE)
    
    logger.info(
        "Saved discovered markets",
        file=str(MARKETS_FILE),
        count=len(symbols)
    )


def load_discovered_markets() -> List[str]:
    """
    Load previously discovered markets from file.
    
    Returns:
        List of spot symbols, or empty list if file doesn't exist
    """
    if not MARKETS_FILE.exists():
        return []
    
    try:
        with open(MARKETS_FILE, 'r') as f:
            data = json.load(f)
            return data.get('markets', [])
    except Exception as e:
        logger.warning(f"Failed to load discovered markets: {e}")
        return []


async def main():
    """Main execution function."""
    try:
        # Discover all markets
        symbols = await discover_all_futures_markets()
        
        if not symbols:
            logger.error("No markets discovered - exiting")
            sys.exit(1)
        
        # Save to file
        save_discovered_markets(symbols)
        
        # Print summary
        print(f"\n{'='*70}")
        print(f"Market Discovery Complete")
        print(f"{'='*70}")
        print(f"Total markets discovered: {len(symbols)}")
        print(f"Saved to: {MARKETS_FILE}")
        print(f"\nSample markets (first 20):")
        for sym in symbols[:20]:
            print(f"  - {sym}")
        if len(symbols) > 20:
            print(f"  ... and {len(symbols) - 20} more")
        print(f"{'='*70}\n")
        
        sys.exit(0)
        
    except Exception as e:
        logger.error("Market discovery failed", error=str(e))
        print(f"\nERROR: Market discovery failed: {e}\n", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
