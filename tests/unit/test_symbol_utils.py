"""
Unit tests for src.data.symbol_utils.

Locks futures_candidate_symbols() as the single source of Kraken BTC/XBT quirks.
"""
import pytest

from src.data.symbol_utils import (
    futures_candidate_symbols,
    normalize_symbol_for_position_match,
    pf_to_unified,
    position_symbol_matches_order,
)


class TestFuturesCandidateSymbols:
    """futures_candidate_symbols: BTC/USD includes XBT; XBT/USD includes BTC; ETH has no cross-asset pollution."""

    def test_btc_usd_includes_xbt_variants(self) -> None:
        candidates = futures_candidate_symbols("BTC/USD")
        assert "PF_XBTUSD" in candidates
        assert "XBT/USD:USD" in candidates
        assert "PF_BTCUSD" in candidates
        assert "BTC/USD:USD" in candidates

    def test_xbt_usd_includes_btc_variants(self) -> None:
        candidates = futures_candidate_symbols("XBT/USD")
        assert "PF_XBTUSD" in candidates
        assert "XBT/USD:USD" in candidates
        assert "PF_BTCUSD" in candidates
        assert "BTC/USD:USD" in candidates

    def test_eth_usd_no_btc_xbt_pollution(self) -> None:
        candidates = futures_candidate_symbols("ETH/USD")
        assert "PF_ETHUSD" in candidates
        assert "ETH/USD:USD" in candidates
        assert "PF_XBTUSD" not in candidates
        assert "PF_BTCUSD" not in candidates
        assert "XBT/USD:USD" not in candidates
        assert "BTC/USD:USD" not in candidates

    def test_empty_or_invalid_returns_empty(self) -> None:
        assert futures_candidate_symbols("") == []
        assert futures_candidate_symbols("BTC") == []  # no slash

    def test_deduplicated(self) -> None:
        candidates = futures_candidate_symbols("BTC/USD")
        assert len(candidates) == len(set(c.upper() for c in candidates))


class TestNormalizeSymbolForPositionMatch:
    def test_various_formats_become_base_quote(self) -> None:
        assert normalize_symbol_for_position_match("ROSE/USD") == "ROSEUSD"
        assert normalize_symbol_for_position_match("ROSE/USD:USD") == "ROSEUSD"
        assert normalize_symbol_for_position_match("PF_ROSEUSD") == "ROSEUSD"
        assert normalize_symbol_for_position_match("PI_ROSEUSD") == "ROSEUSD"


class TestPfToUnified:
    def test_pf_ada_to_unified(self) -> None:
        assert pf_to_unified("PF_ADAUSD") == "ADA/USD:USD"

    def test_pf_xbt_to_btc_unified(self) -> None:
        assert pf_to_unified("PF_XBTUSD") == "BTC/USD:USD"


class TestPositionSymbolMatchesOrder:
    def test_pf_matches_unified(self) -> None:
        assert position_symbol_matches_order("PF_ADAUSD", "ADA/USD:USD") is True

    def test_same_symbol_matches(self) -> None:
        assert position_symbol_matches_order("PF_XBTUSD", "PF_XBTUSD") is True
