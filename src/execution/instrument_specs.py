"""
Instrument spec registry: single source of truth for futures contract specs.

Loads from Kraken futures instruments API (or ccxt markets), caches to disk.
Used for: pre-validate order params, leverage rules (flexible vs fixed), size rounding.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# Default cache path and TTL
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
CACHE_PATH = DATA_DIR / "instrument_specs_cache.json"
CACHE_TTL_SECONDS = 12 * 3600  # 12 hours


@dataclass
class InstrumentSpec:
    """Futures instrument specification from Kraken (or compatible source)."""

    symbol_raw: str  # e.g. PF_XBTUSD, PI_BCHUSD
    symbol_ccxt: str  # e.g. BCH/USD:USD
    base: str
    quote: str
    contract_size: Decimal = Decimal("1")
    min_size: Decimal = Decimal("0")
    size_step: Decimal = Decimal("0.0001")
    price_tick: Optional[Decimal] = None
    max_leverage: int = 50
    leverage_mode: str = "unknown"  # "flexible" | "fixed" | "unknown"
    allowed_leverages: Optional[List[int]] = None  # if fixed
    supports_reduce_only: bool = True
    last_updated_ts: float = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol_raw": self.symbol_raw,
            "symbol_ccxt": self.symbol_ccxt,
            "base": self.base,
            "quote": self.quote,
            "contract_size": str(self.contract_size),
            "min_size": str(self.min_size),
            "size_step": str(self.size_step),
            "price_tick": str(self.price_tick) if self.price_tick is not None else None,
            "max_leverage": self.max_leverage,
            "leverage_mode": self.leverage_mode,
            "allowed_leverages": self.allowed_leverages,
            "supports_reduce_only": self.supports_reduce_only,
            "last_updated_ts": self.last_updated_ts,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "InstrumentSpec":
        return cls(
            symbol_raw=d.get("symbol_raw", ""),
            symbol_ccxt=d.get("symbol_ccxt", ""),
            base=d.get("base", ""),
            quote=d.get("quote", "USD"),
            contract_size=Decimal(str(d.get("contract_size", 1))),
            min_size=Decimal(str(d.get("min_size", 0))),
            size_step=Decimal(str(d.get("size_step", "0.0001"))),
            price_tick=Decimal(str(d["price_tick"])) if d.get("price_tick") is not None else None,
            max_leverage=int(d.get("max_leverage", 50)),
            leverage_mode=str(d.get("leverage_mode", "unknown")),
            allowed_leverages=d.get("allowed_leverages"),
            supports_reduce_only=bool(d.get("supports_reduce_only", True)),
            last_updated_ts=float(d.get("last_updated_ts", 0)),
        )


def _parse_instrument(raw: Dict[str, Any]) -> Optional[InstrumentSpec]:
    """Parse one Kraken v3 instrument (or ccxt market) into InstrumentSpec."""
    symbol = raw.get("symbol") or raw.get("id") or raw.get("tradeable")
    if not symbol:
        return None
    symbol = str(symbol).strip()
    # Kraken uses PF_XBTUSD, PI_ETHUSD, etc. -> base = XBT, ETH
    base = symbol.replace("PF_", "").replace("PI_", "").replace("FI_", "")
    if base.endswith("USD"):
        base = base[:-3]
    if "/" in base:
        base = base.split("/")[0]
    if not base:
        return None
    # CCXT unified format
    symbol_ccxt = f"{base}/USD:USD" if "/" not in symbol else symbol
    contract_size = Decimal(str(raw.get("contractSize", raw.get("contractMultiplier", raw.get("contract_size", 1)))))
    tick_size = raw.get("tickSize") or raw.get("tick_size") or raw.get("precision", {}).get("amount")
    if isinstance(tick_size, dict):
        tick_size = tick_size.get("min") or tick_size.get("step")
    if tick_size is not None and isinstance(tick_size, (int, float, str)):
        ts = Decimal(str(tick_size))
        size_step = ts if ts > 0 else Decimal("0.0001")
    else:
        size_step = Decimal("0.0001")
    # Leverage: Kraken CONTRACT_NOT_FLEXIBLE_FUTURES implies fixed; no API field = unknown
    leverage_mode = "unknown"
    allowed_leverages: Optional[List[int]] = None
    margin_req = raw.get("marginRequirements") or raw.get("marginLevels") or raw.get("leverage", {})
    if isinstance(margin_req, dict):
        allowed_leverages = [int(k) for k in margin_req.keys() if str(k).replace(".", "").isdigit()]
    if raw.get("flexibleLeverage") is False or raw.get("leveragePreference") == "fixed":
        leverage_mode = "fixed"
        if not allowed_leverages:
            allowed_leverages = [1, 2, 3, 5, 10, 20, 50]
    elif raw.get("flexibleLeverage") is True:
        leverage_mode = "flexible"
    max_lev = 50
    for k in ("maxLeverage", "max_leverage"):
        if raw.get(k) is not None:
            try:
                max_lev = int(raw[k])
                break
            except (TypeError, ValueError):
                pass
    # Min size: prefer per-symbol source, then conservative default. Log when using fallback.
    lim = raw.get("limits") or {}
    amount_lim = lim.get("amount") if isinstance(lim, dict) else {}
    # Order: ccxt market limits.amount.min -> Kraken instrument minSize/minimumSize -> default
    min_from_limits = amount_lim.get("min") if isinstance(amount_lim, dict) else None
    min_from_instrument = raw.get("minSize") or raw.get("minimumSize")
    min_sz = min_from_limits if min_from_limits is not None else min_from_instrument
    min_source = "limits.amount.min" if min_from_limits is not None else ("minSize" if min_from_instrument is not None else None)
    min_sz = float(min_sz) if min_sz is not None else 0
    if min_sz <= 0:
        min_sz = 0.001
        logger.warning(
            "SPEC_MIN_SIZE_MISSING",
            symbol=symbol,
            using_fallback=min_sz,
            source="default",
            hint="Fix upstream: set limits.amount.min or minSize per instrument",
        )
    elif min_source:
        min_source = min_source or "minSize"
    tick_val = raw.get("tickSize") or raw.get("tick_size")
    return InstrumentSpec(
        symbol_raw=symbol,
        symbol_ccxt=symbol_ccxt,
        base=base,
        quote="USD",
        contract_size=contract_size,
        min_size=Decimal(str(min_sz)),
        size_step=size_step,
        price_tick=Decimal(str(tick_val)) if tick_val is not None else None,
        max_leverage=max_lev,
        leverage_mode=leverage_mode,
        allowed_leverages=allowed_leverages,
        supports_reduce_only=True,
        last_updated_ts=time.time(),
    )


def resolve_leverage(
    spec: InstrumentSpec,
    requested: int,
) -> Tuple[Optional[int], Optional[str]]:
    """
    Resolve requested leverage against spec.
    Returns (effective_leverage, rejection_reason).
    effective_leverage None with reason means reject; None without reason means "do not set" (venue default).
    """
    if spec.leverage_mode == "flexible":
        effective = min(requested, spec.max_leverage)
        effective = max(1, effective)
        return (effective, None)
    if spec.leverage_mode == "fixed":
        allowed = spec.allowed_leverages or [1, 2, 3, 5, 10, 20, 50]
        if requested in allowed:
            return (requested, None)
        # Choose nearest allowed >= requested, else nearest below
        above = [a for a in sorted(allowed) if a >= requested]
        if above:
            return (above[0], None)
        # All allowed are below requested -> use max allowed
        return (max(allowed), None)
    # unknown: do not set leverage (caller will skip set_leverage)
    return (None, None)


def compute_size_contracts(
    spec: InstrumentSpec,
    size_notional: Decimal,
    price: Decimal,
) -> Tuple[Decimal, Optional[str]]:
    """
    Convert notional to contract size using spec. All sizes in contracts (same unit as min_size/size_step).

    Order of operations:
    1. Compute raw contracts from notional / (price * contract_size)
    2. Round down to size_step
    3. If <= 0 -> SIZE_STEP_ROUND_TO_ZERO
    4. If < min_size -> SIZE_BELOW_MIN
    5. Otherwise return (contracts, None)

    Returns (contracts, rejection_reason). rejection_reason non-None means reject.
    If spec.min_size is 0, uses 0.001 as fallback (see _parse_instrument SPEC_MIN_SIZE_MISSING when upstream missing).
    """
    if price <= 0:
        return (Decimal("0"), "PRICE_INVALID")
    if spec.contract_size <= 0:
        return (Decimal("0"), "CONTRACT_SIZE_INVALID")
    effective_min = spec.min_size
    if effective_min <= 0:
        effective_min = Decimal("0.001")
    # 1. Raw contracts (notional and min_size both in same unit: contracts for futures)
    contracts = size_notional / (price * spec.contract_size)
    # 2. Round down to size_step
    if spec.size_step > 0:
        steps = int(contracts / spec.size_step)
        contracts = Decimal(steps) * spec.size_step
    else:
        contracts = contracts.quantize(Decimal("0.0001"), rounding="ROUND_DOWN")
    # 3. Zero after round-down
    if contracts <= 0:
        return (Decimal("0"), "SIZE_STEP_ROUND_TO_ZERO")
    # 4. Below minimum
    if contracts < effective_min:
        return (contracts, "SIZE_BELOW_MIN")
    return (contracts, None)


class InstrumentSpecRegistry:
    """
    Single source of truth for futures instrument specs.
    Loads from Kraken instruments API, caches to disk with TTL.
    """

    def __init__(
        self,
        get_instruments_fn=None,
        cache_path: Optional[Path] = None,
        cache_ttl_seconds: int = CACHE_TTL_SECONDS,
    ):
        self._get_instruments_fn = get_instruments_fn
        self._cache_path = cache_path or CACHE_PATH
        self._cache_ttl = cache_ttl_seconds
        self._by_raw: Dict[str, InstrumentSpec] = {}
        self._by_ccxt: Dict[str, InstrumentSpec] = {}
        self._loaded_at: float = 0
        self._log_unknown_leverage: Dict[str, bool] = {}  # symbol -> already logged

    def _is_stale(self) -> bool:
        return (time.time() - self._loaded_at) > self._cache_ttl

    def _load_from_disk(self) -> bool:
        if not self._cache_path or not self._cache_path.exists():
            return False
        try:
            with open(self._cache_path) as f:
                data = json.load(f)
            specs = [InstrumentSpec.from_dict(d) for d in data.get("specs", [])]
            self._index(specs)
            self._loaded_at = data.get("loaded_at", time.time())
            logger.debug("InstrumentSpecRegistry loaded from cache", count=len(specs), path=str(self._cache_path))
            return True
        except Exception as e:
            logger.warning("Failed to load instrument specs cache", path=str(self._cache_path), error=str(e))
            return False

    def _index(self, specs: List[InstrumentSpec]) -> None:
        self._by_raw = {}
        self._by_ccxt = {}
        for s in specs:
            self._by_raw[s.symbol_raw.upper()] = s
            self._by_ccxt[s.symbol_ccxt.upper().replace(" ", "")] = s
            # Also index normalized "BASEUSD" for fuzzy lookup
            key = (s.base + "USD").upper()
            if key not in self._by_raw:
                self._by_raw[key] = s

    def _save_to_disk(self) -> None:
        if not self._cache_path:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "loaded_at": self._loaded_at,
                "specs": [s.to_dict() for s in set(self._by_raw.values())],
            }
            with open(self._cache_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning("Failed to save instrument specs cache", path=str(self._cache_path), error=str(e))

    async def refresh(self) -> None:
        """Load from API (get_instruments_fn) and update cache."""
        if not self._get_instruments_fn:
            if self._loaded_at == 0:
                self._load_from_disk()
            return
        try:
            raw_list = await self._get_instruments_fn()
        except Exception as e:
            logger.warning("Failed to fetch instruments for spec registry", error=str(e))
            if self._loaded_at == 0:
                self._load_from_disk()
            return
        specs: List[InstrumentSpec] = []
        for r in raw_list or []:
            s = _parse_instrument(r)
            if s:
                specs.append(s)
        if specs:
            self._index(specs)
            self._loaded_at = time.time()
            self._save_to_disk()
            logger.info("InstrumentSpecRegistry refreshed", count=len(specs))
        elif self._loaded_at == 0:
            self._load_from_disk()

    def ensure_loaded(self) -> bool:
        """Ensure in-memory index is populated (from cache if not stale)."""
        if self._by_raw and not self._is_stale():
            return True
        if self._load_from_disk():
            return True
        return False

    def get_spec(self, futures_symbol_any_format: str) -> Optional[InstrumentSpec]:
        """
        Get spec by futures symbol in any common format.
        Normalizes PF_XBTUSD, XBT/USD:USD, XBTUSD, etc.
        """
        self.ensure_loaded()
        s = (futures_symbol_any_format or "").strip().upper()
        if not s:
            return None
        # Direct
        out = self._by_raw.get(s) or self._by_ccxt.get(s)
        if out:
            return out
        # Normalized
        s = s.replace("PF_", "").replace("PI_", "").replace("FI_", "").replace("/", "").replace(":", "").replace("-", "").replace("_", "")
        if s.endswith("USD"):
            out = self._by_raw.get(s) or self._by_raw.get("PF_" + s)
        else:
            out = self._by_raw.get(s + "USD") or self._by_raw.get("PF_" + s + "USD")
        return out

    def resolve_symbol_to_spec(
        self,
        spot_symbol: str,
        futures_tickers: Optional[Dict[str, Any]] = None,
        futures_markets: Optional[Dict[str, Any]] = None,
    ) -> Optional[InstrumentSpec]:
        """Resolve spot symbol to futures spec using tickers/markets for symbol choice."""
        self.ensure_loaded()
        base = (spot_symbol or "").split("/")[0]
        if base == "XBT":
            base = "BTC"
        candidates = [
            f"{base}/USD:USD",
            f"PF_{base}USD",
            f"PI_{base}USD",
            f"{base}USD",
        ]
        if futures_tickers:
            for c in candidates:
                if c in futures_tickers:
                    return self.get_spec(c)
        if futures_markets:
            for c in candidates:
                if c in futures_markets:
                    return self.get_spec(c)
        for c in candidates:
            spec = self.get_spec(c)
            if spec:
                return spec
        return None

    def log_unknown_leverage_once(self, symbol: str) -> None:
        if symbol not in self._log_unknown_leverage:
            self._log_unknown_leverage[symbol] = True
            logger.warning(
                "Instrument leverage unknown; skipping set_leverage (venue default)",
                symbol=symbol,
                event="LEVERAGE_SKIP_UNKNOWN_SPEC",
            )
