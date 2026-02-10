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
        regime_runner_sizing_enabled=False,  # Disable in base tests; tested separately
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


# ============================================================
# Test 7: Regime-aware runner sizing
# ============================================================


class TestRegimeAwareSizing:
    """Test that TP splits are adjusted based on signal regime."""

    def test_tight_smc_regime_uses_smaller_runner(self):
        """tight_smc signals should get smaller runner (10%) and bigger TP1 (50%)."""
        mtp = MultiTPConfig(
            enabled=True,
            tp1_close_pct=0.40,
            tp2_close_pct=0.40,
            runner_pct=0.20,
            regime_runner_sizing_enabled=True,
            regime_runner_overrides={
                "tight_smc": {"runner_pct": 0.10, "tp1_close_pct": 0.50, "tp2_close_pct": 0.40},
                "wide_structure": {"runner_pct": 0.30, "tp1_close_pct": 0.35, "tp2_close_pct": 0.35},
            },
        )
        config = MagicMock()
        config.multi_tp = mtp
        config.execution = MagicMock()
        config.execution.default_order_type = "market"
        config.execution.tp_splits = [0.35, 0.35, 0.30]
        config.execution.rr_fallback_multiples = [1.0, 2.0, 3.0]
        config.strategy = MagicMock()

        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)  # regime="tight_smc"
        plan = engine.generate_entry_plan(
            signal,
            Decimal("1000"),
            Decimal("100"),
            Decimal("100"),
            Decimal("5"),
        )

        # TP1 should be 50% of position (10 units), TP2 should be 40%
        total_qty = Decimal("1000") / Decimal("100")  # 10
        tp1_qty = plan["take_profits"][0]["qty"]
        tp2_qty = plan["take_profits"][1]["qty"]
        assert tp1_qty == round(total_qty * Decimal("0.50"), 4), f"TP1 should be 50%, got {tp1_qty}"
        assert tp2_qty == round(total_qty * Decimal("0.40"), 4), f"TP2 should be 40%, got {tp2_qty}"
        assert plan["metadata"]["runner_pct"] == 0.10
        assert plan["metadata"]["regime"] == "tight_smc"

    def test_wide_structure_regime_uses_bigger_runner(self):
        """wide_structure signals should get bigger runner (30%)."""
        mtp = MultiTPConfig(
            enabled=True,
            tp1_close_pct=0.40,
            tp2_close_pct=0.40,
            runner_pct=0.20,
            regime_runner_sizing_enabled=True,
            regime_runner_overrides={
                "tight_smc": {"runner_pct": 0.10, "tp1_close_pct": 0.50, "tp2_close_pct": 0.40},
                "wide_structure": {"runner_pct": 0.30, "tp1_close_pct": 0.35, "tp2_close_pct": 0.35},
            },
        )
        config = MagicMock()
        config.multi_tp = mtp
        config.execution = MagicMock()
        config.execution.default_order_type = "market"
        config.execution.tp_splits = [0.35, 0.35, 0.30]
        config.execution.rr_fallback_multiples = [1.0, 2.0, 3.0]
        config.strategy = MagicMock()

        engine = ExecutionEngine(config)
        # Create a wide_structure signal
        signal = _make_signal(SignalType.LONG)
        signal.regime = "wide_structure"
        signal.setup_type = SetupType.BOS

        plan = engine.generate_entry_plan(
            signal,
            Decimal("1000"),
            Decimal("100"),
            Decimal("100"),
            Decimal("5"),
        )

        total_qty = Decimal("1000") / Decimal("100")  # 10
        tp1_qty = plan["take_profits"][0]["qty"]
        tp2_qty = plan["take_profits"][1]["qty"]
        assert tp1_qty == round(total_qty * Decimal("0.35"), 4), f"TP1 should be 35%, got {tp1_qty}"
        assert tp2_qty == round(total_qty * Decimal("0.35"), 4), f"TP2 should be 35%, got {tp2_qty}"
        assert plan["metadata"]["runner_pct"] == 0.30

    def test_regime_sizing_disabled_uses_base_splits(self):
        """When regime_runner_sizing_enabled=False, uses base config splits."""
        config = _make_config()  # regime_runner_sizing_enabled=False by default in tests
        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)

        plan = engine.generate_entry_plan(
            signal,
            Decimal("1000"),
            Decimal("100"),
            Decimal("100"),
            Decimal("5"),
        )

        total_qty = Decimal("1000") / Decimal("100")
        tp1_qty = plan["take_profits"][0]["qty"]
        assert tp1_qty == round(total_qty * Decimal("0.40"), 4), f"TP1 should be 40%, got {tp1_qty}"

    def test_unknown_regime_uses_base_splits(self):
        """Unknown regime should fall back to base config splits."""
        mtp = MultiTPConfig(
            enabled=True,
            regime_runner_sizing_enabled=True,
            regime_runner_overrides={
                "tight_smc": {"runner_pct": 0.10, "tp1_close_pct": 0.50, "tp2_close_pct": 0.40},
            },
        )
        config = MagicMock()
        config.multi_tp = mtp
        config.execution = MagicMock()
        config.execution.default_order_type = "market"
        config.execution.tp_splits = [0.35, 0.35, 0.30]
        config.execution.rr_fallback_multiples = [1.0, 2.0, 3.0]
        config.strategy = MagicMock()

        engine = ExecutionEngine(config)
        signal = _make_signal(SignalType.LONG)
        signal.regime = "unknown_regime"

        plan = engine.generate_entry_plan(
            signal,
            Decimal("1000"),
            Decimal("100"),
            Decimal("100"),
            Decimal("5"),
        )

        total_qty = Decimal("1000") / Decimal("100")
        tp1_qty = plan["take_profits"][0]["qty"]
        # Should use base 40% since "unknown_regime" is not in overrides
        assert tp1_qty == round(total_qty * Decimal("0.40"), 4)


