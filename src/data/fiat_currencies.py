"""
Fiat currency detection helpers.

Goal:
- Exclude real-world (ISO-4217) currencies from the trading universe (e.g. GBP, EUR, JPY).
- Also exclude stablecoins from the trading universe (e.g. USDT, USDC, DAI).

Design notes:
- Kraken spot market discovery is currently "USD-quoted, active" and can include forex pairs
  like "GBP/USD". We treat those as fiat-based instruments and exclude them.
- We intentionally treat "USD" as fiat, but typical crypto markets use USD as *quote/settle*.
  The practical exclusion we apply across the system is **fiat as BASE asset** (e.g. GBP/USD),
  so the system can still trade crypto/USD markets.
"""

from __future__ import annotations

from typing import Optional, Tuple

# ISO 4217 currency codes (+ common "X*" monetary codes).
# This list is intentionally comprehensive to match the user request: "exclude all real world currencies".
# Sources: ISO 4217. (This is a static list; update only if the standard changes.)
FIAT_CURRENCY_CODES: frozenset[str] = frozenset(
    {
        "AED",
        "AFN",
        "ALL",
        "AMD",
        "ANG",
        "AOA",
        "ARS",
        "AUD",
        "AWG",
        "AZN",
        "BAM",
        "BBD",
        "BDT",
        "BGN",
        "BHD",
        "BIF",
        "BMD",
        "BND",
        "BOB",
        "BRL",
        "BSD",
        "BTN",
        "BWP",
        "BYN",
        "BZD",
        "CAD",
        "CDF",
        "CHF",
        "CLP",
        "CNY",
        "COP",
        "CRC",
        "CUP",
        "CVE",
        "CZK",
        "DJF",
        "DKK",
        "DOP",
        "DZD",
        "EGP",
        "ERN",
        "ETB",
        "EUR",
        "FJD",
        "FKP",
        "GBP",
        "GEL",
        "GHS",
        "GIP",
        "GMD",
        "GNF",
        "GTQ",
        "GYD",
        "HKD",
        "HNL",
        "HRK",
        "HTG",
        "HUF",
        "IDR",
        "ILS",
        "INR",
        "IQD",
        "IRR",
        "ISK",
        "JMD",
        "JOD",
        "JPY",
        "KES",
        "KGS",
        "KHR",
        "KMF",
        "KPW",
        "KRW",
        "KWD",
        "KYD",
        "KZT",
        "LAK",
        "LBP",
        "LKR",
        "LRD",
        "LSL",
        "LYD",
        "MAD",
        "MDL",
        "MGA",
        "MKD",
        "MMK",
        "MNT",
        "MOP",
        "MRU",
        "MUR",
        "MVR",
        "MWK",
        "MXN",
        "MYR",
        "MZN",
        "NAD",
        "NGN",
        "NIO",
        "NOK",
        "NPR",
        "NZD",
        "OMR",
        "PAB",
        "PEN",
        "PGK",
        "PHP",
        "PKR",
        "PLN",
        "PYG",
        "QAR",
        "RON",
        "RSD",
        "RUB",
        "RWF",
        "SAR",
        "SBD",
        "SCR",
        "SDG",
        "SEK",
        "SGD",
        "SHP",
        "SLE",
        "SLL",
        "SOS",
        "SRD",
        "SSP",
        "STN",
        "SYP",
        "SZL",
        "THB",
        "TJS",
        "TMT",
        "TND",
        "TOP",
        "TRY",
        "TTD",
        "TWD",
        "TZS",
        "UAH",
        "UGX",
        "USD",
        "UYU",
        "UZS",
        "VES",
        "VND",
        "VUV",
        "WST",
        "XAF",
        "XCD",
        "XOF",
        "XPF",
        "XDR",
        "XAG",
        "XAU",
        "XPD",
        "XPT",
        "YER",
        "ZAR",
        "ZMW",
        "ZWL",
    }
)

