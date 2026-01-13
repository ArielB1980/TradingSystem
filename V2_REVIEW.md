# V2 Architecture Review

**Review Date**: 2025-01-10  
**Reviewer**: Architecture Review  
**System Status**: V2 Core Complete | Ready for Extended Testing

## Executive Summary

The V2 upgrade from v1 architecture is **well-structured and mostly complete**, with excellent separation of concerns and good architectural patterns. However, several **critical issues** need attention before production deployment.

### Overall Assessment

**✅ Strengths:**
- Clean architectural separation (strategy, execution, risk, data layers)
- Well-documented V2 features
- Multi-asset support properly implemented
- Fibonacci confluence engine is well-designed
- Signal scoring system is comprehensive
- Configuration system is robust

**⚠️ Issues Found:**
- 2 critical import errors that will cause runtime failures
- 1 configuration mismatch (multi-TP config not loaded)
- Several architectural inconsistencies

---

## Critical Issues (Must Fix)

### 1. Missing Import: `Tuple` in `signal_scorer.py`

**Location**: `src/strategy/signal_scorer.py:121`

**Issue**: The function `check_score_gate` uses `Tuple[bool, float]` as a return type annotation, but `Tuple` is not imported from `typing`.

**Current Code:**
```python
from typing import Dict, Optional
# ... missing Tuple ...

def check_score_gate(self, score: float, setup_type: str, bias: str) -> Tuple[bool, float]:
```

**Impact**: This will cause a `NameError: name 'Tuple' is not defined` at runtime when the module is imported or the function is called.

**Fix**: Add `Tuple` to the imports:
```python
from typing import Dict, Optional, Tuple
```

---

### 2. Missing Import: `timezone` in `smc_engine.py`

**Location**: `src/strategy/smc_engine.py:257, 774`

**Issue**: The code uses `timezone.utc` but `timezone` is not imported from `datetime`.

**Current Code:**
```python
from datetime import datetime
# ... missing timezone ...

timestamp = current_candle.timestamp if current_candle else datetime.now(timezone.utc)
```

**Impact**: This will cause a `NameError: name 'timezone' is not defined` at runtime.

**Fix**: Update the import:
```python
from datetime import datetime, timezone
```

---

### 3. Configuration Mismatch: Multi-TP Config Not Loaded

**Location**: `src/config/config.yaml:117-127` vs `src/config/config.py`

**Issue**: The `config.yaml` file defines a `multi_tp` configuration section:
```yaml
multi_tp:
  enabled: true
  tp1_r_multiple: 1.0
  tp1_close_pct: 0.40
  tp2_r_multiple: 2.5
  tp2_close_pct: 0.40
  runner_pct: 0.20
  move_sl_to_be_after_tp1: true
  trailing_stop_enabled: true
  trailing_stop_atr_multiplier: 1.5
```

However, there is **no corresponding `MultiTPConfig` class** in `config.py`, and the `Config` class does not include `multi_tp` as a field. This means:
- The configuration values are **silently ignored** (due to `extra="ignore"` in SettingsConfigDict)
- Any code trying to access `config.multi_tp` will fail with `AttributeError`
- The multi-TP feature configuration is **not functional**

**Current State**: Verified - `config.multi_tp` does not exist (tested via Python import)

**Impact**: 
- Multi-TP functionality cannot be configured
- V2 feature (mentioned in V2_README.md) is not properly integrated
- The execution engine uses different TP logic (`tp_splits`, `rr_fallback_multiples`) which doesn't match this config structure

**Fix Options**:
1. **Option A** (Recommended): Create `MultiTPConfig` class and integrate into `Config`
2. **Option B**: Remove `multi_tp` section from config.yaml if not using it
3. **Option C**: Migrate existing `ExecutionConfig.tp_splits` logic to match the multi_tp structure

**Note**: The execution engine appears to use `ExecutionConfig.tp_splits` and `rr_fallback_multiples` instead, which suggests the `multi_tp` config might be legacy/unused. However, V2_README.md mentions "Multi-TP configuration (40%@1R, 40%@2.5R, 20% runner)" which matches the YAML structure, so this needs clarification.

---

## Architecture Review

### ✅ Well-Implemented V2 Features

#### 1. Multi-Asset Support
- **Status**: ✅ Properly implemented
- **Files**: `src/data/coin_universe.py`, `src/config/config.py` (CoinUniverseConfig)
- **Integration**: Used in `live_trading.py` and `run_full_backtest.py`
- **Assessment**: Clean design, proper configuration, well-integrated

#### 2. Fibonacci Confluence Engine
- **Status**: ✅ Well-designed
- **File**: `src/strategy/fibonacci_engine.py`
- **Assessment**: 
  - Deterministic (same input → same output)
  - Properly integrated into signal generation
  - Used for confluence scoring, not signal generation (correct design)

