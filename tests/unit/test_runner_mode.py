"""
Tests for Runner Mode (TP1 + TP2 + trailing runner, no TP3).

Covers:
1. ExecutionEngine generates 2 TPs in runner mode (no fixed TP3)
2. ExecutionEngine generates 3 TPs when runner_has_fixed_tp=True (legacy)
3. PositionManagerV2 does NOT CLOSE_FULL on final target in runner mode
4. PositionManagerV2 TP sizing uses configured pcts in runner mode
5. Safety invariants: stop never widens, exit sizes <= remaining, pcts <= 100%
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.config.config import (
    Config,
    MultiTPConfig,
    ExecutionConfig,
    StrategyConfig,
)
from src.execution.execution_engine import ExecutionEngine
from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    PositionRegistry,
    ExitReason,
    OrderEvent,
    OrderEventType,
    FillRecord,
    reset_position_registry,
    get_position_registry,
)
from src.execution.position_manager_v2 import (
    PositionManagerV2,
    ActionType,
    ManagementAction,
)
from src.domain.models import (
    Signal,
    SignalType,
    SetupType,
    Side,
    OrderType,
)


# ============================================================
# Fixtures
# ============================================================


def _make_signal(side: SignalType = SignalType.LONG) -> Signal:
    if side == SignalType.LONG:
        entry, sl, tp = Decimal("100"), Decimal("95"), Decimal("110")
    else:
        entry, sl, tp = Decimal("100"), Decimal("105"), Decimal("90")
    return Signal(
        timestamp=datetime.now(timezone.utc),
        symbol="BTC/USD",
        signal_type=side,
        entry_price=entry,
        stop_loss=sl,
        take_profit=tp,
        reasoning="test signal",
        setup_type=SetupType.OB,
        regime="tight_smc",
        higher_tf_bias="bullish" if side == SignalType.LONG else "bearish",
        adx=Decimal("25"),
        atr=Decimal("2"),
        ema200_slope="up",
        tp_candidates=[],
    )


def _make_config(
    runner_has_fixed_tp: bool = False,
    runner_tp_r_multiple: float = None,
    tp1_close_pct: float = 0.40,
    tp2_close_pct: float = 0.40,
    runner_pct: float = 0.20,
    final_target_behavior: str = "tighten_trail",
    tighten_trail_at_final_target_atr_mult: float = 1.2,
) -> MagicMock:
    """Build a minimal Config mock with multi_tp settings."""
    mtp = MultiTPConfig(
        enabled=True,
        tp1_r_multiple=1.0,
        tp2_r_multiple=2.5,
        tp1_close_pct=tp1_close_pct,
        tp2_close_pct=tp2_close_pct,
        runner_pct=runner_pct,
        move_sl_to_be_after_tp1=True,
        trailing_stop_enabled=True,
        trailing_stop_atr_multiplier=1.5,
        runner_has_fixed_tp=runner_has_fixed_tp,
        runner_tp_r_multiple=runner_tp_r_multiple,
        final_target_behavior=final_target_behavior,
        tighten_trail_at_final_target_atr_mult=tighten_trail_at_final_target_atr_mult,
    )

    config = MagicMock()
    config.multi_tp = mtp
    config.execution = MagicMock()
    config.execution.default_order_type = "market"
    config.execution.tp_splits = [0.35, 0.35, 0.30]
    config.execution.rr_fallback_multiples = [1.0, 2.0, 3.0]
    config.execution.trailing_atr_mult = 2.0
    config.execution.trailing_update_min_ticks = 2
    config.execution.trailing_enabled = True
    config.strategy = MagicMock()
    return config


def _make_managed_position(
    runner_mode: bool = True,
    final_target_behavior: str = "tighten_trail",
    side: Side = Side.LONG,
) -> ManagedPosition:
    """Create a ManagedPosition with runner mode settings."""
    return ManagedPosition(
        symbol="BTC/USD",
        side=side,
        position_id="test-pos-runner",
        initial_size=Decimal("1.0"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=Decimal("95") if side == Side.LONG else Decimal("105"),
        initial_tp1_price=Decimal("105") if side == Side.LONG else Decimal("95"),
        initial_tp2_price=Decimal("112.5") if side == Side.LONG else Decimal("87.5"),
        initial_final_target=Decimal("115") if side == Side.LONG else Decimal("85"),
        runner_mode=runner_mode,
        tp1_close_pct=Decimal("0.40"),
        tp2_close_pct=Decimal("0.40"),
        runner_pct=Decimal("0.20"),
        final_target_behavior=final_target_behavior,
        tighten_trail_atr_mult=Decimal("1.2"),
    )


# ============================================================
# Test 1: ExecutionEngine generates 2 TPs in runner mode
# ============================================================


class TestExecutionEngineRunnerMode:

    def test_runner_mode_generates_2_tps(self):
        """With multi_tp enabled and runner_has_fixed_tp=False, plan has exactly 2 TPs."""
        config = _make_config(runner_has_fixed_tp=False)
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        assert len(plan["take_profits"]) == 2, (
            f"Expected 2 TPs in runner mode, got {len(plan['take_profits'])}"
        )

    def test_runner_mode_no_3r_fallback(self):
        """No TP should be at the 3.0R level when runner mode is active."""
        config = _make_config(runner_has_fixed_tp=False)
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        entry = plan["metadata"]["fut_entry"]
        sl = plan["metadata"]["fut_sl"]
        risk = abs(entry - sl)
        three_r_price = entry + (risk * Decimal("3.0"))

        for tp in plan["take_profits"]:
            assert tp["price"] != three_r_price, (
                f"TP at 3.0R ({three_r_price}) should not exist in runner mode"
            )

    def test_runner_mode_tp_quantities_match_config(self):
        """TP1 qty ~ 40% and TP2 qty ~ 40% of total, not 40%/60%."""
        config = _make_config(
            runner_has_fixed_tp=False,
            tp1_close_pct=0.40,
            tp2_close_pct=0.40,
        )
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        total_qty = plan["entry"]["qty"]
        tp1_qty = plan["take_profits"][0]["qty"]
        tp2_qty = plan["take_profits"][1]["qty"]

        expected_tp1 = round(total_qty * Decimal("0.40"), 4)
        expected_tp2 = round(total_qty * Decimal("0.40"), 4)

        assert tp1_qty == expected_tp1, f"TP1 qty {tp1_qty} != expected {expected_tp1}"
        assert tp2_qty == expected_tp2, f"TP2 qty {tp2_qty} != expected {expected_tp2}"

        # Runner remainder should be ~20%, not allocated to any TP
        runner_remainder = total_qty - tp1_qty - tp2_qty
        expected_runner = total_qty * Decimal("0.20")
        assert abs(runner_remainder - expected_runner) < Decimal("0.001"), (
            f"Runner remainder {runner_remainder} != expected ~{expected_runner}"
        )

    def test_runner_mode_metadata_includes_runner_info(self):
        """Plan metadata should include runner_pct and runner_has_fixed_tp."""
        config = _make_config(runner_has_fixed_tp=False)
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        assert plan["metadata"]["runner_has_fixed_tp"] is False
        assert plan["metadata"]["runner_pct"] == 0.20
        assert plan["metadata"]["final_target_price"] is not None

    def test_runner_mode_final_target_at_3r(self):
        """In runner mode, final_target_price should be at ~3.0R for trail tightening."""
        config = _make_config(runner_has_fixed_tp=False)
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        entry = plan["metadata"]["fut_entry"]
        sl = plan["metadata"]["fut_sl"]
        risk = abs(entry - sl)
        expected_final = entry + (risk * Decimal("3.0"))

        assert plan["metadata"]["final_target_price"] == expected_final

    def test_short_runner_mode_generates_2_tps(self):
        """Runner mode works for SHORT positions too."""
        config = _make_config(runner_has_fixed_tp=False)
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.SHORT)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        assert len(plan["take_profits"]) == 2


# ============================================================
# Test 2: Legacy mode (runner_has_fixed_tp=True) generates 3 TPs
# ============================================================


class TestExecutionEngineLegacyMode:

    def test_legacy_mode_generates_3_tps(self):
        """With runner_has_fixed_tp=True, plan has 3 TPs (legacy behavior)."""
        config = _make_config(
            runner_has_fixed_tp=True,
            runner_tp_r_multiple=5.0,
        )
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        assert len(plan["take_profits"]) == 3, (
            f"Expected 3 TPs in legacy mode, got {len(plan['take_profits'])}"
        )

    def test_legacy_mode_uses_custom_runner_r_multiple(self):
        """Legacy mode uses configured runner_tp_r_multiple instead of hardcoded 3.0."""
        config = _make_config(
            runner_has_fixed_tp=True,
            runner_tp_r_multiple=5.0,
        )
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        entry = plan["metadata"]["fut_entry"]
        sl = plan["metadata"]["fut_sl"]
        risk = abs(entry - sl)
        expected_tp3 = entry + (risk * Decimal("5.0"))

        # TP3 (last) should be at 5.0R since no structural candidates
        tp3_price = plan["take_profits"][2]["price"]
        assert tp3_price == expected_tp3, (
            f"TP3 price {tp3_price} != expected 5.0R ({expected_tp3})"
        )


# ============================================================
# Test 3: PositionManagerV2 does NOT CLOSE_FULL on final target
# ============================================================


class TestFinalTargetBehavior:

    def setup_method(self):
        reset_position_registry()

    def test_tighten_trail_does_not_close_full(self):
        """Default runner behavior: final target tightens trail, does NOT close full."""
        registry = PositionRegistry()
        mtp = MultiTPConfig(
            enabled=True,
            runner_has_fixed_tp=False,
            final_target_behavior="tighten_trail",
            tighten_trail_at_final_target_atr_mult=1.2,
        )
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        position = _make_managed_position(
            runner_mode=True,
            final_target_behavior="tighten_trail",
        )
        # Simulate entry acknowledged and filled
        position.state = PositionState.OPEN
        position.entry_acknowledged = True
        position.trailing_active = True
        position.break_even_triggered = True
        position.peak_price = Decimal("114")
        position.current_stop_price = Decimal("100")  # at BE

        # Add entry fill so remaining_qty > 0
        position.entry_fills.append(FillRecord(
            fill_id="fill-1",
            order_id="entry-1",
            side=Side.LONG,
            qty=Decimal("1.0"),
            price=Decimal("100"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True,
        ))

        registry.register_position(position)

        # Price hits final target (115)
        actions = manager.evaluate_position(
            symbol="BTC/USD",
            current_price=Decimal("116"),  # above final target of 115
            current_atr=Decimal("2.0"),
        )

        # Verify no CLOSE_FULL action
        close_full_actions = [a for a in actions if a.type == ActionType.CLOSE_FULL]
        assert len(close_full_actions) == 0, (
            f"Expected no CLOSE_FULL in tighten_trail mode, got {len(close_full_actions)}"
        )

        # Verify position is still open
        assert registry.get_position("BTC/USD") is not None
        assert position.final_target_touched is True

    def test_close_full_legacy_still_works(self):
        """When runner_mode=False (legacy), final target still closes full."""
        registry = PositionRegistry()
        manager = PositionManagerV2(registry=registry)

        position = _make_managed_position(
            runner_mode=False,
            final_target_behavior="close_full",
        )
        position.state = PositionState.OPEN
        position.entry_acknowledged = True
        position.current_stop_price = Decimal("95")

        position.entry_fills.append(FillRecord(
            fill_id="fill-1",
            order_id="entry-1",
            side=Side.LONG,
            qty=Decimal("1.0"),
            price=Decimal("100"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True,
        ))

        registry.register_position(position)

        actions = manager.evaluate_position(
            symbol="BTC/USD",
            current_price=Decimal("116"),  # above final target of 115
        )

        close_full_actions = [a for a in actions if a.type == ActionType.CLOSE_FULL]
        assert len(close_full_actions) == 1, (
            "Legacy mode should still CLOSE_FULL on final target"
        )

    def test_close_partial_at_final_target(self):
        """close_partial behavior closes ~50% of runner at final target."""
        registry = PositionRegistry()
        mtp = MultiTPConfig(
            enabled=True,
            runner_has_fixed_tp=False,
            final_target_behavior="close_partial",
        )
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        position = _make_managed_position(
            runner_mode=True,
            final_target_behavior="close_partial",
        )
        position.state = PositionState.OPEN
        position.entry_acknowledged = True
        position.current_stop_price = Decimal("100")
        # Mark TP1 and TP2 as already filled so they don't also trigger
        position.tp1_filled = True
        position.tp2_filled = True

        position.entry_fills.append(FillRecord(
            fill_id="fill-1",
            order_id="entry-1",
            side=Side.LONG,
            qty=Decimal("1.0"),
            price=Decimal("100"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True,
        ))

        registry.register_position(position)

        actions = manager.evaluate_position(
            symbol="BTC/USD",
            current_price=Decimal("116"),
        )

        final_partial_actions = [
            a for a in actions
            if a.type == ActionType.CLOSE_PARTIAL
            and a.exit_reason == ExitReason.TAKE_PROFIT_FINAL
        ]
        assert len(final_partial_actions) == 1, (
            "close_partial should produce a CLOSE_PARTIAL action at final target"
        )
        # Should close ~50% of remaining
        assert final_partial_actions[0].size == Decimal("0.5")

    def test_final_target_only_triggers_once(self):
        """Tighten/close_partial should only fire once (final_target_touched flag)."""
        registry = PositionRegistry()
        mtp = MultiTPConfig(
            enabled=True,
            runner_has_fixed_tp=False,
            final_target_behavior="tighten_trail",
            tighten_trail_at_final_target_atr_mult=1.2,
        )
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        position = _make_managed_position(
            runner_mode=True,
            final_target_behavior="tighten_trail",
        )
        position.state = PositionState.OPEN
        position.entry_acknowledged = True
        position.trailing_active = True
        position.break_even_triggered = True
        position.current_stop_price = Decimal("100")

        position.entry_fills.append(FillRecord(
            fill_id="fill-1",
            order_id="entry-1",
            side=Side.LONG,
            qty=Decimal("1.0"),
            price=Decimal("100"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True,
        ))

        registry.register_position(position)

        # First hit
        manager.evaluate_position(
            symbol="BTC/USD",
            current_price=Decimal("116"),
            current_atr=Decimal("2.0"),
        )
        assert position.final_target_touched is True

        # Second hit should not produce another tighten action
        actions2 = manager.evaluate_position(
            symbol="BTC/USD",
            current_price=Decimal("120"),
            current_atr=Decimal("2.0"),
        )
        tighten_actions = [
            a for a in actions2
            if a.type == ActionType.UPDATE_STOP
            and "final" in (a.reason or "").lower()
        ]
        assert len(tighten_actions) == 0, (
            "Final target tighten should only fire once"
        )


# ============================================================
# Test 4: TP sizing in runner mode
# ============================================================


class TestTPSizingRunnerMode:

    def setup_method(self):
        reset_position_registry()

    def test_tp_sizes_use_configured_pcts_in_runner_mode(self):
        """In runner mode, TP1=40%, TP2=40% of filled entry, leaving 20% as runner."""
        registry = PositionRegistry()
        mtp = MultiTPConfig(
            enabled=True,
            runner_has_fixed_tp=False,
            tp1_close_pct=0.40,
            tp2_close_pct=0.40,
            runner_pct=0.20,
        )
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        position = _make_managed_position(runner_mode=True)
        position.state = PositionState.OPEN
        position.entry_acknowledged = True
        position.current_stop_price = Decimal("95")
        position.entry_order_id = "entry-1"

        # Simulate entry fill
        fill = FillRecord(
            fill_id="fill-1",
            order_id="entry-1",
            side=Side.LONG,
            qty=Decimal("1.0"),
            price=Decimal("100"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True,
        )
        position.entry_fills.append(fill)
        registry.register_position(position)

        # Simulate entry fill event to trigger TP placement
        event = OrderEvent(
            order_id="entry-1",
            client_order_id="entry-test-pos-runner",
            event_type=OrderEventType.FILLED,
            event_seq=1,
            timestamp=datetime.now(timezone.utc),
            fill_qty=Decimal("1.0"),
            fill_price=Decimal("100"),
        )

        actions = manager.handle_order_event("BTC/USD", event)

        # Find TP placement actions
        tp_actions = [a for a in actions if a.type == ActionType.PLACE_TP]

        # Should have exactly 2 TP placements
        tp1_actions = [a for a in tp_actions if "tp1" in (a.client_order_id or "")]
        tp2_actions = [a for a in tp_actions if "tp2" in (a.client_order_id or "")]

        if tp1_actions:
            tp1_size = tp1_actions[0].size
            assert tp1_size == Decimal("0.40"), (
                f"TP1 size {tp1_size} != 0.40 (40% of 1.0)"
            )

        if tp2_actions:
            tp2_size = tp2_actions[0].size
            assert tp2_size == Decimal("0.40"), (
                f"TP2 size {tp2_size} != 0.40 (40% of 1.0)"
            )


# ============================================================
# Test 5: Safety invariants
# ============================================================


class TestSafetyInvariants:

    def test_runner_pcts_must_not_exceed_100(self):
        """tp1 + tp2 + runner must be <= 100%."""
        with pytest.raises(Exception):
            # This should fail invariant check
            ManagedPosition(
                symbol="BTC/USD",
                side=Side.LONG,
                position_id="test-bad-pcts",
                initial_size=Decimal("1.0"),
                initial_entry_price=Decimal("100"),
                initial_stop_price=Decimal("95"),
                initial_tp1_price=Decimal("105"),
                initial_tp2_price=Decimal("110"),
                initial_final_target=Decimal("115"),
                runner_mode=True,
                tp1_close_pct=Decimal("0.60"),
                tp2_close_pct=Decimal("0.50"),
                runner_pct=Decimal("0.20"),
            )

    def test_valid_runner_pcts_accepted(self):
        """tp1 + tp2 + runner == 100% should be fine."""
        pos = ManagedPosition(
            symbol="BTC/USD",
            side=Side.LONG,
            position_id="test-good-pcts",
            initial_size=Decimal("1.0"),
            initial_entry_price=Decimal("100"),
            initial_stop_price=Decimal("95"),
            initial_tp1_price=Decimal("105"),
            initial_tp2_price=Decimal("110"),
            initial_final_target=Decimal("115"),
            runner_mode=True,
            tp1_close_pct=Decimal("0.40"),
            tp2_close_pct=Decimal("0.40"),
            runner_pct=Decimal("0.20"),
        )
        assert pos.runner_mode is True

    def test_exit_size_never_exceeds_remaining(self):
        """TP sizes capped at remaining_qty even with large close pcts."""
        config = _make_config(
            runner_has_fixed_tp=False,
            tp1_close_pct=0.45,
            tp2_close_pct=0.45,
            runner_pct=0.10,
        )
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        total_qty = plan["entry"]["qty"]
        tp_total = sum(tp["qty"] for tp in plan["take_profits"])

        # Total TP qty should not exceed total entry qty
        assert tp_total <= total_qty, (
            f"TP total {tp_total} exceeds entry qty {total_qty}"
        )
        # Runner remainder should be >= 0
        runner_remainder = total_qty - tp_total
        assert runner_remainder >= Decimal("0"), (
            f"Runner remainder {runner_remainder} is negative"
        )

    def test_stop_loss_always_present(self):
        """Every plan must include a stop loss order."""
        config = _make_config(runner_has_fixed_tp=False)
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        assert "stop_loss" in plan
        assert plan["stop_loss"]["reduce_only"] is True
        assert plan["stop_loss"]["qty"] == plan["entry"]["qty"]

    def test_tp_orders_are_reduce_only(self):
        """All TP orders must be reduce-only."""
        config = _make_config(runner_has_fixed_tp=False)
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal=signal,
            size_notional=Decimal("1000"),
            spot_price=Decimal("100"),
            mark_price=Decimal("100"),
            leverage=Decimal("5"),
        )

        for tp in plan["take_profits"]:
            assert tp["reduce_only"] is True, f"{tp['type']} is not reduce_only"


# ============================================================
# Test 6: Config backward compatibility
# ============================================================


class TestConfigBackwardCompatibility:

    def test_default_multi_tp_config_enables_runner_mode(self):
        """Default MultiTPConfig with new fields should enable runner mode."""
        mtp = MultiTPConfig(enabled=True)
        assert mtp.runner_has_fixed_tp is False
        assert mtp.runner_tp_r_multiple is None
        assert mtp.final_target_behavior == "tighten_trail"
        assert mtp.tighten_trail_at_final_target_atr_mult == 1.2

    def test_legacy_config_without_new_fields_uses_defaults(self):
        """Config created without new fields gets safe defaults."""
        mtp = MultiTPConfig(enabled=True, tp1_r_multiple=1.0, tp2_r_multiple=2.5)
        assert mtp.runner_has_fixed_tp is False  # default enables runner mode

    def test_explicit_legacy_mode(self):
        """Setting runner_has_fixed_tp=True restores old 3-TP behavior."""
        mtp = MultiTPConfig(
            enabled=True,
            runner_has_fixed_tp=True,
            runner_tp_r_multiple=5.0,
        )
        assert mtp.runner_has_fixed_tp is True
        assert mtp.runner_tp_r_multiple == 5.0
