"""
Utility functions for loading discovered markets from the discovery process.
"""
import json
from pathlib import Path
from typing import List, Optional
from datetime import datetime, timezone
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# Path to discovered markets file
MARKETS_FILE = Path(__file__).parent.parent.parent / "data" / "discovered_markets.json"


def load_discovered_mapping() -> Optional[dict]:
    """Load spot->futures mapping from discovered_markets.json. Returns None if missing."""
    if not MARKETS_FILE.exists():
        return None
    try:
        with open(MARKETS_FILE, "r") as f:
            data = json.load(f)
            return data.get("mapping")
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
        with open(MARKETS_FILE, 'r') as f:
            data = json.load(f)
            markets = data.get('markets', [])
            discovered_at = data.get('discovered_at', '')
            
            if markets:
                logger.info(
                    "Loaded discovered markets",
                    count=len(markets),
                    discovered_at=discovered_at
                )
                return markets
            else:
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
        with open(MARKETS_FILE, 'r') as f:
            data = json.load(f)
            discovered_at_str = data.get('discovered_at', '')
            if discovered_at_str:
                return datetime.fromisoformat(discovered_at_str.replace('Z', '+00:00'))
    except Exception:
        pass
    
    return None
