# Regime Classification Fix

## Problem

The dashboard was only showing two regime types: **"consolidation"** and **"wide_structure"**, even though the system is designed to classify four distinct market regimes:

1. **"tight_smc"** - Order Block (OB) and Fair Value Gap (FVG) setups
2. **"wide_structure"** - Break of Structure (BOS) and Trend setups  
3. **"consolidation"** - Low volatility, ranging markets (ADX < 25)
4. **"no_data"** - Insufficient data for analysis

## Root Cause

The regime classification was happening **too late** in the signal generation pipeline:

1. **Early rejections** (insufficient data, no structure detected, filters failed) occurred BEFORE regime classification
2. These early rejections fell back to an ADX-based heuristic in `_no_signal()`:
   - ADX < 25 → "consolidation"
   - ADX ≥ 25 → "wide_structure"
3. **"tight_smc" regime** only appeared when:
   - An Order Block OR Fair Value Gap was detected
   - ALL filters passed (Fibonacci, score gates, etc.)
   - An actual trading signal was generated (not rejected)

Since the 6-gate strategy has strict entry criteria, most coins were being rejected at early stages, and the dashboard showed only the fallback regime classifications.

## Solution Implemented

### 1. Early Regime Classification (Primary Fix)

**File**: `src/strategy/smc_engine.py`

Added regime classification immediately after structure detection (Step 2.5):

```python
# EARLY REGIME CLASSIFICATION (NEW)
# Classify regime immediately after structure detection
# This ensures rejected signals still show correct regime
regime_early = self._classify_regime_from_structure(structure_signal)
reasoning_parts.append(f"📊 Market Regime: {regime_early}")
```

**New Helper Method**: `_classify_regime_from_structure()`

```python
def _classify_regime_from_structure(self, structure: dict) -> str:
    """
    Classify regime from detected structure (early classification).
    
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
        return "wide_structure"
```

### 2. Pass Regime Through Rejection Flow

Updated all `_no_signal()` calls after structure detection to pass the early-classified regime:

```python
signal = self._no_signal(
    symbol, 
    reasoning_parts, 
    exec_candles_1h[-1] if exec_candles_1h else None,
    adx=adx_value,
    atr=atr_value,
    regime=regime_early  # Pass early-classified regime
)
```

### 3. Improved Fallback Logic

Enhanced the ADX-based heuristic in `_no_signal()` for cases where structure hasn't been analyzed yet:

```python
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
    regime = "wide_structure"
```

## Expected Outcome

After this fix, the dashboard should now show:

1. **"tight_smc"** - When Order Blocks or Fair Value Gaps are detected (even if signal is rejected for other reasons)
2. **"wide_structure"** - When Break of Structure is detected, or trending markets without tight SMC structures
3. **"consolidation"** - Only for genuinely ranging markets (ADX < 25) where structure hasn't been analyzed yet
4. **"no_data"** - Only when there's truly insufficient data

This provides a much more accurate representation of market conditions across your universe of coins.

## Testing

Run smoke test to verify:
```bash
make smoke
```

Check dashboard after deployment to confirm regime distribution is now more diverse and accurate.

## Files Modified

- `src/strategy/smc_engine.py`
  - Added `_classify_regime_from_structure()` method
  - Added early regime classification in `generate_signal()` 
  - Updated `_no_signal()` calls to pass regime parameter
  - Improved fallback logic in `_no_signal()`
