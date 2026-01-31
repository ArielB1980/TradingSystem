import pytest
from unittest.mock import MagicMock

from src.execution.futures_adapter import FuturesAdapter


def test_map_spot_to_futures_prefers_tickers_candidates_over_override():
    """
    Regression: market discovery can return odd unified symbols like 'ADA/USD:ADA'.
    When futures tickers are available, adapter should prefer executable candidates (PF_*, etc.)
    instead of trusting the override.
    """
    adapter = FuturesAdapter(kraken_client=MagicMock(), spot_to_futures_override={"ADA/USD": "ADA/USD:ADA"})
    futures_tickers = {"PF_ADAUSD": 1, "ADA/USD:USD": 1}

    out = adapter.map_spot_to_futures("ADA/USD", futures_tickers=futures_tickers)
    assert out == "PF_ADAUSD"


def test_map_spot_to_futures_falls_back_to_override_when_no_tickers():
    adapter = FuturesAdapter(kraken_client=MagicMock(), spot_to_futures_override={"ADA/USD": "ADA/USD:ADA"})
    out = adapter.map_spot_to_futures("ADA/USD", futures_tickers=None)
    assert out == "ADA/USD:ADA"

