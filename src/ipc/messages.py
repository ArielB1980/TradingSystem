from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from datetime import datetime
from src.domain.models import Candle

@dataclass
class MarketUpdate:
    """Event sent from Data Service to Trading Service."""
    symbol: str
    candles: List[Candle]  # Might be 1 (tick) or 500 (history)
    timeframe: str
    is_historical: bool = False  # True if from DB hydration

@dataclass
class ServiceCommand:
    """Control command sent to services."""
    command: str  # STOP, PAUSE, RESUME
    payload: Optional[Dict[str, Any]] = None

@dataclass
class ServiceStatus:
    """Heartbeat/Status update from services."""
    service_name: str
    status: str
    timestamp: datetime
    details: Optional[Dict[str, Any]] = None
