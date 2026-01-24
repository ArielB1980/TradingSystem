"""
Integration test: one LiveTrading _tick with mocked Kraken + DB.

Guards against regressions (e.g. markets.keys() on list) and ensures
the tick path runs without crashing.
"""
import asyncio
import os
import pytest
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config.config import load_config
from src.live.live_trading import LiveTrading


@pytest.fixture
def mock_env():
    with patch.dict(os.environ, {"ENVIRONMENT": "dev", "DATABASE_URL": "postgresql://localhost/test_db"}, clear=False):
        yield


@pytest.fixture
def minimal_config(mock_env):
    """Minimal config with 2 symbols so _market_symbols / markets stay small."""
    cfg = load_config(Path(__file__).resolve().parents[2] / "src" / "config" / "config.yaml")
    cfg.coin_universe.enabled = False
    cfg.exchange.spot_markets = ["BTC/USD", "ETH/USD"]
    cfg.exchange.use_market_discovery = False
    return cfg


def test_live_trading_tick_mocked(minimal_config, mock_env):
    """Run one _tick with mocked Kraken and DB; assert no crash."""
    async def _run():
        with patch("src.live.live_trading.record_event"), \
             patch("src.live.live_trading.record_metrics_snapshot"), \
             patch("src.storage.repository.async_record_event", new_callable=AsyncMock), \
             patch("src.storage.repository.sync_active_positions"), \
             patch("src.storage.repository.save_candles_bulk"), \
             patch("src.storage.repository.save_account_state"), \
             patch("src.storage.repository.get_candles", return_value=[]), \
             patch("src.storage.repository.get_latest_candle_timestamp", return_value=None), \
             patch("src.storage.repository.load_candles_map", return_value={}), \
             patch("src.live.live_trading.KrakenClient", return_value=MagicMock()) as kc, \
             patch("src.live.live_trading.DataAcquisition") as daq, \
             patch("src.live.live_trading.CandleManager") as cm, \
             patch("src.live.live_trading.KillSwitch") as ksc, \
             patch("src.live.live_trading.Executor") as ex, \
             patch("src.live.live_trading.DatabasePruner", MagicMock()):
            kc.return_value.initialize = AsyncMock()
            kc.return_value.close = AsyncMock()
            kc.return_value.has_valid_futures_credentials = lambda: True
            kc.return_value.get_all_futures_positions = AsyncMock(return_value=[])
            kc.return_value.get_spot_tickers_bulk = AsyncMock(
                return_value={"BTC/USD": {"last": 50000}, "ETH/USD": {"last": 3000}}
            )
            kc.return_value.get_futures_tickers_bulk = AsyncMock(
                return_value={"PF_XBTUSD": Decimal("50000"), "PF_ETHUSD": Decimal("3000")}
            )
            kc.return_value.get_futures_balance = AsyncMock(
                return_value={"total": {"USD": 10000}, "free": {"USD": 8000}, "used": {"USD": 2000}}
            )
            kc.return_value.get_ticker = AsyncMock(return_value={"last": 50000})
            daq.return_value.start = AsyncMock()
            daq.return_value.stop = AsyncMock()
            daq.return_value.is_healthy = lambda: True
            daq.return_value.update_symbols = MagicMock()
            cm.return_value.initialize = AsyncMock()
            cm.return_value.update_candles = AsyncMock()
            cm.return_value.get_candles = lambda s, tf: []
            cm.return_value.flush_pending = AsyncMock()
            ksc.return_value.is_active = lambda: False
            ex.return_value.sync_open_orders = AsyncMock()
            ex.return_value.check_order_timeouts = AsyncMock(return_value=0)

            engine = LiveTrading(minimal_config)
            engine.active = True
            engine.markets = ["BTC/USD", "ETH/USD"]
            symbols = engine._market_symbols()
            assert isinstance(symbols, list)
            assert "BTC/USD" in symbols and "ETH/USD" in symbols
            await engine._tick()

    asyncio.run(_run())