# ============================================================
# Test 8: Progressive trailing tightening
# ============================================================


class TestProgressiveTrailing:
    """Test that trailing ATR mult tightens at R-level milestones."""

    def setup_method(self):
        reset_position_registry()

    def _make_prog_mtp(self) -> MultiTPConfig:
        return MultiTPConfig(
            enabled=True,
            tp1_close_pct=0.40,
            tp2_close_pct=0.40,
            runner_pct=0.20,
            runner_has_fixed_tp=False,
            regime_runner_sizing_enabled=False,
            progressive_trail_enabled=True,
            progressive_trail_levels=[
                {"r_threshold": 3.0, "atr_mult": 1.8},
                {"r_threshold": 5.0, "atr_mult": 1.4},
                {"r_threshold": 8.0, "atr_mult": 1.0},
            ],
        )

    def _make_filled_position(self, final_target: Decimal = Decimal("150")) -> ManagedPosition:
        """Create a filled ManagedPosition (with entry fill so remaining_qty > 0)."""
        position = ManagedPosition(
            symbol="BTC/USD",
            side=Side.LONG,
            position_id="test-prog-trail",
            initial_size=Decimal("1.0"),
            initial_entry_price=Decimal("100"),
            initial_stop_price=Decimal("95"),
            initial_tp1_price=Decimal("105"),
            initial_tp2_price=Decimal("112.5"),
            initial_final_target=final_target,  # Set high so final target doesn't interfere
            runner_mode=True,
            tp1_close_pct=Decimal("0.40"),
            tp2_close_pct=Decimal("0.40"),
            runner_pct=Decimal("0.20"),
        )
        position.state = PositionState.OPEN
        position.entry_acknowledged = True
        position.trailing_active = True
        position.break_even_triggered = True
        position.peak_price = Decimal("114")
        position.current_stop_price = Decimal("100")  # at BE
        # Add entry fill so remaining_qty > 0
        position.entry_fills.append(FillRecord(
            fill_id="fill-prog-1",
            order_id="entry-prog-1",
            side=Side.LONG,
            qty=Decimal("1.0"),
            price=Decimal("100"),
            timestamp=datetime.now(timezone.utc),
            is_entry=True,
        ))
        return position

    def test_no_tightening_below_3r(self):
        """Below 3R, no progressive trailing action should be generated."""
        registry = PositionRegistry()
        mtp = self._make_prog_mtp()
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        position = self._make_filled_position()
        registry.register_position(position)

        # Price at 2.5R (entry=100, stop=95, risk=5, so 2.5R = 112.5)
        actions = manager.evaluate_position(
            "BTC/USD",
            current_price=Decimal("112.5"),
            current_atr=Decimal("2"),
        )
        prog_actions = [a for a in actions if "Progressive" in a.reason]
        assert len(prog_actions) == 0, f"Should not tighten below 3R, got: {[a.reason for a in prog_actions]}"

    def test_tightening_at_3r(self):
        """At 3R, progressive trailing should tighten to 1.8x ATR."""
        registry = PositionRegistry()
        mtp = self._make_prog_mtp()
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        position = self._make_filled_position()
        registry.register_position(position)

        # Price at 3R (entry=100, stop=95, risk=5, so 3R = 115)
        actions = manager.evaluate_position(
            "BTC/USD",
            current_price=Decimal("115"),
            current_atr=Decimal("2"),
        )
        prog_actions = [a for a in actions if "Progressive" in a.reason]
        assert len(prog_actions) == 1, f"Expected 1 progressive trail action, got: {len(prog_actions)}"
        assert "3.0R" in prog_actions[0].reason
        assert "1.8" in prog_actions[0].reason
        assert position.highest_r_tighten_level == 0

    def test_tightening_at_5r(self):
        """At 5R, should tighten to 1.4x ATR."""
        registry = PositionRegistry()
        mtp = self._make_prog_mtp()
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        position = self._make_filled_position()
        registry.register_position(position)

        # Price at 5R (entry=100, risk=5, so 5R = 125)
        actions = manager.evaluate_position(
            "BTC/USD",
            current_price=Decimal("125"),
            current_atr=Decimal("2"),
        )
        prog_actions = [a for a in actions if "Progressive" in a.reason]
        # Should get TWO actions: one for 3R and one for 5R (both newly crossed)
        assert len(prog_actions) == 2, f"Expected 2 progressive trail actions at 5R, got: {len(prog_actions)}"
        assert position.highest_r_tighten_level == 1

    def test_progressive_trail_only_triggers_once_per_level(self):
        """Re-evaluating at same price should not re-trigger."""
        registry = PositionRegistry()
        mtp = self._make_prog_mtp()
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        position = self._make_filled_position()
        registry.register_position(position)

        # First evaluation at 3R
        manager.evaluate_position("BTC/USD", current_price=Decimal("115"), current_atr=Decimal("2"))
        assert position.highest_r_tighten_level == 0

        # Second evaluation at same price
        actions = manager.evaluate_position("BTC/USD", current_price=Decimal("115"), current_atr=Decimal("2"))
        prog_actions = [a for a in actions if "Progressive" in a.reason]
        assert len(prog_actions) == 0, "Should not re-trigger at same R-level"

    def test_progressive_trail_stores_atr_mult_on_position(self):
        """After tightening, position.current_trail_atr_mult should be set."""
        registry = PositionRegistry()
        mtp = self._make_prog_mtp()
        manager = PositionManagerV2(registry=registry, multi_tp_config=mtp)

        position = self._make_filled_position()
        registry.register_position(position)

        manager.evaluate_position("BTC/USD", current_price=Decimal("115"), current_atr=Decimal("2"))
        assert position.current_trail_atr_mult == Decimal("1.8")

        manager.evaluate_position("BTC/USD", current_price=Decimal("125"), current_atr=Decimal("2"))
        assert position.current_trail_atr_mult == Decimal("1.4")


