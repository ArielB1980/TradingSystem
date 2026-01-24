import pytest
from decimal import Decimal
from unittest.mock import Mock
from src.strategy.signal_scorer import SignalScorer
from src.config.config import StrategyConfig
from src.domain.models import Signal, SignalType, SetupType
from src.strategy.fibonacci_engine import FibonacciLevels

@pytest.fixture
def scorer():
    config = StrategyConfig()
    # Gates
    config.min_score_tight_smc_aligned = 75
    config.min_score_tight_smc_neutral = 80
    config.min_score_wide_structure_aligned = 70
    config.min_score_wide_structure_neutral = 75
    return SignalScorer(config)

def test_score_components(scorer):
    """Test individual scoring components."""
    # SMC Quality
    structures_full = {"order_block": True, "fvg": True, "bos": True}
    assert scorer._score_smc_quality(structures_full) == 25.0
    
    structures_partial = {"order_block": True}
    assert scorer._score_smc_quality(structures_partial) == 10.0
    
    # ADX
    assert scorer._score_adx_strength(45.0) == 15.0
    assert scorer._score_adx_strength(22.0) == 7.0
    assert scorer._score_adx_strength(15.0) == 0.0
    
    # Cost
    assert scorer._score_cost_efficiency(Mock(), Decimal("5")) == 20.0 # <= 10 bps
    assert scorer._score_cost_efficiency(Mock(), Decimal("100")) == 0.0 # > 50 bps

def test_score_gate_tight(scorer):
    """Test scoring gates for tight_smc regime."""
    # Aligned (Threshold 75)
    passed, thresh = scorer.check_score_gate(76.0, SetupType.OB, "bullish")
    assert passed
    assert thresh == 75.0
    
    passed, thresh = scorer.check_score_gate(74.0, SetupType.OB, "bullish")
    assert not passed
    
    # Neutral (Threshold 80)
    passed, thresh = scorer.check_score_gate(78.0, SetupType.OB, "neutral")
    assert not passed
    assert thresh == 80.0
    
    passed, thresh = scorer.check_score_gate(81.0, SetupType.OB, "neutral")
    assert passed

def test_fib_confluence_scoring(scorer):
    """Test fib confluence scoring logic."""
    signal = Mock(spec=Signal)
    signal.entry_price = Decimal("50000")
    
    # Case 1: In OTE (0.618-0.79)
    # 50000 in [49000, 51000]
    fibs = Mock(spec=FibonacciLevels)
    fibs.ote_low = Decimal("49000")
    fibs.ote_high = Decimal("51000")
    
    score = scorer._score_fib_confluence(signal, fibs)
    assert score == 15.0

    # Case 2: Near 0.382
    fibs.ote_low = Decimal("10000") # Far away
    fibs.ote_high = Decimal("11000")
    fibs.fib_0_382 = Decimal("50050") # 0.1% away (tolerance 0.2%)
    fibs.fib_0_618 = Decimal("10000")
    fibs.fib_0_500 = Decimal("10000")
    fibs.fib_0_786 = Decimal("10000")
    
    score = scorer._score_fib_confluence(signal, fibs)
    assert score == 10.0


def test_fib_confluence_uses_config_tolerance():
    """_score_fib_confluence uses config.fib_proximity_bps for tolerance."""
    config = StrategyConfig()
    config.fib_proximity_bps = 10.0  # 0.1%
    scorer = SignalScorer(config)
    signal = Mock(spec=Signal)
    signal.entry_price = Decimal("50000")
    fibs = Mock(spec=FibonacciLevels)
    fibs.ote_low = Decimal("10000")
    fibs.ote_high = Decimal("11000")
    fibs.fib_0_382 = Decimal("50075")   # 0.15% away from 50000
    fibs.fib_0_618 = fibs.fib_0_500 = fibs.fib_0_786 = Decimal("10000")
    fibs.fib_1_272 = fibs.fib_1_618 = Decimal("10000")
    # 0.15% > 0.1% tolerance -> no match
    score = scorer._score_fib_confluence(signal, fibs)
    assert score == 0.0
    config.fib_proximity_bps = 20.0  # 0.2%
    scorer.config = config
    score = scorer._score_fib_confluence(signal, fibs)
    assert score == 10.0