def test_market_symbols_list(minimal_config, mock_env):
    """_market_symbols() returns list when markets is list (no .keys() crash)."""
    with patch("src.live.live_trading.KrakenClient", MagicMock()), \
         patch("src.live.live_trading.DataAcquisition", MagicMock()), \
         patch("src.live.live_trading.CandleManager", MagicMock()):
        engine = LiveTrading(minimal_config)
        engine.markets = ["BTC/USD", "ETH/USD"]
        out = engine._market_symbols()
    assert isinstance(out, list)
    assert out == ["BTC/USD", "ETH/USD"]


def test_market_symbols_dict(minimal_config, mock_env):
    """_market_symbols() returns list of keys when markets is dict (e.g. after discovery)."""
    with patch("src.live.live_trading.KrakenClient", MagicMock()), \
         patch("src.live.live_trading.DataAcquisition", MagicMock()), \
         patch("src.live.live_trading.CandleManager", MagicMock()):
        engine = LiveTrading(minimal_config)
        engine.markets = {"BTC/USD": "PF_XBTUSD", "ETH/USD": "PF_ETHUSD"}
        out = engine._market_symbols()
    assert isinstance(out, list)
    assert set(out) == {"BTC/USD", "ETH/USD"}


def test_tick_processes_futures_only_symbol(minimal_config, mock_env):
    """When a symbol has futures ticker but no spot ticker, we still process it (futures-only path)."""
    async def _run():
        with patch("src.live.live_trading.record_event"), \
             patch("src.storage.repository.async_record_event", new_callable=AsyncMock), \
             patch("src.storage.repository.sync_active_positions"), \
             patch("src.storage.repository.save_candles_bulk"), \
             patch("src.storage.repository.save_account_state"), \
             patch("src.storage.repository.load_candles_map", return_value={}), \
             patch("src.storage.repository.get_candles", return_value=[]), \
             patch("src.storage.repository.get_latest_candle_timestamp", return_value=None), \
             patch("src.live.live_trading.KrakenClient", return_value=MagicMock()) as kc, \
             patch("src.live.live_trading.DataAcquisition", MagicMock()), \
             patch("src.live.live_trading.CandleManager") as cm, \
             patch("src.live.live_trading.KillSwitch") as ksc, \
             patch("src.live.live_trading.Executor") as ex, \
             patch("src.live.live_trading.DatabasePruner", MagicMock()), \
             patch("src.live.live_trading.record_metrics_snapshot"):
            kc.return_value.initialize = AsyncMock()
            kc.return_value.close = AsyncMock()
            kc.return_value.has_valid_futures_credentials = lambda: True
            kc.return_value.get_all_futures_positions = AsyncMock(return_value=[])
            kc.return_value.get_spot_tickers_bulk = AsyncMock(
                return_value={"BTC/USD": {"last": 50000}, "ETH/USD": {"last": 3000}}
            )
            kc.return_value.get_futures_tickers_bulk = AsyncMock(
                return_value={
                    "PF_XBTUSD": Decimal("50000"),
                    "PF_ETHUSD": Decimal("3000"),
                    "PF_ZILUSD": Decimal("0.02"),
                }
            )
            kc.return_value.get_futures_balance = AsyncMock(
                return_value={"total": {"USD": 10000}, "free": {"USD": 8000}, "used": {"USD": 2000}}
            )
            ksc.return_value.is_active = lambda: False
            ex.return_value.sync_open_orders = AsyncMock()
            ex.return_value.check_order_timeouts = AsyncMock(return_value=0)
            cm.return_value.initialize = AsyncMock()
            cm.return_value.update_candles = AsyncMock()
            cm.return_value.get_candles = lambda s, tf: [MagicMock()] * 60 if tf == "15m" else []
            cm.return_value.flush_pending = AsyncMock()
            cm.return_value.pop_futures_fallback_count = lambda: 0

            minimal_config.exchange.spot_markets = ["BTC/USD", "ETH/USD", "ZIL/USD"]
            engine = LiveTrading(minimal_config)
            engine.active = True
            engine.markets = ["BTC/USD", "ETH/USD", "ZIL/USD"]
            await engine._tick()

            cm.return_value.update_candles.assert_any_call("ZIL/USD")

    asyncio.run(_run())
