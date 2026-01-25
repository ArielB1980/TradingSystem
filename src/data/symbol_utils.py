"""
Shared symbol helpers for Kraken Futures.

- PF_* (Kraken raw) <-> X/USD:USD (CCXT unified)
- Position symbol vs order symbol matching (positions use PF_*, orders use unified)
"""
from __future__ import annotations


def pf_to_unified(s: str) -> str:
    """
    PF_ADAUSD -> ADA/USD:USD. PF_XBTUSD -> BTC/USD:USD (XBT->BTC for CCXT).
    """
    if not s or not s.startswith("PF_") or not s.endswith("USD"):
        return s
    base = s[3:-3]
    if base == "XBT":
        base = "BTC"
    return f"{base}/USD:USD"


def position_symbol_matches_order(position_symbol: str, order_symbol: str) -> bool:
    """
    Position uses Kraken native (PF_ADAUSD); orders use CCXT unified (ADA/USD:USD).
    Return True if they refer to the same market.
    """
    if not position_symbol or not order_symbol:
        return False
    if position_symbol == order_symbol:
        return True
    if position_symbol.startswith("PF_") and position_symbol.endswith("USD"):
        unified = pf_to_unified(position_symbol)
        return order_symbol == unified
    return False
