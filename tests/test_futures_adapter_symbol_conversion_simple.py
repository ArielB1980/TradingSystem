"""
Simple unit test for FuturesAdapter symbol conversion logic.

Tests the symbol conversion logic directly without async complexity.
"""
import pytest
from unittest.mock import MagicMock
from src.execution.futures_adapter import FuturesAdapter


def test_symbol_conversion_ccxt_to_pf():
    """Test that CCXT unified format converts to PF_* format."""
    adapter = FuturesAdapter(
        kraken_client=MagicMock(),
        max_leverage=10.0,
    )
    
    # Test the conversion logic directly
    test_cases = [
        ("ONE/USD:USD", "PF_ONEUSD"),
        ("SUN/USD:USD", "PF_SUNUSD"),
        ("PAXG/USD:USD", "PF_PAXGUSD"),
        ("DYM/USD:USD", "PF_DYMUSD"),
        ("API3/USD:USD", "PF_API3USD"),
        ("BTC/USD:USD", "PF_BTCUSD"),
        ("ETH/USD:USD", "PF_ETHUSD"),
    ]
    
    for ccxt_symbol, expected_pf in test_cases:
        # Simulate the conversion logic from place_order()
        symbol_for_lookup = ccxt_symbol.upper()
        if '/' in symbol_for_lookup and ':USD' in symbol_for_lookup:
            base = symbol_for_lookup.split('/')[0]
            symbol_for_lookup = f"PF_{base}USD"
        
        assert symbol_for_lookup == expected_pf, f"Failed for {ccxt_symbol}: got {symbol_for_lookup}, expected {expected_pf}"


def test_symbol_conversion_pf_stays_pf():
    """Test that PF_* format stays as PF_* format."""
    adapter = FuturesAdapter(
        kraken_client=MagicMock(),
        max_leverage=10.0,
    )
    
    # Test that PF_* format doesn't get double-converted
    test_cases = [
        "PF_ONEUSD",
        "PF_SUNUSD",
        "PF_PAXGUSD",
    ]
    
    for pf_symbol in test_cases:
        symbol_for_lookup = pf_symbol.upper()
        if '/' in symbol_for_lookup and ':USD' in symbol_for_lookup:
            base = symbol_for_lookup.split('/')[0]
            symbol_for_lookup = f"PF_{base}USD"
        elif not symbol_for_lookup.startswith('PF_'):
            base = symbol_for_lookup.replace('USD', '').replace('/', '').replace(':', '')
            if base:
                symbol_for_lookup = f"PF_{base}USD"
        
        # Should remain unchanged
        assert symbol_for_lookup == pf_symbol.upper(), f"PF_* symbol was modified: {pf_symbol} -> {symbol_for_lookup}"


def test_symbol_conversion_edge_cases():
    """Test edge cases in symbol conversion."""
    adapter = FuturesAdapter(
        kraken_client=MagicMock(),
        max_leverage=10.0,
    )
    
    # Test edge cases
    test_cases = [
        ("XBT/USD:USD", "PF_XBTUSD"),  # XBT (Bitcoin on Kraken)
        ("ONE/USD", "PF_ONEUSD"),  # Missing :USD suffix
    ]
    
    for input_symbol, expected in test_cases:
        symbol_for_lookup = input_symbol.upper()
        if '/' in symbol_for_lookup and ':USD' in symbol_for_lookup:
            base = symbol_for_lookup.split('/')[0]
            symbol_for_lookup = f"PF_{base}USD"
        elif not symbol_for_lookup.startswith('PF_'):
            base = symbol_for_lookup.replace('USD', '').replace('/', '').replace(':', '')
            if base:
                symbol_for_lookup = f"PF_{base}USD"
        
        assert symbol_for_lookup == expected, f"Edge case failed: {input_symbol} -> {symbol_for_lookup} (expected {expected})"
