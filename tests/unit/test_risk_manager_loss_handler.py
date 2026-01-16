import pytest
from decimal import Decimal
from datetime import datetime
from unittest.mock import Mock, patch
from src.risk.risk_manager import RiskManager
from src.config.config import RiskConfig
from src.domain.models import SetupType

@pytest.fixture
def risk_config():
    config = RiskConfig()
    # Configure for testing
    config.loss_streak_cooldown_tight = 3
    config.loss_streak_pause_minutes_tight = 120
    config.loss_streak_cooldown_wide = 4
    config.loss_streak_pause_minutes_wide = 90
    config.loss_streak_min_loss_bps = 10.0 # 0.1%
    return config

@pytest.fixture
def risk_manager(risk_config):
    return RiskManager(risk_config)

def test_regime_aware_cooldown_tight(risk_manager):
    """Test cooldown triggers correctly for tight_smc regime."""
    equity = Decimal("10000")
    loss_amount = Decimal("-50") # 50 bps loss (meaningful)
    
    # record 2 losses (limit is 3)
    risk_manager.record_trade_result(loss_amount, equity, setup_type=SetupType.OB)
    risk_manager.record_trade_result(loss_amount, equity, setup_type=SetupType.FVG)
    
    assert risk_manager.consecutive_losses_tight == 2
    assert risk_manager.consecutive_losses_wide == 0
    assert risk_manager.cooldown_until is None
    
    # 3rd loss triggers cooldown
    risk_manager.record_trade_result(loss_amount, equity, setup_type=SetupType.OB)
    
    assert risk_manager.consecutive_losses_tight == 0 # Resets on trigger
    assert risk_manager.cooldown_until is not None
    
    # Verify duration (approx 120 mins from now)
    from datetime import timezone
    now = datetime.now(timezone.utc)
    diff = (risk_manager.cooldown_until - now).total_seconds() / 60
    assert 119 <= diff <= 121

def test_regime_aware_cooldown_wide(risk_manager):
    """Test cooldown triggers correctly for wide_structure regime."""
    equity = Decimal("10000")
    loss_amount = Decimal("-50")
    
    # record 3 losses (limit is 4 for wide)
    risk_manager.record_trade_result(loss_amount, equity, setup_type=SetupType.BOS)
    risk_manager.record_trade_result(loss_amount, equity, setup_type=SetupType.TREND)
    risk_manager.record_trade_result(loss_amount, equity, setup_type=SetupType.BOS)
    
    assert risk_manager.consecutive_losses_wide == 3
    assert risk_manager.consecutive_losses_tight == 0
    assert risk_manager.cooldown_until is None
    
    # 4th loss triggers cooldown
    risk_manager.record_trade_result(loss_amount, equity, setup_type=SetupType.TREND)
    
    assert risk_manager.consecutive_losses_wide == 0 # Resets
    assert risk_manager.cooldown_until is not None
    
    # Verify duration (approx 90 mins)
    from datetime import timezone
    now = datetime.now(timezone.utc)
    diff = (risk_manager.cooldown_until - now).total_seconds() / 60
    assert 89 <= diff <= 91

def test_win_resets_streaks(risk_manager):
    """Test that a win resets all streaks."""
    equity = Decimal("10000")
    loss_amount = Decimal("-50")
    win_amount = Decimal("100")
    
    # Accumulate some losses
    risk_manager.record_trade_result(loss_amount, equity, setup_type=SetupType.OB) # Tight: 1
    risk_manager.record_trade_result(loss_amount, equity, setup_type=SetupType.BOS) # Wide: 1
    
    assert risk_manager.consecutive_losses_tight == 1
    assert risk_manager.consecutive_losses_wide == 1
    
    # Record a WIN
    risk_manager.record_trade_result(win_amount, equity, setup_type=SetupType.OB)
    
    assert risk_manager.consecutive_losses_tight == 0
    assert risk_manager.consecutive_losses_wide == 0

def test_small_loss_ignored(risk_manager):
    """Test that losses below threshold are ignored."""
    equity = Decimal("10000")
    small_loss = Decimal("-5") # 5 bps (threshold 10)
    
    risk_manager.record_trade_result(small_loss, equity, setup_type=SetupType.OB)
    
    assert risk_manager.consecutive_losses_tight == 0
