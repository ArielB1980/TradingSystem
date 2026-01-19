"""
Market Structure Change Tracker.

Tracks market structure changes and requires confirmation + reconfirmation before entry.
Prevents "too early" entries by waiting for structure breaks to be confirmed.
"""
from typing import Dict, Optional, Tuple
from decimal import Decimal
from datetime import datetime, timezone
from dataclasses import dataclass, field
from enum import Enum

from src.domain.models import Candle
from src.strategy.indicators import Indicators
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class MarketStructureState(str, Enum):
    """Market structure states."""
    BULLISH = "bullish"  # Higher highs, higher lows
    BEARISH = "bearish"  # Lower highs, lower lows
    NEUTRAL = "neutral"  # No clear structure
    CHANGING = "changing"  # Structure break detected, awaiting confirmation


@dataclass
class StructureChange:
    """Represents a market structure change event."""
    timestamp: datetime
    previous_state: MarketStructureState
    new_state: MarketStructureState
    break_price: Decimal
    break_type: str  # "swing_high" or "swing_low"
    confirmed: bool = False
    confirmed_at: Optional[datetime] = None
    reconfirmed: bool = False
    reconfirmed_at: Optional[datetime] = None
    entry_ready: bool = False


class MarketStructureTracker:
    """
    Tracks market structure changes and manages confirmation/reconfirmation logic.
    
    Flow:
    1. Detect structure change (BOS)
    2. Wait for confirmation (price holds above/below break)
    3. Wait for reconfirmation (retrace to entry zone)
    4. Signal entry ready
    """
    
    def __init__(
        self, 
        confirmation_candles: int = 3, 
        reconfirmation_candles: int = 2,
        entry_zone_tolerance_pct: float = 0.015,
        entry_zone_tolerance_adaptive: bool = True,
        entry_zone_tolerance_atr_mult: float = 0.3
    ):
        """
        Initialize tracker.
        
        Args:
            confirmation_candles: Number of candles price must hold after break
            reconfirmation_candles: Number of candles for reconfirmation
            entry_zone_tolerance_pct: Base tolerance for "near zone" entries (default 1.5%)
            entry_zone_tolerance_adaptive: If True, scale tolerance with ATR
            entry_zone_tolerance_atr_mult: ATR multiplier for adaptive tolerance
        """
        self.indicators = Indicators()
        self.confirmation_candles = confirmation_candles
        self.reconfirmation_candles = reconfirmation_candles
        
        # Entry zone tolerance settings
        self.entry_zone_tolerance_pct = entry_zone_tolerance_pct
        self.entry_zone_tolerance_adaptive = entry_zone_tolerance_adaptive
        self.entry_zone_tolerance_atr_mult = entry_zone_tolerance_atr_mult
        
        # Per-symbol tracking
        self.structure_state: Dict[str, MarketStructureState] = {}
        self.structure_changes: Dict[str, StructureChange] = {}
        self.swing_highs: Dict[str, Decimal] = {}
        self.swing_lows: Dict[str, Decimal] = {}
    
    def update_structure(
        self,
        symbol: str,
        candles_1h: list[Candle],
        lookback: int = 20
    ) -> Tuple[MarketStructureState, Optional[StructureChange]]:
        """
        Update market structure for symbol and detect changes.
        
        Args:
            symbol: Trading symbol
            candles_1h: 1h candles for structure analysis
            lookback: Lookback period for swing detection
            
        Returns:
            (current_state, latest_change_or_none)
        """
        if len(candles_1h) < lookback + 5:
            # Not enough data
            current_state = self.structure_state.get(symbol, MarketStructureState.NEUTRAL)
            return current_state, None
        
        # Detect swing points
        swing_highs = self.indicators.find_swing_points(candles_1h, lookback=lookback, find_highs=True)
        swing_lows = self.indicators.find_swing_points(candles_1h, lookback=lookback, find_highs=False)
        
        if not swing_highs or not swing_lows:
            current_state = self.structure_state.get(symbol, MarketStructureState.NEUTRAL)
            return current_state, None
        
        # Get most recent swings
        recent_high = swing_highs[-1] if swing_highs else None
        recent_low = swing_lows[-1] if swing_lows else None
        prev_high = swing_highs[-2] if len(swing_highs) >= 2 else None
        prev_low = swing_lows[-2] if len(swing_lows) >= 2 else None
        
        # Determine current structure
        current_state = self._determine_structure(recent_high, recent_low, prev_high, prev_low)
        previous_state = self.structure_state.get(symbol, MarketStructureState.NEUTRAL)
        
        # Check for structure change
        structure_change = None
        if current_state != previous_state and previous_state != MarketStructureState.NEUTRAL:
            # Structure change detected
            current_price = candles_1h[-1].close
            
            if current_state == MarketStructureState.BULLISH and previous_state == MarketStructureState.BEARISH:
                # Bearish to bullish change (break of swing high)
                break_price = recent_high if recent_high else current_price
                structure_change = StructureChange(
                    timestamp=candles_1h[-1].timestamp,
                    previous_state=previous_state,
                    new_state=current_state,
                    break_price=break_price,
                    break_type="swing_high"
                )
            elif current_state == MarketStructureState.BEARISH and previous_state == MarketStructureState.BULLISH:
                # Bullish to bearish change (break of swing low)
                break_price = recent_low if recent_low else current_price
                structure_change = StructureChange(
                    timestamp=candles_1h[-1].timestamp,
                    previous_state=previous_state,
                    new_state=current_state,
                    break_price=break_price,
                    break_type="swing_low"
                )
            
            if structure_change:
                logger.info(
                    "Market structure change detected",
                    symbol=symbol,
                    previous=previous_state.value,
                    new=current_state.value,
                    break_price=str(structure_change.break_price),
                    break_type=structure_change.break_type
                )
                self.structure_changes[symbol] = structure_change
                self.structure_state[symbol] = MarketStructureState.CHANGING
        
        # Update state
        if structure_change is None:
            self.structure_state[symbol] = current_state
        
        # Update stored swings
        self.swing_highs[symbol] = recent_high
        self.swing_lows[symbol] = recent_low
        
        return current_state, structure_change
    
    def check_confirmation(
        self,
        symbol: str,
        candles_1h: list[Candle],
        structure_change: StructureChange,
        required_candles: Optional[int] = None 
    ) -> bool:
        """
        Check if structure change is confirmed.
        
        Confirmation: Price holds above/below break for N candles.
        
        Args:
            symbol: Trading symbol
            candles_1h: Recent candles
            structure_change: The structure change to confirm
            required_candles: Override default confirmation candles (Adaptive Strategy)
            
        Returns:
            True if confirmed
        """
        if structure_change.confirmed:
            return True
        
        threshold = required_candles if required_candles is not None else self.confirmation_candles
        
        if len(candles_1h) < threshold:
            return False
        
        # Check if price has held above/below break
        recent_candles = candles_1h[-threshold:]
        break_price = structure_change.break_price
        
        if structure_change.new_state == MarketStructureState.BULLISH:
            # Bullish break: price must stay above break
            all_above = all(c.low >= break_price * Decimal("0.995") for c in recent_candles)  # 0.5% tolerance
            if all_above:
                structure_change.confirmed = True
                structure_change.confirmed_at = recent_candles[-1].timestamp
                logger.info(
                    "Market structure change confirmed",
                    symbol=symbol,
                    state=structure_change.new_state.value,
                    confirmed_at=structure_change.confirmed_at
                )
                return True
        elif structure_change.new_state == MarketStructureState.BEARISH:
            # Bearish break: price must stay below break
            all_below = all(c.high <= break_price * Decimal("1.005") for c in recent_candles)  # 0.5% tolerance
            if all_below:
                structure_change.confirmed = True
                structure_change.confirmed_at = recent_candles[-1].timestamp
                logger.info(
                    "Market structure change confirmed",
                    symbol=symbol,
                    state=structure_change.new_state.value,
                    confirmed_at=structure_change.confirmed_at
                )
                return True
        
        return False
    
    def check_reconfirmation(
        self,
        symbol: str,
        candles_15m: list[Candle],
        candles_1h: list[Candle],
        structure_change: StructureChange,
        entry_zone: Optional[dict] = None,
        atr_value: Optional[Decimal] = None
    ) -> Tuple[bool, bool]:
        """
        Check if structure change is reconfirmed (ready for entry).
        
        Reconfirmation: Price retraces to entry zone (OB/FVG) after confirmation.
        Now supports configurable tolerance for "near zone" entries.
        
        Args:
            symbol: Trading symbol
            candles_15m: 15m candles for entry timing
            candles_1h: 1h candles for structure
            structure_change: The confirmed structure change
            entry_zone: Optional entry zone (order block or FVG)
            atr_value: Optional ATR value for adaptive tolerance
            
        Returns:
            Tuple of (is_reconfirmed, used_tolerance)
            - is_reconfirmed: True if ready for entry
            - used_tolerance: True if entry used tolerance (not exact zone hit)
        """
        if not structure_change.confirmed:
            return False, False
        
        if structure_change.reconfirmed:
            return True, getattr(structure_change, '_used_tolerance', False)
        
        if not candles_15m or len(candles_15m) < 3:
            return False, False
        
        current_price = candles_15m[-1].close
        break_price = structure_change.break_price
        
        # Calculate adaptive tolerance
        # Base tolerance from config (e.g., 1.5%)
        base_tolerance = Decimal(str(self.entry_zone_tolerance_pct))
        
        # If adaptive and ATR provided, scale tolerance with volatility
        if self.entry_zone_tolerance_adaptive and atr_value:
            # Higher ATR = higher tolerance (more room in volatile markets)
            # Calculate ATR as % of current price
            atr_pct = atr_value / current_price if current_price > 0 else Decimal("0")
            atr_adjustment = atr_pct * Decimal(str(self.entry_zone_tolerance_atr_mult))
            tolerance = base_tolerance + atr_adjustment
            # Cap at 5% max
            tolerance = min(tolerance, Decimal("0.05"))
        else:
            tolerance = base_tolerance
        
        # Helper to check zone proximity with tolerance
        def is_in_or_near_zone(price: Decimal, zone_low: Decimal, zone_high: Decimal) -> Tuple[bool, bool]:
            """
            Check if price is in zone or within tolerance.
            Returns (in_or_near, used_tolerance)
            """
            # Exact zone hit
            if zone_low <= price <= zone_high:
                return True, False
            
            # Tolerance zone (extends both sides of OB/FVG)
            tolerance_buffer_low = zone_low * (Decimal("1") - tolerance)
            tolerance_buffer_high = zone_high * (Decimal("1") + tolerance)
            
            if tolerance_buffer_low <= price <= tolerance_buffer_high:
                return True, True
            
            return False, False
        
        used_tolerance = False
        
        if structure_change.new_state == MarketStructureState.BULLISH:
            # Bullish: Price should have moved up, then retraced to OB/FVG
            if entry_zone:
                zone_top = entry_zone.get('high') or entry_zone.get('top')
                zone_bottom = entry_zone.get('low') or entry_zone.get('bottom')
                
                if zone_top and zone_bottom:
                    zone_top = Decimal(str(zone_top))
                    zone_bottom = Decimal(str(zone_bottom))
                    
                    # Check if price is in or near entry zone (with tolerance)
                    in_zone, used_tolerance = is_in_or_near_zone(current_price, zone_bottom, zone_top)
                    
                    if in_zone:
                        # Check if we've had a pullback (price went up then came back)
                        recent_high = max(c.high for c in candles_15m[-10:])
                        if recent_high > break_price:  # Price moved up after break
                            structure_change.reconfirmed = True
                            structure_change.reconfirmed_at = candles_15m[-1].timestamp
                            structure_change.entry_ready = True
                            structure_change._used_tolerance = used_tolerance
                            
                            entry_type = "TOLERANCE" if used_tolerance else "EXACT"
                            logger.info(
                                f"Market structure reconfirmed ({entry_type}) - entry ready",
                                symbol=symbol,
                                entry_zone=f"${zone_bottom}-${zone_top}",
                                current_price=str(current_price),
                                tolerance=f"{float(tolerance)*100:.2f}%",
                                used_tolerance=used_tolerance
                            )
                            return True, used_tolerance
            else:
                # FALLBACK: No OB/FVG zone defined - use tolerance zone around break price
                # For bullish: reconfirm if price retraces near break price after moving up
                recent_high = max(c.high for c in candles_15m[-10:])
                if recent_high > break_price:  # Confirmed move up
                    # Check if price has retraced toward break price (within tolerance)
                    tolerance_zone_top = break_price * (Decimal("1") + tolerance)
                    tolerance_zone_bottom = break_price * (Decimal("1") - tolerance)
                    
                    if tolerance_zone_bottom <= current_price <= tolerance_zone_top:
                        structure_change.reconfirmed = True
                        structure_change.reconfirmed_at = candles_15m[-1].timestamp
                        structure_change.entry_ready = True
                        structure_change._used_tolerance = True
                        
                        logger.info(
                            f"Market structure reconfirmed (BREAK_PRICE_TOLERANCE) - entry ready",
                            symbol=symbol,
                            break_price=str(break_price),
                            current_price=str(current_price),
                            tolerance=f"{float(tolerance)*100:.2f}%",
                            used_tolerance=True
                        )
                        return True, True
        
        elif structure_change.new_state == MarketStructureState.BEARISH:
            # Bearish: Price should have moved down, then retraced to OB/FVG
            if entry_zone:
                zone_top = entry_zone.get('high') or entry_zone.get('top')
                zone_bottom = entry_zone.get('low') or entry_zone.get('bottom')
                
                if zone_top and zone_bottom:
                    zone_top = Decimal(str(zone_top))
                    zone_bottom = Decimal(str(zone_bottom))
                    
                    # Check if price is in or near entry zone (with tolerance)
                    in_zone, used_tolerance = is_in_or_near_zone(current_price, zone_bottom, zone_top)
                    
                    if in_zone:
                        # Check if we've had a pullback (price went down then came back)
                        recent_low = min(c.low for c in candles_15m[-10:])
                        if recent_low < break_price:  # Price moved down after break
                            structure_change.reconfirmed = True
                            structure_change.reconfirmed_at = candles_15m[-1].timestamp
                            structure_change.entry_ready = True
                            structure_change._used_tolerance = used_tolerance
                            
                            entry_type = "TOLERANCE" if used_tolerance else "EXACT"
                            logger.info(
                                f"Market structure reconfirmed ({entry_type}) - entry ready",
                                symbol=symbol,
                                entry_zone=f"${zone_bottom}-${zone_top}",
                                current_price=str(current_price),
                                tolerance=f"{float(tolerance)*100:.2f}%",
                                used_tolerance=used_tolerance
                            )
                            return True, used_tolerance
            else:
                # FALLBACK: No OB/FVG zone defined - use tolerance zone around break price
                # For bearish: reconfirm if price retraces near break price after moving down
                recent_low = min(c.low for c in candles_15m[-10:])
                if recent_low < break_price:  # Confirmed move down
                    # Check if price has retraced toward break price (within tolerance)
                    tolerance_zone_top = break_price * (Decimal("1") + tolerance)
                    tolerance_zone_bottom = break_price * (Decimal("1") - tolerance)
                    
                    if tolerance_zone_bottom <= current_price <= tolerance_zone_top:
                        structure_change.reconfirmed = True
                        structure_change.reconfirmed_at = candles_15m[-1].timestamp
                        structure_change.entry_ready = True
                        structure_change._used_tolerance = True
                        
                        logger.info(
                            f"Market structure reconfirmed (BREAK_PRICE_TOLERANCE) - entry ready",
                            symbol=symbol,
                            break_price=str(break_price),
                            current_price=str(current_price),
                            tolerance=f"{float(tolerance)*100:.2f}%",
                            used_tolerance=True
                        )
                        return True, True
        
        return False, False
    
    def is_entry_ready(self, symbol: str) -> bool:
        """Check if entry is ready for symbol (structure change reconfirmed)."""
        change = self.structure_changes.get(symbol)
        return change is not None and change.entry_ready
    
    def get_entry_signal(self, symbol: str) -> Optional[Tuple[str, Decimal]]:
        """
        Get entry signal if ready.
        
        Returns:
            (signal_type, entry_price) or None
        """
        change = self.structure_changes.get(symbol)
        if not change or not change.entry_ready:
            return None
        
        if change.new_state == MarketStructureState.BULLISH:
            return ("LONG", change.break_price)
        elif change.new_state == MarketStructureState.BEARISH:
            return ("SHORT", change.break_price)
        
        return None
    
    def _determine_structure(
        self,
        recent_high: Optional[Decimal],
        recent_low: Optional[Decimal],
        prev_high: Optional[Decimal],
        prev_low: Optional[Decimal]
    ) -> MarketStructureState:
        """Determine market structure from swing points."""
        if not recent_high or not recent_low or not prev_high or not prev_low:
            return MarketStructureState.NEUTRAL
        
        # Bullish: Higher highs and higher lows
        if recent_high > prev_high and recent_low > prev_low:
            return MarketStructureState.BULLISH
        
        # Bearish: Lower highs and lower lows
        if recent_high < prev_high and recent_low < prev_low:
            return MarketStructureState.BEARISH
        
        # Neutral: Mixed or unclear
        return MarketStructureState.NEUTRAL
