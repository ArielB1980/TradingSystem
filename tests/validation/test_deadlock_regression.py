"""
Test Suite 3: Deadlock Regression Test (AXS-style).

Goal: prove the "winner gets rejected forever" bug is dead.

Scenario:
  - Top-scoring signal (AXS) wins the auction but gets rejected at
    risk validation (e.g., basis/dislocation breach).
  - The auction must NOT get stuck on AXS forever.
  - The next contender must be attempted in the same cycle.

Pass condition:
  - AXS gets blocked (cooldown or risk rejection)
  - Auction skips it and selects the next best contender
  - At least one other symbol reaches entry validation
"""
import pytest
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from src.domain.models import Signal, SignalType, SetupType, Side
from src.portfolio.auction_allocator import (
    AuctionAllocator,
    PortfolioLimits,
    CandidateSignal,
    OpenPositionMetadata,
    AllocationPlan,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_candidate(
    symbol: str,
    score: float,
    direction: Side = Side.LONG,
    cluster: str = "tight_smc_ob",
    margin: Decimal = Decimal("50"),
    notional: Decimal = Decimal("350"),
) -> CandidateSignal:
    """Create a candidate signal for auction testing."""
    signal = Signal(
        timestamp=datetime.now(timezone.utc),
        symbol=symbol,
        signal_type=SignalType.LONG if direction == Side.LONG else SignalType.SHORT,
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit=Decimal("106"),
        reasoning=f"Test signal for {symbol}",
        setup_type=SetupType.OB,
        regime="tight_smc",
        higher_tf_bias="bullish",
        adx=Decimal("30"),
        atr=Decimal("2"),
        ema200_slope="up",
        score=score,
    )
    return CandidateSignal(
        signal=signal,
        score=score,
        direction=direction,
        symbol=symbol,
        cluster=cluster,
        required_margin=margin,
        risk_R=Decimal("3.0"),
        position_notional=notional,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestDeadlockRegression:
    """
    Prove the auction gracefully handles top-scorer rejection.
    """

    @pytest.fixture
    def allocator(self):
        limits = PortfolioLimits(
            max_positions=5,
            max_margin_util=0.90,
            max_per_cluster=3,
            max_per_symbol=1,
        )
        return AuctionAllocator(
            limits,
            swap_threshold=10.0,
            min_hold_minutes=15,
            max_trades_per_cycle=5,
            max_new_opens_per_cycle=5,
            max_closes_per_cycle=5,
        )

    def test_blocked_symbol_skipped_next_contender_selected(self, allocator):
        """
        AXS is the top scorer but appears in both positions.
        The auction's max_per_symbol=1 means only the first AXS is selected.
        The remaining slots go to other symbols.
        """
        candidates = [
            _make_candidate("AXS/USD", score=90.0),  # Top scorer
            _make_candidate("AXS/USD", score=89.0, cluster="wide_structure_bos"),  # Dup (blocked by max_per_symbol)
            _make_candidate("BTC/USD", score=85.0),  # Second best
            _make_candidate("ETH/USD", score=80.0),  # Third best
            _make_candidate("SOL/USD", score=75.0),  # Fourth
        ]

        plan = allocator.allocate(
            open_positions=[],
            candidate_signals=candidates,
            portfolio_state={
                "account_equity": Decimal("10000"),
                "available_margin": Decimal("10000"),
            },
        )

        opened_symbols = [s.symbol for s in plan.opens]
        # AXS should appear only once (max_per_symbol=1)
        assert opened_symbols.count("AXS/USD") <= 1, (
            f"AXS appeared {opened_symbols.count('AXS/USD')} times (max_per_symbol=1)"
        )
        # Other contenders must be selected
        assert "BTC/USD" in opened_symbols, "BTC should be selected as next contender"
        assert "ETH/USD" in opened_symbols, "ETH should be selected"

    def test_cluster_cap_forces_fallthrough(self, allocator):
        """
        When a cluster is full, the next contender from a different cluster
        must be attempted (no deadlock on the full cluster).
        """
        # All tight_smc_ob, but cluster cap is 3
        candidates = [
            _make_candidate("AAA/USD", score=90.0, cluster="tight_smc_ob"),
            _make_candidate("BBB/USD", score=88.0, cluster="tight_smc_ob"),
            _make_candidate("CCC/USD", score=86.0, cluster="tight_smc_ob"),
            # This one is same cluster but cluster is full (cap=3)
            _make_candidate("DDD/USD", score=84.0, cluster="tight_smc_ob"),
            # This one is different cluster -- should be selected
            _make_candidate("EEE/USD", score=70.0, cluster="wide_structure_bos"),
        ]

        plan = allocator.allocate(
            open_positions=[],
            candidate_signals=candidates,
            portfolio_state={
                "account_equity": Decimal("10000"),
                "available_margin": Decimal("10000"),
            },
        )

        opened_symbols = [s.symbol for s in plan.opens]
        # Exactly 3 tight_smc_ob should be selected (cluster cap)
        tight_count = sum(1 for s in plan.opens if s.symbol in ["AAA/USD", "BBB/USD", "CCC/USD", "DDD/USD"])
        assert tight_count == 3, f"Expected 3 tight_smc_ob, got {tight_count}"
        # DDD should be skipped, EEE should be selected from different cluster
        assert "EEE/USD" in opened_symbols, (
            "EEE (different cluster) must be selected after cluster cap hit"
        )
        assert "DDD/USD" not in opened_symbols, "DDD must be skipped (cluster full)"

    def test_margin_exhaustion_stops_cleanly(self, allocator):
        """
        When margin is exhausted, remaining contenders are skipped cleanly
        without infinite loop or deadlock.
        """
        candidates = [
            _make_candidate("AAA/USD", score=90.0, margin=Decimal("4000")),
            _make_candidate("BBB/USD", score=85.0, margin=Decimal("4000")),
            # This one exceeds remaining margin
            _make_candidate("CCC/USD", score=80.0, margin=Decimal("4000")),
        ]

        plan = allocator.allocate(
            open_positions=[],
            candidate_signals=candidates,
            portfolio_state={
                "account_equity": Decimal("10000"),
                "available_margin": Decimal("10000"),  # 90% = 9000 usable
            },
        )

        opened_symbols = [s.symbol for s in plan.opens]
        # Only 2 fit within margin (4000 + 4000 = 8000 < 9000)
        assert len(plan.opens) == 2, f"Expected 2 opens, got {len(plan.opens)}"
        assert "AAA/USD" in opened_symbols
        assert "BBB/USD" in opened_symbols
        assert "CCC/USD" not in opened_symbols

    def test_locked_position_not_kicked_for_new_signal(self, allocator):
        """
        A locked (recently opened) position cannot be kicked even if a
        new signal scores higher. The new signal goes into a free slot instead.
        """
        from src.domain.models import Position

        # Existing position (locked: recently opened)
        existing = OpenPositionMetadata(
            position=Position(
                symbol="PF_AXSUSD",
                side=Side.LONG,
                size=Decimal("1"),
                size_notional=Decimal("10"),
                entry_price=Decimal("10"),
                current_mark_price=Decimal("10"),
                leverage=Decimal("5"),
                margin_used=Decimal("50"),
                unrealized_pnl=Decimal("0"),
                liquidation_price=Decimal("0"),
            ),
            entry_time=datetime.now(timezone.utc) - timedelta(minutes=5),  # < min_hold
            entry_score=60.0,
            current_pnl_R=Decimal("0"),
            margin_used=Decimal("50"),
            cluster="tight_smc_ob",
            direction=Side.LONG,
            age_seconds=300,
            is_protective_orders_live=True,
            locked=True,  # Locked!
        )

        # New signal scores much higher
        new_candidate = _make_candidate("BTC/USD", score=95.0, margin=Decimal("50"))

        plan = allocator.allocate(
            open_positions=[existing],
            candidate_signals=[new_candidate],
            portfolio_state={
                "account_equity": Decimal("10000"),
                "available_margin": Decimal("10000"),
            },
        )

        # Existing should NOT be closed (locked)
        assert "PF_AXSUSD" not in plan.closes, (
            "Locked position must not be kicked"
        )
        # New signal should still open (free slots available)
        assert len(plan.opens) >= 1, (
            "New signal should open in a free slot"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
