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
    ensure_size_step_aligned,
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


def test_registry_get_spec_by_raw(tmp_path):
    """get_spec resolves by symbol_raw (e.g. PF_BCHUSD) and normalized forms."""
    spec = InstrumentSpec(
        symbol_raw="PF_BCHUSD",
        symbol_ccxt="BCH/USD:USD",
        base="BCH",
        quote="USD",
    )
    # Use tmp_path to avoid loading from real cache
    reg = InstrumentSpecRegistry(get_instruments_fn=None, cache_path=tmp_path / "nonexistent_cache.json")
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


# ---------- size_step_source and ensure_size_step_aligned ----------


def test_size_step_source_from_precision_amount():
    """_parse_instrument sets size_step_source to precision.amount when precision.amount is present."""
    raw = {
        "symbol": "PF_XBTUSD",
        "contractSize": 1,
        "precision": {"amount": 0.001},
        "limits": {"amount": {"min": 0.001}},
    }
    spec = _parse_instrument(raw)
    assert spec is not None
    assert spec.size_step_source == "precision.amount"
    assert spec.size_step == Decimal("0.001")


def test_ensure_size_step_aligned_passes_when_aligned():
    """ensure_size_step_aligned returns (size_contracts, None) when already a multiple of size_step."""
    spec = InstrumentSpec(
        symbol_raw="PF_ETHUSD",
        symbol_ccxt="ETH/USD:USD",
        base="ETH",
        quote="USD",
        min_size=Decimal("0.001"),
        size_step=Decimal("0.001"),
    )
    out, reason = ensure_size_step_aligned(spec, Decimal("0.015"))
    assert reason is None
    assert out == Decimal("0.015")


def test_ensure_size_step_aligned_when_size_step_zero():
    """ensure_size_step_aligned returns (size_contracts, None) when size_step is 0."""
    spec = InstrumentSpec(
        symbol_raw="PF_LEGACY",
        symbol_ccxt="LEG/USD:USD",
        base="LEG",
        quote="USD",
        size_step=Decimal("0"),
    )
    out, reason = ensure_size_step_aligned(spec, Decimal("1.2345"))
    assert reason is None
    assert out == Decimal("1.2345")


def test_ensure_size_step_aligned_rounds_down_for_entries():
    """ensure_size_step_aligned rounds DOWN for entries (reduce_only=False) to never increase exposure."""
    spec = InstrumentSpec(
        symbol_raw="PF_BCHUSD",
        symbol_ccxt="BCH/USD:USD",
        base="BCH",
        quote="USD",
        min_size=Decimal("0.001"),
        size_step=Decimal("0.001"),
    )
    # 0.0151 is not an exact multiple of 0.001 (drift); ROUND_DOWN -> 0.015 (never increases)
    out, reason = ensure_size_step_aligned(spec, Decimal("0.0151"), reduce_only=False)
    assert reason is None
    assert out == Decimal("0.015")
    # 0.0155 with ROUND_DOWN -> 0.015 (not 0.016)
    out2, reason2 = ensure_size_step_aligned(spec, Decimal("0.0155"), reduce_only=False)
    assert reason2 is None
    assert out2 == Decimal("0.015")


def test_ensure_size_step_aligned_rounds_up_for_exits():
    """ensure_size_step_aligned rounds UP for exits (reduce_only=True) to fully close position if needed."""
    spec = InstrumentSpec(
        symbol_raw="PF_BCHUSD",
        symbol_ccxt="BCH/USD:USD",
        base="BCH",
        quote="USD",
        min_size=Decimal("0.001"),
        size_step=Decimal("0.001"),
    )
    # 0.0151 is not an exact multiple of 0.001 (drift); ROUND_UP -> 0.016 (may be needed to fully close)
    out, reason = ensure_size_step_aligned(spec, Decimal("0.0151"), reduce_only=True)
    assert reason is None
    assert out == Decimal("0.016")


def test_ensure_size_step_aligned_rejects_when_rounded_below_min():
    """ensure_size_step_aligned returns SIZE_STEP_MISALIGNED when rounded value is 0 or below min_size."""
    spec = InstrumentSpec(
        symbol_raw="PF_TINY",
        symbol_ccxt="TINY/USD:USD",
        base="TINY",
        quote="USD",
        min_size=Decimal("0.01"),
        size_step=Decimal("0.01"),
    )
    # 0.004 is misaligned (step 0.01); ROUND_DOWN -> 0.00 < min_size -> reject
    out, reason = ensure_size_step_aligned(spec, Decimal("0.004"), reduce_only=False)
    assert reason == "SIZE_STEP_MISALIGNED"
    assert out == Decimal("0.004")
