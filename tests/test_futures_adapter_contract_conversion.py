"""
Test for FuturesAdapter contract conversion methods.

Verifies that notional_to_contracts uses the same logic as ExecutionEngine.
"""
import pytest
from decimal import Decimal

from src.execution.futures_adapter import FuturesAdapter
from unittest.mock import MagicMock


def test_notional_to_contracts():
    """Test that notional_to_contracts converts correctly."""
    adapter = FuturesAdapter(
        kraken_client=MagicMock(),
        max_leverage=10.0,
    )
    
    # Test conversion: notional / mark_price = contracts
    notional = Decimal("50000")
    mark_price = Decimal("50000")
    
    contracts = adapter.notional_to_contracts(notional, mark_price)
    assert contracts == Decimal("1")
    
    # Test with different mark price
    mark_price2 = Decimal("48000")
    contracts2 = adapter.notional_to_contracts(notional, mark_price2)
    expected = notional / mark_price2
    assert contracts2 == expected
    
    # Test with smaller notional
    notional3 = Decimal("25000")
    contracts3 = adapter.notional_to_contracts(notional3, mark_price)
    assert contracts3 == Decimal("0.5")


def test_notional_to_contracts_invalid_price():
    """Test that invalid mark price raises error."""
    adapter = FuturesAdapter(
        kraken_client=MagicMock(),
        max_leverage=10.0,
    )
    
    with pytest.raises(ValueError, match="Invalid mark price"):
        adapter.notional_to_contracts(Decimal("50000"), Decimal("0"))
    
    with pytest.raises(ValueError, match="Invalid mark price"):
        adapter.notional_to_contracts(Decimal("50000"), Decimal("-1"))


def test_map_spot_to_futures_uses_cached_tickers():
    """Test that map_spot_to_futures uses cached tickers when futures_tickers not provided."""
    adapter = FuturesAdapter(
        kraken_client=MagicMock(),
        max_leverage=10.0,
    )
    
    # Set cached tickers
    cached_tickers = {
        "PI_THETAUSD": Decimal("2.50"),
        "PF_THETAUSD": Decimal("2.50"),
        "THETA/USD:USD": Decimal("2.50"),
    }
    adapter.update_cached_futures_tickers(cached_tickers)
    
    # Map without providing futures_tickers - should use cache
    result = adapter.map_spot_to_futures("THETA/USD")
    
    # Should find one of the cached symbols
    assert result in ["PI_THETAUSD", "PF_THETAUSD", "THETA/USD:USD"]
