"""
Unit tests for instrument specs: leverage resolution, size rounding, registry missing spec.
No network calls.
"""
from __future__ import annotations

import pytest
from decimal import Decimal

from src.execution.instrument_specs import (
    InstrumentSpec,
    InstrumentSpecRegistry,
    resolve_leverage,
    compute_size_contracts,
    _parse_instrument,
)


def test_fixed_leverage_adjusts_to_nearest_allowed():
    """resolve_leverage with leverage_mode=fixed, allowed=[2,3,5], requested=4 -> (5, None)."""
    spec = InstrumentSpec(
        symbol_raw="PF_ADAUSD",
        symbol_ccxt="ADA/USD:USD",
        base="ADA",
        quote="USD",
        contract_size=Decimal("1"),
        min_size=Decimal("0.001"),
        size_step=Decimal("0.001"),
        max_leverage=50,
        leverage_mode="fixed",
        allowed_leverages=[2, 3, 5, 10],
    )
    effective, reason = resolve_leverage(spec, 4)
    assert reason is None
    assert effective == 5


def test_fixed_leverage_exact_match():
    """resolve_leverage with requested in allowed -> (requested, None)."""
    spec = InstrumentSpec(
        symbol_raw="PF_BCHUSD",
        symbol_ccxt="BCH/USD:USD",
        base="BCH",
        quote="USD",
        leverage_mode="fixed",
        allowed_leverages=[2, 3, 5, 10],
    )
    effective, reason = resolve_leverage(spec, 5)
    assert reason is None
    assert effective == 5


def test_fixed_leverage_above_max_uses_max_allowed():
    """resolve_leverage with requested > max(allowed) -> (max(allowed), None)."""
    spec = InstrumentSpec(
        symbol_raw="PF_XBTUSD",
        symbol_ccxt="BTC/USD:USD",
        base="XBT",
        quote="USD",
        leverage_mode="fixed",
        allowed_leverages=[2, 3, 5],
    )
    effective, reason = resolve_leverage(spec, 10)
    assert reason is None
    assert effective == 5


def test_flexible_leverage_clamps_to_max():
    """resolve_leverage with leverage_mode=flexible, requested > max_leverage -> (max_leverage, None)."""
    spec = InstrumentSpec(
        symbol_raw="PF_ETHUSD",
        symbol_ccxt="ETH/USD:USD",
        base="ETH",
        quote="USD",
        max_leverage=10,
        leverage_mode="flexible",
    )
    effective, reason = resolve_leverage(spec, 50)
    assert reason is None
    assert effective == 10


def test_unknown_leverage_returns_none_no_reason():
    """resolve_leverage with leverage_mode=unknown -> (None, None)."""
    spec = InstrumentSpec(
        symbol_raw="PF_SOLUSD",
        symbol_ccxt="SOL/USD:USD",
        base="SOL",
        quote="USD",
        leverage_mode="unknown",
    )
    effective, reason = resolve_leverage(spec, 7)
    assert reason is None
    assert effective is None


def test_size_rounding_min_size_rejected():
    """compute_size_contracts with notional small -> rounds to 0 -> SIZE_STEP_ROUND_TO_ZERO."""
    spec = InstrumentSpec(
        symbol_raw="PF_XBTUSD",
        symbol_ccxt="BTC/USD:USD",
        base="XBT",
        quote="USD",
        contract_size=Decimal("1"),
        min_size=Decimal("0.0001"),
        size_step=Decimal("0.0001"),
    )
    # notional 1 USD, price 100_000 -> 0.00001 contracts -> rounds down to 0
    contracts, reason = compute_size_contracts(spec, Decimal("1"), Decimal("100000"))
    assert reason == "SIZE_STEP_ROUND_TO_ZERO"
    assert contracts == 0


def test_size_below_min_rejected():
    """compute_size_contracts with rounded result > 0 but < min_size -> SIZE_BELOW_MIN."""
    spec = InstrumentSpec(
        symbol_raw="PF_ETHUSD",
        symbol_ccxt="ETH/USD:USD",
        base="ETH",
        quote="USD",
        contract_size=Decimal("1"),
        min_size=Decimal("0.01"),
        size_step=Decimal("0.001"),
    )
    # notional 15 USD, price 3000 -> 0.005 contracts -> rounds to 0.005 < 0.01
    contracts, reason = compute_size_contracts(spec, Decimal("15"), Decimal("3000"))
    assert reason == "SIZE_BELOW_MIN"
    assert contracts < spec.min_size


