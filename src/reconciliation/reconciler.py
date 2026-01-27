"""
Reconciliation engine for position state synchronization.

Ensures internal DB and in-memory state match exchange reality.
- Unmanaged (ghost): exchange has position, we don't → adopt or force_close.
- Zombie: we track position, exchange doesn't → remove from state.
"""
import asyncio
from typing import List, Dict, Optional, Callable, Any, Union, Awaitable
from decimal import Decimal
from datetime import datetime, timezone

from src.monitoring.logger import get_logger
from src.data.kraken_client import KrakenClient
from src.domain.models import Position, Side
from src.storage.repository import get_active_positions, delete_position, save_position

logger = get_logger(__name__)


def _exchange_dict_to_position(data: Dict[str, Any], config: Optional[Any] = None) -> Position:
    """Build a Position from raw exchange position dict (same shape as get_all_futures_positions).
    Uses config.exchange.position_size_is_notional to interpret size: if True, size is already
    notional USD; if False (Kraken default), size is in contracts and we compute notional = size * price.
    """
    symbol = data.get("symbol") or ""
    side_raw = (data.get("side") or "long").lower()
    side = Side.LONG if side_raw in ("long", "buy") else Side.SHORT
    size = Decimal(str(data.get("size") or 0))
    entry_price = Decimal(str(data.get("entryPrice") or data.get("entry_price") or 0))
    mark_price = Decimal(str(data.get("markPrice") or data.get("mark_price") or entry_price))
    price = mark_price if mark_price else entry_price
    liq = Decimal(str(data.get("liquidationPrice") or data.get("liquidation_price") or 0))
    unrealized_pnl = Decimal(str(data.get("unrealizedPnl") or data.get("unrealized_pnl") or 0))
    leverage = Decimal(str(data.get("leverage") or 1))
    margin_used = Decimal(str(data.get("initialMargin") or data.get("margin_used") or 0))
    # Same conversion logic as FuturesAdapter.position_size_notional
    exchange_cfg = getattr(config, "exchange", None) if config else None
    position_size_is_notional = getattr(exchange_cfg, "position_size_is_notional", False) if exchange_cfg else False
    if position_size_is_notional:
        size_notional = size  # exchange already returns notional USD
    else:
        size_notional = size * price if price else size  # size in contracts -> notional
    return Position(
        symbol=symbol,
        side=side,
        size=size,
        size_notional=size_notional,
        entry_price=entry_price,
        current_mark_price=mark_price,
        liquidation_price=liq,
        unrealized_pnl=unrealized_pnl,
        leverage=leverage,
        margin_used=margin_used,
        opened_at=datetime.now(timezone.utc),
        is_protected=False,
        protection_reason="ADOPTED_UNMANAGED",
    )


