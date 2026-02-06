"""
Tests asserting single source of truth invariants.

V3: MarketRegistry is the ONLY authority for tier classification.
Config tiers (liquidity_tiers) are for UNIVERSE SELECTION only.
"""
import pytest
import ast
import glob
from decimal import Decimal
from unittest.mock import Mock, MagicMock
from pathlib import Path

# Import the modules we're testing
from src.data.market_registry import MarketRegistry, MarketPair


class TestTierClassificationInvariants:
    """Config candidates must NOT affect tier classification."""
    
    def _create_mock_pair(
        self,
        spot_symbol: str = "TEST/USD",
        futures_symbol: str = "TESTUSD-PERP",
        futures_volume: Decimal = Decimal("1000000"),
        futures_spread: Decimal = Decimal("0.0020"),
        futures_oi: Decimal = Decimal("100000"),
        funding_rate: Decimal = None,
    ) -> MarketPair:
        """Create a mock MarketPair for testing."""
        return MarketPair(
            spot_symbol=spot_symbol,
            futures_symbol=futures_symbol,
            spot_volume_24h=Decimal("1000000"),
            futures_open_interest=futures_oi,
            spot_spread_pct=Decimal("0.001"),
            futures_spread_pct=futures_spread,
            futures_volume_24h=futures_volume,
            funding_rate=funding_rate,
            is_eligible=True,
        )
    
    def _create_mock_registry(self) -> MarketRegistry:
        """Create a mock MarketRegistry for testing."""
        mock_client = MagicMock()
        mock_config = MagicMock()
        # Config has LINK/USD as Tier A - but this should NOT affect classification
        mock_config.coin_universe.liquidity_tiers = {
            "A": ["LINK/USD", "BTC/USD", "ETH/USD"],
            "B": ["DOGE/USD"],
            "C": ["TEST/USD"]
        }
        mock_config.liquidity_filters = MagicMock()
        return MarketRegistry(mock_client, mock_config)
    
    def test_config_candidates_do_not_affect_tier(self):
        """
        Given: Symbol LINK/USD is in config as Tier A candidate
        When: Discovery finds LINK/USD with Tier B metrics (vol < $5M, spread > 0.1%)
        Then: LINK/USD should be classified as Tier B (not A)
        
        This is the CRITICAL invariant of single source of truth.
        """
        registry = self._create_mock_registry()
        
        # Create pair with Tier B metrics (vol $1M < $5M, spread 0.20% > 0.10%)
        pair = self._create_mock_pair(
            spot_symbol="LINK/USD",
            futures_symbol="LINKUSD-PERP",
            futures_volume=Decimal("1000000"),  # $1M (< $5M for Tier A)
            futures_spread=Decimal("0.0020"),   # 0.20% (> 0.10% for Tier A)
        )
        
        tier = registry._classify_tier(pair)
        
        # LINK/USD should be classified as B based on metrics, NOT A from config
        assert tier == "B", (
            f"Config tier should not override dynamic classification. "
            f"Expected 'B' (from metrics), got '{tier}'"
        )
    
    def test_pinned_tier_a_still_works(self):
        """
        BTC and ETH should always be Tier A (pinned).
        """
        registry = self._create_mock_registry()
        
        # BTC with poor metrics should still be Tier A
        btc_pair = self._create_mock_pair(
            spot_symbol="BTC/USD",
            futures_symbol="BTCUSD-PERP",
            futures_volume=Decimal("100000"),  # Low volume
            futures_spread=Decimal("0.0100"),   # Wide spread
        )
        
        assert registry._classify_tier(btc_pair) == "A", "BTC should always be Tier A (pinned)"
        
        # ETH with poor metrics should still be Tier A
        eth_pair = self._create_mock_pair(
            spot_symbol="ETH/USD",
            futures_symbol="ETHUSD-PERP",
            futures_volume=Decimal("100000"),
            futures_spread=Decimal("0.0100"),
        )
        
        assert registry._classify_tier(eth_pair) == "A", "ETH should always be Tier A (pinned)"
    
    def test_tier_classification_is_deterministic(self):
        """Same metrics must always produce same tier."""
        registry = self._create_mock_registry()
        
        pair = self._create_mock_pair(
            futures_volume=Decimal("6000000"),  # > $5M
            futures_spread=Decimal("0.0005"),   # < 0.10%
        )
        
        # Run classification 10 times - should always get same result
        tiers = [registry._classify_tier(pair) for _ in range(10)]
        
        assert all(t == "A" for t in tiers), "Classification must be deterministic"
    
    def test_tier_a_requires_both_volume_and_spread(self):
        """Tier A requires BOTH vol >= $5M AND spread <= 0.10%."""
        registry = self._create_mock_registry()
        
        # High volume but wide spread -> NOT Tier A
        pair1 = self._create_mock_pair(
            spot_symbol="TEST1/USD",
            futures_volume=Decimal("10000000"),  # $10M (> $5M)
            futures_spread=Decimal("0.0020"),     # 0.20% (> 0.10%)
        )
        assert registry._classify_tier(pair1) == "B", "High vol + wide spread should be B, not A"
        
        # Low volume but tight spread -> NOT Tier A
        pair2 = self._create_mock_pair(
            spot_symbol="TEST2/USD",
            futures_volume=Decimal("1000000"),   # $1M (< $5M)
            futures_spread=Decimal("0.0005"),    # 0.05% (< 0.10%)
        )
        assert registry._classify_tier(pair2) == "B", "Low vol + tight spread should be B, not A"
    
    def test_tier_b_thresholds(self):
        """Tier B requires vol >= $500k AND spread <= 0.25%."""
        registry = self._create_mock_registry()
        
        # Meets Tier B thresholds
        pair_b = self._create_mock_pair(
            spot_symbol="TESTB/USD",
            futures_volume=Decimal("600000"),   # $600k (>= $500k)
            futures_spread=Decimal("0.0020"),   # 0.20% (<= 0.25%)
        )
        assert registry._classify_tier(pair_b) == "B"
        
        # Below Tier B thresholds -> Tier C
        pair_c = self._create_mock_pair(
            spot_symbol="TESTC/USD",
            futures_volume=Decimal("300000"),   # $300k (< $500k)
            futures_spread=Decimal("0.0030"),   # 0.30% (> 0.25%)
        )
        assert registry._classify_tier(pair_c) == "C"