def test_size_ok_passes():
    """compute_size_contracts with sufficient notional -> (contracts, None)."""
    spec = InstrumentSpec(
        symbol_raw="PF_BCHUSD",
        symbol_ccxt="BCH/USD:USD",
        base="BCH",
        quote="USD",
        contract_size=Decimal("1"),
        min_size=Decimal("0.001"),
        size_step=Decimal("0.001"),
    )
    contracts, reason = compute_size_contracts(spec, Decimal("600"), Decimal("300"))
    assert reason is None
    assert contracts >= spec.min_size
    assert contracts == Decimal("2")  # 600 / 300


def test_registry_missing_spec_returns_none():
    """get_spec for symbol not in registry -> None (candidate rejected at planning)."""
    reg = InstrumentSpecRegistry(get_instruments_fn=None)
    reg._by_raw = {}
    reg._by_ccxt = {}
    reg._loaded_at = 1
    out = reg.get_spec("NONEXISTENT_SYMBOL_XYZ")
    assert out is None


def test_registry_get_spec_by_raw():
    """get_spec resolves by symbol_raw (e.g. PF_BCHUSD) and normalized forms."""
    spec = InstrumentSpec(
        symbol_raw="PF_BCHUSD",
        symbol_ccxt="BCH/USD:USD",
        base="BCH",
        quote="USD",
    )
    reg = InstrumentSpecRegistry(get_instruments_fn=None)
    reg._by_raw = {"PF_BCHUSD": spec, "BCHUSD": spec}
    reg._by_ccxt = {"BCH/USD:USD": spec}
    reg._loaded_at = 1
    assert reg.get_spec("PF_BCHUSD") is spec
    assert reg.get_spec("BCHUSD") is spec
    assert reg.get_spec("BCH/USD:USD") is spec
    assert reg.get_spec("NONEXISTENT_XYZ") is None


# ---------- PAXG-style min-size / venue rejection (proves fix) ----------


def test_parse_instrument_min_size_missing_uses_fallback(caplog):
    """When minSize/limits.amount.min missing or 0, parsed spec gets min_size=0.001 (fallback)."""
    raw = {
        "symbol": "PF_PAXGUSD",
        "contractSize": 1,
        "limits": {"amount": {"max": 1000}},  # no "min"
    }
    spec = _parse_instrument(raw)
    assert spec is not None
    assert spec.min_size == Decimal("0.001")
    assert "SPEC_MIN_SIZE_MISSING" in caplog.text or spec.min_size == Decimal("0.001")


def test_paxg_notional_small_rounds_to_zero_rejected():
    """Notional so small that contracts round to 0 -> SIZE_STEP_ROUND_TO_ZERO (PAXG-style)."""
    spec = InstrumentSpec(
        symbol_raw="PF_PAXGUSD",
        symbol_ccxt="PAXG/USD:USD",
        base="PAXG",
        quote="USD",
        contract_size=Decimal("1"),
        min_size=Decimal("0.001"),
        size_step=Decimal("0.001"),
    )
    # e.g. notional 0.20 USD, price 2650 -> 0.000075... -> rounds down to 0
    contracts, reason = compute_size_contracts(spec, Decimal("0.20"), Decimal("2650"))
    assert reason == "SIZE_STEP_ROUND_TO_ZERO"
    assert contracts == 0


def test_paxg_notional_slightly_bigger_below_min_rejected():
    """Notional gives contracts > 0 but < 0.001 -> SIZE_BELOW_MIN (PAXG venue min)."""
    spec = InstrumentSpec(
        symbol_raw="PF_PAXGUSD",
        symbol_ccxt="PAXG/USD:USD",
        base="PAXG",
        quote="USD",
        contract_size=Decimal("1"),
        min_size=Decimal("0.001"),
        size_step=Decimal("0.0001"),
    )
    # notional 2 USD, price 2650 -> 0.000754... -> rounds to 0.0007 < 0.001
    contracts, reason = compute_size_contracts(spec, Decimal("2"), Decimal("2650"))
    assert reason == "SIZE_BELOW_MIN"
    assert contracts < spec.min_size
    assert contracts > 0


def test_paxg_notional_sufficient_passes():
    """Notional sufficient -> passes and contracts >= 0.001 (no venue reject)."""
    spec = InstrumentSpec(
        symbol_raw="PF_PAXGUSD",
        symbol_ccxt="PAXG/USD:USD",
        base="PAXG",
        quote="USD",
        contract_size=Decimal("1"),
        min_size=Decimal("0.001"),
        size_step=Decimal("0.0001"),
    )
    # notional 4 USD, price 2650 -> 0.001509... -> rounds to 0.0015 >= 0.001
    contracts, reason = compute_size_contracts(spec, Decimal("4"), Decimal("2650"))
    assert reason is None
    assert contracts >= Decimal("0.001")
    assert contracts == Decimal("0.0015")
