"""
Unit tests for the trade recording pipeline.

Tests:
1. Close via stop records one trade
2. Close via TP records one trade
3. Double-close (idempotency) — no double-write
4. Reconciliation close records trade
5. Force close records trade
6. Fee calculation uses config rates
7. Fee fallback to taker when unknown
8. VWAP calculation from multiple fills
9. Fill-type inference logic
10. _mark_closed() does not mutate qty/price/fills
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

from src.execution.position_state_machine import (
    ManagedPosition,
    PositionState,
    FillRecord,
    OrderEvent,
    OrderEventType,
    ExitReason,
)
from src.execution.trade_recorder import (
    record_closed_trade,
    _infer_fill_type,
    _MAKER,
    _TAKER,
)
from src.domain.models import Side


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_position(
    symbol: str = "PF_BTCUSD",
    side: Side = Side.LONG,
    entry_price: Decimal = Decimal("100"),
    stop_price: Decimal = Decimal("90"),
    size: Decimal = Decimal("10"),
    position_id: str = "test-pos-001",
) -> ManagedPosition:
    """Create a ManagedPosition with sane defaults."""
    return ManagedPosition(
        symbol=symbol,
        side=side,
        position_id=position_id,
        initial_size=size,
        initial_entry_price=entry_price,
        initial_stop_price=stop_price,
        initial_tp1_price=Decimal("110"),
        initial_tp2_price=Decimal("120"),
        initial_final_target=Decimal("130"),
    )


def _add_entry_fill(
    pos: ManagedPosition,
    qty: Decimal = Decimal("10"),
    price: Decimal = Decimal("100"),
    order_id: str = "entry-001",
    fill_id: str = "fill-entry-001",
) -> None:
    """Simulate an entry fill."""
    fill = FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        side=pos.side,
        qty=qty,
        price=price,
        timestamp=datetime.now(timezone.utc) - timedelta(hours=2),
        is_entry=True,
    )
    pos.entry_fills.append(fill)
    pos.entry_order_id = order_id
    pos.state = PositionState.OPEN
    pos.entry_acknowledged = True


def _add_exit_fill(
    pos: ManagedPosition,
    qty: Decimal = Decimal("10"),
    price: Decimal = Decimal("95"),
    order_id: str = "stop-001",
    fill_id: str = "fill-exit-001",
) -> None:
    """Simulate an exit fill (stop, TP, etc.)."""
    fill = FillRecord(
        fill_id=fill_id,
        order_id=order_id,
        side=Side.SHORT if pos.side == Side.LONG else Side.LONG,
        qty=qty,
        price=price,
        timestamp=datetime.now(timezone.utc),
        is_entry=False,
    )
    pos.exit_fills.append(fill)


MAKER_RATE = Decimal("0.0002")
TAKER_RATE = Decimal("0.0005")


# ---------------------------------------------------------------------------
# 1. Close via stop records one trade
# ---------------------------------------------------------------------------


class TestCloseViaStop:
    @patch("src.storage.repository.save_trade")
    def test_stop_close_records_trade(self, mock_save):
        pos = _make_position()
        pos.stop_order_id = "stop-001"
        _add_entry_fill(pos, qty=Decimal("10"), price=Decimal("100"))
        _add_exit_fill(pos, qty=Decimal("10"), price=Decimal("95"), order_id="stop-001")
        pos._mark_closed(ExitReason.STOP_LOSS)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)

        assert trade is not None
        assert trade.trade_id == pos.position_id
        assert trade.symbol == "PF_BTCUSD"
        assert trade.side == Side.LONG
        assert trade.entry_price == Decimal("100")
        assert trade.exit_price == Decimal("95")
        assert trade.size == Decimal("10")
        assert trade.gross_pnl == Decimal("-50")  # (95 - 100) * 10
        assert trade.fees > 0
        assert trade.net_pnl < trade.gross_pnl  # fees make it worse
        assert trade.exit_reason == "stop_loss"
        assert trade.taker_fills_count >= 1  # stop = taker
        assert pos.trade_recorded is True
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# 2. Close via TP records one trade
# ---------------------------------------------------------------------------


class TestCloseViaTP:
    @patch("src.storage.repository.save_trade")
    def test_tp_close_records_trade(self, mock_save):
        pos = _make_position()
        pos.tp1_order_id = "tp1-001"
        pos.tp2_order_id = "tp2-001"
        _add_entry_fill(pos, qty=Decimal("10"), price=Decimal("100"))

        # TP1 fills 5 contracts
        _add_exit_fill(
            pos, qty=Decimal("5"), price=Decimal("110"),
            order_id="tp1-001", fill_id="fill-tp1",
        )
        # TP2 fills remaining 5 contracts
        _add_exit_fill(
            pos, qty=Decimal("5"), price=Decimal("120"),
            order_id="tp2-001", fill_id="fill-tp2",
        )
        pos._mark_closed(ExitReason.TAKE_PROFIT_1)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)

        assert trade is not None
        # Exit VWAP = (5*110 + 5*120) / 10 = 115
        assert trade.exit_price == Decimal("115")
        assert trade.gross_pnl == Decimal("150")  # (115 - 100) * 10
        assert trade.maker_fills_count >= 2  # TP1 and TP2 are limit (maker)
        assert trade.exit_reason == "take_profit_1"
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# 3. Double-close idempotency — no double-write
# ---------------------------------------------------------------------------


class TestIdempotency:
    @patch("src.storage.repository.save_trade")
    def test_double_close_no_double_write(self, mock_save):
        pos = _make_position()
        pos.stop_order_id = "stop-001"
        _add_entry_fill(pos)
        _add_exit_fill(pos, order_id="stop-001")
        pos._mark_closed(ExitReason.STOP_LOSS)

        trade1 = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)
        assert trade1 is not None
        assert pos.trade_recorded is True

        # Second call — should return None
        trade2 = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)
        assert trade2 is None
        mock_save.assert_called_once()  # Only one DB write

    @patch("src.storage.repository.save_trade")
    def test_duplicate_pk_sets_flag_anyway(self, mock_save):
        """If DB raises IntegrityError (duplicate), trade_recorded still set."""
        from sqlalchemy.exc import IntegrityError

        mock_save.side_effect = IntegrityError(
            "duplicate key", params=None, orig=Exception("duplicate")
        )

        pos = _make_position()
        pos.stop_order_id = "stop-001"
        _add_entry_fill(pos)
        _add_exit_fill(pos, order_id="stop-001")
        pos._mark_closed(ExitReason.STOP_LOSS)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)
        assert trade is None  # Couldn't insert
        assert pos.trade_recorded is True  # But flag is set


# ---------------------------------------------------------------------------
# 4. Reconciliation close records trade
# ---------------------------------------------------------------------------


class TestReconciliationClose:
    @patch("src.storage.repository.save_trade")
    def test_recon_close_records_trade(self, mock_save):
        pos = _make_position()
        _add_entry_fill(pos, qty=Decimal("10"), price=Decimal("100"))

        # Synthetic recon exit fill
        _add_exit_fill(
            pos, qty=Decimal("10"), price=Decimal("98"),
            order_id="reconcile-exit-12345-1", fill_id="fill-recon-1",
        )
        pos._mark_closed(ExitReason.RECONCILIATION)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)

        assert trade is not None
        assert trade.exit_reason == "reconciliation"
        # Recon fills are always taker (conservative)
        assert trade.taker_fills_count >= 1
        mock_save.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Force close records trade
# ---------------------------------------------------------------------------


class TestForceClose:
    @patch("src.storage.repository.save_trade")
    def test_force_close_records_trade(self, mock_save):
        pos = _make_position()
        _add_entry_fill(pos, qty=Decimal("5"), price=Decimal("200"))
        _add_exit_fill(
            pos, qty=Decimal("5"), price=Decimal("190"),
            order_id="exit-force-001",
        )
        pos.force_close(ExitReason.KILL_SWITCH)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)

        assert trade is not None
        assert trade.exit_reason == "kill_switch"
        mock_save.assert_called_once()

    @patch("src.storage.repository.save_trade")
    def test_force_close_no_exit_fills_leaves_unrecorded_for_retry(self, mock_save):
        """Force-close with no exit fills can't compute VWAP — leaves unrecorded for backfill retry."""
        pos = _make_position()
        _add_entry_fill(pos)
        # No exit fills
        pos.force_close(ExitReason.KILL_SWITCH)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)

        assert trade is None  # Can't compute PnL
        assert pos.trade_recorded is False  # Stays False so backfill retry can succeed
        mock_save.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Fee calculation uses config rates
