"""
Test: Kill switch activation and latching.
"""
import pytest
from src.utils.kill_switch import KillSwitch, KillSwitchReason


def test_kill_switch_activation():
    """Test kill switch activates and latches (sync version)."""
    ks = KillSwitch()

    assert ks.is_active() is False
    assert ks.is_latched() is False

    ks.activate_sync(KillSwitchReason.MANUAL)

    assert ks.is_active() is True
    assert ks.is_latched() is True
    assert ks.reason == KillSwitchReason.MANUAL


def test_kill_switch_requires_ack():
    """Test that kill switch requires manual acknowledgment."""
    ks = KillSwitch()

    ks.activate_sync(KillSwitchReason.API_ERROR)

    # Should stay latched
    assert ks.is_latched() is True

    # Acknowledge
    acknowledged = ks.acknowledge()

    assert acknowledged is True
    assert ks.is_active() is False
    assert ks.is_latched() is False
