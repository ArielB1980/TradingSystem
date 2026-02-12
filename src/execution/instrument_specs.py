"""
Instrument spec registry: single source of truth for futures contract specs.

Loads from Kraken futures instruments API (or ccxt markets), caches to disk.
Used for: pre-validate order params, leverage rules (flexible vs fixed), size rounding.
"""
from __future__ import annotations

import math
import os
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN, ROUND_UP
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from src.data.symbol_utils import futures_candidate_symbols
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

# Default cache path and TTL (env override: INSTRUMENT_SPECS_CACHE_PATH)
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _REPO_ROOT / "data"
_DEFAULT_CACHE_PATH = _DATA_DIR / "instrument_specs_cache.json"


def _instrument_specs_cache_path() -> Path:
    """Cache file path: INSTRUMENT_SPECS_CACHE_PATH env, or data/instrument_specs_cache.json under repo root."""
    env_path = os.environ.get("INSTRUMENT_SPECS_CACHE_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_CACHE_PATH


CACHE_PATH = _instrument_specs_cache_path()  # For backward compatibility
CACHE_TTL_SECONDS = 12 * 3600  # 12 hours

# Kraken min_size now parsed from contractValueTradePrecision at refresh. Overrides only for
# edge cases where API is wrong or we discover post-deploy. Keys: normalized base+USD (e.g. UNIUSD)
VENUE_MIN_OVERRIDES: Dict[str, Decimal] = {}


def _normalize_symbol_for_override(symbol: str) -> str:
    """Normalize symbol for VENUE_MIN_OVERRIDES lookup. PF_UNIUSD, UNI/USD:USD -> UNIUSD."""
    s = (symbol or "").strip().upper().replace(" ", "")
    s = s.replace("PF_", "").replace("PI_", "").replace("FI_", "")
    # Remove / and : before stripping quote (avoid UNI/USD:USD -> UNIUSDUSD)
    if "/" in s:
        s = s.split("/")[0] + s.split("/")[-1].replace(":", "").replace("USD", "")
    else:
        s = s.replace("/", "").replace(":", "").replace("-", "").replace("_", "")
    s = s.replace("USD", "")  # PF_UNIUSD -> UNI, UNIUSD -> UNI
    return (s + "USD") if s else "USD"


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
    size_step_source: str = "missing"  # "precision.amount" | "info.lotSize" | "info.quantityIncrement" | "missing"
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
            "size_step_source": self.size_step_source,
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
            size_step_source=str(d.get("size_step_source", "missing")),
            price_tick=Decimal(str(d["price_tick"])) if d.get("price_tick") is not None else None,
            max_leverage=int(d.get("max_leverage", 50)),
            leverage_mode=str(d.get("leverage_mode", "unknown")),
            allowed_leverages=d.get("allowed_leverages"),
            supports_reduce_only=bool(d.get("supports_reduce_only", True)),
            last_updated_ts=float(d.get("last_updated_ts", 0)),
        )


