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
        
        Args:
            symbol: Spot symbol (e.g., "BTC/USD")
            bias_candles_4h: 4H spot candles for bias
            bias_candles_1d: 1D spot candles for bias
            exec_candles_15m: 15m spot candles for execution
            exec_candles_1h: 1H spot candles for execution
        
        Returns:
            Signal object with full reasoning
        """
        reasoning_parts = []
        
        # Step 1: Higher-timeframe bias (4H/1D)
        bias = self._determine_bias(bias_candles_4h, bias_candles_1d, reasoning_parts)
        
        if bias == "neutral":
            return self._no_signal(symbol, reasoning_parts, exec_candles_1h[-1] if exec_candles_1h else None)
        
        # Step 2: Execution timeframe structure (15m/1H)
        structure_signal = self._detect_structure(
            exec_candles_15m,
            exec_candles_1h,
            bias,
            reasoning_parts,
        )
        
        if structure_signal is None:
            return self._no_signal(symbol, reasoning_parts, exec_candles_1h[-1] if exec_candles_1h else None)
        
        # Step 3: Filters (ADX, ATR)
        if not self._apply_filters(exec_candles_1h, reasoning_parts):
            return self._no_signal(symbol, reasoning_parts, exec_candles_1h[-1] if exec_candles_1h else None)
        
        # Step 4: Calculate entry, stop-loss, take-profit
        signal_type, entry_price, stop_loss, take_profit = self._calculate_levels(
            structure_signal,
            exec_candles_1h,
            bias,
            reasoning_parts,
        )
        
        # Step 5: Optional RSI divergence confirmation
        if self.config.rsi_divergence_enabled:
            self._check_rsi_divergence(exec_candles_1h, reasoning_parts)
        
        # Get current candle for metadata
        current_candle = exec_candles_1h[-1]
        
        # Calculate indicators for metadata
        ema_values = self.indicators.calculate_ema(bias_candles_1d, self.config.ema_period)
        adx_df = self.indicators.calculate_adx(exec_candles_1h, self.config.adx_period)
        atr_values = self.indicators.calculate_atr(exec_candles_1h, self.config.atr_period)
        
        signal = Signal(
            timestamp=current_candle.timestamp,
            symbol=symbol,
            signal_type=signal_type,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            reasoning="\n".join(reasoning_parts),
            higher_tf_bias=bias,
            adx=Decimal(str(adx_df['ADX_14'].iloc[-1])) if not adx_df.empty else Decimal("0"),
            atr=Decimal(str(atr_values.iloc[-1])) if not atr_values.empty else Decimal("0"),
            ema200_slope=self.indicators.get_ema_slope(ema_values) if not ema_values.empty else "flat",
        )
        
        logger.info(
            "Signal generated",
            symbol=symbol,
            signal_type=signal_type.value,
            entry=str(entry_price),
            stop=str(stop_loss),
            tp=str(take_profit) if take_profit else "None",
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
        """Find order block (last bullish/bearish candle before move)."""
        lookback = min(self.config.orderblock_lookback, len(candles) - 1)
        
        for i in range(len(candles) - 1, len(candles) - lookback - 1, -1):
            candle = candles[i]
            
            if bias == "bullish":
                # Look for bullish order block (green candle)
                if candle.close > candle.open:
                    return {
                        'type': 'bullish',
                        'price': candle.low,  # Entry at low of order block
                        'index': i,
                    }
            else:  # bearish
                # Look for bearish order block (red candle)
                if candle.close < candle.open:
                    return {
                        'type': 'bearish',
                        'price': candle.high,  # Entry at high of order block
                        'index': i,
                    }
        
        return None
    
    def _find_fair_value_gap(self, candles: List[Candle], bias: str) -> Optional[dict]:
        """Find fair value gap (imbalance in price action)."""
        for i in range(len(candles) - 3, 0, -1):
            c1 = candles[i]
            c2 = candles[i + 1]
            c3 = candles[i + 2]
            
            if bias == "bullish":
                # Bullish FVG: gap between c1.high and c3.low
                gap = c3.low - c1.high
                if gap / c3.low > self.config.fvg_min_size_pct:
                    return {
                        'price': (c1.high + c3.low) / 2,  # Midpoint
                        'size_pct': float(gap / c3.low),
                    }
            else:  # bearish
                # Bearish FVG: gap between c3.high and c1.low
                gap = c1.low - c3.high
                if gap / c1.low > self.config.fvg_min_size_pct:
                    return {
                        'price': (c3.high + c1.low) / 2,  # Midpoint
                        'size_pct': float(gap / c1.low),
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
    ) -> Tuple[SignalType, Decimal, Decimal, Optional[Decimal]]:
        """Calculate entry, stop-loss, and take-profit levels."""
        current_price = candles[-1].close
        order_block = structure['order_block']
        
        # Entry at order block price
        entry_price = order_block['price']
        
        # Calculate ATR for stop sizing
        atr_values = self.indicators.calculate_atr(candles, self.config.atr_period)
        atr = Decimal(str(atr_values.iloc[-1]))
        
        # Stop-loss: ATR-based buffer from invalidation level
        if bias == "bullish":
            signal_type = SignalType.LONG
            # Stop below order block with ATR buffer
            stop_loss = entry_price - (atr * Decimal(str(self.config.atr_multiplier_stop)))
            # Take-profit at next resistance (simplified: 2× risk)
            take_profit = entry_price + (entry_price - stop_loss) * 2
        else:  # bearish
            signal_type = SignalType.SHORT
            # Stop above order block with ATR buffer
            stop_loss = entry_price + (atr * Decimal(str(self.config.atr_multiplier_stop)))
            # Take-profit at next support (simplified: 2× risk)
            take_profit = entry_price - (stop_loss - entry_price) * 2
        
        reasoning.append(
            f"✓ Levels: Entry ${entry_price}, Stop ${stop_loss}, TP ${take_profit}, ATR ${atr}"
        )
        
        return signal_type, entry_price, stop_loss, take_profit
    
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