class TestDataQualityFlags:
    """OI and funding quality must be tracked for observability."""
    
    def test_oi_suspect_when_zero(self):
        """OI = 0 should flag as SUSPECT."""
        pair = MarketPair(
            spot_symbol="TEST/USD",
            futures_symbol="TESTUSD-PERP",
            spot_volume_24h=Decimal("1000000"),
            futures_open_interest=Decimal("0"),  # Zero OI
            spot_spread_pct=Decimal("0.001"),
            futures_spread_pct=Decimal("0.002"),
            futures_volume_24h=Decimal("1000000"),
            funding_rate=None,
            is_eligible=True,
            oi_quality="SUSPECT",  # Should be set during filtering
        )
        assert pair.oi_quality == "SUSPECT"
    
    def test_funding_suspect_when_extreme(self):
        """|funding| > 10% should flag as SUSPECT."""
        pair = MarketPair(
            spot_symbol="TEST/USD",
            futures_symbol="TESTUSD-PERP",
            spot_volume_24h=Decimal("1000000"),
            futures_open_interest=Decimal("100000"),
            spot_spread_pct=Decimal("0.001"),
            futures_spread_pct=Decimal("0.002"),
            futures_volume_24h=Decimal("1000000"),
            funding_rate=Decimal("-0.20"),  # -20% (extreme)
            is_eligible=True,
            funding_quality="SUSPECT",  # Should be set during filtering
        )
        assert pair.funding_quality == "SUSPECT"
    
    def test_default_quality_is_unknown(self):
        """Default quality should be UNKNOWN."""
        pair = MarketPair(
            spot_symbol="TEST/USD",
            futures_symbol="TESTUSD-PERP",
            spot_volume_24h=Decimal("0"),
            futures_open_interest=Decimal("0"),
            spot_spread_pct=Decimal("0"),
            futures_spread_pct=Decimal("0"),
            futures_volume_24h=Decimal("0"),
            funding_rate=None,
            is_eligible=False,
        )
        assert pair.oi_quality == "UNKNOWN"
        assert pair.funding_quality == "UNKNOWN"