def _precision_amount_to_size_step(precision_amount: Any) -> Optional[Decimal]:
    """
    Convert CCXT precision.amount to size_step.
    - Integer or whole number (e.g. 3.0) → decimal places: size_step = 10**(-n)
    - Float < 1 → step value directly (exchange already gives step)
    Do not use tickSize here; it is a price increment, not amount.
    """
    if precision_amount is None:
        return None
    try:
        prec = Decimal(str(precision_amount))
    except (ValueError, TypeError):
        return None
    if prec <= 0:
        return None
    # Float < 1: treat as step directly
    if prec < 1:
        return prec
    # Integer type or whole number (e.g. 3.0): treat as decimal places
    if isinstance(precision_amount, int):
        return Decimal("10") ** (-precision_amount)
    try:
        f = float(prec)
        if abs(f - round(f)) < 1e-9:
            return Decimal("10") ** (-int(round(f)))
    except (ValueError, OverflowError):
        pass
    # Fallback: >= 1 treat as decimal places
    return Decimal("10") ** (-int(prec))


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
    
    # Size step: precision.amount only (never tickSize — that is price increment).
    # Fallback: info.lotSize or exchange amount increment; else 0 + log warning.
    precision_data = raw.get("precision", {})
    precision_amount = precision_data.get("amount") if isinstance(precision_data, dict) else None
    size_step = _precision_amount_to_size_step(precision_amount) if precision_amount is not None else None
    size_step_source = "precision.amount" if (size_step is not None and size_step > 0) else "missing"

    if size_step is None or size_step <= 0:
        info = raw.get("info", {}) or {}
        lot_size = None
        if info.get("lotSize") is not None or info.get("lot_size") is not None:
            lot_size = info.get("lotSize") or info.get("lot_size")
            size_step_source = "info.lotSize"
        elif info.get("quantityIncrement") is not None:
            lot_size = info.get("quantityIncrement")
            size_step_source = "info.quantityIncrement"
        if lot_size is not None:
            try:
                ls = Decimal(str(lot_size))
                size_step = ls if ls > 0 else Decimal("0")
            except (ValueError, TypeError):
                size_step = Decimal("0")
        else:
            # Kraken: contractValueTradePrecision implies size step for PF_ symbols
            cv_prec = raw.get("contractValueTradePrecision")
            if cv_prec is not None:
                try:
                    p = int(cv_prec)
                    size_step = Decimal("10") ** (-p) if p >= 0 else Decimal("0.0001")
                    size_step_source = "contractValueTradePrecision"
                except (TypeError, ValueError):
                    size_step = Decimal("0")
                    size_step_source = "missing"
            else:
                size_step = Decimal("0")
                size_step_source = "missing"
        if size_step <= 0:
            logger.warning(
                "SPEC_SIZE_STEP_MISSING",
                symbol=symbol,
                hint="precision.amount and info.lotSize missing; relying on min_size only",
            )
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
    # Min size: Kraken contractValueTradePrecision = decimal places -> min = 10^(-n)
    # e.g. precision=1 -> min 0.1, precision=2 -> min 0.01. Applies to all PF_ symbols.
    min_sz = None
    min_source = None
    cv_prec = raw.get("contractValueTradePrecision")
    if cv_prec is not None:
        try:
            p = int(cv_prec)
            min_sz = float(10 ** (-p)) if p >= 0 else 0.001
            min_source = "contractValueTradePrecision"
        except (TypeError, ValueError):
            pass
    if min_sz is None or min_sz <= 0:
        lim = raw.get("limits") or {}
        amount_lim = lim.get("amount") if isinstance(lim, dict) else {}
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
    tick_val = raw.get("tickSize") or raw.get("tick_size")
    return InstrumentSpec(
        symbol_raw=symbol,
        symbol_ccxt=symbol_ccxt,
        base=base,
        quote="USD",
        contract_size=contract_size,
        min_size=Decimal(str(min_sz)),
        size_step=size_step,
        size_step_source=size_step_source,
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
    *,
    effective_min_size: Optional[Decimal] = None,
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
    effective_min_size: Use when venue enforces stricter min than spec (e.g. UNI: 0.1).
    """
    if price <= 0:
        return (Decimal("0"), "PRICE_INVALID")
    if spec.contract_size <= 0:
        return (Decimal("0"), "CONTRACT_SIZE_INVALID")
    effective_min = effective_min_size if effective_min_size is not None else spec.min_size
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


# Symbols we've already logged for size-step alignment correction this process (once per symbol per run)
_size_step_alignment_logged: set = set()


def ensure_size_step_aligned(
    spec: InstrumentSpec,
    size_contracts: Decimal,
    reduce_only: bool = False,
) -> Tuple[Decimal, Optional[str]]:
    """
    Last-resort guard before order placement: ensure size_contracts is a multiple of size_step.
    Uses pure Decimal arithmetic (no float/epsilon).
    
    Rounding direction:
    - ROUND_DOWN for entries (reduce_only=False): never increases exposure
    - ROUND_UP for exits (reduce_only=True): may be needed to fully close position
    
    If size_step == 0, returns (size_contracts, None). If misaligned, rounds using the
    appropriate direction and logs (once per symbol per run); if rounded value is 0 or
    below min_size, rejects.
    
    Returns (adjusted_contracts, rejection_reason).
    """
    if spec.size_step <= 0:
        return (size_contracts, None)
    
    # Pure Decimal arithmetic: compute k = (size_contracts / step).to_integral_value()
    ratio = size_contracts / spec.size_step
    rounding_mode = ROUND_UP if reduce_only else ROUND_DOWN
    k = ratio.to_integral_value(rounding=rounding_mode)
    rounded = k * spec.size_step
    
    # Exact match: already aligned
    if rounded == size_contracts:
        return (size_contracts, None)
    
    # Misaligned: check if rounded value is valid
    effective_min = spec.min_size if spec.min_size > 0 else Decimal("0.001")
    if rounded <= 0 or rounded < effective_min:
        return (
            size_contracts,
            "SIZE_STEP_MISALIGNED",
        )
    
    # Log correction (once per symbol per process)
    if spec.symbol_raw not in _size_step_alignment_logged:
        _size_step_alignment_logged.add(spec.symbol_raw)
        direction = "up" if rounded > size_contracts else "down"
        logger.warning(
            "SIZE_STEP_ALIGNMENT_CORRECTED",
            symbol=spec.symbol_raw,
            original=str(size_contracts),
            rounded=str(rounded),
            size_step=str(spec.size_step),
            direction=direction,
            reduce_only=reduce_only,
            message=f"Spec drift: size_contracts was not a multiple of size_step; rounded {direction} to valid step",
        )
    return (rounded, None)


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
        ccxt_exchange=None,  # Optional CCXT exchange for precision.amount enrichment
    ):
        self._get_instruments_fn = get_instruments_fn
        self._ccxt_exchange = ccxt_exchange  # Optional: for precision.amount from CCXT markets
        # Use provided path, or env var, or default (data/ under repo root)
        self._cache_path = cache_path or _instrument_specs_cache_path()
        self._cache_ttl = cache_ttl_seconds
        self._by_raw: Dict[str, InstrumentSpec] = {}
        self._by_ccxt: Dict[str, InstrumentSpec] = {}
        self._loaded_at: float = 0
        self._log_unknown_leverage: Dict[str, bool] = {}  # symbol -> already logged

    def _is_stale(self) -> bool:
        """True if never loaded, empty, or past TTL. Treat never-loaded as stale."""
        if not self._by_raw or self._loaded_at == 0:
            return True
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
            # Startup sanity check: fail fast if size_step >> min_size
            self._validate_specs_sanity()
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
    
    async def _enrich_from_ccxt_markets(self, specs: List[InstrumentSpec]) -> None:
        """
        Enrich specs with precision.amount from CCXT markets if available.
        This ensures we use the correct size_step from CCXT market data.
        """
        if not self._ccxt_exchange:
            return
        try:
            # Load CCXT markets if not already loaded
            if not hasattr(self._ccxt_exchange, 'markets') or not self._ccxt_exchange.markets:
                await self._ccxt_exchange.load_markets()
            
            # Map specs to CCXT markets by symbol
            for spec in specs:
                # Try multiple symbol formats
                ccxt_symbols = [
                    spec.symbol_ccxt,
                    spec.symbol_raw,
                    spec.symbol_raw.replace("PF_", "").replace("PI_", "").replace("FI_", ""),
                ]
                for ccxt_sym in ccxt_symbols:
                    market = self._ccxt_exchange.markets.get(ccxt_sym)
                    if market:
                        precision = market.get("precision", {})
                        if isinstance(precision, dict):
                            precision_amount = precision.get("amount")
                            step = _precision_amount_to_size_step(precision_amount)
                            if step is not None and step > 0:
                                spec.size_step = step
                                spec.size_step_source = "precision.amount"
                                logger.debug(
                                    "Enriched spec from CCXT market",
                                    symbol=spec.symbol_raw,
                                    precision_amount=str(precision_amount),
                                    size_step=str(spec.size_step),
                                )
                                break
        except Exception as e:
            logger.debug("Failed to enrich specs from CCXT markets (non-critical)", error=str(e))

    def _validate_specs_sanity(self) -> None:
        """
        Startup sanity check: log specs and fail fast if size_step >> min_size (indicates parsing bug).
        
        Logs once per symbol showing min_size, size_step, and precision.amount (if available).
        If size_step is significantly larger than min_size (e.g., 10x+), this indicates a parsing
        error (likely using wrong precision source) and fails fast.
        """
        # Skip only when explicitly disabled (prod always runs unless env set)
        if os.environ.get("TRADING_SYSTEM_SKIP_SPEC_SANITY") == "1":
            return

        # Log all specs once at startup (for debugging)
        for spec in sorted(self._by_raw.values(), key=lambda s: s.symbol_raw):
            if spec.min_size <= 0 or spec.size_step <= 0:
                logger.warning(
                    "SPEC_SANITY_SKIP_RATIO",
                    symbol=spec.symbol_raw,
                    min_size=str(spec.min_size),
                    size_step=str(spec.size_step),
                    reason="min_size or size_step <= 0; skipping ratio check",
                )
                continue

            # Calculate precision.amount from size_step (reverse: if size_step = 0.001, precision.amount = 3)
            precision_amount = None
            if spec.size_step > 0:
                try:
                    # size_step = 10**(-precision_amount) → precision_amount = -log10(size_step)
                    precision_amount = int(-math.log10(float(spec.size_step)))
                except (ValueError, OverflowError):
                    pass
            
            logger.info(
                "Instrument spec loaded",
                symbol=spec.symbol_raw,
                min_size=str(spec.min_size),
                size_step=str(spec.size_step),
                size_step_source=spec.size_step_source,
                precision_amount=precision_amount,
            )
            
            # Check if size_step is much larger than min_size (red flag)
            ratio = float(spec.size_step / spec.min_size)
            if ratio > 10.0:  # size_step is 10x+ larger than min_size
                logger.critical(
                    "INSTRUMENT_SPEC_SANITY_CHECK_FAILED",
                    symbol=spec.symbol_raw,
                    min_size=str(spec.min_size),
                    size_step=str(spec.size_step),
                    precision_amount=precision_amount,
                    ratio=f"{ratio:.1f}x",
                    message="size_step >> min_size indicates parsing bug (check precision.amount)",
                )
                raise ValueError(
                    f"Instrument spec sanity check failed for {spec.symbol_raw}: "
                    f"size_step ({spec.size_step}) >> min_size ({spec.min_size}). "
                    f"This indicates a parsing bug - check precision.amount handling."
                )
            elif ratio > 2.0:  # Warn if size_step is 2x+ larger (still suspicious)
                logger.warning(
                    "INSTRUMENT_SPEC_SANITY_WARNING",
                    symbol=spec.symbol_raw,
                    min_size=str(spec.min_size),
                    size_step=str(spec.size_step),
                    precision_amount=precision_amount,
                    ratio=f"{ratio:.1f}x",
                    message="size_step > min_size (may be correct, but verify precision.amount)",
                )

    def _save_to_disk(self) -> None:
        if not self._cache_path:
            return
        try:
            # Ensure parent directory exists (data/ or custom path from env)
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            by_symbol = {s.symbol_raw: s for s in self._by_raw.values()}
            data = {
                "loaded_at": self._loaded_at,
                "specs": [s.to_dict() for s in by_symbol.values()],
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
        if self._by_raw and not self._is_stale():
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
            # Enrich with CCXT market precision.amount if available
            if self._ccxt_exchange:
                await self._enrich_from_ccxt_markets(specs)
            self._index(specs)
            self._loaded_at = time.time()
            self._save_to_disk()
            # Startup sanity check: fail fast if size_step >> min_size
            self._validate_specs_sanity()
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

    def get_effective_min_size(self, futures_symbol_any_format: str) -> Decimal:
        """
        Get venue minimum size for order placement. Uses VENUE_MIN_OVERRIDES when
        Kraken enforces stricter mins than API/cache reports (e.g. UNI: 0.1).
        """
        norm = _normalize_symbol_for_override(futures_symbol_any_format)
        override = VENUE_MIN_OVERRIDES.get(norm)
        if override is not None:
            return override
        spec = self.get_spec(futures_symbol_any_format)
        if not spec or spec.min_size <= 0:
            return Decimal("1")
        return spec.min_size

    def resolve_symbol_to_spec(
        self,
        spot_symbol: str,
        futures_tickers: Optional[Dict[str, Any]] = None,
        futures_markets: Optional[Dict[str, Any]] = None,
    ) -> Optional[InstrumentSpec]:
        """Resolve spot symbol to futures spec using tickers/markets for symbol choice."""
        self.ensure_loaded()
        candidates = futures_candidate_symbols(spot_symbol)
        if not candidates:
            return None
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