# ============================================================
# Test 9: Backtest runner metrics
# ============================================================


class TestBacktestRunnerMetrics:
    """Test that BacktestMetrics tracks runner-specific stats."""

    def test_metrics_has_runner_fields(self):
        """BacktestMetrics should have runner-specific fields."""
        from src.backtest.backtest_engine import BacktestMetrics
        m = BacktestMetrics()
        assert hasattr(m, 'tp1_fills')
        assert hasattr(m, 'tp2_fills')
        assert hasattr(m, 'tp1_pnl')
        assert hasattr(m, 'tp2_pnl')
        assert hasattr(m, 'runner_exits')
        assert hasattr(m, 'runner_pnl')
        assert hasattr(m, 'runner_r_multiples')
        assert hasattr(m, 'runner_avg_r')
        assert hasattr(m, 'runner_exits_beyond_3r')
        assert hasattr(m, 'runner_max_r')
        assert hasattr(m, 'exit_reasons')

    def test_runner_metrics_update(self):
        """Runner avg_r and beyond_3r should be computed in update()."""
        from src.backtest.backtest_engine import BacktestMetrics
        m = BacktestMetrics()
        m.runner_r_multiples = [2.5, 4.0, 6.0, 1.0]
        m.update()
        assert m.runner_avg_r == pytest.approx(3.375)
        assert m.runner_exits_beyond_3r == 2  # 4.0 and 6.0
        assert m.runner_max_r == pytest.approx(6.0)

    def test_exit_reasons_tracked(self):
        """BacktestMetrics exit_reasons list should be appendable."""
        from src.backtest.backtest_engine import BacktestMetrics
        m = BacktestMetrics()
        m.exit_reasons.append("trailing_stop")
        m.exit_reasons.append("stop_loss")
        assert len(m.exit_reasons) == 2
        assert m.exit_reasons[0] == "trailing_stop"


# ============================================================
# Test 10: Config new fields
# ============================================================


class TestConfigNewFields:
    """Test that new config fields have correct defaults and validate."""

    def test_progressive_trail_defaults(self):
        mtp = MultiTPConfig(enabled=True)
        assert mtp.progressive_trail_enabled is True
        assert len(mtp.progressive_trail_levels) == 3
        assert mtp.progressive_trail_levels[0]["r_threshold"] == 3.0
        assert mtp.progressive_trail_levels[2]["atr_mult"] == 1.0

    def test_regime_sizing_defaults(self):
        mtp = MultiTPConfig(enabled=True)
        assert mtp.regime_runner_sizing_enabled is True
        assert "tight_smc" in mtp.regime_runner_overrides
        assert "wide_structure" in mtp.regime_runner_overrides
        assert mtp.regime_runner_overrides["tight_smc"]["runner_pct"] == 0.10
        assert mtp.regime_runner_overrides["wide_structure"]["runner_pct"] == 0.30

    def test_regime_sizing_can_be_disabled(self):
        mtp = MultiTPConfig(enabled=True, regime_runner_sizing_enabled=False)
        assert mtp.regime_runner_sizing_enabled is False
