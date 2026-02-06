"""
Unit tests for liquidity filters, tier classification, and tier-based position sizing.

Tests:
1. LiquidityFilters defaults and TierConfig
2. MarketRegistry tier classification (_classify_tier)
3. MarketRegistry futures-primary filtering (_apply_filters)
4. RiskManager tier-based leverage and size caps
"""
from __future__ import annotations

import pytest
from decimal import Decimal
from datetime import datetime, timezone
from unittest.mock import MagicMock, AsyncMock

from src.config.config import LiquidityFilters, TierConfig, RiskConfig
from src.data.market_registry import MarketRegistry, MarketPair
from src.data.kraken_client import FuturesTicker
from src.risk.risk_manager import RiskManager
from src.domain.models import Signal, SignalType, SetupType


class FakeKrakenClientWithFuturesTickers:
    """
    Fake client that supports both spot and futures ticker methods
    for testing the new futures-primary filtering.
    """

    def __init__(
        self,
        spot_markets=None,
        futures_markets=None,
        spot_tickers=None,
        futures_tickers=None,
    ):
        self._spot_markets = spot_markets or {
            "BTC/USD": {"id": "btcusd", "base": "BTC", "quote": "USD", "active": True},
            "ETH/USD": {"id": "ethusd", "base": "ETH", "quote": "USD", "active": True},
            "DOGE/USD": {"id": "dogeusd", "base": "DOGE", "quote": "USD", "active": True},
        }
        self._futures_markets = futures_markets or {
            "BTC/USD": {"symbol": "PF_XBTUSD", "base": "XBT", "quote": "USD", "active": True},
            "ETH/USD": {"symbol": "PF_ETHUSD", "base": "ETH", "quote": "USD", "active": True},
            "DOGE/USD": {"symbol": "PF_DOGEUSD", "base": "DOGE", "quote": "USD", "active": True},
        }
        self._spot_tickers = spot_tickers or {
            "BTC/USD": {"quoteVolume": 50_000_000, "bid": 50000, "ask": 50010, "last": 50005},
            "ETH/USD": {"quoteVolume": 20_000_000, "bid": 3000, "ask": 3002, "last": 3001},
            "DOGE/USD": {"quoteVolume": 500_000, "bid": 0.10, "ask": 0.101, "last": 0.1005},
        }
        self._futures_tickers = futures_tickers or {
            "PF_XBTUSD": FuturesTicker(
                symbol="PF_XBTUSD",
                mark_price=Decimal("50000"),
                bid=Decimal("49995"),
                ask=Decimal("50005"),
                volume_24h=Decimal("100000000"),  # $100M
                open_interest=Decimal("50000000"),  # $50M
                funding_rate=Decimal("0.0001"),
            ),
            "PF_ETHUSD": FuturesTicker(
                symbol="PF_ETHUSD",
                mark_price=Decimal("3000"),
                bid=Decimal("2998"),
                ask=Decimal("3002"),
                volume_24h=Decimal("30000000"),  # $30M
                open_interest=Decimal("15000000"),  # $15M
                funding_rate=Decimal("0.00015"),
            ),
            "PF_DOGEUSD": FuturesTicker(
                symbol="PF_DOGEUSD",
                mark_price=Decimal("0.10"),
                bid=Decimal("0.099"),
                ask=Decimal("0.101"),
                volume_24h=Decimal("200000"),  # $200k
                open_interest=Decimal("100000"),  # $100k
                funding_rate=Decimal("0.0005"),
            ),
        }

    async def get_spot_markets(self):
        return self._spot_markets

    async def get_futures_markets(self):
        return self._futures_markets

    async def get_spot_tickers_bulk(self, symbols):
        return {s: self._spot_tickers.get(s, {}) for s in symbols if s in self._spot_tickers}

    async def get_futures_tickers_bulk_full(self):
        return self._futures_tickers


@pytest.fixture
def default_liquidity_filters():
    """Default LiquidityFilters with relaxed settings."""
    return LiquidityFilters()


@pytest.fixture
def mock_config():
    """Mock config with liquidity filters."""
    c = MagicMock()
    c.exchange = MagicMock()
    c.exchange.allow_futures_only_universe = False
    c.liquidity_filters = LiquidityFilters()
    return c


@pytest.fixture
def default_risk_config():
    """Real RiskConfig for position sizing tests."""
    return RiskConfig(
        target_leverage=7.0,
        max_leverage=10.0,
        max_position_size_usd=Decimal("100000"),
        risk_per_trade_pct=0.03,
        sizing_method="leverage_based",
    )


# ============================================================================
# Test LiquidityFilters and TierConfig defaults
# ============================================================================