# ---------------------------------------------------------------------------


class TestFeeCalculation:
    @patch("src.storage.repository.save_trade")
    def test_fee_uses_config_rates(self, mock_save):
        """Verify fees are computed from the provided rates, not hardcoded."""
        pos = _make_position()
        pos.stop_order_id = "stop-001"
        pos.tp1_order_id = "tp1-001"
        _add_entry_fill(pos, qty=Decimal("10"), price=Decimal("100"))

        # TP1 fill (maker: 10 * 110 * 0.0002 = 0.22)
        _add_exit_fill(
            pos, qty=Decimal("5"), price=Decimal("110"),
            order_id="tp1-001", fill_id="fill-tp1",
        )
        # Stop fill (taker: 10 * 95 * 0.0005 = 0.475 ... but only 5 qty)
        _add_exit_fill(
            pos, qty=Decimal("5"), price=Decimal("95"),
            order_id="stop-001", fill_id="fill-stop",
        )
        pos._mark_closed(ExitReason.STOP_LOSS)

        custom_maker = Decimal("0.0001")  # 1 bps
        custom_taker = Decimal("0.001")   # 10 bps

        trade = record_closed_trade(pos, custom_maker, custom_taker)

        assert trade is not None
        # Entry: 10 * 100 = 1000, maker → 1000 * 0.0001 = 0.1
        # TP1 exit: 5 * 110 = 550, maker → 550 * 0.0001 = 0.055
        # Stop exit: 5 * 95 = 475, taker → 475 * 0.001 = 0.475
        expected_fees = Decimal("0.1") + Decimal("0.055") + Decimal("0.475")
        assert abs(trade.fees - expected_fees) < Decimal("0.001")


