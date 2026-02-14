"""
ReplayDataStore — Provides candle data and synthetic liquidity parameters
for the replay harness.

Candles are loaded from parquet/CSV files.
Liquidity parameters (spread, depth, volatility regime) are either loaded
from a file or derived from candle data.
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.domain.models import Candle


@dataclass
class LiquidityParams:
    """Per-symbol per-minute liquidity parameters."""
    spread_bps: float = 5.0       # bid-ask spread in basis points
    depth_usd_at_1bp: float = 50_000.0  # order book depth in USD at 1bp from mid
    volatility_regime: str = "normal"    # "low", "normal", "high", "extreme"
    fill_delay_seconds: float = 0.5      # simulated fill latency

    @property
    def spread_fraction(self) -> Decimal:
        return Decimal(str(self.spread_bps / 10_000))


@dataclass
class CandleBar:
    """Lightweight candle for fast replay (avoids full Candle overhead)."""
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class ReplayDataStore:
    """Loads and serves candle data + liquidity params for replay.

    Data layout (expected):
        data_dir/
          candles/
            BTC_USD_1m.csv    (columns: timestamp,open,high,low,close,volume)
            ETH_USD_1m.csv
            ...
          liquidity/
            BTC_USD.csv       (columns: timestamp,spread_bps,depth_usd,vol_regime)
            ...  (optional — will be derived from candles if missing)
    """

    def __init__(self, data_dir: Path, symbols: List[str], timeframes: Optional[List[str]] = None):
        self._data_dir = Path(data_dir)
        self._symbols = symbols
        self._timeframes = timeframes or ["1m"]

        # symbol -> timeframe -> sorted list of CandleBars
        self._candles: Dict[str, Dict[str, List[CandleBar]]] = {}
        # symbol -> sorted list of (datetime, LiquidityParams)
        self._liquidity: Dict[str, List[Tuple[datetime, LiquidityParams]]] = {}
        # symbol -> timeframe -> index cursor for current replay position
        self._cursors: Dict[str, Dict[str, int]] = {}

    def load(self) -> None:
        """Load all data from disk."""
        for symbol in self._symbols:
            self._candles[symbol] = {}
            self._cursors[symbol] = {}
            for tf in self._timeframes:
                bars = self._load_candles(symbol, tf)
                self._candles[symbol][tf] = bars
                self._cursors[symbol][tf] = 0
            self._liquidity[symbol] = self._load_or_derive_liquidity(symbol)

    def _load_candles(self, symbol: str, timeframe: str) -> List[CandleBar]:
        """Load candles from CSV. Expected columns: timestamp,open,high,low,close,volume."""
        safe_sym = symbol.replace("/", "_").replace(":", "_")
        path = self._data_dir / "candles" / f"{safe_sym}_{timeframe}.csv"
        if not path.exists():
            return []
        bars: List[CandleBar] = []
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts_str = row["timestamp"]
                # Support both ISO format and Unix timestamp
                try:
                    if "T" in ts_str or "-" in ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    else:
                        ts = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                bars.append(CandleBar(
                    timestamp=ts,
                    open=Decimal(row["open"]),
                    high=Decimal(row["high"]),
                    low=Decimal(row["low"]),
                    close=Decimal(row["close"]),
                    volume=Decimal(row.get("volume", "0")),
                ))
        bars.sort(key=lambda b: b.timestamp)
        return bars

    def _load_or_derive_liquidity(self, symbol: str) -> List[Tuple[datetime, LiquidityParams]]:
        """Load liquidity from CSV or derive from 1m candles."""
        safe_sym = symbol.replace("/", "_").replace(":", "_")
        path = self._data_dir / "liquidity" / f"{safe_sym}.csv"
        if path.exists():
            return self._load_liquidity_csv(path)
        # Derive from 1m candles
        return self._derive_liquidity(symbol)

    def _load_liquidity_csv(self, path: Path) -> List[Tuple[datetime, LiquidityParams]]:
        result: List[Tuple[datetime, LiquidityParams]] = []
        with open(path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                ts_str = row["timestamp"]
                try:
                    if "T" in ts_str or "-" in ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    else:
                        ts = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                result.append((ts, LiquidityParams(
                    spread_bps=float(row.get("spread_bps", 5.0)),
                    depth_usd_at_1bp=float(row.get("depth_usd", 50000)),
                    volatility_regime=row.get("vol_regime", "normal"),
                )))
        result.sort(key=lambda x: x[0])
        return result

    def _derive_liquidity(self, symbol: str) -> List[Tuple[datetime, LiquidityParams]]:
        """Derive liquidity model from 1m candles using ATR-based heuristics."""
        bars = self._candles.get(symbol, {}).get("1m", [])
        if not bars:
            return []

        result: List[Tuple[datetime, LiquidityParams]] = []
        window = 20  # 20-bar ATR for volatility regime

        for i, bar in enumerate(bars):
            # Compute ATR% for volatility regime
            recent = bars[max(0, i - window):i + 1]
            if len(recent) < 2:
                atr_pct = 0.005  # default 0.5%
            else:
                ranges = [float(b.high - b.low) / max(float(b.close), 0.001) for b in recent]
                atr_pct = sum(ranges) / len(ranges)

            # Spread increases with volatility
            if atr_pct < 0.003:
                regime = "low"
                spread_bps = 3.0
                depth = 100_000.0
            elif atr_pct < 0.008:
                regime = "normal"
                spread_bps = 5.0
                depth = 50_000.0
            elif atr_pct < 0.02:
                regime = "high"
                spread_bps = 12.0
                depth = 20_000.0
            else:
                regime = "extreme"
                spread_bps = 25.0
                depth = 5_000.0

            # Depth decreases when volume drops
            vol = float(bar.volume)
            if vol > 0:
                vol_factor = min(1.0, vol / 100_000)  # normalized
                depth *= max(0.2, vol_factor)

            result.append((bar.timestamp, LiquidityParams(
                spread_bps=spread_bps,
                depth_usd_at_1bp=depth,
                volatility_regime=regime,
            )))

        return result

    # -- Query interface --

    def get_candles_up_to(
        self,
        symbol: str,
        timeframe: str,
        up_to: datetime,
        limit: int = 500,
    ) -> List[CandleBar]:
        """Return candles up to (inclusive) the given time, most recent last."""
        bars = self._candles.get(symbol, {}).get(timeframe, [])
        # Binary search for efficiency
        idx = self._bisect_right(bars, up_to)
        start = max(0, idx - limit)
        return bars[start:idx]

    def get_candle_at(self, symbol: str, timeframe: str, at: datetime) -> Optional[CandleBar]:
        """Return the candle whose timestamp is <= at (current bar)."""
        bars = self._candles.get(symbol, {}).get(timeframe, [])
        idx = self._bisect_right(bars, at)
        if idx > 0:
            return bars[idx - 1]
        return None

    def get_liquidity_at(self, symbol: str, at: datetime) -> LiquidityParams:
        """Return liquidity params at the given time."""
        liq = self._liquidity.get(symbol, [])
        if not liq:
            return LiquidityParams()  # defaults
        # Binary search
        idx = self._bisect_right_liq(liq, at)
        if idx > 0:
            return liq[idx - 1][1]
        return liq[0][1]

    def get_all_symbols(self) -> List[str]:
        return list(self._symbols)

    def get_time_range(self, symbol: str, timeframe: str = "1m") -> Optional[Tuple[datetime, datetime]]:
        """Return (first_timestamp, last_timestamp) for a symbol."""
        bars = self._candles.get(symbol, {}).get(timeframe, [])
        if not bars:
            return None
        return (bars[0].timestamp, bars[-1].timestamp)

    @staticmethod
    def _bisect_right(bars: List[CandleBar], target: datetime) -> int:
        lo, hi = 0, len(bars)
        while lo < hi:
            mid = (lo + hi) // 2
            if bars[mid].timestamp <= target:
                lo = mid + 1
            else:
                hi = mid
        return lo

    @staticmethod
    def _bisect_right_liq(liq: List[Tuple[datetime, LiquidityParams]], target: datetime) -> int:
        lo, hi = 0, len(liq)
        while lo < hi:
            mid = (lo + hi) // 2
            if liq[mid][0] <= target:
                lo = mid + 1
            else:
                hi = mid
        return lo