class Reconciler:
    """
    Position reconciliation: adopt or force-close unmanaged (ghost) positions,
    and remove zombies from DB. Logs RECONCILE_SUMMARY with counts.
    """

    def __init__(
        self,
        client: KrakenClient,
        config: Any,
        *,
        place_futures_order_fn: Optional[Callable[..., Union[Any, Awaitable[Any]]]] = None,
        place_protection_callback: Optional[Callable[[Position], Union[None, Awaitable[None]]]] = None,
    ):
        self.client = client
        self.config = config
        self.place_futures_order_fn = place_futures_order_fn
        self.place_protection_callback = place_protection_callback

        recon_cfg = getattr(config, "reconciliation", None)
        self.reconcile_enabled = getattr(recon_cfg, "reconcile_enabled", True)
        self.unmanaged_policy = getattr(recon_cfg, "unmanaged_position_policy", "adopt")
        self.adopt_place_protection = getattr(
            recon_cfg, "unmanaged_position_adopt_place_protection", True
        )

    def _normalize_symbol_for_comparison(self, symbol: str) -> str:
        if not symbol:
            return ""
        s = str(symbol).upper()
        s = s.replace("PF_", "").replace("PI_", "").replace("FI_", "")
        s = s.split(":")[0]
        s = s.replace("/", "").replace("-", "").replace("_", "")
        if s.endswith("USD"):
            s = s[:-3]
        return s

    async def _fetch_exchange_positions(self) -> Dict[str, Dict]:
        try:
            if not self.client.has_valid_futures_credentials():
                return {}
            raw = await self.client.get_all_futures_positions()
            out: Dict[str, Dict] = {}
            for p in raw:
                sym = p.get("symbol")
                if sym and (float(p.get("size") or 0)) != 0:
                    out[str(sym)] = p
            return out
        except Exception as e:
            logger.warning("Failed to fetch exchange positions for reconciliation", error=str(e))
            return {}

    async def reconcile_all(self) -> Dict[str, int]:
        """
        Run full reconciliation. Returns summary counts.
        Logs RECONCILE_SUMMARY with on_exchange, tracked, adopted, force_closed, zombies_cleaned.
        """
        if not self.reconcile_enabled:
            logger.info("RECONCILE_SUMMARY", reconcile_disabled=True, on_exchange=0, tracked=0, adopted=0, force_closed=0, zombies_cleaned=0)
            return {"on_exchange": 0, "tracked": 0, "adopted": 0, "force_closed": 0, "zombies_cleaned": 0}

        logger.info("RECONCILE_START")
        summary = {"on_exchange": 0, "tracked": 0, "adopted": 0, "force_closed": 0, "zombies_cleaned": 0}

        try:
            exchange_pos = await self._fetch_exchange_positions()
            summary["on_exchange"] = len(exchange_pos)
            system_pos = get_active_positions()

            exchange_norm = {
                self._normalize_symbol_for_comparison(s): (s, d) for s, d in exchange_pos.items()
            }
            system_norm = {self._normalize_symbol_for_comparison(p.symbol): (p.symbol, p) for p in system_pos}
            summary["tracked"] = len(system_pos)

            exchange_set = set(exchange_norm.keys())
            system_set = set(system_norm.keys())

            # Unmanaged (ghost): exchange has it, we don't
            ghosts_norm = exchange_set - system_set
            for g in ghosts_norm:
                orig_sym, pos_data = exchange_norm[g]
                if self.unmanaged_policy == "adopt":
                    try:
                        pos = _exchange_dict_to_position(pos_data, self.config)
                        save_position(pos)
                        summary["adopted"] += 1
                        logger.info(
                            "RECONCILE_ADOPTED",
                            symbol=orig_sym,
                            size=str(pos.size),
                            side=pos.side.value,
                        )
                        if self.adopt_place_protection and self.place_protection_callback:
                            try:
                                r = self.place_protection_callback(pos)
                                if asyncio.iscoroutine(r):
                                    await r
                            except Exception as e:
                                logger.warning("Adopt place protection failed", symbol=orig_sym, error=str(e))
                    except Exception as e:
                        logger.error("Adopt failed", symbol=orig_sym, error=str(e))
                else:  # force_close
                    try:
                        side = (pos_data.get("side") or "long").lower()
                        close_side = "sell" if side in ("long", "buy") else "buy"
                        size = float(pos_data.get("size") or 0)
                        if self.place_futures_order_fn and size > 0:
                            coro = self.place_futures_order_fn(
                                symbol=orig_sym,
                                side=close_side,
                                order_type="market",
                                size=size,
                                reduce_only=True,
                            )
                            if asyncio.iscoroutine(coro):
                                await coro
                            else:
                                coro  # sync call already executed
                            summary["force_closed"] += 1
                            logger.critical(
                                "RECONCILE_FORCE_CLOSED",
                                symbol=orig_sym,
                                size=size,
                                reason="unmanaged_position_policy=force_close",
                            )
                    except Exception as e:
                        logger.error("Force-close failed", symbol=orig_sym, error=str(e))

            # Zombies: we have it, exchange doesn't
            zombies_norm = system_set - exchange_set
            for z in zombies_norm:
                orig_sym = system_norm[z][0]
                try:
                    delete_position(orig_sym)
                    summary["zombies_cleaned"] += 1
                    logger.info("RECONCILE_ZOMBIE_CLEANED", symbol=orig_sym, zombie_removed=True)
                except Exception as e:
                    logger.warning("Failed to delete zombie", symbol=orig_sym, error=str(e))

            logger.info(
                "RECONCILE_SUMMARY",
                on_exchange=summary["on_exchange"],
                tracked=summary["tracked"],
                adopted=summary["adopted"],
                force_closed=summary["force_closed"],
                zombies_cleaned=summary["zombies_cleaned"],
            )
            logger.info("RECONCILE_END")
        except Exception as e:
            logger.error("Reconciliation failed", error=str(e))
            raise

        return summary
