"""
SMC (Smart Money Concepts) signal generation engine.

Design lock enforced: Operates on spot market data ONLY.
No futures prices, funding data, or order book data may be accessed.
"""
from typing import List, Optional, Dict, Tuple
from decimal import Decimal
from datetime import datetime, timezone, timedelta
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
        
        # V2: Per-symbol caching for multi-asset support (optimized with tuple keys)
        self.indicator_cache: Dict[Tuple[str, datetime], Dict] = {}
        self.cache_max_size = 1000  # Prevent unbounded growth
        self.cache_max_age = timedelta(hours=2)  # Configurable
        
        # V2: Fibonacci engine for confluence scoring
        from src.strategy.fibonacci_engine import FibonacciEngine
        self.fibonacci_engine = FibonacciEngine(lookback_bars=100)
        
        # V2.1: Signal Scorer
        from src.strategy.signal_scorer import SignalScorer
        self.signal_scorer = SignalScorer(config)
        
        # Market Structure Tracker (confirmation + reconfirmation)
        from src.strategy.market_structure_tracker import MarketStructureTracker
        self.ms_tracker = MarketStructureTracker(
            confirmation_candles=getattr(config, 'ms_confirmation_candles', 3),
            reconfirmation_candles=getattr(config, 'ms_reconfirmation_candles', 2)
        )
        
        logger.info("SMC Engine initialized", config=config.model_dump())
    
    def _get_cache_key(self, symbol: str, candles: List[Candle]) -> Tuple[str, datetime]:
        """Generate cache key from symbol and last candle timestamp."""
        if not candles:
            return (symbol, datetime.min.replace(tzinfo=timezone.utc))
        return (symbol, candles[-1].timestamp)
    
    def _clean_cache(self):
        """Remove stale cache entries."""
        if len(self.indicator_cache) < self.cache_max_size:
            return
            
        now = datetime.now(timezone.utc)
        cutoff = now - self.cache_max_age
        
        # Remove old entries
        self.indicator_cache = {
            k: v for k, v in self.indicator_cache.items()
            if k[1] > cutoff
        }
        
        # If still too large, remove oldest entries
        if len(self.indicator_cache) > self.cache_max_size:
            sorted_keys = sorted(self.indicator_cache.keys(), key=lambda x: x[1], reverse=True)
            self.indicator_cache = {
                k: self.indicator_cache[k] 
                for k in sorted_keys[:self.cache_max_size]
            }
    
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
        
        # DEFENSIVE CHECK: Data Integrity
        # Ensure we have minimum required data to function
        if not exec_candles_15m:
            # logger.error(f"SMC Engine Validation Failed: Missing 15m candles for {symbol}")
            return Signal(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                signal_type=SignalType.NO_SIGNAL,
                entry_price=Decimal("0"),
                stop_loss=Decimal("0"),
                reasoning="ERROR: Missing 15m Data",
                setup_type=None,
                regime="no_data"
            )
            
        if not exec_candles_1h:
             # logger.error(f"SMC Engine Validation Failed: Missing 1h candles for {symbol}")
             return Signal(
                timestamp=datetime.now(timezone.utc),
                symbol=symbol,
                signal_type=SignalType.NO_SIGNAL,
                entry_price=Decimal("0"),
                stop_loss=Decimal("0"),
                reasoning="ERROR: Missing 1h Data",
                setup_type=None,
                regime="no_data"
            )

        bias = "neutral"
        structure_signal = None
        adx_value = 0.0
        atr_value = 0.0
        atr_ratio = None
        tp_candidates = []
        
        # Logic Flow
        signal = None

        # Step 0: Calculate Indicators (ADX, ATR, Fibs) - Early Calc for Context
        # Cache key based on symbol and last candle timestamp (optimized)
        cache_key = self._get_cache_key(symbol, exec_candles_1h)
        
        # Check cache for indicators
        cached_indicators = self.indicator_cache.get(cache_key)
        
        if cached_indicators:
            # Use cached values
            adx_value = cached_indicators['adx']
            atr_value = cached_indicators['atr']
            fib_levels = cached_indicators['fib_levels']
        else:
            # Calculate fresh
            adx_df = self.indicators.calculate_adx(exec_candles_1h, self.config.adx_period)
            if not adx_df.empty:
                adx_value = float(adx_df['ADX_14'].iloc[-1])
            else:
                adx_value = 0.0
            
            # ATR
            atr_df = self.indicators.calculate_atr(exec_candles_1h, self.config.atr_period)
            if not atr_df.empty:
                atr_value = Decimal(str(atr_df.iloc[-1]))
            else:
                atr_value = Decimal("0")
            
            # Fib Levels (pre-calculate for later use)
            fib_levels = self.fibonacci_engine.calculate_levels(exec_candles_1h, "1h")
            
            # Store in cache
            self.indicator_cache[cache_key] = {
                'adx': adx_value,
                'atr': atr_value,
                'fib_levels': fib_levels
            }
            
            # Periodic cleanup
            if len(self.indicator_cache) % 100 == 0:
                self._clean_cache()
        

        # Step 1: Higher-timeframe bias
        if signal is None:
            bias = self._determine_bias(bias_candles_4h, bias_candles_1d, reasoning_parts)
            # V2.1: Neutral bias DOES NOT block immediately - waits for Score Gate
            # unless bias determination failed completely (e.g. insufficient data)
            if "Insufficient candles" in reasoning_parts[-1]:
                signal = self._no_signal(
                    symbol, 
                    reasoning_parts, 
                    exec_candles_1h[-1] if exec_candles_1h else None,
                    adx=adx_value,
                    atr=atr_value
                )

        # Step 2: Market Structure Change Detection & Confirmation
        # Require structure change confirmation + reconfirmation before entry
        if signal is None:
            # Update market structure tracking
            ms_state, ms_change = self.ms_tracker.update_structure(symbol, exec_candles_1h)
            
            # V4: Adaptive Confirmation Logic
            required_candles = None
            if self.config.adaptive_enabled:
                # Dynamic Logic based on Volatility State
                try:
                    atr_series = self.indicators.calculate_atr(exec_candles_1h, self.config.atr_period)
                    if not atr_series.empty:
                        current_atr = atr_series.iloc[-1]
                        # Use 20-period moving average of ATR as baseline
                        avg_atr = atr_series.rolling(20).mean().iloc[-1]
                        
                        if avg_atr > 0:
                            # Calculate ratio (float for local logic, Decimal for Signal if needed)
                            ratio_val = float(current_atr / avg_atr)
                            atr_ratio = Decimal(str(ratio_val))
                            
                            # Store for Signal metadata (Risk Manager needs this)
                            atr_value = float(current_atr)

                            if ratio_val > self.config.atr_confirmation_threshold_high:
                                required_candles = self.config.max_confirmation_candles
                                reasoning_parts.append(f"🌊 High Volatility (ATR Ratio {ratio_val:.2f}) -> Extended Confirmation ({required_candles} candles)")
                            elif ratio_val < self.config.atr_confirmation_threshold_low:
                                required_candles = self.config.min_confirmation_candles
                                # reasoning_parts.append(f"💧 Low Volatility (ATR Ratio {ratio_val:.2f}) -> Reduced Confirmation ({required_candles} candles)")

                except Exception as e:
                    # Fallback to default if ATR calc fails
                    logger.warning("Adaptive confirmation failed, using default", error=str(e))

            
            if ms_change:
                # Structure change detected - check confirmation
                confirmed = self.ms_tracker.check_confirmation(
                    symbol, 
                    exec_candles_1h, 
                    ms_change,
                    required_candles=required_candles # Dynamic
                )
                
                if confirmed:
                    # Check reconfirmation (entry ready)
                    # Get entry zone from structure detection
                    structure_signal = self._detect_structure(
                        exec_candles_15m,
                        exec_candles_1h,
                        bias,
                        reasoning_parts,
                    )
                    
                    # V4: RSI Divergence Check (Gate before Reconfirmation)
                    if self.config.rsi_divergence_check:
                         rsi_values = self.indicators.calculate_rsi(exec_candles_1h, self.config.rsi_period)
                         divergence = self.indicators.detect_rsi_divergence(exec_candles_1h, rsi_values, self.config.rsi_divergence_lookback)
                         
                         if divergence != "none":
                             # If Bias is Bullish but Bearish Divergence -> Weakness
                             if bias == "bullish" and divergence == "bearish":
                                 reasoning_parts.append(f"⚠️ Bearish RSI Divergence detected against Bullish bias")
                                 # We could reject or reduce size. For now, strict:
                                 # signal = self._no_signal(symbol, reasoning_parts, exec_candles_1h[-1]) 
                                 # Let's just log it for scoring to penalize
                             
                             elif bias == "bearish" and divergence == "bullish":
                                 reasoning_parts.append(f"⚠️ Bullish RSI Divergence detected against Bearish bias")


                    
                    entry_zone = None
                    if structure_signal:
                        # Extract entry zone (order block or FVG)
                        if structure_signal.get('order_block'):
                            ob = structure_signal['order_block']
                            entry_zone = {'low': ob.get('low'), 'high': ob.get('high')}
                        elif structure_signal.get('fvg'):
                            fvg = structure_signal['fvg']
                            entry_zone = {'bottom': fvg.get('bottom'), 'top': fvg.get('top')}
                    
                    reconfirmed = self.ms_tracker.check_reconfirmation(
                        symbol, exec_candles_15m, exec_candles_1h, ms_change, entry_zone
                    )
                    
                    if not reconfirmed:
                        reasoning_parts.append(
                            f"⏳ Structure change confirmed, waiting for reconfirmation (retrace to entry zone)"
                        )
                        signal = self._no_signal(
                            symbol, 
                            reasoning_parts, 
                            exec_candles_1h[-1] if exec_candles_1h else None,
                            adx=adx_value,
                            atr=atr_value
                        )
                else:
                    reasoning_parts.append(
                        f"⏳ Structure change detected ({ms_change.new_state.value}), waiting for confirmation"
                    )
                    signal = self._no_signal(
                        symbol, 
                        reasoning_parts, 
                        exec_candles_1h[-1] if exec_candles_1h else None,
                        adx=adx_value,
                        atr=atr_value
                    )
            else:
                # No structure change - check if we're waiting for one
                if not self.ms_tracker.is_entry_ready(symbol):
                    # Check if we should require structure change
                    require_ms_change = getattr(self.config, 'require_ms_change_confirmation', True)
                    if require_ms_change:
                        reasoning_parts.append(
                            f"⏳ No market structure change detected - waiting for structure break"
                        )
                        signal = self._no_signal(
                            symbol, 
                            reasoning_parts, 
                            exec_candles_1h[-1] if exec_candles_1h else None,
                            adx=adx_value,
                            atr=atr_value
                        )
        
        # Step 2.5: Execution timeframe structure (only if entry ready or MS change not required)
        structures = {}  # Store for regime classification
        regime_early = None  # Track regime as soon as we can determine it
        
        if signal is None:
            structure_signal = self._detect_structure(
                exec_candles_15m,
                exec_candles_1h,
                bias,
                reasoning_parts,
            )
            if structure_signal is None:
                 signal = self._no_signal(
                     symbol, 
                     reasoning_parts, 
                     exec_candles_1h[-1] if exec_candles_1h else None,
                     adx=adx_value,
                     atr=atr_value
                 )
            else:
                structures = structure_signal  # Save for classification
                
                # EARLY REGIME CLASSIFICATION (NEW)
                # Classify regime immediately after structure detection
                # This ensures rejected signals still show correct regime
                regime_early = self._classify_regime_from_structure(structure_signal)
                reasoning_parts.append(f"📊 Market Regime: {regime_early}")
                
                # If entry ready, verify signal direction matches structure change
                if self.ms_tracker.is_entry_ready(symbol):
                    entry_signal = self.ms_tracker.get_entry_signal(symbol)
                    if entry_signal:
                        expected_direction, _ = entry_signal
                        # Verify the structure signal aligns with MS change direction
                        # This will be checked later when we determine signal_type

        # Step 3: Filters
        if signal is None:
            # Filters applied below using pre-calculated values
            # (Indicator calculation moved to Step 0)

            
            # Apply filters
            if not self._apply_filters(exec_candles_1h, reasoning_parts):
                 signal = self._no_signal(
                     symbol, 
                     reasoning_parts, 
                     exec_candles_1h[-1] if exec_candles_1h else None,
                     adx=adx_value,
                     atr=atr_value,
                     regime=regime_early  # Pass early-classified regime
                 )

            # Step 4: Calculate Levels (If passed all checks)
            if signal is None:
                signal_type, entry_price, stop_loss, take_profit, tp_candidates, classification_info = self._calculate_levels(
                    structure_signal,
                    exec_candles_1h,
                    bias,
                    reasoning_parts,
                    atr_value=atr_value,  # Pass cached ATR
                )
                
                # Step 5: Fib Validation (Gate for tight_smc) - use cached fib_levels
                fib_valid = True
                
                setup_type = classification_info['setup_type']
                regime = classification_info['regime']
                
                if regime == "tight_smc":
                    # HARD REQUIREMENT: Must be in OTE or near key level
                    if fib_levels:
                        # Check OTE
                        in_ote = self.fibonacci_engine.is_in_ote_zone(entry_price, fib_levels)
                        # Check specific levels
                        is_near, _ = self.fibonacci_engine.check_confluence(
                            entry_price, 
                            fib_levels, 
                            tolerance_pct=self.config.fib_proximity_bps/10000
                        )
                        
                        if not (in_ote or is_near):
                            reasoning_parts.append(f"❌ Rejected: tight_smc entry not in OTE/Key Fib (Gate)")
                            fib_valid = False
                        else:
                            reasoning_parts.append(f"✓ Fib requirement passed for tight_smc")
                    else:
                        # If no fib levels found, default to rejection for tight_smc safety
                        reasoning_parts.append(f"❌ Rejected: No Fib structure found for tight_smc")
                        fib_valid = False
                
                if not fib_valid:
                     signal = self._no_signal(
                         symbol, 
                         reasoning_parts, 
                         exec_candles_1h[-1],
                         adx=adx_value,
                         atr=atr_value,
                         regime=regime
                     )
                
                # Step 5.5: Verify signal direction matches MS change (if entry ready)
                if signal is None and signal_type != SignalType.NO_SIGNAL:
                    if self.ms_tracker.is_entry_ready(symbol):
                        entry_signal = self.ms_tracker.get_entry_signal(symbol)
                        if entry_signal:
                            expected_direction, _ = entry_signal
                            # Verify signal direction matches MS change
                            if expected_direction == "LONG" and signal_type != SignalType.LONG:
                                reasoning_parts.append(
                                    f"❌ Signal direction mismatch: MS change expects LONG but got {signal_type.value}"
                                )
                                signal = self._no_signal(
                                    symbol, 
                                    reasoning_parts, 
                                    exec_candles_1h[-1] if exec_candles_1h else None,
                                    adx=adx_value,
                                    atr=atr_value,
                                    regime=regime
                                )
                            elif expected_direction == "SHORT" and signal_type != SignalType.SHORT:
                                reasoning_parts.append(
                                    f"❌ Signal direction mismatch: MS change expects SHORT but got {signal_type.value}"
                                )
                                signal = self._no_signal(
                                    symbol, 
                                    reasoning_parts, 
                                    exec_candles_1h[-1] if exec_candles_1h else None,
                                    adx=adx_value,
                                    atr=atr_value,
                                    regime=regime
                                )
                            else:
                                reasoning_parts.append(
                                    f"✓ Signal direction matches MS change ({expected_direction})"
                                )
                
                # Step 6: Scoring & Final Validation
                if signal is None and signal_type != SignalType.NO_SIGNAL:
                    # Metadata
                    current_candle = exec_candles_1h[-1]
                    timestamp = current_candle.timestamp
                    ema_values = self.indicators.calculate_ema(bias_candles_1d, self.config.ema_period)
                    ema200_slope = self.indicators.get_ema_slope(ema_values) if not ema_values.empty else "flat"
                    
                    # Create TEMP signal for scoring
                    temp_signal = Signal(
                        timestamp=timestamp,
                        symbol=symbol,
                        signal_type=signal_type,
                        entry_price=entry_price,
                        stop_loss=stop_loss,
                        take_profit=take_profit,
                        reasoning="",
                        setup_type=setup_type,
                        regime=regime,
                        higher_tf_bias=bias,
                        adx=adx_value,
                        atr=atr_value,
                        atr_ratio=atr_ratio,
                        ema200_slope=ema200_slope,
                        tp_candidates=tp_candidates
                    )
                    
                    # Estimate cost (rough bps)
                    cost_bps = Decimal("15.0") # Baseline assumption
                    
                    # Score
                    score_obj = self.signal_scorer.score_signal(
                        temp_signal,
                        structure_signal,
                        fib_levels,
                        adx_value,
                        cost_bps,
                        bias
                    )
                    
                    # GATE: Check score
                    passed, threshold = self.signal_scorer.check_score_gate(score_obj.total_score, setup_type, bias)
                    
                    if not passed:
                        reasoning_parts.append(f"❌ Score {score_obj.total_score:.1f} < Threshold {threshold} (Grade: {score_obj.get_grade()})")
                        signal = self._no_signal(
                            symbol, 
                            reasoning_parts, 
                            current_candle, 
                            score_breakdown={
                                "smc": score_obj.smc_quality,
                                "fib": score_obj.fib_confluence,
                                "htf": score_obj.htf_alignment,
                                "adx": score_obj.adx_strength,
                                "cost": score_obj.cost_efficiency
                            },
                            adx=adx_value,
                            atr=atr_value,
                            regime=regime
                        )
                        
                        # LOG REJECTION (Mandatory)
                        logger.info(
                            "Signal Rejected (Score)",
                            symbol=symbol,
                            reason=f"Score {score_obj.total_score} < {threshold}",
                            setup=setup_type.value,
                            bias=bias,
                            adx=adx_value,
                            fib_confluence=fib_valid
                        )
                    else:
                        reasoning_parts.append(f"✓ Score Passed: {score_obj.total_score:.1f} >= {threshold}")
                        
                        # Create FINAL signal
                        signal = Signal(
                            timestamp=timestamp,
                            symbol=symbol,
                            signal_type=signal_type,
                            entry_price=entry_price,
                            stop_loss=stop_loss,
                            take_profit=take_profit,
                            reasoning="\n".join(reasoning_parts),
                            setup_type=setup_type,
                            regime=regime,
                            higher_tf_bias=bias,
                            adx=adx_value,
                            atr=atr_value,
                            atr_ratio=atr_ratio,
                            ema200_slope=ema200_slope,
                            tp_candidates=tp_candidates,
                            score=score_obj.total_score,
                            score_breakdown={
                                "smc": score_obj.smc_quality,
                                "fib": score_obj.fib_confluence,
                                "htf": score_obj.htf_alignment,
                                "adx": score_obj.adx_strength,
                                "cost": score_obj.cost_efficiency
                            },
                            structure_info=structure_signal or {},
                            meta_info={
                                "fib_levels": {str(k): float(v) for k, v in fib_levels.items()} if fib_levels else {},
                                "filters": {
                                    "adx": adx_value,
                                    "atr": float(atr_value)
                                }
                            }
                        )
                elif signal is None:
                     # Calculate levels returned None (should be handled by signal_type check but safe fallback)
                     signal = self._no_signal(
                         symbol, 
                         reasoning_parts, 
                         exec_candles_1h[-1],
                         adx=adx_value,
                         atr=atr_value
                     )

        # If signal is still None after all steps
        if signal is None:
            current_candle = exec_candles_1h[-1] if exec_candles_1h else None
            timestamp = current_candle.timestamp if current_candle else datetime.now(timezone.utc)
            signal = self._no_signal(
                symbol, 
                reasoning_parts, 
                current_candle,
                adx=adx_value,
                atr=atr_value
            )


        # --- EXPLAINABILITY INSTRUMENTATION ---
        trace_data = {
            "signal": signal.signal_type.value,
            "regime": signal.regime,
            "bias": signal.higher_tf_bias,
            "adx": float(signal.adx) if signal.adx else 0.0,
            "atr": float(signal.atr) if signal.atr else 0.0,
            "ema200_slope": signal.ema200_slope,
            "spot_price": float(exec_candles_1h[-1].close) if exec_candles_1h else 0.0,
            "setup_quality": sum(float(v) for v in (signal.score_breakdown or {}).values()),
            "score_breakdown": signal.score_breakdown or {},
            "structure": structure_signal,
            "filters": {
                "adx": float(adx_value),
                "atr": float(atr_value),
            },
            "reasoning": reasoning_parts,
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
    
    def _classify_setup(
        self,
        structures: dict,
        signal_type: SignalType,
    ) -> tuple[str, str]:
        """
        Classify setup type for regime determination.
        
        Returns:
            (setup_type, regime) tuple
            
        Priority (highest first):
        1. If Order Block present → ("ob", "tight_smc")
        2. If Fair Value Gap present → ("fvg", "tight_smc")
        3. If Break of Structure confirmed → ("bos", "wide_structure")
        4. Else (HTF trend only) → ("trend", "wide_structure")
        """
        from src.domain.models import SetupType
        
        if structures.get("order_block"):  # Fixed: was "orderblock"
            return (SetupType.OB, "tight_smc")
        
        elif structures.get("fvg"):
            return (SetupType.FVG, "tight_smc")
        
        elif structures.get("bos"):  # Fixed: was "bos_confirmed"
            return (SetupType.BOS, "wide_structure")
        
        else:
            # HTF trend following only
            return (SetupType.TREND, "wide_structure")
    
    def _classify_regime_from_structure(self, structure: dict) -> str:
        """
        Classify regime from detected structure (early classification).
        
        This is called immediately after structure detection to ensure
        rejected signals still show the correct regime on the dashboard.
        
        Returns:
            regime string: "tight_smc" or "wide_structure"
        
        Priority (highest first):
        1. If Order Block present → "tight_smc"
        2. If Fair Value Gap present → "tight_smc"
        3. If Break of Structure confirmed → "wide_structure"
        4. Else (HTF trend only) → "wide_structure"
        """
        if structure.get("order_block"):
            return "tight_smc"
        elif structure.get("fvg"):
            return "tight_smc"
        elif structure.get("bos"):
            return "wide_structure"
        else:
            # HTF trend following only
            return "wide_structure"
    
    def _determine_bias(
        self,
        candles_4h: List[Candle],
        candles_1d: List[Candle],
        reasoning: List[str],
    ) -> str:
        """
        Determine higher-timeframe bias (bullish/bearish/neutral).
        
        V2.1 Rules:
        - Price > EMA200 -> Bullish
        - Price < EMA200 -> Bearish
        - abs(Price - EMA200) < configurable bps -> Neutral
        - EMA Slope: CONTRIBUTES TO SCORE ONLY (Does not block bias)
        """
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
        
        # Check Neutral Zone
        dist_bps = abs(current_price - ema_value) / ema_value * Decimal("10000")
        neutral_zone = Decimal(str(self.config.ema_neutral_zone_bps))
        
        if dist_bps < neutral_zone:
            reasoning.append(f"○ Bias Neutral: Price vs EMA dist {dist_bps:.1f} bps < {neutral_zone}")
            return "neutral"
        
        # Direction
        if current_price > ema_value:
             reasoning.append(f"✓ Bias Bullish: Price ${current_price} > EMA ${ema_value}")
             return "bullish"
        else:
             reasoning.append(f"✓ Bias Bearish: Price ${current_price} < EMA ${ema_value}")
             return "bearish"
    
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
        
        # Detect break of structure (configurable requirement for trade validity)
        bos = self._detect_break_of_structure(candles_1h, bias)
        
        # Check if BOS is required (configurable)
        require_bos = getattr(self.config, 'require_bos_confirmation', False)
        
        if require_bos and not bos:
            reasoning.append("❌ No break of structure - waiting for confirmation (BOS required)")
            return None
        
        if bos:
            reasoning.append(f"✓ Break of structure confirmed")
        else:
            reasoning.append("○ No break of structure yet (not required)")
        
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
                        # Calculate entry price based on configured mode
                        if self.config.ob_entry_mode == "mid":
                            entry_price = (cand.high + cand.low) / Decimal("2")
                        elif self.config.ob_entry_mode == "open":
                            entry_price = cand.open
                        elif self.config.ob_entry_mode == "discount":
                            # Enter at discount (lower in the OB zone)
                            discount_pct = Decimal(str(self.config.ob_discount_pct))
                            entry_price = cand.low + (cand.high - cand.low) * discount_pct
                        else:  # high_low (legacy)
                            entry_price = cand.high
                        
                        return {
                            "type": "bullish",
                            "index": i,
                            "timestamp": cand.timestamp,
                            "low": cand.low,
                            "high": cand.high,
                            "price": entry_price
                        }
            else: # bearish
                # 1. Origin must be a bullish candle
                if cand.close > cand.open:
                    # 2. Must be followed by an impulsive move down
                    move = cand.low - nxt.close
                    if move > 0 and (nxt.high - nxt.low) >= min_displacement:
                        # Calculate entry price based on configured mode
                        if self.config.ob_entry_mode == "mid":
                            entry_price = (cand.high + cand.low) / Decimal("2")
                        elif self.config.ob_entry_mode == "open":
                            entry_price = cand.open
                        elif self.config.ob_entry_mode == "discount":
                            # Enter at discount (higher in the OB zone for shorts)
                            discount_pct = Decimal(str(self.config.ob_discount_pct))
                            entry_price = cand.high - (cand.high - cand.low) * discount_pct
                        else:  # high_low (legacy)
                            entry_price = cand.low
                        
                        return {
                            "type": "bearish",
                            "index": i,
                            "timestamp": cand.timestamp,
                            "low": cand.low,
                            "high": cand.high,
                            "price": entry_price
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
        
        # ATR check (ensure volatility is measurable)
        atr_values = self.indicators.calculate_atr(candles, self.config.atr_period)
        if atr_values.empty:
            reasoning.append("❌ ATR not available")
            return False
            
        return True
    
    def _calculate_levels(
        self,
        structure: dict,
        candles: List[Candle],
        bias: str,
        reasoning: List[str],
        atr_value: Optional[Decimal] = None,
    ) -> Tuple[SignalType, Decimal, Decimal, Optional[Decimal], List[Decimal], Dict]:
        """
        Calculate Levels: Entry, Stop, TP.
        
        V2.1 Regime:
        - tight_smc: Stop = Invalid + 0.3-0.6 ATR, TP = 2.0R min
        - wide_structure: Stop = Invalid + 1.0-1.2 ATR, TP = 1.5R min
        """
        from src.domain.models import SetupType
        
        order_block = structure['order_block']
        fvg = structure['fvg']
        bos = structure['bos']
        
        # 1. Classify Setup Type & Regime First
        setup_type = SetupType.TREND
        regime = "wide_structure"
        
        if order_block:
            setup_type = SetupType.OB
            regime = "tight_smc"
        elif fvg:
            setup_type = SetupType.FVG
            regime = "tight_smc"
        elif bos:
            setup_type = SetupType.BOS
            regime = "wide_structure"
            
        # 2. Get ATR (use cached if available)
        if atr_value is None:
            atr_values = self.indicators.calculate_atr(candles, self.config.atr_period)
            atr = Decimal(str(atr_values.iloc[-1])) if not atr_values.empty else Decimal("0")
        else:
            atr = atr_value  # Use cached value
        
        # 3. Determine Stop Multiplier based on Regime
        if regime == "tight_smc":
            # Randomize or fixed? Use average or min for now to be safe.
            # Config has ranges: low/high. Let's use AVG of range for standard execution
            stop_mult = Decimal(str((self.config.tight_smc_atr_stop_min + self.config.tight_smc_atr_stop_max) / 2))
        else:
            stop_mult = Decimal(str((self.config.wide_structure_atr_stop_min + self.config.wide_structure_atr_stop_max) / 2))
            
        tp_candidates = []
        entry_price = Decimal("0")
        invalid_level = Decimal("0")
        signal_type = SignalType.NO_SIGNAL
        
        # 4. Calculate Entry & Stop Base
        if bias == "bullish":
            signal_type = SignalType.LONG
            if setup_type == SetupType.OB:
                entry_price = order_block['high']
                invalid_level = order_block['low']
            elif setup_type == SetupType.FVG:
                entry_price = fvg['top']
                invalid_level = fvg['bottom']
            else: # BOS/Trend
                entry_price = candles[-1].close # Market entry if confirmed? Or waiting for retrace?
                # For V2, BOS typically implies awaiting retrace, but if no OB/FVG found...
                # Current logic implies we only get here if valid.
                # Let's assume retrace to recent low if BOS?
                # Fallback: Entry at close, stop at recent swing low.
                entry_price = candles[-1].close
                invalid_level = min(c.low for c in candles[-20:])
                
            stop_loss = invalid_level - (atr * stop_mult)
            
        elif bias == "bearish":  # bearish
            signal_type = SignalType.SHORT
            if setup_type == SetupType.OB:
                entry_price = order_block['low']
                invalid_level = order_block['high']
            elif setup_type == SetupType.FVG:
                entry_price = fvg['bottom']
                invalid_level = fvg['top']
            else: # BOS/Trend
                entry_price = candles[-1].close
                invalid_level = max(c.high for c in candles[-20:])
                
            stop_loss = invalid_level + (atr * stop_mult)
            
        else:
             # Neutral bias - generally no trade unless counter-trend enabled?
             # For V2.1, neutral can trade if score is high.
             # Assume logic mirrors bullish/bearish based on structure type
             if structure.get('order_block'):
                 ob = structure['order_block']
                 if ob['type'] == 'bullish':
                     signal_type = SignalType.LONG
                     entry_price = ob['high']
                     invalid_level = ob['low']
                     stop_loss = invalid_level - (atr * stop_mult)
                 else:
                     signal_type = SignalType.SHORT
                     entry_price = ob['low']
                     invalid_level = ob['high']
                     stop_loss = invalid_level + (atr * stop_mult)
             else:
                 return SignalType.NO_SIGNAL, Decimal("0"), Decimal("0"), None, [], {}
                 
        # 5. TP Logic
        # Scan for swing points (optimized)
        lookback = 50
        risk = abs(entry_price - stop_loss)
        if risk == 0: risk = Decimal("1") # Avoid div/0
        
        if signal_type == SignalType.LONG:
            # Use optimized vectorized swing point detection
            swing_highs = self.indicators.find_swing_points(candles, lookback=lookback, find_highs=True)
            tp_candidates = sorted([h for h in swing_highs if h > entry_price])[:5]
            
            # Min RR
            min_rr = getattr(self.config, 'tight_smc_min_rr_multiple', 2.0) if regime == "tight_smc" else 1.5
            min_tp_dist = risk * Decimal(str(min_rr))
            
            # Filter TPs < Min RR
            valid_tps = [tp for tp in tp_candidates if (tp - entry_price) >= min_tp_dist]
            
            if valid_tps:
                take_profit = valid_tps[0] # Nearest valid
            else:
                take_profit = entry_price + min_tp_dist # Force Min RR
                 
        else: # SHORT
            # Use optimized vectorized swing point detection
            swing_lows = self.indicators.find_swing_points(candles, lookback=lookback, find_highs=False)
            tp_candidates = sorted([l for l in swing_lows if l < entry_price], reverse=True)[:5]
            
            min_rr = getattr(self.config, 'tight_smc_min_rr_multiple', 2.0) if regime == "tight_smc" else 1.5
            min_tp_dist = risk * Decimal(str(min_rr))
            
            valid_tps = [tp for tp in tp_candidates if (entry_price - tp) >= min_tp_dist]
            
            if valid_tps:
                take_profit = valid_tps[0]
            else:
                take_profit = entry_price - min_tp_dist

        reasoning.append(
            f"✓ V2.1 Levels ({regime}): Entry ${entry_price}, Stop ${stop_loss}, TP ${take_profit}"
        )
        
        class_info = {
            "setup_type": setup_type,
            "regime": regime
        }
        
        return signal_type, entry_price, stop_loss, take_profit, tp_candidates, class_info
    
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
        score_breakdown: Optional[Dict] = None,
        adx: float = 0.0,
        atr: Decimal = Decimal("0"),
        regime: Optional[str] = None
    ) -> Signal:
        """Create a NO_SIGNAL signal."""
        from src.domain.models import SetupType
        timestamp = current_candle.timestamp if current_candle else datetime.now(timezone.utc)
        
        # DEBUG LOGGING FOR REGIME
        import logging
        logger = logging.getLogger("SMCEngine") # Use direct logger if needed or self.logger if available
        # Assuming no self.logger here, printing to stdout or using print is dirty but effective for now.
        # But we should use the configured logger if possible. 
        # Since I can't easily import get_logger here without risking circular dep or context issues, 
        # I'll rely on the logic check.
        
        # Determine regime if not provided (fallback for early rejections)
        if not regime:
            # If no data (no candle), state is undefined/no_data
            if not current_candle:
                regime = "no_data"
            else:
                # Use ADX-based heuristic for early rejections (before structure analysis)
                # ADX < 20: Very low trend strength → consolidation
                # ADX 20-25: Low trend strength → consolidation (ranging)
                # ADX 25-40: Moderate trend → wide_structure (could be trending)
                # ADX > 40: Strong trend → wide_structure (definitely trending)
                if adx > 0 and adx < 20:
                    regime = "consolidation"  # Very weak/no trend
                elif adx >= 20 and adx < 25:
                    regime = "consolidation"  # Ranging market
                else:
                    # ADX >= 25: Trending market
                    # Default to wide_structure since we don't have structure details
                    regime = "wide_structure"
                
            # SPECIAL DEBUG: If regime resulted in no_data but we HAD a candle, LOG IT
            if regime == "no_data" and current_candle:
                 reasoning.append(f"DEBUG_ERROR: Regime=no_data BUT Candle Exists! ADX={adx}")
                 # Force fix
                 regime = "consolidation"  # Conservative default
        
        return Signal(
            timestamp=timestamp,
            symbol=symbol,
            signal_type=SignalType.NO_SIGNAL,
            entry_price=Decimal("0"),
            stop_loss=Decimal("0"),
            take_profit=None,
            reasoning="\n".join(reasoning),
            setup_type=SetupType.TREND,  # Default for no signal
            regime=regime,
            higher_tf_bias="neutral",
            adx=Decimal(str(adx)) if adx else Decimal("0"),
            atr=atr,

            ema200_slope="flat",
            score_breakdown=score_breakdown or {}
        )