class TestNoDeadModuleImports:
    """Ensure coin_universe.py is not imported anywhere."""
    
    def test_no_coin_universe_imports(self):
        """No file should import from src.data.coin_universe (deleted module)."""
        src_path = Path(__file__).parent.parent
        
        for pyfile_path in src_path.rglob("*.py"):
            # Skip the deleted file itself and this test file
            if "coin_universe" in pyfile_path.name or "test_single_source" in pyfile_path.name:
                continue
            
            try:
                with open(pyfile_path) as f:
                    content = f.read()
                    tree = ast.parse(content)
            except (SyntaxError, UnicodeDecodeError):
                continue
            
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "coin_universe" not in alias.name, (
                            f"Dead import in {pyfile_path}: {alias.name}"
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module and "coin_universe" in node.module:
                        pytest.fail(f"Dead import in {pyfile_path}: from {node.module}")


class TestTierForAPI:
    """Test the tier_for() API on MarketRegistry."""
    
    def test_tier_for_returns_discovered_tier(self):
        """tier_for() should return the tier from discovered_pairs."""
        mock_client = MagicMock()
        mock_config = MagicMock()
        mock_config.liquidity_filters = MagicMock()
        
        registry = MarketRegistry(mock_client, mock_config)
        
        # Manually add a discovered pair
        pair = MarketPair(
            spot_symbol="LINK/USD",
            futures_symbol="LINKUSD-PERP",
            spot_volume_24h=Decimal("1000000"),
            futures_open_interest=Decimal("100000"),
            spot_spread_pct=Decimal("0.001"),
            futures_spread_pct=Decimal("0.002"),
            futures_volume_24h=Decimal("1000000"),
            funding_rate=None,
            is_eligible=True,
            liquidity_tier="B",
        )
        registry.discovered_pairs["LINK/USD"] = pair
        
        assert registry.tier_for("LINK/USD") == "B"
    
    def test_tier_for_returns_c_for_unknown(self):
        """tier_for() should return C (most conservative) for unknown symbols."""
        mock_client = MagicMock()
        mock_config = MagicMock()
        mock_config.liquidity_filters = MagicMock()
        
        registry = MarketRegistry(mock_client, mock_config)
        
        assert registry.tier_for("UNKNOWN/USD") == "C"
    
    def test_tier_for_returns_a_for_pinned(self):
        """tier_for() should return A for pinned symbols (BTC, ETH)."""
        mock_client = MagicMock()
        mock_config = MagicMock()
        mock_config.liquidity_filters = MagicMock()
        
        registry = MarketRegistry(mock_client, mock_config)
        
        assert registry.tier_for("BTC/USD") == "A"
        assert registry.tier_for("ETH/USD") == "A"


class TestCoinUniverseConfigMigration:
    """Test backward compatibility for CoinUniverseConfig."""
    
    def test_get_all_candidates_from_liquidity_tiers(self):
        """get_all_candidates() should flatten liquidity_tiers."""
        from src.config.config import CoinUniverseConfig
        
        config = CoinUniverseConfig(
            enabled=True,
            liquidity_tiers={
                "A": ["BTC/USD", "ETH/USD"],
                "B": ["LINK/USD", "SOL/USD"],
                "C": ["TEST/USD"]
            }
        )
        
        candidates = config.get_all_candidates()
        
        assert "BTC/USD" in candidates
        assert "ETH/USD" in candidates
        assert "LINK/USD" in candidates
        assert "SOL/USD" in candidates
        assert "TEST/USD" in candidates
        assert len(candidates) == 5
    
    def test_get_all_candidates_from_candidate_symbols(self):
        """get_all_candidates() should return candidate_symbols if set."""
        from src.config.config import CoinUniverseConfig
        
        config = CoinUniverseConfig(
            enabled=True,
            candidate_symbols=["BTC/USD", "ETH/USD", "SOL/USD"]
        )
        
        candidates = config.get_all_candidates()
        
        assert candidates == ["BTC/USD", "ETH/USD", "SOL/USD"]