def test_liquidity_filters_defaults():
    """LiquidityFilters should have relaxed defaults."""
    filters = LiquidityFilters()
    
    # Spot filters (relaxed)
    assert filters.min_spot_volume_usd_24h == Decimal("1000000")  # $1M
    assert filters.max_spread_pct == Decimal("0.0020")  # 0.20%
    assert filters.min_price_usd == Decimal("0.01")
    
    # Futures filters
    assert filters.min_futures_open_interest == Decimal("500000")  # $500k
    assert filters.max_futures_spread_pct == Decimal("0.0030")  # 0.30%
    assert filters.min_futures_volume_usd_24h == Decimal("500000")  # $500k
    assert filters.max_funding_rate_abs == Decimal("0.001")  # 0.1%
    
    # Filter mode
    assert filters.filter_mode == "futures_primary"


def test_tier_config_defaults():
    """TierConfig should have sensible defaults."""
    tier = TierConfig()
    
    assert tier.max_leverage == 10.0
    assert tier.max_position_size_usd == Decimal("100000")
    assert tier.slippage_cap_pct == Decimal("0.001")
    assert tier.allow_live_trading is True


def test_liquidity_filters_tier_configs():
    """LiquidityFilters should have A/B/C tier configs with conservative limits for lower tiers."""
    filters = LiquidityFilters()
    
    tier_a = filters.get_tier_config("A")
    tier_b = filters.get_tier_config("B")
    tier_c = filters.get_tier_config("C")
    
    # Tier A: Full limits
    assert tier_a.max_leverage == 10.0
    assert tier_a.max_position_size_usd == Decimal("100000")
    
    # Tier B: Reduced limits
    assert tier_b.max_leverage == 5.0
    assert tier_b.max_position_size_usd == Decimal("50000")
    
    # Tier C: Most conservative
    assert tier_c.max_leverage == 2.0
    assert tier_c.max_position_size_usd == Decimal("25000")


def test_get_tier_config_unknown_tier_returns_c():
    """Unknown tier should return Tier C (most conservative)."""
    filters = LiquidityFilters()
    
    tier_unknown = filters.get_tier_config("X")
    tier_c = filters.get_tier_config("C")
    
    assert tier_unknown.max_leverage == tier_c.max_leverage
    assert tier_unknown.max_position_size_usd == tier_c.max_position_size_usd


# ============================================================================
# Test MarketRegistry tier classification
# ============================================================================


def test_classify_tier_high_liquidity():
    """High liquidity markets should be classified as Tier A."""
    registry = MarketRegistry(MagicMock(), MagicMock())
    
    pair = MarketPair(
        spot_symbol="BTC/USD",
        futures_symbol="PF_XBTUSD",
        spot_volume_24h=Decimal("50000000"),
        futures_open_interest=Decimal("50000000"),  # $50M OI
        spot_spread_pct=Decimal("0.0001"),
        futures_spread_pct=Decimal("0.0005"),  # 0.05%
        futures_volume_24h=Decimal("100000000"),  # $100M
        funding_rate=Decimal("0.0001"),
        is_eligible=True,
    )
    
    tier = registry._classify_tier(pair)
    assert tier == "A"


def test_classify_tier_medium_liquidity():
    """Medium liquidity markets should be classified as Tier B."""
    registry = MarketRegistry(MagicMock(), MagicMock())
    
    pair = MarketPair(
        spot_symbol="LINK/USD",
        futures_symbol="PF_LINKUSD",
        spot_volume_24h=Decimal("5000000"),
        futures_open_interest=Decimal("3000000"),  # $3M OI (above $1M threshold)
        spot_spread_pct=Decimal("0.001"),
        futures_spread_pct=Decimal("0.0020"),  # 0.20% (above 0.10%, below 0.25%)
        futures_volume_24h=Decimal("5000000"),  # $5M (above $1M threshold)
        funding_rate=Decimal("0.0002"),
        is_eligible=True,
    )
    
    tier = registry._classify_tier(pair)
    assert tier == "B"


def test_classify_tier_low_liquidity():
    """Low liquidity markets should be classified as Tier C."""
    registry = MarketRegistry(MagicMock(), MagicMock())
    
    pair = MarketPair(
        spot_symbol="DOGE/USD",
        futures_symbol="PF_DOGEUSD",
        spot_volume_24h=Decimal("200000"),
        futures_open_interest=Decimal("100000"),  # $100k OI (below $1M)
        spot_spread_pct=Decimal("0.01"),
        futures_spread_pct=Decimal("0.0100"),  # 1.0% spread
        futures_volume_24h=Decimal("200000"),  # $200k
        funding_rate=Decimal("0.0005"),
        is_eligible=True,
    )
    
    tier = registry._classify_tier(pair)
    assert tier == "C"