# ---------------------------------------------------------------------------
# 7. Fee fallback to taker when unknown
# ---------------------------------------------------------------------------


class TestFeeFallback:
    @patch("src.storage.repository.save_trade")
    def test_unknown_fill_type_defaults_to_taker(self, mock_save):
        pos = _make_position()
        # Entry fill with order_id that doesn't match any tracked order
        _add_entry_fill(pos, order_id="unknown-order-999")
        # Exit fill with unmatched order_id
        _add_exit_fill(pos, order_id="random-exit-777")
        pos._mark_closed(ExitReason.MANUAL)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)

        assert trade is not None
        # Entry has is_entry=True, which defaults to maker in inference
        # Exit order_id doesn't match any known order → taker
        assert trade.taker_fills_count >= 1


# ---------------------------------------------------------------------------
# 8. VWAP calculation from multiple fills
# ---------------------------------------------------------------------------


class TestVWAPCalculation:
    @patch("src.storage.repository.save_trade")
    def test_vwap_multiple_entry_fills(self, mock_save):
        pos = _make_position(size=Decimal("20"))
        pos.stop_order_id = "stop-001"

        # Two entry fills at different prices
        _add_entry_fill(pos, qty=Decimal("10"), price=Decimal("100"), fill_id="fe-1")
        pos.entry_fills.append(FillRecord(
            fill_id="fe-2",
            order_id="entry-001",
            side=pos.side,
            qty=Decimal("10"),
            price=Decimal("110"),
            timestamp=datetime.now(timezone.utc) - timedelta(hours=1),
            is_entry=True,
        ))

        # Single exit fill
        _add_exit_fill(pos, qty=Decimal("20"), price=Decimal("108"), order_id="stop-001")
        pos._mark_closed(ExitReason.STOP_LOSS)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)

        assert trade is not None
        # Entry VWAP = (10*100 + 10*110) / 20 = 105
        assert trade.entry_price == Decimal("105")
        assert trade.exit_price == Decimal("108")
        assert trade.size == Decimal("20")
        # Gross PnL = (108 - 105) * 20 = 60
        assert trade.gross_pnl == Decimal("60")

    @patch("src.storage.repository.save_trade")
    def test_vwap_multiple_exit_fills(self, mock_save):
        pos = _make_position()
        pos.tp1_order_id = "tp1-001"
        pos.tp2_order_id = "tp2-001"
        _add_entry_fill(pos, qty=Decimal("10"), price=Decimal("100"))

        # Two exit fills
        _add_exit_fill(pos, qty=Decimal("4"), price=Decimal("110"), order_id="tp1-001", fill_id="fx-1")
        _add_exit_fill(pos, qty=Decimal("6"), price=Decimal("120"), order_id="tp2-001", fill_id="fx-2")
        pos._mark_closed(ExitReason.TAKE_PROFIT_1)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)

        # Exit VWAP = (4*110 + 6*120) / 10 = (440 + 720) / 10 = 116
        assert trade.exit_price == Decimal("116")

    @patch("src.storage.repository.save_trade")
    def test_short_side_pnl(self, mock_save):
        pos = _make_position(side=Side.SHORT, entry_price=Decimal("100"), stop_price=Decimal("110"))
        pos.stop_order_id = "stop-001"
        _add_entry_fill(pos, qty=Decimal("10"), price=Decimal("100"))
        _add_exit_fill(pos, qty=Decimal("10"), price=Decimal("90"), order_id="stop-001")
        pos._mark_closed(ExitReason.TAKE_PROFIT_1)

        trade = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)

        assert trade is not None
        # Short PnL = (entry - exit) * qty = (100 - 90) * 10 = +100
        assert trade.gross_pnl == Decimal("100")


