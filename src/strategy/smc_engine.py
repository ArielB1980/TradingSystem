"""
SMC (Smart Money Concepts) signal generation engine.

Design lock enforced: Operates on spot market data ONLY.
No futures prices, funding data, or order book data may be accessed.
"""
from typing import List, Optional, Tuple
from decimal import Decimal
from datetime import datetime
import pandas as pd
from src.domain.models import Candle, Signal, SignalType
from src.strategy.indicators import Indicators
from src.config.config import StrategyConfig
from src.monitoring.logger import get_logger
from src.storage.repository import record_event
import uuid

logger = get_logger(__name__)


class SMCEngine:
    """
    SMC signal generation engine using spot market data.
    
    CRITICAL DESIGN LOCKS:
    - Analyzes SPOT data only (BTC/USD, ETH/USD)
    - No futures prices, funding, or order book access
    - Deterministic: same input → same signal
    - All parameters configurable (no hardcoded values)
    """
    
    def __init__(self, config: StrategyConfig):
        """
        Initialize SMC engine.
        
        Args:
            config: Strategy configuration
        """
        self.config = config
        self.indicators = Indicators()
        
        logger.info("SMC Engine initialized", config=config.model_dump())
    
    def generate_signal(
        self,
        symbol: str,
        bias_candles_4h: List[Candle],
        bias_candles_1d: List[Candle],
        exec_candles_15m: List[Candle],
        exec_candles_1h: List[Candle],
    ) -> Signal:
        """
        Generate trading signal from spot market data.
        """
        # Context Variables for Trace
        decision_id = str(uuid.uuid4())
        reasoning_parts = []
        bias = "neutral"
        structure_signal = None
        adx_value = 0.0
        atr_value = 0.0
        tp_candidates = []
        
        # Logic Flow
        signal = None
        
        # Step 1: Higher-timeframe bias
        if signal is None:
            bias = self._determine_bias(bias_candles_4h, bias_candles_1d, reasoning_parts)
            if bias == "neutral":
                signal = self._no_signal(symbol, reasoning_parts, exec_candles_1h[-1] if exec_candles_1h else None)

        # Step 2: Execution timeframe structure
        if signal is None:
            structure_signal = self._detect_structure(
                exec_candles_15m,
                exec_candles_1h,
                bias,
                reasoning_parts,
            )
            if structure_signal is None:
                 signal = self._no_signal(symbol, reasoning_parts, exec_candles_1h[-1] if exec_candles_1h else None)

        # Step 3: Filters
        if signal is None:
            # ADX
            adx_df = self.indicators.calculate_adx(exec_candles_1h, self.config.adx_period)
            if not adx_df.empty:
                adx_value = float(adx_df['ADX_14'].iloc[-1])
            
            # ATR
            atr_df = self.indicators.calculate_atr(exec_candles_1h, self.config.atr_period)
            if not atr_df.empty:
                atr_value = Decimal(str(atr_df.iloc[-1])) # Convert to Decimal

            if not self._apply_filters(exec_candles_1h, reasoning_parts):
                 signal = self._no_signal(symbol, reasoning_parts, exec_candles_1h[-1] if exec_candles_1h else None)

            # Step 4: Calculate Levels (If passed all checks)
            if signal is None:
                signal_type, entry_price, stop_loss, take_profit, tp_candidates = self._calculate_levels(
                    structure_signal,
                    exec_candles_1h,
                    bias,
                    reasoning_parts,
                )
                
                # Step 5: RSI Divergence
                if self.config.rsi_divergence_enabled:
                    self._check_rsi_divergence(exec_candles_1h, reasoning_parts)
                
                # Metadata
                current_candle = exec_candles_1h[-1]
                ema_values = self.indicators.calculate_ema(bias_candles_1d, self.config.ema_period)
                
                timestamp = current_candle.timestamp
                ema200_slope = self.indicators.get_ema_slope(ema_values) if not ema_values.empty else "flat"

                if signal_type != SignalType.NO_SIGNAL:
                    signal = Signal(
                        timestamp=timestamp,
                        symbol=symbol,
                        signal_type=signal_type,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        reasoning="\n".join(reasoning_parts),
                        higher_tf_bias=bias,
                        adx=adx_value,
                        atr=atr_value,
                        ema200_slope=ema200_slope,
                        tp_candidates=tp_candidates
                    )
                else:
                    signal = Signal(
                        timestamp=timestamp,
                        symbol=symbol,
                        signal_type=SignalType.NO_SIGNAL,
                        entry_price=Decimal("0"),
                        stop_loss=Decimal("0"),
                        take_profit=None,
                        reasoning="\n".join(reasoning_parts),
                        higher_tf_bias=bias,
                        adx=adx_value,
                        atr=atr_value,
                        ema200_slope=ema200_slope,
                        tp_candidates=[]
                    )
                    
                    
        # If signal is still None after all steps (e.g., if _calculate_levels returned None for signal_type)
        if signal is None:
            current_candle = exec_candles_1h[-1] if exec_candles_1h else None
            timestamp = current_candle.timestamp if current_candle else datetime.now(timezone.utc)
            signal = self._no_signal(symbol, reasoning_parts, current_candle)


        # --- EXPLAINABILITY INSTRUMENTATION ---
        trace_data = {
            "bias": bias,
            "structure": structure_signal,
            "filters": {
                "adx": float(adx_value),
                "atr": float(atr_value),
            },
            "reasoning": reasoning_parts,
            "signal_type": signal.signal_type.value,
            "tp_candidates": [float(tp) for tp in tp_candidates]
        }
        
        # Record generic decision trace
        record_event("DECISION_TRACE", symbol, trace_data, decision_id=decision_id)
        
        if signal.signal_type != SignalType.NO_SIGNAL:
            logger.info(
                "Signal generated",
                symbol=symbol,
                signal_type=signal.signal_type.value,
                entry=str(signal.entry_price),
                stop=str(signal.stop_loss),
            )
            # Record explicit signal event
            record_event(
                "SIGNAL_GENERATED", 
                symbol, 
                {
                    "type": signal.signal_type.value,
                    "entry": float(signal.entry_price),
                    "stop": float(signal.stop_loss),
                    "tp": float(signal.take_profit) if signal.take_profit else None,
                    "tp_candidates": [float(tp) for tp in signal.tp_candidates]
                },
                decision_id=decision_id
            )
        
        return signal
    
    def _determine_bias(
        self,
        candles_4h: List[Candle],
        candles_1d: List[Candle],
        reasoning: List[str],
    ) -> str:
        """Determine higher-timeframe bias (bullish/bearish/neutral)."""
        if not candles_4h or not candles_1d:
            reasoning.append("❌ Insufficient candles for bias determination")
            return "neutral"
        
        # EMA 200 on 1D
        ema_1d = self.indicators.calculate_ema(candles_1d, self.config.ema_period)
        
        if ema_1d.empty or len(ema_1d) < 1:
            reasoning.append("❌ EMA 200 not available on 1D")
            return "neutral"
        
        current_price = candles_1d[-1].close
        ema_value = Decimal(str(ema_1d.iloc[-1]))
        slope = self.indicators.get_ema_slope(ema_1d)
        
        # Simple bias: price above/below EMA 200 with slope confirmation
        if current_price > ema_value and slope == "up":
            reasoning.append(f"✓ Bullish bias: Price ${current_price} above EMA200 ${ema_value}, slope {slope}")
            return "bullish"
        elif current_price < ema_value and slope == "down":
            reasoning.append(f"✓ Bearish bias: Price ${current_price} below EMA200 ${ema_value}, slope {slope}")
            return "bearish"
        else:
            reasoning.append(f"○ Neutral bias: Price ${current_price}, EMA200 ${ema_value}, slope {slope}")
            return "neutral"
    
    def _detect_structure(
        self,
        candles_15m: List[Candle],
        candles_1h: List[Candle],
        bias: str,
        reasoning: List[str],
    ) -> Optional[dict]:
        """
        Detect SMC structure (order blocks, FVGs, break of structure).
        
        Returns:
            Dict with structure details or None if no valid structure
        """
        if not candles_1h or len(candles_1h) < self.config.orderblock_lookback:
            reasoning.append("❌ Insufficient candles for structure detection")
            return None
        
        # Detect order blocks
        order_block = self._find_order_block(candles_1h, bias)
        
        if not order_block:
            reasoning.append("❌ No valid order block found")
            return None
        
        reasoning.append(
            f"✓ Order block detected: {order_block['type']} at ${order_block['price']}"
        )
        
        # Detect fair value gaps
        fvg = self._find_fair_value_gap(candles_1h, bias)
        
        if fvg:
            reasoning.append(f"✓ Fair value gap detected at ${fvg['price']}")
        
        # Detect break of structure
        bos = self._detect_break_of_structure(candles_1h, bias)
        
        if bos:
            reasoning.append(f"✓ Break of structure confirmed")
        else:
            reasoning.append("○ No break of structure yet")
        
        return {
            'order_block': order_block,
            'fvg': fvg,
            'bos': bos,
        }
    
    def _find_order_block(self, candles: List[Candle], bias: str) -> Optional[dict]:
        """
        Find SMC-style Order Block:
        - Bullish OB: last DOWN candle before an impulsive UP move (displacement)
        - Bearish OB: last UP candle before an impulsive DOWN move (displacement)
        
        Returns a zone: {'type', 'index', 'low', 'high', 'timestamp', 'price'}
        """
        if len(candles) < 3:
            return None
            
        lookback = min(self.config.orderblock_lookback, len(candles) - 3)
        
        # Calculate volatility-adjusted displacement threshold
        recent_ranges = [abs(c.high - c.low) for c in candles[-20:]]
        typical_range = sorted(recent_ranges)[len(recent_ranges)//2]
        min_displacement = typical_range * Decimal("1.5")
        
        # Iterate backwards to find the most recent valid OB
        for i in range(len(candles) - 2, len(candles) - lookback - 2, -1):
            cand = candles[i]
            nxt = candles[i + 1]
            
            if bias == "bullish":
                # 1. Origin must be a bearish candle
                if cand.close < cand.open:
                    # 2. Must be followed by an impulsive move up (displacement)
                    # The displacement must break the high of the OB candle
                    move = nxt.close - cand.high
                    if move > 0 and (nxt.high - nxt.low) >= min_displacement:
                        return {
                            "type": "bullish",
                            "index": i,
                            "timestamp": cand.timestamp,
                            "low": cand.low,
                            "high": cand.high,
                            "price": cand.high # Entry at OB high
                        }
            else: # bearish
                # 1. Origin must be a bullish candle
                if cand.close > cand.open:
                    # 2. Must be followed by an impulsive move down
                    move = cand.low - nxt.close
                    if move > 0 and (nxt.high - nxt.low) >= min_displacement:
                        return {
                            "type": "bearish",
                            "index": i,
                            "timestamp": cand.timestamp,
                            "low": cand.low,
                            "high": cand.high,
                            "price": cand.low # Entry at OB low
                        }
        return None
    
    def _find_fair_value_gap(self, candles: List[Candle], bias: str) -> Optional[dict]:
        """
        Find most recent UNMITIGATED FVG.
        """
        if len(candles) < 3:
            return None

        # Iterate backwards from current candle
        for i in range(len(candles) - 3, -1, -1):
            c1, c2, c3 = candles[i], candles[i+1], candles[i+2]
            
            # Check for gap formation
            gap_zone = None
            if bias == "bullish" and c3.low > c1.high:
                gap_zone = (c1.high, c3.low)
            elif bias == "bearish" and c1.low > c3.high:
                gap_zone = (c3.high, c1.low)
                
            if gap_zone:
                # Check for mitigation by any candle AFTER the gap formation (from i+3 to end)
                # If any candle's wick enters the gap, it is mitigated.
                mitigated = False
                for j in range(i + 3, len(candles)):
                    fc = candles[j]
                    if bias == "bullish":
                        if fc.low <= gap_zone[1]: # Price returned to or below gap top
                            mitigated = True
                            break
                    else:
                        if fc.high >= gap_zone[0]: # Price returned to or above gap bottom
                            mitigated = True
                            break
                
                if not mitigated:
                    return {
                        "type": "bullish" if bias == "bullish" else "bearish",
                        "index": i,
                        "timestamp": c2.timestamp,
                        "bottom": gap_zone[0],
                        "top": gap_zone[1],
                        "size": gap_zone[1] - gap_zone[0],
                        "price": gap_zone[1] if bias == "bullish" else gap_zone[0]
                    }
        return None
    
    def _detect_break_of_structure(self, candles: List[Candle], bias: str) -> bool:
        """Detect break of structure (BOS)."""
        if len(candles) < self.config.bos_confirmation_candles + 5:
            return False
        
        # Simple BOS: recent candles break previous swing high/low
        recent = candles[-self.config.bos_confirmation_candles:]
        previous = candles[-10:-self.config.bos_confirmation_candles]
        
        if bias == "bullish":
            prev_high = max(c.high for c in previous)
            recent_high = max(c.high for c in recent)
            return recent_high > prev_high
        else:  # bearish
            prev_low = min(c.low for c in previous)
            recent_low = min(c.low for c in recent)
            return recent_low < prev_low
    
    def _apply_filters(self, candles: List[Candle], reasoning: List[str]) -> bool:
        """Apply ADX and ATR filters."""
        # ADX filter
        adx_df = self.indicators.calculate_adx(candles, self.config.adx_period)
        
        if adx_df.empty:
            reasoning.append("❌ ADX not available")
            return False
        
        adx_value = adx_df['ADX_14'].iloc[-1]
        
        if adx_value < self.config.adx_threshold:
            reasoning.append(f"❌ ADX too low: {adx_value:.1f} < {self.config.adx_threshold}")
            return False
        
        reasoning.append(f"✓ ADX filter passed: {adx_value:.1f} > {self.config.adx_threshold}")
        
        # ATR check (ensure volatility is measurable)
        atr_values = self.indicators.calculate_atr(candles, self.config.atr_period)
        
        if atr_values.empty:
            reasoning.append("❌ ATR not available")
            return False
        
        atr_value = atr_values.iloc[-1]
        reasoning.append(f"✓ ATR available: {atr_value:.2f}")
        
        return True
    
    def _calculate_levels(
        self,
        structure: dict,
        candles: List[Candle],
        bias: str,
        reasoning: List[str],
    ) -> Tuple[SignalType, Decimal, Decimal, Optional[Decimal], List[Decimal]]:
        """Calculate entry, stop-loss, and take-profit levels with candidates."""
        order_block = structure['order_block']
        
        # Calculate ATR for stop buffering
        atr_values = self.indicators.calculate_atr(candles, self.config.atr_period)
        atr = Decimal(str(atr_values.iloc[-1])) if not atr_values.empty else Decimal("0")
        
        tp_candidates = []
        
        if bias == "bullish":
            signal_type = SignalType.LONG
            # Entry: Top of Bullish OB (retest entry)
            entry_price = order_block['high']
            
            # Stop-loss: Below Bottom of OB + buffer
            invalidation_level = order_block['low']
            stop_loss = invalidation_level - (atr * Decimal(str(self.config.atr_multiplier_stop)))
            
            # Take-profit Candidates
            # 1. Recent Swing Highs (Liquidity)
            # Scan last 50 candles for local maxima > entry
            lookback = 50
            for i in range(len(candles) - 2, max(0, len(candles) - lookback), -1):
                c = candles[i]
                # Simple swing high check: High > surrounding highs
                if (c.high > candles[i-1].high and c.high > candles[i+1].high):
                    if c.high > entry_price:
                         tp_candidates.append(c.high)
            
            # Sort nearest to farthest
            tp_candidates = sorted(list(set(tp_candidates)))[:5]
            
            # Default TP (2R) if no structure found
            risk = entry_price - stop_loss
            take_profit = entry_price + (risk * 2)
            if not tp_candidates:
                tp_candidates.append(take_profit)
            
        else:  # bearish
            signal_type = SignalType.SHORT
            # Entry: Bottom of Bearish OB (retest entry)
            entry_price = order_block['low']
            
            # Stop-loss: Above Top of OB + buffer
            invalidation_level = order_block['high']
            stop_loss = invalidation_level + (atr * Decimal(str(self.config.atr_multiplier_stop)))
            
            # Take-profit Candidates
            # 1. Recent Swing Lows (Liquidity)
            lookback = 50
            for i in range(len(candles) - 2, max(0, len(candles) - lookback), -1):
                c = candles[i]
                if (c.low < candles[i-1].low and c.low < candles[i+1].low):
                    if c.low < entry_price:
                        tp_candidates.append(c.low)
                        
            # Sort nearest to farthest (descending for shorts)
            tp_candidates = sorted(list(set(tp_candidates)), reverse=True)[:5]

            # Default TP (2R)
            risk = stop_loss - entry_price
            take_profit = entry_price - (risk * 2)
            if not tp_candidates:
                tp_candidates.append(take_profit)
        
        reasoning.append(
            f"✓ Levels: Entry ${entry_price}, Stop ${stop_loss}, TP ${take_profit}, ATR ${atr}"
        )
        if tp_candidates:
             reasoning.append(f"✓ Found {len(tp_candidates)} TP candidates from structure")
        
        return signal_type, entry_price, stop_loss, take_profit, tp_candidates
    
    def _check_rsi_divergence(self, candles: List[Candle], reasoning: List[str]):
        """Optional RSI divergence confirmation."""
        rsi_values = self.indicators.calculate_rsi(candles, self.config.rsi_period)
        
        if rsi_values.empty:
            reasoning.append("○ RSI divergence: not available")
            return
        
        divergence = self.indicators.detect_rsi_divergence(candles, rsi_values)
        
        if divergence != "none":
            reasoning.append(f"✓ RSI divergence detected: {divergence}")
        else:
            reasoning.append("○ RSI divergence: none")
    
    def _no_signal(
        self,
        symbol: str,
        reasoning: List[str],
        current_candle: Optional[Candle],
    ) -> Signal:
        """Create a NO_SIGNAL signal."""
        timestamp = current_candle.timestamp if current_candle else datetime.now()
        
        return Signal(
            timestamp=timestamp,
            symbol=symbol,
            signal_type=SignalType.NO_SIGNAL,
            entry_price=Decimal("0"),
            stop_loss=Decimal("0"),
            take_profit=None,
            reasoning="\n".join(reasoning),
            higher_tf_bias="neutral",
            adx=Decimal("0"),
            atr=Decimal("0"),
            ema200_slope="flat",
        )
