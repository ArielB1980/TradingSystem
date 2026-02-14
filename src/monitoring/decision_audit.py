"""
DecisionAuditLogger: Complete decision context logging for post-mortems.

Logs not just what happened, but WHY it happened:
- Signal snapshot (score, regime, reasoning)
- Thresholds applied
- Alternatives considered and rejected
- Final decision and reason
- Execution outcome

Every trade (and non-trade) should be fully explainable.

Usage:
    audit_logger = DecisionAuditLogger()
    
    # Record a decision
    audit = audit_logger.record_decision(
        symbol="BTC/USD",
        cycle_id="cycle_20260201_1042",
        signal=signal,
        thresholds={"min_score": 75, "max_positions": 5},
        alternatives=[{"symbol": "ETH/USD", "score": 72, "reason": "score_below_threshold"}],
        decision="TRADE",
        reason="Score 85 exceeds threshold, position slot available",
        equity=Decimal("10000"),
        margin=Decimal("8000"),
        positions=["SOL/USD"],
    )
    
    # Update with execution result
    audit_logger.update_execution_result(
        symbol="BTC/USD",
        result="FILLED",
        order_id="ord_123",
        fill_price=Decimal("45000"),
    )
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Dict, List, Any, Optional
import json

from src.exceptions import OperationalError, DataError
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DecisionAudit:
    """Complete record of a trading decision."""
    
    # Context
    timestamp: datetime
    symbol: str
    cycle_id: str
    
    # Signal snapshot
    signal_type: str
    signal_score: float
    signal_regime: str
    signal_reasoning: str
    score_breakdown: Dict[str, float] = field(default_factory=dict)
    
    # Market context
    spot_price: Optional[Decimal] = None
    mark_price: Optional[Decimal] = None
    
    # Thresholds applied
    thresholds: Dict[str, Any] = field(default_factory=dict)
    
    # Alternatives considered
    alternatives_rejected: List[Dict[str, Any]] = field(default_factory=list)
    
    # Final decision
    decision: str = "UNKNOWN"  # "TRADE", "REJECT", "SKIP"
    decision_reason: str = ""
    rejection_reasons: List[str] = field(default_factory=list)
    
    # Execution outcome (if TRADE)
    execution_result: Optional[str] = None
    order_id: Optional[str] = None
    fill_price: Optional[Decimal] = None
    execution_error: Optional[str] = None
    
    # Position context
    existing_positions: List[str] = field(default_factory=list)
    existing_position_count: int = 0
    equity_at_decision: Optional[Decimal] = None
    margin_available: Optional[Decimal] = None
    
    # Invariant state
    system_state: Optional[str] = None
    active_violations: List[str] = field(default_factory=list)


class DecisionAuditLogger:
    """
    Logs complete decision context for post-mortems.
    
    Every trade (and non-trade) should be fully explainable.
    Answers the question: "Why did the system do (or not do) this trade?"
    """
    
    def __init__(self, max_buffer_size: int = 100):
        """
        Initialize decision audit logger.
        
        Args:
            max_buffer_size: Maximum audits to buffer before auto-flush
        """
        self._buffer: List[DecisionAudit] = []
        self._max_buffer_size = max_buffer_size
        self._total_audits = 0
        
        logger.info("DecisionAuditLogger initialized", max_buffer_size=max_buffer_size)
    
    def record_decision(
        self,
        symbol: str,
        cycle_id: str,
        signal: Any,  # Signal object
        thresholds: Dict[str, Any],
        alternatives: List[Dict[str, Any]],
        decision: str,
        reason: str,
        rejection_reasons: Optional[List[str]] = None,
        spot_price: Optional[Decimal] = None,
        mark_price: Optional[Decimal] = None,
        equity: Optional[Decimal] = None,
        margin: Optional[Decimal] = None,
        positions: Optional[List[str]] = None,
        system_state: Optional[str] = None,
        active_violations: Optional[List[str]] = None,
    ) -> DecisionAudit:
        """
        Record a complete trading decision.
        
        This should be called for EVERY signal, whether traded or not.
        
        Args:
            symbol: Trading symbol
            cycle_id: Current cycle identifier
            signal: Signal object (can be None for NO_SIGNAL)
            thresholds: Dict of thresholds that were applied
            alternatives: List of alternative signals that were rejected
            decision: Final decision ("TRADE", "REJECT", "SKIP")
            reason: Human-readable reason for the decision
            rejection_reasons: List of rejection reasons (if applicable)
            spot_price: Current spot price
            mark_price: Current mark price
            equity: Current account equity
            margin: Available margin
            positions: List of existing position symbols
            system_state: Current system state (ACTIVE, DEGRADED, etc.)
            active_violations: List of active invariant violations
            
        Returns:
            DecisionAudit record
        """
        now = datetime.now(timezone.utc)
        
        # Extract signal info safely
        signal_type = "NO_SIGNAL"
        signal_score = 0.0
        signal_regime = "unknown"
        signal_reasoning = ""
        score_breakdown = {}
        
        if signal:
            signal_type = getattr(signal, 'signal_type', None)
            if hasattr(signal_type, 'value'):
                signal_type = signal_type.value
            signal_type = str(signal_type) if signal_type else "NO_SIGNAL"
            
            score_breakdown = getattr(signal, 'score_breakdown', {}) or {}
            signal_score = sum(float(v) for v in score_breakdown.values()) if score_breakdown else 0.0
            signal_regime = getattr(signal, 'regime', 'unknown') or 'unknown'
            signal_reasoning = getattr(signal, 'reasoning', '') or ''
        
        audit = DecisionAudit(
            timestamp=now,
            symbol=symbol,
            cycle_id=cycle_id,
            signal_type=signal_type,
            signal_score=signal_score,
            signal_regime=signal_regime,
            signal_reasoning=signal_reasoning,
            score_breakdown=score_breakdown,
            spot_price=spot_price,
            mark_price=mark_price,
            thresholds=thresholds,
            alternatives_rejected=alternatives,
            decision=decision,
            decision_reason=reason,
            rejection_reasons=rejection_reasons or [],
            existing_positions=positions or [],
            existing_position_count=len(positions) if positions else 0,
            equity_at_decision=equity,
            margin_available=margin,
            system_state=system_state,
            active_violations=active_violations or [],
        )
        
        self._buffer.append(audit)
        self._total_audits += 1
        
        # Log structured decision for immediate visibility
        log_data = {
            "symbol": symbol,
            "decision": decision,
            "reason": reason,
            "signal_type": signal_type,
            "signal_score": signal_score,
            "regime": signal_regime,
            "equity": str(equity) if equity else None,
            "margin": str(margin) if margin else None,
            "positions": positions,
            "alternatives_count": len(alternatives),
        }
        
        if rejection_reasons:
            log_data["rejection_reasons"] = rejection_reasons
        
        if decision == "TRADE":
            logger.info("DECISION_TRADE", **log_data)
        elif decision == "REJECT":
            logger.info("DECISION_REJECT", **log_data)
        else:
            logger.debug("DECISION_SKIP", **log_data)
        
        # Auto-flush if buffer is full
        if len(self._buffer) >= self._max_buffer_size:
            self._flush_buffer()
        
        return audit
    
    def record_no_signal(
        self,
        symbol: str,
        cycle_id: str,
        reason: str,
        regime: str = "unknown",
        candle_count: int = 0,
        spot_price: Optional[Decimal] = None,
    ) -> DecisionAudit:
        """
        Record a NO_SIGNAL decision (convenience method).
        
        Args:
            symbol: Trading symbol
            cycle_id: Current cycle identifier
            reason: Why no signal was generated
            regime: Current market regime
            candle_count: Number of candles available
            spot_price: Current spot price
            
        Returns:
            DecisionAudit record
        """
        return self.record_decision(
            symbol=symbol,
            cycle_id=cycle_id,
            signal=None,
            thresholds={"candle_count": candle_count},
            alternatives=[],
            decision="SKIP",
            reason=reason,
            spot_price=spot_price,
        )
    
    def update_execution_result(
        self,
        symbol: str,
        result: str,
        order_id: Optional[str] = None,
        fill_price: Optional[Decimal] = None,
        error: Optional[str] = None,
    ):
        """
        Update the most recent decision with execution result.
        
        Args:
            symbol: Trading symbol
            result: Execution result ("FILLED", "REJECTED", "ERROR", etc.)
            order_id: Exchange order ID if available
            fill_price: Actual fill price if filled
            error: Error message if failed
        """
        # Find most recent audit for this symbol
        for audit in reversed(self._buffer):
            if audit.symbol == symbol and audit.execution_result is None:
                audit.execution_result = result
                audit.order_id = order_id
                audit.fill_price = fill_price
                audit.execution_error = error
                
                logger.info(
                    "DECISION_EXECUTION_UPDATE",
                    symbol=symbol,
                    result=result,
                    order_id=order_id,
                    fill_price=str(fill_price) if fill_price else None,
                    error=error,
                )
                break
    
    def get_audit_summary(self, hours: int = 24) -> Dict[str, Any]:
        """
        Get summary of recent decisions for debugging.
        
        Args:
            hours: Number of hours to look back
            
        Returns:
            Summary dict with decision counts and patterns
        """
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        recent = [a for a in self._buffer if a.timestamp > since]
        
        if not recent:
            return {"total_decisions": 0, "note": "No decisions in time window"}
        
        trades = [a for a in recent if a.decision == "TRADE"]
        rejects = [a for a in recent if a.decision == "REJECT"]
        skips = [a for a in recent if a.decision == "SKIP"]
        
        return {
            "period_hours": hours,
            "total_decisions": len(recent),
            "trades_executed": len(trades),
            "rejects": len(rejects),
            "skips": len(skips),
            "trade_rate_pct": round(100 * len(trades) / len(recent), 1) if recent else 0,
            "by_symbol": self._count_by_symbol(recent),
            "by_regime": self._count_by_key(recent, "signal_regime"),
            "common_reject_reasons": self._get_common_reasons(rejects),
            "avg_score_traded": self._avg_score(trades),
            "avg_score_rejected": self._avg_score(rejects),
        }
    
    def get_recent_audits(self, limit: int = 20, symbol: Optional[str] = None) -> List[Dict]:
        """
        Get recent audit records for debugging.
        
        Args:
            limit: Maximum number of records
            symbol: Optional symbol filter
            
        Returns:
            List of audit dicts
        """
        audits = self._buffer
        if symbol:
            audits = [a for a in audits if a.symbol == symbol]
        
        return [
            {
                "timestamp": a.timestamp.isoformat(),
                "symbol": a.symbol,
                "decision": a.decision,
                "reason": a.decision_reason,
                "signal_type": a.signal_type,
                "signal_score": a.signal_score,
                "regime": a.signal_regime,
                "execution_result": a.execution_result,
                "order_id": a.order_id,
            }
            for a in audits[-limit:]
        ]
    
    def get_decision_trail(self, symbol: str, limit: int = 10) -> List[Dict]:
        """
        Get decision trail for a specific symbol.
        
        Useful for understanding why a symbol was/wasn't traded.
        
        Args:
            symbol: Trading symbol
            limit: Maximum number of decisions
            
        Returns:
            List of decision dicts in chronological order
        """
        symbol_audits = [a for a in self._buffer if a.symbol == symbol][-limit:]
        
        return [
            {
                "timestamp": a.timestamp.isoformat(),
                "decision": a.decision,
                "reason": a.decision_reason,
                "signal_type": a.signal_type,
                "score": a.signal_score,
                "regime": a.signal_regime,
                "thresholds": a.thresholds,
                "execution_result": a.execution_result,
            }
            for a in symbol_audits
        ]
    
    def _flush_buffer(self):
        """Persist buffered audits to storage (async)."""
        if not self._buffer:
            return
        
        try:
            from src.storage.repository import record_event
            
            # Persist each audit as a DECISION_AUDIT event
            for audit in self._buffer:
                # Convert to dict, handling Decimal serialization
                audit_dict = {
                    "timestamp": audit.timestamp.isoformat(),
                    "symbol": audit.symbol,
                    "cycle_id": audit.cycle_id,
                    "signal_type": audit.signal_type,
                    "signal_score": audit.signal_score,
                    "signal_regime": audit.signal_regime,
                    "signal_reasoning": audit.signal_reasoning,
                    "score_breakdown": audit.score_breakdown,
                    "spot_price": str(audit.spot_price) if audit.spot_price else None,
                    "mark_price": str(audit.mark_price) if audit.mark_price else None,
                    "thresholds": audit.thresholds,
                    "alternatives_count": len(audit.alternatives_rejected),
                    "decision": audit.decision,
                    "decision_reason": audit.decision_reason,
                    "rejection_reasons": audit.rejection_reasons,
                    "execution_result": audit.execution_result,
                    "order_id": audit.order_id,
                    "fill_price": str(audit.fill_price) if audit.fill_price else None,
                    "existing_position_count": audit.existing_position_count,
                    "equity": str(audit.equity_at_decision) if audit.equity_at_decision else None,
                    "margin": str(audit.margin_available) if audit.margin_available else None,
                    "system_state": audit.system_state,
                }
                
                record_event(
                    event_type="DECISION_AUDIT",
                    symbol=audit.symbol,
                    details=audit_dict,
                    timestamp=audit.timestamp,
                )
            
            logger.debug("Decision audits flushed", count=len(self._buffer))
            self._buffer.clear()
            
        except (OperationalError, DataError, OSError, ValueError, TypeError, KeyError) as e:
            logger.error("Failed to flush decision audits", error=str(e), error_type=type(e).__name__)
    
    def _count_by_symbol(self, audits: List[DecisionAudit]) -> Dict[str, int]:
        """Count decisions by symbol."""
        counts: Dict[str, int] = {}
        for a in audits:
            counts[a.symbol] = counts.get(a.symbol, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: -x[1])[:10])
    
    def _count_by_key(self, audits: List[DecisionAudit], key: str) -> Dict[str, int]:
        """Count decisions by attribute."""
        counts: Dict[str, int] = {}
        for a in audits:
            val = getattr(a, key, "unknown")
            counts[val] = counts.get(val, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: -x[1]))
    
    def _get_common_reasons(self, audits: List[DecisionAudit], limit: int = 5) -> Dict[str, int]:
        """Get common rejection reasons."""
        reasons: Dict[str, int] = {}
        for a in audits:
            for reason in a.rejection_reasons:
                reasons[reason] = reasons.get(reason, 0) + 1
            # Also count decision_reason
            if a.decision_reason:
                # Truncate long reasons
                short_reason = a.decision_reason[:50]
                reasons[short_reason] = reasons.get(short_reason, 0) + 1
        return dict(sorted(reasons.items(), key=lambda x: -x[1])[:limit])
    
    def _avg_score(self, audits: List[DecisionAudit]) -> float:
        """Calculate average signal score."""
        if not audits:
            return 0.0
        return round(sum(a.signal_score for a in audits) / len(audits), 1)


# ===== GLOBAL SINGLETON =====
_decision_audit_logger: Optional[DecisionAuditLogger] = None


def get_decision_audit_logger() -> DecisionAuditLogger:
    """Get global decision audit logger instance."""
    global _decision_audit_logger
    if _decision_audit_logger is None:
        _decision_audit_logger = DecisionAuditLogger()
    return _decision_audit_logger
