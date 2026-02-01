"""
Tests for DecisionAuditLogger - decision-complete logging.

These tests verify that all trading decisions are properly logged
with complete context for post-mortems.
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

from src.monitoring.decision_audit import (
    DecisionAuditLogger,
    DecisionAudit,
    get_decision_audit_logger,
)


class MockSignal:
    """Mock signal for testing."""
    def __init__(
        self,
        symbol: str,
        signal_type: str,
        score_breakdown: dict = None,
        regime: str = "bullish",
        reasoning: str = "Test signal",
    ):
        self.symbol = symbol
        self.signal_type = type('MockType', (), {'value': signal_type})()
        self.score_breakdown = score_breakdown or {"pattern": 30, "confirmation": 25}
        self.regime = regime
        self.reasoning = reasoning


class TestDecisionAudit:
    """Test DecisionAudit dataclass."""
    
    def test_basic_audit(self):
        """Test creating a basic audit record."""
        audit = DecisionAudit(
            timestamp=datetime.now(timezone.utc),
            symbol="BTC/USD",
            cycle_id="cycle_123",
            signal_type="LONG",
            signal_score=85.0,
            signal_regime="bullish",
            signal_reasoning="Strong momentum",
            decision="TRADE",
            decision_reason="Score above threshold",
        )
        
        assert audit.symbol == "BTC/USD"
        assert audit.decision == "TRADE"
        assert audit.signal_score == 85.0


class TestDecisionAuditLogger:
    """Test DecisionAuditLogger core functionality."""
    
    @pytest.fixture
    def logger(self):
        """Create a fresh logger for each test."""
        return DecisionAuditLogger(max_buffer_size=50)
    
    def test_record_trade_decision(self, logger):
        """Test recording a trade decision."""
        signal = MockSignal("BTC/USD", "LONG", reasoning="Bullish engulfing")
        
        audit = logger.record_decision(
            symbol="BTC/USD",
            cycle_id="cycle_123",
            signal=signal,
            thresholds={"min_score": 70, "max_positions": 5},
            alternatives=[],
            decision="TRADE",
            reason="Score 55 exceeds threshold 50",
            equity=Decimal("100000"),
            margin=Decimal("80000"),
            positions=["ETH/USD"],
        )
        
        assert audit.symbol == "BTC/USD"
        assert audit.decision == "TRADE"
        assert audit.signal_type == "LONG"
        assert audit.signal_score == 55.0  # 30 + 25
        assert audit.equity_at_decision == Decimal("100000")
        assert len(logger._buffer) == 1
    
    def test_record_reject_decision(self, logger):
        """Test recording a rejected decision."""
        signal = MockSignal("BTC/USD", "LONG", score_breakdown={"pattern": 20})
        
        audit = logger.record_decision(
            symbol="BTC/USD",
            cycle_id="cycle_123",
            signal=signal,
            thresholds={"min_score": 70},
            alternatives=[],
            decision="REJECT",
            reason="Score 20 below threshold 70",
            rejection_reasons=["score_below_threshold", "weak_confirmation"],
        )
        
        assert audit.decision == "REJECT"
        assert len(audit.rejection_reasons) == 2
    
    def test_record_no_signal(self, logger):
        """Test recording a no-signal decision."""
        audit = logger.record_no_signal(
            symbol="BTC/USD",
            cycle_id="cycle_123",
            reason="No pattern detected",
            regime="ranging",
            candle_count=288,
        )
        
        assert audit.decision == "SKIP"
        assert audit.signal_type == "NO_SIGNAL"
        assert "candle_count" in audit.thresholds
    
    def test_record_with_alternatives(self, logger):
        """Test recording decision with rejected alternatives."""
        signal = MockSignal("BTC/USD", "LONG")
        
        alternatives = [
            {"symbol": "ETH/USD", "score": 48, "reason": "score_below_threshold"},
            {"symbol": "SOL/USD", "score": 35, "reason": "weak_volume"},
        ]
        
        audit = logger.record_decision(
            symbol="BTC/USD",
            cycle_id="cycle_123",
            signal=signal,
            thresholds={"min_score": 50},
            alternatives=alternatives,
            decision="TRADE",
            reason="Best signal of the cycle",
        )
        
        assert len(audit.alternatives_rejected) == 2
    
    def test_update_execution_result(self, logger):
        """Test updating decision with execution result."""
        signal = MockSignal("BTC/USD", "LONG")
        
        logger.record_decision(
            symbol="BTC/USD",
            cycle_id="cycle_123",
            signal=signal,
            thresholds={},
            alternatives=[],
            decision="TRADE",
            reason="Test",
        )
        
        logger.update_execution_result(
            symbol="BTC/USD",
            result="FILLED",
            order_id="ord_abc123",
            fill_price=Decimal("45000"),
        )
        
        # Check that the buffer was updated
        audit = logger._buffer[0]
        assert audit.execution_result == "FILLED"
        assert audit.order_id == "ord_abc123"
        assert audit.fill_price == Decimal("45000")
    
    def test_update_execution_error(self, logger):
        """Test updating decision with execution error."""
        signal = MockSignal("BTC/USD", "LONG")
        
        logger.record_decision(
            symbol="BTC/USD",
            cycle_id="cycle_123",
            signal=signal,
            thresholds={},
            alternatives=[],
            decision="TRADE",
            reason="Test",
        )
        
        logger.update_execution_result(
            symbol="BTC/USD",
            result="ERROR",
            error="Insufficient margin",
        )
        
        audit = logger._buffer[0]
        assert audit.execution_result == "ERROR"
        assert audit.execution_error == "Insufficient margin"
    
    def test_get_audit_summary(self, logger):
        """Test generating audit summary."""
        # Record some decisions
        for i in range(5):
            signal = MockSignal(f"COIN{i}/USD", "LONG")
            logger.record_decision(
                symbol=f"COIN{i}/USD",
                cycle_id="cycle_123",
                signal=signal,
                thresholds={},
                alternatives=[],
                decision="TRADE" if i < 3 else "REJECT",
                reason="Test",
                rejection_reasons=["low_score"] if i >= 3 else None,
            )
        
        summary = logger.get_audit_summary(hours=24)
        
        assert summary["total_decisions"] == 5
        assert summary["trades_executed"] == 3
        assert summary["rejects"] == 2
    
    def test_get_recent_audits(self, logger):
        """Test retrieving recent audits."""
        for i in range(10):
            signal = MockSignal(f"COIN{i}/USD", "LONG")
            logger.record_decision(
                symbol=f"COIN{i}/USD",
                cycle_id="cycle_123",
                signal=signal,
                thresholds={},
                alternatives=[],
                decision="TRADE",
                reason="Test",
            )
        
        recent = logger.get_recent_audits(limit=5)
        
        assert len(recent) == 5
        assert all("symbol" in a for a in recent)
        assert all("decision" in a for a in recent)
    
    def test_get_recent_audits_filtered(self, logger):
        """Test retrieving audits filtered by symbol."""
        for symbol in ["BTC/USD", "ETH/USD", "BTC/USD", "SOL/USD"]:
            signal = MockSignal(symbol, "LONG")
            logger.record_decision(
                symbol=symbol,
                cycle_id="cycle_123",
                signal=signal,
                thresholds={},
                alternatives=[],
                decision="TRADE",
                reason="Test",
            )
        
        btc_audits = logger.get_recent_audits(limit=10, symbol="BTC/USD")
        
        assert len(btc_audits) == 2
        assert all(a["symbol"] == "BTC/USD" for a in btc_audits)
    
    def test_get_decision_trail(self, logger):
        """Test getting decision trail for a symbol."""
        # Record multiple decisions for same symbol
        for i in range(3):
            signal = MockSignal("BTC/USD", "LONG" if i % 2 == 0 else "SHORT")
            logger.record_decision(
                symbol="BTC/USD",
                cycle_id=f"cycle_{i}",
                signal=signal,
                thresholds={"iteration": i},
                alternatives=[],
                decision="TRADE" if i < 2 else "REJECT",
                reason=f"Decision {i}",
            )
        
        trail = logger.get_decision_trail("BTC/USD", limit=10)
        
        assert len(trail) == 3
        assert trail[0]["decision"] == "TRADE"
        assert trail[2]["decision"] == "REJECT"
    
    def test_null_signal_handling(self, logger):
        """Test handling of null signal."""
        audit = logger.record_decision(
            symbol="BTC/USD",
            cycle_id="cycle_123",
            signal=None,  # No signal
            thresholds={},
            alternatives=[],
            decision="SKIP",
            reason="No signal generated",
        )
        
        assert audit.signal_type == "NO_SIGNAL"
        assert audit.signal_score == 0.0
        assert audit.signal_regime == "unknown"
    
    def test_buffer_auto_flush(self, logger):
        """Test buffer auto-flush at max size."""
        # Set very small buffer
        logger._max_buffer_size = 5
        
        # Record enough to trigger flush
        with patch.object(logger, '_flush_buffer') as mock_flush:
            for i in range(6):
                signal = MockSignal(f"COIN{i}/USD", "LONG")
                logger.record_decision(
                    symbol=f"COIN{i}/USD",
                    cycle_id="cycle_123",
                    signal=signal,
                    thresholds={},
                    alternatives=[],
                    decision="TRADE",
                    reason="Test",
                )
            
            # Should have called flush once when hitting 5
            assert mock_flush.called


class TestGlobalSingleton:
    """Test global singleton functions."""
    
    def test_get_decision_audit_logger(self):
        """Test getting global logger instance."""
        logger1 = get_decision_audit_logger()
        logger2 = get_decision_audit_logger()
        
        assert logger1 is logger2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