# Stablecoins.
# Note: These are not ISO-4217 fiat currencies, but we still exclude them from the trading universe per user request.
STABLECOIN_CODES: frozenset[str] = frozenset(
    {
        "USDT",
        "USDC",
        "DAI",
        "TUSD",
        "USDP",
        "FDUSD",
        "PYUSD",
        "EURT",
        "EURC",
        "USDE",
        "FRAX",
        "LUSD",
        "GUSD",
        "BUSD",
    }
)


def normalize_currency_code(code: Optional[str]) -> str:
    """
    Normalize a currency/asset code to an uppercase ISO-ish form.

    Handles common venue prefixes (e.g., Kraken "ZUSD"/"ZEUR" style codes).
    """
    if not code:
        return ""
    c = str(code).strip().upper()
    # Kraken sometimes prefixes fiat with Z (e.g. ZUSD) and crypto with X (e.g. XXBT),
    # but CCXT usually normalizes to "USD", "EUR", etc. We handle both.
    if len(c) == 4 and c[0] in ("X", "Z"):
        c = c[1:]
    return c


def is_fiat_currency(code: Optional[str]) -> bool:
    """Return True iff code is a real-world fiat currency (ISO 4217)."""
    c = normalize_currency_code(code)
    if not c:
        return False
    return c in FIAT_CURRENCY_CODES


def is_stablecoin(code: Optional[str]) -> bool:
    """Return True iff code is a known stablecoin ticker used on major venues."""
    c = normalize_currency_code(code)
    if not c:
        return False
    return c in STABLECOIN_CODES


def is_disallowed_trading_base(code: Optional[str]) -> bool:
    """
    True if this asset should be excluded from the trading universe as a BASE asset:
    - any ISO-4217 fiat currency, or
    - any stablecoin.
    """
    return is_fiat_currency(code) or is_stablecoin(code)


def parse_base_quote(symbol: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """
    Best-effort base/quote extraction for symbols used in this codebase.

    Supported examples:
    - Spot: "BTC/USD" -> ("BTC", "USD")
    - CCXT futures: "BTC/USD:USD" -> ("BTC", "USD")
    - Kraken futures raw: "PF_XBTUSD" -> ("XBT", "USD")
    - Kraken futures raw: "PI_GBPUSD" -> ("GBP", "USD")
    - Legacy: "BTCUSD-PERP" -> ("BTC", "USD")
    """
    if not symbol:
        return (None, None)
    s = str(symbol).strip().upper()
    if not s:
        return (None, None)

    # CCXT unified futures "BASE/QUOTE:SETTLE" -> use BASE/QUOTE part.
    if ":" in s:
        s = s.split(":", 1)[0]

    # Spot/unified: "BASE/QUOTE"
    if "/" in s:
        base, quote = s.split("/", 1)
        base = base.strip()
        quote = quote.strip()
        return (base or None, quote or None)

    # Kraken raw: PF_BASEQUOTE (mostly USD quote here)
    for prefix in ("PF_", "PI_", "FI_"):
        if s.startswith(prefix):
            s2 = s[len(prefix) :]
            if s2.endswith("USD") and len(s2) > 3:
                return (s2[:-3] or None, "USD")
            return (s2 or None, None)

    # Legacy "BASEUSD-PERP"
    if s.endswith("-PERP"):
        root = s[: -len("-PERP")]
        if root.endswith("USD") and len(root) > 3:
            return (root[:-3] or None, "USD")
        return (root or None, None)

    # Plain "BASEUSD"
    if s.endswith("USD") and len(s) > 3:
        return (s[:-3] or None, "USD")

    return (s, None)


def has_disallowed_base(symbol: Optional[str]) -> bool:
    """True if the symbol's BASE asset is fiat or stablecoin."""
    base, _quote = parse_base_quote(symbol)
    return is_disallowed_trading_base(base)


# Backwards-compatible alias (deprecated): kept to avoid accidental import breaks.
has_fiat_base = has_disallowed_base

