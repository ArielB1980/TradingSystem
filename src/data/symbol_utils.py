"""
Shared symbol helpers for Kraken Futures.

- PF_* (Kraken raw) <-> X/USD:USD (CCXT unified)
- Position symbol vs order symbol matching (positions use PF_*, orders use unified)
- futures_candidate_symbols: single source of truth for Kraken BTC/XBT and variants
"""
from __future__ import annotations

from typing import List


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


def futures_candidate_symbols(spot_symbol: str) -> List[str]:
    """
    Futures symbol candidates for a spot symbol (Kraken: PF_*, PI_*, BASE/USD:USD, BASEUSD).

    Single source of truth for Kraken BTC/XBT: when base is BTC or XBT, returns candidates
    for both bases so specs keyed by XBT (e.g. PF_XBTUSD) are found when resolving "BTC/USD".
    Other bases get only their own candidates (no cross-asset pollution).
    """
    if not spot_symbol or "/" not in spot_symbol:
        return []
    base = (spot_symbol or "").strip().upper().split("/")[0]
    if not base:
        return []
    if base in ("BTC", "XBT"):
        bases = ["XBT", "BTC"]
    else:
        bases = [base]
    seen: set = set()
    out: List[str] = []
    for b in bases:
        for cand in (f"{b}/USD:USD", f"PF_{b}USD", f"PI_{b}USD", f"{b}USD"):
            key = cand.upper()
            if key not in seen:
                seen.add(key)
                out.append(cand)
    return out


def position_symbol_matches_order(position_symbol: str, order_symbol: str) -> bool:
    """
    Position uses Kraken native (PF_ADAUSD) or unified (ADA/USD or ADA/USD:USD);
    orders use CCXT unified (ADA/USD:USD). Return True if they refer to the same market.
    """
    if not position_symbol or not order_symbol:
        return False
    if position_symbol == order_symbol:
        return True
    if position_symbol.startswith("PF_") and position_symbol.endswith("USD"):
        unified = pf_to_unified(position_symbol)
        return order_symbol == unified
    # Registry may store unified without :USD (e.g. PEPE/USD); orders use PEPE/USD:USD
    return normalize_symbol_for_position_match(position_symbol) == normalize_symbol_for_position_match(order_symbol)
