"""
Shared equity/margin calculation for Kraken Futures.

Handles Multi-Collateral (Flex), Single-Collateral (Inverse), and standard USD balance.
Used by TradingService and LiveTrading.
"""
from decimal import Decimal
from typing import Dict, Any, Tuple, Optional

from src.exceptions import OperationalError, DataError
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


async def calculate_effective_equity(
    balance: Dict[str, Any],
    base_currency: str = "USD",
    kraken_client: Optional[Any] = None,
) -> Tuple[Decimal, Decimal, Decimal]:
    """
    Calculate effective equity, available margin, and margin used from balance dict.

    Handles:
    1. Multi-Collateral (Flex) – Kraken 'info.accounts.flex'
    2. Single-Collateral (Inverse) – Value crypto collateral via ticker when equity < 10
    3. Standard – total[base_currency], free, used

    Args:
        balance: Balance dict from Kraken futures API (CCXT-style or raw)
        base_currency: Base currency (default USD)
        kraken_client: Optional Kraken client for ticker fetch (inverse path). Must have get_ticker(symbol).

    Returns:
        (equity, available_margin, margin_used)
    """
    total = balance.get("total", {}) or {}
    free = balance.get("free", {}) or {}
    used = balance.get("used", {}) or {}

    equity = Decimal(str(total.get(base_currency, 0)))
    avail_margin = Decimal(str(free.get(base_currency, 0)))
    margin_used = Decimal(str(used.get(base_currency, 0)))

    info = balance.get("info", {}) or {}
    if isinstance(info, dict) and "accounts" in info and "flex" in info["accounts"]:
        flex = info["accounts"]["flex"]
        pv = flex.get("portfolioValue")
        am = flex.get("availableMargin")
        im = flex.get("initialMargin")
        if pv is not None:
            equity = Decimal(str(pv))
        if am is not None:
            avail_margin = Decimal(str(am))
        if im is not None:
            margin_used = Decimal(str(im))
        return equity, avail_margin, margin_used

    if equity >= Decimal("10") or not kraken_client:
        return equity, avail_margin, margin_used

    for asset in ["XBT", "BTC", "ETH", "SOL", "USDT", "USDC"]:
        if asset == base_currency:
            continue
        asset_qty = Decimal(str(total.get(asset, 0)))
        if asset_qty <= 0:
            continue
        try:
            sym = "BTC/USD" if asset == "XBT" else f"{asset}/USD"
            ticker = await kraken_client.get_ticker(sym)
            price = Decimal(str(ticker["last"]))
            asset_equity = asset_qty * price
            equity += asset_equity
            if avail_margin == 0:
                avail_margin += asset_equity
            logger.info("Valued non-USD collateral", asset=asset, qty=str(asset_qty), usd=str(asset_equity))
        except (OperationalError, DataError, ValueError) as ex:
            logger.warning("Could not value collateral", asset=asset, error=str(ex))

    return equity, avail_margin, margin_used