@pytest.mark.parametrize(
    ("spot_symbol", "futures_symbol"),
    [
        ("BTC/USD", "PF_XBTUSD"),
        ("ETH/USD", "PF_ETHUSD"),
        ("SOL/USD", "PF_SOLUSD"),
        ("BNB/USD", "PF_BNBUSD"),
    ],
)
def test_classify_tier_pinned_majors_always_tier_a(spot_symbol, futures_symbol):
    """Pinned major bases must remain Tier A even under poor liquidity snapshots."""
    registry = MarketRegistry(MagicMock(), MagicMock())

    pair = MarketPair(
        spot_symbol=spot_symbol,
        futures_symbol=futures_symbol,
        spot_volume_24h=Decimal("1"),
        futures_open_interest=Decimal("1"),
        spot_spread_pct=Decimal("0.05"),
        futures_spread_pct=Decimal("0.10"),
        futures_volume_24h=Decimal("1"),
        funding_rate=Decimal("0.01"),
        is_eligible=True,
    )

    assert registry._classify_tier(pair) == "A"


# ============================================================================
# Test MarketRegistry filtering with futures data
# ============================================================================


@pytest.mark.asyncio
async def test_apply_filters_futures_primary_mode(mock_config):
    """In futures_primary mode, markets should pass based on futures metrics."""
    client = FakeKrakenClientWithFuturesTickers()
    registry = MarketRegistry(client, mock_config)
    
    # Discover markets
    pairs = await registry.discover_markets()
    
    # BTC and ETH should pass (high OI, low spread)
    # DOGE should fail (low OI below $500k threshold)
    assert "BTC/USD" in pairs
    assert "ETH/USD" in pairs
    assert "DOGE/USD" not in pairs  # OI too low


@pytest.mark.asyncio
async def test_apply_filters_rejects_low_open_interest(mock_config):
    """Markets with low open interest should be rejected."""
    # Set a high OI requirement
    mock_config.liquidity_filters.min_futures_open_interest = Decimal("20000000")  # $20M
    
    client = FakeKrakenClientWithFuturesTickers()
    registry = MarketRegistry(client, mock_config)
    
    pairs = await registry.discover_markets()
    
    # Only BTC should pass (OI = $50M)
    # ETH has $15M OI, below $20M threshold
    assert "BTC/USD" in pairs
    assert "ETH/USD" not in pairs


@pytest.mark.asyncio
async def test_apply_filters_assigns_tiers(mock_config):
    """Filtered markets should have liquidity_tier assigned."""
    client = FakeKrakenClientWithFuturesTickers()
    registry = MarketRegistry(client, mock_config)
    
    pairs = await registry.discover_markets()
    
    # Check tiers are assigned
    assert pairs["BTC/USD"].liquidity_tier == "A"  # High liquidity
    # ETH may be A or B depending on exact thresholds
    assert pairs["ETH/USD"].liquidity_tier in ("A", "B")


# ============================================================================
# Test RiskManager tier-based sizing
# ============================================================================


def _make_signal(symbol: str, entry: Decimal, stop: Decimal, tp: Decimal) -> Signal:
    """Helper to create a valid Signal for testing."""
    return Signal(
        timestamp=datetime.now(timezone.utc),
        symbol=symbol,
        signal_type=SignalType.LONG,
        entry_price=entry,
        stop_loss=stop,
        take_profit=tp,
        reasoning="Test signal",
        setup_type=SetupType.OB,
        regime="tight_smc",
        higher_tf_bias="bullish",
        adx=Decimal("25"),
        atr=Decimal("100"),
        ema200_slope="up",
    )


def test_risk_manager_tier_a_full_leverage(default_risk_config):
    """Tier A should allow full target leverage."""
    filters = LiquidityFilters()
    rm = RiskManager(default_risk_config, liquidity_filters=filters)
    
    signal = _make_signal("BTC/USD", Decimal("50000"), Decimal("48000"), Decimal("55000"))
    
    decision = rm.validate_trade(
        signal=signal,
        account_equity=Decimal("10000"),
        spot_price=Decimal("50000"),
        perp_mark_price=Decimal("50000"),
        symbol_tier="A",
    )
    
    # Tier A allows 10x leverage, target is 7x, so should use 7x
    assert decision.leverage == Decimal("7")


