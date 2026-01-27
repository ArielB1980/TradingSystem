"""
Unit tests for symbol_utils.

Covers normalize_symbol_for_position_match used by the executor's pyramiding guard
so that positions in PF_* / PI_* format are correctly matched to mapped futures symbols
(ROSE/USD:USD, etc.) and duplicate opens on the same contract are rejected.
"""
import pytest
from src.data.symbol_utils import normalize_symbol_for_position_match


class TestNormalizeSymbolForPositionMatch:
    """normalize_symbol_for_position_match produces one canonical form per asset."""

    def test_spot_and_ccxt_unified_collapse(self):
        """ROSE/USD and ROSE/USD:USD both normalize to ROSEUSD."""
        assert normalize_symbol_for_position_match("ROSE/USD") == "ROSEUSD"
        assert normalize_symbol_for_position_match("ROSE/USD:USD") == "ROSEUSD"

    def test_kraken_pf_and_pi_collapse(self):
        """PF_ROSEUSD and PI_ROSEUSD both normalize to ROSEUSD."""
        assert normalize_symbol_for_position_match("PF_ROSEUSD") == "ROSEUSD"
        assert normalize_symbol_for_position_match("PI_ROSEUSD") == "ROSEUSD"

    def test_cross_format_same_asset(self):
        """All formats for the same asset share one canonical form."""
        rose_variants = [
            "ROSE/USD",
            "ROSE/USD:USD",
            "PF_ROSEUSD",
            "PI_ROSEUSD",
            "rose/usd",
            "Rose/USD:USD",
        ]
        canonical = normalize_symbol_for_position_match(rose_variants[0])
        for s in rose_variants:
            assert normalize_symbol_for_position_match(s) == canonical, f"Failed for {s!r}"

    def test_different_assets_differ(self):
        """Different assets do not collide."""
        assert normalize_symbol_for_position_match("ROSE/USD") != normalize_symbol_for_position_match("BTC/USD")
        assert normalize_symbol_for_position_match("PF_ROSEUSD") != normalize_symbol_for_position_match("PF_XBTUSD")
        assert normalize_symbol_for_position_match("ETH/USD:USD") != normalize_symbol_for_position_match("ROSE/USD:USD")

    def test_empty_returns_empty(self):
        """Empty string returns empty string."""
        assert normalize_symbol_for_position_match("") == ""

    def test_none_returns_empty(self):
        """None is treated as falsy and returns empty (defensive)."""
        assert normalize_symbol_for_position_match(None) == ""  # type: ignore[arg-type]

    def test_xbt_btc_not_merged(self):
        """XBT and BTC are left as-is by this helper (no XBT->BTC mapping here)."""
        # Helper does not map XBT->BTC; it only strips prefixes/suffixes.
        # PF_XBTUSD -> XBTUSD, BTC/USD:USD -> BTCUSD. They stay different.
        assert normalize_symbol_for_position_match("PF_XBTUSD") == "XBTUSD"
        assert normalize_symbol_for_position_match("BTC/USD:USD") == "BTCUSD"
