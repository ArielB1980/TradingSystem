"""
Load discovered markets from JSON (file-based). Used by dashboard.

Live trading uses MarketDiscoveryService (API-based) in src.services.market_discovery.
"""
import json
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timezone
from src.monitoring.logger import get_logger
from src.data.fiat_currencies import has_disallowed_base

logger = get_logger(__name__)

MARKETS_FILE = Path(__file__).parent.parent.parent / "data" / "discovered_markets.json"


def load_discovered_mapping() -> Optional[dict]:
    """Load spot->futures mapping from discovered_markets.json. Returns None if missing."""
    if not MARKETS_FILE.exists():
        return None
    try:
        with open(MARKETS_FILE, "r") as f:
            data = json.load(f)
            mapping = data.get("mapping") or None
            if not isinstance(mapping, dict):
                return None
            # Filter out any excluded bases (fiat + stablecoin) from cached discovery output.
            return {
                spot: fut
                for spot, fut in mapping.items()
                if not has_disallowed_base(spot) and not has_disallowed_base(fut)
            }
    except Exception:
        return None


def load_discovered_markets() -> Optional[List[str]]:
    """
    Load discovered markets from the daily discovery process.

    Returns:
        List of spot symbols if file exists and is valid, None otherwise
    """
    if not MARKETS_FILE.exists():
        logger.debug("Discovered markets file does not exist", file=str(MARKETS_FILE))
        return None

    try:
        with open(MARKETS_FILE, "r") as f:
            data = json.load(f)
            markets = data.get("markets", [])
            discovered_at = data.get("discovered_at", "")

            if markets:
                # Filter out any excluded bases (fiat + stablecoin) from cached discovery output.
                markets = [m for m in markets if not has_disallowed_base(m)]
                logger.info(
                    "Loaded discovered markets",
                    count=len(markets),
                    discovered_at=discovered_at,
                )
                return markets
            logger.warning("Discovered markets file is empty")
            return None
    except json.JSONDecodeError as e:
        logger.error("Failed to parse discovered markets file", error=str(e))
        return None
    except Exception as e:
        logger.error("Failed to load discovered markets", error=str(e))
        return None


def get_discovered_markets_timestamp() -> Optional[datetime]:
    """Get the timestamp when markets were last discovered."""
    if not MARKETS_FILE.exists():
        return None
    try:
        with open(MARKETS_FILE, "r") as f:
            data = json.load(f)
            discovered_at_str = data.get("discovered_at", "")
            if discovered_at_str:
                return datetime.fromisoformat(discovered_at_str.replace("Z", "+00:00"))
    except Exception:
        pass
    return None
