"""
Tests for CycleGuard - timing and duplicate protection.

These tests verify that the trading loop is protected against timing issues.
"""
import pytest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch
import time

from src.runtime.cycle_guard import (
    CycleGuard,
    CycleState,
    get_cycle_guard,
    init_cycle_guard,
)


class TestCycleGuard:
    """Test CycleGuard core functionality."""
    
    @pytest.fixture
    def guard(self):
        """Create a fresh guard for each test."""
        return CycleGuard(
            min_cycle_interval_seconds=5,
            max_cycle_duration_seconds=60,
            max_candle_age_seconds=30,
        )
    
    def test_initial_state(self, guard):
        """Guard should start with no current cycle."""
        assert guard.current_cycle is None
        assert guard.last_completed_cycle is None
        assert guard._total_cycles == 0
    
    def test_start_cycle_success(self, guard):
        """Starting first cycle should succeed."""
        success, error = guard.start_cycle()
        
        assert success is True
        assert error is None
        assert guard.current_cycle is not None
        assert guard.current_cycle.is_complete is False
        assert guard._total_cycles == 1
    
    def test_end_cycle(self, guard):
        """Ending a cycle should mark it complete."""
        guard.start_cycle()
        cycle = guard.end_cycle()
        
        assert cycle.is_complete is True
        assert guard.current_cycle is None
        assert guard.last_completed_cycle is cycle
        assert len(guard.cycle_history) == 1
    
    def test_overlapping_cycle_prevented(self, guard):
        """Starting a cycle while previous is running should fail."""
        guard.start_cycle()
        
        # Try to start another immediately
        success, error = guard.start_cycle()
        
        assert success is False
        assert "OVERLAPPING_CYCLE" in error
        assert guard._skipped_cycles == 1
    
    def test_too_soon_prevented(self, guard):
        """Starting too soon after previous cycle should fail."""
        guard.start_cycle()
        guard.end_cycle()
        
        # Try to start immediately (within min_interval)
        success, error = guard.start_cycle()
        
        assert success is False
        assert "TOO_SOON" in error
    
    def test_stale_candle_rejected(self, guard):
        """Stale candles should be rejected."""
        guard.start_cycle()
        
        # Create a candle that's 60 seconds old (> 30 second max)
        old_timestamp = datetime.now(timezone.utc) - timedelta(seconds=60)
        
        is_fresh = guard.is_candle_fresh("BTC/USD", old_timestamp)
        
        assert is_fresh is False
    
    def test_fresh_candle_accepted(self, guard):
        """Fresh candles should be accepted."""
        guard.start_cycle()
        
        # Create a candle that's 10 seconds old
        recent_timestamp = datetime.now(timezone.utc) - timedelta(seconds=10)
        
        is_fresh = guard.is_candle_fresh("BTC/USD", recent_timestamp)
        
        assert is_fresh is True
    
    def test_duplicate_candle_rejected(self, guard):
        """Processing same candle twice should fail."""
        guard.start_cycle()
        
        timestamp = datetime.now(timezone.utc) - timedelta(seconds=5)
        
        # First time should succeed
        assert guard.is_candle_fresh("BTC/USD", timestamp) is True
        
        # Second time should fail (duplicate)
        assert guard.is_candle_fresh("BTC/USD", timestamp) is False
    
    def test_future_candle_rejected(self, guard):
        """Candles in the future should be rejected (clock skew protection)."""
        guard.start_cycle()
        
        # Create a candle that's 5 minutes in the future
        future_timestamp = datetime.now(timezone.utc) + timedelta(minutes=5)
        
        is_fresh = guard.is_candle_fresh("BTC/USD", future_timestamp)
        
        assert is_fresh is False
    
    def test_record_counters(self, guard):
        """Test cycle event recording."""
        guard.start_cycle()
        
        assert guard.current_cycle.coins_processed == 0
        guard.record_coin_processed()
        assert guard.current_cycle.coins_processed == 1
        
        guard.record_signal_generated()
        assert guard.current_cycle.signals_generated == 1
        
        guard.record_order_placed()
        assert guard.current_cycle.orders_placed == 1
        
        guard.record_order_rejected()
        assert guard.current_cycle.orders_rejected == 1
    
    def test_get_cycle_stats(self, guard):
        """Test cycle stats retrieval."""
        guard.start_cycle()
        guard.record_coin_processed()
        guard.end_cycle()
        
        stats = guard.get_cycle_stats()
        
        assert stats["total_cycles"] == 1
        assert stats["current_cycle_running"] is False
        assert stats["last_completed_at"] is not None
    
    def test_get_recent_cycles(self, guard):
        """Test recent cycle history retrieval."""
        # Create multiple cycles (waiting for min_interval)
        guard.min_interval = timedelta(seconds=0)  # Disable for test
        
        guard.start_cycle()
        guard.record_coin_processed()
        guard.record_coin_processed()
        guard.end_cycle()
        
        guard.start_cycle()
        guard.record_coin_processed()
        guard.end_cycle()
        
        recent = guard.get_recent_cycles(limit=5)
        
        assert len(recent) == 2
        assert recent[0]["coins_processed"] == 2
        assert recent[1]["coins_processed"] == 1
    
    def test_clear_candle_history(self, guard):
        """Test clearing old candle timestamps."""
        guard.start_cycle()
        
        # Process some candles
        now = datetime.now(timezone.utc)
        guard._processed_candle_timestamps["BTC/USD"] = now
        guard._processed_candle_timestamps["ETH/USD"] = now - timedelta(hours=48)  # Old
        
        # Clear old ones (older than 24h)
        guard.clear_candle_history(older_than_hours=24)
        
        assert "BTC/USD" in guard._processed_candle_timestamps
        assert "ETH/USD" not in guard._processed_candle_timestamps
    
    def test_force_complete_stale_cycle(self, guard):
        """Stale cycles should be force-completed after max duration."""
        # Create a guard with very short max duration
        short_guard = CycleGuard(
            min_cycle_interval_seconds=0,
            max_cycle_duration_seconds=1,  # 1 second max
        )
        
        short_guard.start_cycle()
        
        # Wait for cycle to become stale
        time.sleep(1.5)
        
        # Starting new cycle should force-complete the old one
        success, error = short_guard.start_cycle()
        
        assert success is True
        assert short_guard._overlapped_cycles == 1
        assert short_guard.last_completed_cycle.error == "TIMEOUT_FORCE_COMPLETED"


class TestGlobalSingleton:
    """Test global singleton functions."""
    
    def test_get_cycle_guard(self):
        """Test getting global guard instance."""
        guard1 = get_cycle_guard()
        guard2 = get_cycle_guard()
        
        # Should return same instance
        assert guard1 is guard2
    
    def test_init_cycle_guard(self):
        """Test initializing global guard with custom settings."""
        guard = init_cycle_guard(
            min_cycle_interval_seconds=120,
            max_cycle_duration_seconds=600,
        )
        
        assert guard.min_interval.total_seconds() == 120
        assert guard.max_duration.total_seconds() == 600


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