# ---------------------------------------------------------------------------
# 9. Fill-type inference
# ---------------------------------------------------------------------------


class TestFillTypeInference:
    def test_tp1_is_maker(self):
        pos = _make_position()
        pos.tp1_order_id = "tp1-abc"
        fill = FillRecord("f1", "tp1-abc", Side.SHORT, Decimal("1"), Decimal("100"),
                          datetime.now(timezone.utc), is_entry=False)
        assert _infer_fill_type(fill, pos) == _MAKER

    def test_tp2_is_maker(self):
        pos = _make_position()
        pos.tp2_order_id = "tp2-def"
        fill = FillRecord("f1", "tp2-def", Side.SHORT, Decimal("1"), Decimal("100"),
                          datetime.now(timezone.utc), is_entry=False)
        assert _infer_fill_type(fill, pos) == _MAKER

    def test_stop_is_taker(self):
        pos = _make_position()
        pos.stop_order_id = "stop-xyz"
        fill = FillRecord("f1", "stop-xyz", Side.SHORT, Decimal("1"), Decimal("100"),
                          datetime.now(timezone.utc), is_entry=False)
        assert _infer_fill_type(fill, pos) == _TAKER

    def test_exit_pending_is_taker(self):
        pos = _make_position()
        pos.pending_exit_order_id = "exit-123"
        fill = FillRecord("f1", "exit-123", Side.SHORT, Decimal("1"), Decimal("100"),
                          datetime.now(timezone.utc), is_entry=False)
        assert _infer_fill_type(fill, pos) == _TAKER

    def test_entry_fill_defaults_maker(self):
        pos = _make_position()
        fill = FillRecord("f1", "entry-001", Side.LONG, Decimal("1"), Decimal("100"),
                          datetime.now(timezone.utc), is_entry=True)
        assert _infer_fill_type(fill, pos) == _MAKER

    def test_reconcile_fill_is_taker(self):
        pos = _make_position()
        fill = FillRecord("f1", "reconcile-exit-12345-1", Side.SHORT, Decimal("1"), Decimal("100"),
                          datetime.now(timezone.utc), is_entry=False)
        assert _infer_fill_type(fill, pos) == _TAKER

    def test_synthetic_fill_is_taker(self):
        pos = _make_position()
        fill = FillRecord("f1", "sync-adopted-001", Side.LONG, Decimal("1"), Decimal("100"),
                          datetime.now(timezone.utc), is_entry=True)
        assert _infer_fill_type(fill, pos) == _TAKER

    def test_unknown_order_exit_is_taker(self):
        pos = _make_position()
        fill = FillRecord("f1", "who-knows-999", Side.SHORT, Decimal("1"), Decimal("100"),
                          datetime.now(timezone.utc), is_entry=False)
        assert _infer_fill_type(fill, pos) == _TAKER


