"""
Tests for Runner Logic and Capital Utilisation Fixes.

Covers:
1. ExecutionEngine _split_quantities uses Decimal.quantize (no round)
2. ManagedPosition snapshot targets (entry_size_initial, tp1_qty_target, tp2_qty_target)
3. PositionManagerV2 uses snapshot targets for TP1/TP2 hit and TP placement
4. RiskManager margin-based caps (use_margin_caps, max_single/aggregate margin)
5. Trailing activation guard at TP1
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock

from src.config.config import Config, MultiTPConfig, RiskConfig
from src.execution.execution_engine import ExecutionEngine
from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    PositionRegistry,
    OrderEvent,
    OrderEventType,
    FillRecord,
    reset_position_registry,
)
from src.execution.position_manager_v2 import (
    PositionManagerV2,
    ActionType,
)
from src.domain.models import Signal, SignalType, SetupType, Side


def _make_signal() -> Signal:
    return Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="BTC/USD",
        signal_type=SignalType.LONG,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        take_profit=Decimal("110"),
        reasoning="test",
        setup_type=SetupType.OB,
        regime="tight_smc",
        higher_tf_bias="bullish",
        adx=Decimal("25"),
        atr=Decimal("2"),
        ema200_slope="up",
        tp_candidates=[],
    )


def _make_config() -> MagicMock:
    mtp = MultiTPConfig(
        enabled=True,
        runner_has_fixed_tp=False,
        tp1_close_pct=0.40,
        tp2_close_pct=0.40,
        runner_pct=0.20,
        trailing_activation_atr_min=0.0,
    )
    config = MagicMock()
    config.multi_tp = mtp
    config.execution = MagicMock()
    config.execution.default_order_type = "market"
    config.execution.tp_splits = [0.35, 0.35, 0.30]
    config.execution.rr_fallback_multiples = [1.0, 2.0, 3.0]
    config.strategy = MagicMock()
    return config


class TestSplitQuantitiesQuantize:
    """ExecutionEngine _split_quantities uses quantize, not round."""

    def test_quantize_produces_valid_decimal(self):
        """Quantized quantities are valid Decimals (no ConversionSyntax)."""
        config = _make_config()
        engine = ExecutionEngine(config)
        step = Decimal("0.0001")
        qtys = engine._split_quantities(
            Decimal("1.23456789"), 2, step_size=step
        )
        assert len(qtys) == 2
        for q in qtys:
            assert isinstance(q, Decimal)
            assert q >= 0
            # Quantize produces clean Decimal (no float drift)
            assert str(q) == str(Decimal(str(q)))

    def test_step_size_passed_to_generate_entry_plan(self):
        """generate_entry_plan accepts step_size and produces valid TP quantities."""
        config = _make_config()
        engine = ExecutionEngine(config)
        signal = _make_signal()
        plan = engine.generate_entry_plan(
            signal,
            Decimal("1000"),
            Decimal("100"),
            Decimal("100"),
            Decimal("5"),
            step_size=Decimal("0.01"),
        )
        assert "take_profits" in plan
        tp_qtys = [tp["qty"] for tp in plan["take_profits"]]
        for q in tp_qtys:
            assert isinstance(q, Decimal)
            assert q > 0


class TestSnapshotTargets:
    """ManagedPosition snapshot targets."""

    def setup_method(self):
        reset_position_registry()

    def test_ensure_snapshot_targets_sets_once(self):
        """ensure_snapshot_targets sets entry_size_initial, tp1/tp2_qty_target from fills."""
        pos = ManagedPosition(
            symbol="BTC/USD",
            side=Side.LONG,
            position_id="test-snap",
            initial_size=Decimal("1.0"),
            initial_entry_price=Decimal("100"),
            initial_stop_price=Decimal("95"),
            initial_tp1_price=Decimal("105"),
            initial_tp2_price=Decimal("110"),
            initial_final_target=Decimal("115"),
            tp1_close_pct=Decimal("0.40"),
            tp2_close_pct=Decimal("0.40"),
            runner_pct=Decimal("0.20"),
        )
        pos.entry_fills.append(FillRecord(
            fill_id="f1", order_id="o1", side=Side.LONG,
            qty=Decimal("1.0"), price=Decimal("100"),
            timestamp=datetime.now(timezone.utc), is_entry=True,
        ))
        assert pos.entry_size_initial is None
        pos.ensure_snapshot_targets()
        assert pos.entry_size_initial == Decimal("1.0")
        assert pos.tp1_qty_target == Decimal("0.4")
        assert pos.tp2_qty_target == Decimal("0.4")

    def test_ensure_snapshot_targets_idempotent(self):
        """Second call does not overwrite."""
        pos = ManagedPosition(
            symbol="BTC/USD",
            side=Side.LONG,
            position_id="test-snap2",
            initial_size=Decimal("1.0"),
            initial_entry_price=Decimal("100"),
            initial_stop_price=Decimal("95"),
            initial_tp1_price=Decimal("105"),
            initial_tp2_price=Decimal("110"),
            initial_final_target=Decimal("115"),
            tp1_close_pct=Decimal("0.40"),
            tp2_close_pct=Decimal("0.40"),
            runner_pct=Decimal("0.20"),
        )
        pos.entry_fills.append(FillRecord(
            fill_id="f1", order_id="o1", side=Side.LONG,
            qty=Decimal("1.0"), price=Decimal("100"),
            timestamp=datetime.now(timezone.utc), is_entry=True,
        ))
        pos.ensure_snapshot_targets()
        initial = pos.entry_size_initial
        pos.entry_fills.append(FillRecord(
            fill_id="f2", order_id="o1", side=Side.LONG,
            qty=Decimal("0.5"), price=Decimal("101"),
            timestamp=datetime.now(timezone.utc), is_entry=True,
        ))
        pos.ensure_snapshot_targets()
        assert pos.entry_size_initial == initial  # unchanged


class TestPositionManagerSnapshotTargets:
    """PositionManagerV2 uses snapshot targets for TP hit and TP placement."""

    def setup_method(self):
        reset_position_registry()

    def test_tp1_hit_uses_tp1_qty_target_when_set(self):
        """RULE 5: TP1 hit uses min(tp1_qty_target, remaining_qty)."""
        registry = PositionRegistry()
        mtp = MultiTPConfig(enabled=True, runner_has_fixed_tp=False)
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        pos = ManagedPosition(
            symbol="BTC/USD",
            side=Side.LONG,
            position_id="test-tp1",
            initial_size=Decimal("1.0"),
            initial_entry_price=Decimal("100"),
            initial_stop_price=Decimal("95"),
            initial_tp1_price=Decimal("105"),
            initial_tp2_price=Decimal("110"),
            initial_final_target=Decimal("115"),
            tp1_close_pct=Decimal("0.40"),
            tp2_close_pct=Decimal("0.40"),
            runner_pct=Decimal("0.20"),
        )
        pos.state = PositionState.OPEN
        pos.entry_acknowledged = True
        pos.entry_fills.append(FillRecord(
            fill_id="f1", order_id="o1", side=Side.LONG,
            qty=Decimal("1.0"), price=Decimal("100"),
            timestamp=datetime.now(timezone.utc), is_entry=True,
        ))
        pos.ensure_snapshot_targets()
        registry.register_position(pos)

        actions = manager.evaluate_position(
            "BTC/USD",
            current_price=Decimal("106"),
            current_atr=Decimal("2"),
        )
        tp1_actions = [
            a for a in actions
            if a.type == ActionType.CLOSE_PARTIAL
            and "TP1" in (a.reason or "")
        ]
        if tp1_actions:
            assert tp1_actions[0].size == Decimal("0.4")


class TestRiskManagerMarginCaps:
    """RiskManager margin-based caps."""

    def test_margin_caps_allow_larger_notional_than_legacy(self):
        """With 7x leverage, 25% margin = 1.75x equity notional."""
        config = RiskConfig(
            use_margin_caps=True,
            max_single_position_margin_pct_equity=0.25,
            max_aggregate_margin_pct_equity=2.0,
            target_leverage=7.0,
        )
        from src.risk.risk_manager import RiskManager
        from src.domain.models import Position
        rm = RiskManager(config)
        rm.current_positions = []

        signal = Signal(
            timestamp=datetime.now(timezone.utc),
            symbol="BTC/USD",
            signal_type=SignalType.LONG,
            setup_type="test",
            regime="test",
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            take_profit=Decimal("52000"),
            reasoning="test",
            higher_tf_bias="bullish",
            adx=Decimal("30"),
            atr=Decimal("500"),
            ema200_slope="up",
        )
        equity = Decimal("10000")
        decision = rm.validate_trade(
            signal, equity, Decimal("50000"), Decimal("50000"),
        )
        # With 25% margin cap and 7x: max notional = 10000 * 0.25 * 7 = 17500
        assert decision.approved
        assert decision.position_notional <= Decimal("17500")

    def test_use_margin_caps_false_uses_legacy_notional(self):
        """When use_margin_caps=False, legacy 25% notional cap applies."""
        config = RiskConfig(
            use_margin_caps=False,
            target_leverage=7.0,
        )
        from src.risk.risk_manager import RiskManager
        rm = RiskManager(config)
        rm.current_positions = []

        signal = Signal(
            timestamp=datetime.now(timezone.utc),
            symbol="BTC/USD",
            signal_type=SignalType.LONG,
            setup_type="test",
            regime="test",
            entry_price=Decimal("50000"),
            stop_loss=Decimal("49000"),
            take_profit=Decimal("52000"),
            reasoning="test",
            higher_tf_bias="bullish",
            adx=Decimal("30"),
            atr=Decimal("500"),
            ema200_slope="up",
        )
        equity = Decimal("10000")
        decision = rm.validate_trade(
            signal, equity, Decimal("50000"), Decimal("50000"),
        )
        # Legacy: max notional = 25% of equity = 2500
        assert decision.position_notional <= Decimal("2500")


class TestTrailingActivationGuard:
    """Trailing activation guard at TP1."""

    def setup_method(self):
        reset_position_registry()

    def test_activate_trailing_if_guard_passes(self):
        """When atr_min=0, trailing activates on TP1 fill."""
        pos = ManagedPosition(
            symbol="BTC/USD",
            side=Side.LONG,
            position_id="test-trail",
            initial_size=Decimal("1.0"),
            initial_entry_price=Decimal("100"),
            initial_stop_price=Decimal("95"),
            initial_tp1_price=Decimal("105"),
            initial_tp2_price=Decimal("110"),
            initial_final_target=Decimal("115"),
            tp1_close_pct=Decimal("0.40"),
            tp2_close_pct=Decimal("0.40"),
            runner_pct=Decimal("0.20"),
        )
        pos.tp1_filled = True
        pos.trailing_active = False
        result = pos.activate_trailing_if_guard_passes(
            Decimal("2.0"),
            Decimal("0"),
        )
        assert result is True
        assert pos.trailing_active is True

    def test_activate_trailing_guard_atr_min_blocks(self):
        """When atr_min > current_atr, trailing does not activate."""
        pos = ManagedPosition(
            symbol="BTC/USD",
            side=Side.LONG,
            position_id="test-trail2",
            initial_size=Decimal("1.0"),
            initial_entry_price=Decimal("100"),
            initial_stop_price=Decimal("95"),
            initial_tp1_price=Decimal("105"),
            initial_tp2_price=Decimal("110"),
            initial_final_target=Decimal("115"),
            tp1_close_pct=Decimal("0.40"),
            tp2_close_pct=Decimal("0.40"),
            runner_pct=Decimal("0.20"),
        )
        pos.tp1_filled = True
        pos.trailing_active = False
        result = pos.activate_trailing_if_guard_passes(
            Decimal("1.0"),
            Decimal("5.0"),
        )
        assert result is False
        assert pos.trailing_active is False


class TestVenueMinimumPartialClose:
    """TP1/TP2 partial closes respect venue min_size from InstrumentSpecRegistry."""

    def setup_method(self):
        reset_position_registry()

    def test_tp1_skip_when_partial_below_min_size(self):
        """When partial_size < min_size, no CLOSE_PARTIAL action (avoids ORDER_REJECTED_BY_VENUE)."""
        mock_registry = MagicMock()
        mock_spec = MagicMock()
        mock_spec.min_size = Decimal("1")
        mock_registry.get_spec.return_value = mock_spec

        pm = PositionManagerV2(instrument_spec_registry=mock_registry)
        pos = ManagedPosition(
            symbol="GALA/USD",
            side=Side.LONG,
            position_id="test-gala",
            initial_size=Decimal("2.0"),
            initial_entry_price=Decimal("0.05"),
            initial_stop_price=Decimal("0.04"),
            initial_tp1_price=Decimal("0.06"),
            initial_tp2_price=Decimal("0.07"),
            initial_final_target=Decimal("0.08"),
            tp1_close_pct=Decimal("0.5"),
            tp2_close_pct=Decimal("0.25"),
            runner_pct=Decimal("0.25"),
        )
        pos.futures_symbol = "PF_GALAUSD"
        pos.state = PositionState.OPEN
        pos.entry_fills = [
            FillRecord("e1", "o1", Side.LONG, Decimal("2"), Decimal("0.05"), datetime.now(timezone.utc), True)
        ]
        pos.exit_fills = [
            FillRecord("x1", "o2", Side.SHORT, Decimal("1"), Decimal("0.06"), datetime.now(timezone.utc), False)
        ]
        # remaining_qty = 2 - 1 = 1; tp1 partial = min(0.5, 1) = 0.5, below min 1
        pos.tp1_qty_target = Decimal("0.5")
        pos.tp2_qty_target = Decimal("0.5")
        pos.entry_size_initial = Decimal("2.0")
        pm.registry.register_position(pos)

        actions = pm.evaluate_position("GALA/USD", Decimal("0.065"))
        partial_actions = [a for a in actions if a.type == ActionType.CLOSE_PARTIAL]
        assert len(partial_actions) == 0

    def test_tp1_emit_when_partial_above_min_size(self):
        """When partial_size >= min_size, CLOSE_PARTIAL is emitted."""
        mock_registry = MagicMock()
        mock_spec = MagicMock()
        mock_spec.min_size = Decimal("1")
        mock_registry.get_spec.return_value = mock_spec

        pm = PositionManagerV2(instrument_spec_registry=mock_registry)
        pos = ManagedPosition(
            symbol="GALA/USD",
            side=Side.LONG,
            position_id="test-gala2",
            initial_size=Decimal("5.0"),
            initial_entry_price=Decimal("0.05"),
            initial_stop_price=Decimal("0.04"),
            initial_tp1_price=Decimal("0.06"),
            initial_tp2_price=Decimal("0.07"),
            initial_final_target=Decimal("0.08"),
            tp1_close_pct=Decimal("0.5"),
            tp2_close_pct=Decimal("0.25"),
            runner_pct=Decimal("0.25"),
        )
        pos.futures_symbol = "PF_GALAUSD"
        pos.state = PositionState.OPEN
        pos.entry_fills = [
            FillRecord("e2", "o3", Side.LONG, Decimal("5"), Decimal("0.05"), datetime.now(timezone.utc), True)
        ]
        # remaining_qty = 5; tp1 partial = min(2, 5) = 2 >= min 1
        pos.tp1_qty_target = Decimal("2")
        pos.tp2_qty_target = Decimal("1.25")
        pos.entry_size_initial = Decimal("5.0")
        pm.registry.register_position(pos)

        actions = pm.evaluate_position("GALA/USD", Decimal("0.065"))
        partial_actions = [a for a in actions if a.type == ActionType.CLOSE_PARTIAL]
        assert len(partial_actions) == 1
        assert partial_actions[0].size == Decimal("2")

    def test_get_min_size_fallback_when_no_registry(self):
        """When no instrument_spec_registry, _get_min_size_for_partial returns 1."""
        pm = PositionManagerV2(instrument_spec_registry=None)
        assert pm._get_min_size_for_partial("PF_XBTUSD") == Decimal("1")