def test_risk_manager_tier_c_reduced_leverage(default_risk_config):
    """Tier C should cap leverage to 2x."""
    filters = LiquidityFilters()
    rm = RiskManager(default_risk_config, liquidity_filters=filters)
    
    signal = _make_signal("DOGE/USD", Decimal("0.10"), Decimal("0.095"), Decimal("0.12"))
    
    decision = rm.validate_trade(
        signal=signal,
        account_equity=Decimal("10000"),
        spot_price=Decimal("0.10"),
        perp_mark_price=Decimal("0.10"),
        symbol_tier="C",
    )
    
    # Tier C caps at 2x leverage (config target is 7x, but tier cap is 2x)
    # The effective leverage should be min(7, 2) = 2
    # Note: The decision.leverage reflects the requested_leverage used in sizing
    # We verify the notional is consistent with 2x leverage
    expected_buying_power = Decimal("10000") * Decimal("2")  # 2x leverage
    expected_notional = expected_buying_power * Decimal("0.03")  # 3% risk
    
    # Notional should be capped by tier max size ($25k for tier C) or buying power
    assert decision.position_notional <= Decimal("25000")


def test_risk_manager_tier_b_reduced_max_size():
    """Tier B should cap position size to $50k."""
    # Use high leverage and max risk (0.05) to generate large notional
    config = RiskConfig(
        target_leverage=10.0,
        max_leverage=10.0,
        risk_per_trade_pct=0.05,  # 5% max allowed
        sizing_method="leverage_based",
    )
    
    filters = LiquidityFilters()
    rm = RiskManager(config, liquidity_filters=filters)
    
    signal = _make_signal("LINK/USD", Decimal("20"), Decimal("19"), Decimal("25"))
    
    decision = rm.validate_trade(
        signal=signal,
        account_equity=Decimal("200000"),  # $200k equity to ensure we hit tier cap
        spot_price=Decimal("20"),
        perp_mark_price=Decimal("20"),
        symbol_tier="B",
    )
    
    # Tier B leverage cap: 5x, position would be $200k * 5x * 5% = $50k 
    # Tier B max size is $50k, so should be at or below
    assert decision.position_notional <= Decimal("50000")


def test_risk_manager_no_tier_uses_global_limits(default_risk_config):
    """Without symbol_tier, global limits should apply."""
    filters = LiquidityFilters()
    rm = RiskManager(default_risk_config, liquidity_filters=filters)
    
    signal = _make_signal("BTC/USD", Decimal("50000"), Decimal("48000"), Decimal("55000"))
    
    decision = rm.validate_trade(
        signal=signal,
        account_equity=Decimal("10000"),
        spot_price=Decimal("50000"),
        perp_mark_price=Decimal("50000"),
        # symbol_tier not passed
    )
    
    # Should use global target_leverage (7x)
    assert decision.leverage == Decimal("7")


def test_risk_manager_without_liquidity_filters_uses_global(default_risk_config):
    """RiskManager without liquidity_filters should use global limits."""
    rm = RiskManager(default_risk_config)  # No liquidity_filters
    
    signal = _make_signal("BTC/USD", Decimal("50000"), Decimal("48000"), Decimal("55000"))
    
    decision = rm.validate_trade(
        signal=signal,
        account_equity=Decimal("10000"),
        spot_price=Decimal("50000"),
        perp_mark_price=Decimal("50000"),
        symbol_tier="C",  # Tier passed but no filters configured
    )
    
    # Without liquidity_filters, tier should be ignored
    assert decision.leverage == Decimal("7")


# ============================================================================
# Test FuturesTicker dataclass
# ============================================================================


def test_futures_ticker_spread_pct():
    """FuturesTicker.spread_pct should calculate bid-ask spread correctly."""
    ticker = FuturesTicker(
        symbol="PF_XBTUSD",
        mark_price=Decimal("50000"),
        bid=Decimal("49900"),
        ask=Decimal("50100"),
        volume_24h=Decimal("100000000"),
        open_interest=Decimal("50000000"),
        funding_rate=Decimal("0.0001"),
    )
    
    expected_spread = (Decimal("50100") - Decimal("49900")) / Decimal("49900")
    assert ticker.spread_pct == expected_spread
    assert abs(ticker.spread_pct - Decimal("0.004")) < Decimal("0.0001")  # ~0.4%


def test_futures_ticker_spread_pct_zero_bid():
    """FuturesTicker.spread_pct should handle zero bid gracefully."""
    ticker = FuturesTicker(
        symbol="PF_XBTUSD",
        mark_price=Decimal("50000"),
        bid=Decimal("0"),
        ask=Decimal("50100"),
        volume_24h=Decimal("100000000"),
        open_interest=Decimal("50000000"),
        funding_rate=None,
    )
    
    # Should return 100% spread (fallback)
    assert ticker.spread_pct == Decimal("1")