# ---------------------------------------------------------------------------
# 10. _mark_closed() does not mutate qty/price/fills
# ---------------------------------------------------------------------------


class TestMarkClosedPurity:
    def test_mark_closed_does_not_mutate_fills(self):
        pos = _make_position()
        _add_entry_fill(pos, qty=Decimal("10"), price=Decimal("100"))
        _add_exit_fill(pos, qty=Decimal("10"), price=Decimal("95"))

        entry_fills_before = list(pos.entry_fills)
        exit_fills_before = list(pos.exit_fills)
        entry_qty_before = pos.filled_entry_qty
        exit_qty_before = pos.filled_exit_qty
        entry_price_before = pos.avg_entry_price

        pos._mark_closed(ExitReason.STOP_LOSS)

        assert pos.state == PositionState.CLOSED
        assert pos.entry_fills == entry_fills_before
        assert pos.exit_fills == exit_fills_before
        assert pos.filled_entry_qty == entry_qty_before
        assert pos.filled_exit_qty == exit_qty_before
        assert pos.avg_entry_price == entry_price_before

    def test_mark_closed_sets_reason_and_time(self):
        pos = _make_position()
        _add_entry_fill(pos)
        _add_exit_fill(pos)

        assert pos.exit_time is None
        pos._mark_closed(ExitReason.TAKE_PROFIT_1)

        assert pos.state == PositionState.CLOSED
        assert pos.exit_reason == ExitReason.TAKE_PROFIT_1
        assert pos.exit_time is not None
        assert pos.trade_recorded is False  # Not yet recorded

    def test_mark_closed_preserves_existing_exit_reason(self):
        """If exit_reason was already set (e.g. by initiate_exit), don't overwrite."""
        pos = _make_position()
        pos.exit_reason = ExitReason.TAKE_PROFIT_1
        _add_entry_fill(pos)
        _add_exit_fill(pos)

        pos._mark_closed(ExitReason.STOP_LOSS)

        # Original reason preserved
        assert pos.exit_reason == ExitReason.TAKE_PROFIT_1


# ---------------------------------------------------------------------------
# 11. Not-eligible positions are skipped
# ---------------------------------------------------------------------------


class TestSkipNonEligible:
    @patch("src.storage.repository.save_trade")
    def test_open_position_not_recorded(self, mock_save):
        pos = _make_position()
        _add_entry_fill(pos)
        assert pos.state == PositionState.OPEN

        result = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)
        assert result is None
        assert pos.trade_recorded is False
        mock_save.assert_not_called()

    @patch("src.storage.repository.save_trade")
    def test_zero_qty_not_recorded(self, mock_save):
        pos = _make_position()
        # Closed without any fills
        pos._mark_closed(ExitReason.RECONCILIATION)

        result = record_closed_trade(pos, MAKER_RATE, TAKER_RATE)
        assert result is None
        assert pos.trade_recorded is False  # Stays False for backfill retry
        mock_save.assert_not_called()