#### 3. Signal Quality Scorer
- **Status**: ✅ Comprehensive implementation
- **File**: `src/strategy/signal_scorer.py`
- **Assessment**:
  - Well-structured scoring system (5 components)
  - Proper integration with signal generation
  - Good gate logic
  - **Issue**: Missing `Tuple` import (see Critical Issue #1)

#### 4. Loss Streak Cooldown (Time-Based)
- **Status**: ✅ Implemented
- **File**: `src/risk/risk_manager.py`
- **Assessment**: 
  - Regime-aware (separate tracking for tight/wide)
  - Time-based pause (not permanent block)
  - Properly integrated into risk management

#### 5. Enhanced Backtest Engine
- **Status**: ✅ Multi-asset capable
- **File**: `src/backtest/backtest_engine.py`
- **Assessment**: 
  - Symbol parameter properly handled
  - Good separation of concerns
  - Multi-asset support working

---

### ⚠️ Architectural Concerns

#### 1. Config Schema Evolution

The configuration has evolved but not all paths are updated:

- **ExecutionConfig** uses: `tp_splits: [0.35, 0.35, 0.30]`, `rr_fallback_multiples: [1.0, 2.0, 3.0]`
- **config.yaml** defines: `multi_tp` with `tp1_close_pct: 0.40`, `tp2_close_pct: 0.40`, `runner_pct: 0.20`

These are **different structures** for the same feature. Need clarification:
- Which is the active configuration?
- Should they be unified?
- Is multi_tp legacy/unused?

**Recommendation**: Consolidate to a single, clear configuration structure.

---

#### 2. Position Model Evolution

The `Position` model has fields for both:
- **V2-style**: `tp_order_ids: list[str]` (multi-TP ladder)
- **V3-style**: `tp1_price`, `tp2_price`, `tp1_hit`, `tp2_hit` (active trade management)

This suggests **multiple evolution paths** or different implementations for different runtimes (backtest vs live).

**Assessment**: This is acceptable if intentional (different runtimes use different fields), but should be documented. However, the mixing of V2 and V3 concepts suggests incomplete migration.

---

#### 3. Execution Engine TP Logic

The `ExecutionEngine._generate_tp_ladder` method uses `ExecutionConfig.tp_splits` and `rr_fallback_multiples`, not the `multi_tp` config from YAML. This suggests:

1. The `multi_tp` YAML config is unused/legacy
2. OR the execution engine needs to be updated to use `multi_tp`
3. OR there are two different TP systems (one for backtest, one for live)

**Recommendation**: Clarify and align the TP configuration approach.

---

### ✅ Good Practices Observed

1. **Design Locks Enforced**: Mark price requirement, leverage cap, etc. properly validated
2. **Error Handling**: Good use of try/except in critical paths
3. **Logging**: Comprehensive structured logging
4. **Type Hints**: Good use of type annotations (where imports are correct)
5. **Documentation**: Good docstrings and comments
6. **Configuration Validation**: Pydantic models with validation

---

## Recommendations

### Immediate Actions (Before Production)

1. **Fix Import Errors** (Critical)
   - Add `Tuple` import to `signal_scorer.py`
   - Add `timezone` import to `smc_engine.py`

2. **Resolve Multi-TP Config** (Critical)
   - Decide on configuration approach (ExecutionConfig.tp_splits vs multi_tp)
   - Either create MultiTPConfig class OR remove multi_tp from YAML
   - Ensure execution engine uses the intended config

3. **Add Runtime Tests**
   - Create simple import test script
   - Test configuration loading
   - Verify all V2 features are accessible

### Short-Term Improvements

1. **Documentation**
   - Clarify TP configuration approach in docs
   - Document Position model field usage (V2 vs V3 fields)
   - Update V2_README.md with actual config structure

2. **Code Consistency**
   - Align TP configuration structures
   - Review Position model fields (consolidate if possible)
   - Ensure all config.yaml sections have corresponding Config classes

3. **Testing**
   - Add integration tests for V2 features
   - Test multi-asset backtesting
   - Test signal scoring system
   - Test Fibonacci engine

### Long-Term Considerations

1. **Position Model Refactoring**
   - Consider separating V2 and V3 position models if they serve different purposes
   - OR consolidate to a single model if possible

2. **Configuration Management**
   - Consider versioning config schemas
   - Add migration utilities for config changes

---

## Testing Recommendations

1. **Import Tests**
   ```python
   # Test all V2 modules can be imported
   from src.strategy.signal_scorer import SignalScorer
   from src.strategy.fibonacci_engine import FibonacciEngine
   from src.strategy.smc_engine import SMCEngine
   from src.data.coin_universe import CoinClassifier
   ```

2. **Config Loading Test**
   ```python
   from src.config.config import load_config
   config = load_config()
   assert hasattr(config, 'coin_universe')
   # Check multi_tp resolution
   ```

3. **Integration Tests**
   - Test signal generation with Fibonacci engine
   - Test signal scoring
   - Test multi-asset backtesting
   - Test loss streak cooldown

---

## Conclusion

The V2 upgrade is **architecturally sound** and well-implemented overall. The main issues are:
1. **Two critical import errors** that will cause runtime failures
2. **One configuration mismatch** that prevents multi-TP config from working

These are **fixable issues** and do not indicate fundamental architectural problems. Once fixed, V2 should be ready for extended testing as planned.

**Priority**: Fix the 3 critical issues before proceeding with extended testing or production deployment.

---

## Appendix: Files Reviewed

### Core V2 Modules
- `src/data/coin_universe.py` ✅
- `src/strategy/fibonacci_engine.py` ✅
- `src/strategy/signal_scorer.py` ⚠️ (import issue)
- `src/strategy/smc_engine.py` ⚠️ (import issue)
- `src/config/config.py` ⚠️ (missing multi_tp)
- `src/config/config.yaml` ⚠️ (multi_tp not loaded)

### Integration Points
- `src/backtest/backtest_engine.py` ✅
- `src/live/live_trading.py` ✅
- `src/execution/execution_engine.py` ⚠️ (TP config mismatch)
- `src/risk/risk_manager.py` ✅

### Domain Models
- `src/domain/models.py` ⚠️ (V2/V3 field mixing)
