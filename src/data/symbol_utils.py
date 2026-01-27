"""
Shared symbol helpers for Kraken Futures.

- PF_* (Kraken raw) <-> X/USD:USD (CCXT unified)
- Position symbol vs order symbol matching (positions use PF_*, orders use unified)
"""
from __future__ import annotations


def normalize_symbol_for_position_match(symbol: str) -> str:
    """
    Canonical form for "same asset" comparison across formats.

    ROSE/USD, ROSE/USD:USD, PF_ROSEUSD, PI_ROSEUSD -> ROSEUSD.
    Used so the pyramiding guard treats exchange positions (e.g. PF_*)
    and mapped futures symbols (e.g. ROSE/USD:USD) as the same market.
    """
    if not symbol:
        return ""
    s = str(symbol).upper()
    s = s.replace("PF_", "").replace("PI_", "").replace("FI_", "")
    s = s.split(":")[0]
    s = s.replace("/", "").replace("-", "").replace("_", "")
    return s


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
