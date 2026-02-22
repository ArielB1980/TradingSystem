"""
Integration test: one LiveTrading _tick with mocked Kraken + DB.

Guards against regressions (e.g. markets.keys() on list) and ensures
the tick path runs without crashing.
"""
import asyncio
import os
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.data.kraken_client import FuturesTicker

from src.config.config import load_config
from src.live.live_trading import LiveTrading
from src.runtime.startup_phases import StartupPhase


def _make_futures_ticker(symbol: str, price: Decimal) -> FuturesTicker:
    """Create a FuturesTicker with healthy spread + volume for tests."""
    return FuturesTicker(
        symbol=symbol,
        mark_price=price,
        bid=price * Decimal("0.999"),
        ask=price * Decimal("1.001"),
        volume_24h=Decimal("500000"),
        open_interest=Decimal("10000"),
        funding_rate=Decimal("0.0001"),
    )


def _make_candle(hours_ago: float = 0):
    """Return a mock candle with a recent timestamp."""
    c = MagicMock()
    c.timestamp = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return c


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
             patch("src.storage.db.get_db", side_effect=_mock_db), \
             patch("src.storage.repository.get_db", side_effect=_mock_db), \
             patch("src.live.live_trading.KrakenClient") as kc, \
             patch("src.live.live_trading.DataAcquisition") as daq, \
             patch("src.live.live_trading.CandleManager") as cm, \
             patch("src.live.live_trading.KillSwitch") as ksc, \
             patch("src.live.live_trading.Executor") as ex, \
             patch("src.live.live_trading.DatabasePruner", MagicMock()):
            mock_client = AsyncMock()
            kc.return_value = mock_client
            mock_client.has_valid_futures_credentials = lambda: True
            mock_client.get_all_futures_positions = AsyncMock(return_value=[])
            mock_client.get_spot_tickers_bulk = AsyncMock(
                return_value={"BTC/USD": {"last": 50000}, "ETH/USD": {"last": 3000}}
            )
            mock_client.get_futures_tickers_bulk = AsyncMock(
                return_value={"PF_XBTUSD": Decimal("50000"), "PF_ETHUSD": Decimal("3000")}
            )
            mock_client.get_futures_balance = AsyncMock(
                return_value={"total": {"USD": 10000}, "free": {"USD": 8000}, "used": {"USD": 2000}}
            )
            mock_client.get_ticker = AsyncMock(return_value={"last": 50000})
            mock_client.get_futures_account_info = AsyncMock(return_value={
                "equity": Decimal("10000"), "margin_used": Decimal("2000"),
                "available_margin": Decimal("8000"), "unrealized_pnl": Decimal("0"),
            })
            mock_client.get_futures_tickers_bulk_full = AsyncMock(return_value={
                "PF_XBTUSD": _make_futures_ticker("PF_XBTUSD", Decimal("50000")),
                "PF_ETHUSD": _make_futures_ticker("PF_ETHUSD", Decimal("3000")),
            })
            mock_client.get_open_orders = AsyncMock(return_value=[])
            daq.return_value.start = AsyncMock()
            daq.return_value.stop = AsyncMock()
            daq.return_value.is_healthy = lambda: True
            daq.return_value.update_symbols = MagicMock()
            cm.return_value.initialize = AsyncMock()
            cm.return_value.update_candles = AsyncMock()
            cm.return_value.get_candles = lambda s, tf: []
            cm.return_value.flush_pending = AsyncMock()
            cm.return_value.pop_futures_fallback_count = lambda: 0
            ksc.return_value.is_active = lambda: False
            ex.return_value.sync_open_orders = AsyncMock()
            ex.return_value.check_order_timeouts = AsyncMock(return_value=0)

            engine = LiveTrading(minimal_config)
            engine.active = True
            engine.markets = ["BTC/USD", "ETH/USD"]
            if hasattr(engine, "_startup_sm"):
                engine._startup_sm.advance_to(StartupPhase.SYNCING, reason="test")
                engine._startup_sm.advance_to(StartupPhase.RECONCILING, reason="test")
                engine._startup_sm.advance_to(StartupPhase.READY, reason="test")
            symbols = engine._market_symbols()
            assert isinstance(symbols, list)
            assert "BTC/USD" in symbols and "ETH/USD" in symbols
            await engine._tick()

    asyncio.run(_run())


def _mock_db():
    mock_db = MagicMock()
    mock_session = MagicMock()
    mock_session.query.return_value.filter.return_value.count.return_value = 0
    mock_session.query.return_value.filter.return_value.all.return_value = []
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=mock_session)
    cm.__exit__ = MagicMock(return_value=False)
    mock_db.get_session.return_value = cm
    mock_db.database_url = "postgresql://localhost/test_db"
    return mock_db


def test_market_symbols_list(minimal_config, mock_env):
    """_market_symbols() returns list when markets is list (no .keys() crash)."""
    with patch("src.live.live_trading.KrakenClient", MagicMock()), \
         patch("src.live.live_trading.DataAcquisition", MagicMock()), \
         patch("src.live.live_trading.CandleManager", MagicMock()), \
         patch("src.live.live_trading.DatabasePruner", MagicMock()), \
         patch("src.storage.db.get_db", side_effect=_mock_db), \
         patch("src.storage.repository.get_db", side_effect=_mock_db):
        engine = LiveTrading(minimal_config)
        engine.markets = ["BTC/USD", "ETH/USD"]
        out = engine._market_symbols()
    assert isinstance(out, list)
    assert out == ["BTC/USD", "ETH/USD"]


def test_market_symbols_dict(minimal_config, mock_env):
    """_market_symbols() returns list of keys when markets is dict (e.g. after discovery)."""
    with patch("src.live.live_trading.KrakenClient", MagicMock()), \
         patch("src.live.live_trading.DataAcquisition", MagicMock()), \
         patch("src.live.live_trading.CandleManager", MagicMock()), \
         patch("src.live.live_trading.DatabasePruner", MagicMock()), \
         patch("src.storage.db.get_db", side_effect=_mock_db), \
         patch("src.storage.repository.get_db", side_effect=_mock_db):
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
             patch("src.storage.db.get_db", side_effect=_mock_db), \
             patch("src.storage.repository.get_db", side_effect=_mock_db), \
             patch("src.live.live_trading.KrakenClient") as kc, \
             patch("src.live.live_trading.DataAcquisition", MagicMock()), \
             patch("src.live.live_trading.CandleManager") as cm, \
             patch("src.live.live_trading.KillSwitch") as ksc, \
             patch("src.live.live_trading.Executor") as ex, \
             patch("src.live.live_trading.DatabasePruner", MagicMock()), \
             patch("src.live.live_trading.record_metrics_snapshot"):
            mock_client = AsyncMock()
            kc.return_value = mock_client
            mock_client.has_valid_futures_credentials = lambda: True
            mock_client.get_all_futures_positions = AsyncMock(return_value=[])
            mock_client.get_spot_tickers_bulk = AsyncMock(
                return_value={"BTC/USD": {"last": 50000}, "ETH/USD": {"last": 3000}}
            )
            mock_client.get_futures_tickers_bulk = AsyncMock(
                return_value={
                    "PF_XBTUSD": Decimal("50000"),
                    "PF_ETHUSD": Decimal("3000"),
                    "PF_ZILUSD": Decimal("0.02"),
                }
            )
            mock_client.get_futures_tickers_bulk_full = AsyncMock(
                return_value={
                    "PF_XBTUSD": _make_futures_ticker("PF_XBTUSD", Decimal("50000")),
                    "PF_ETHUSD": _make_futures_ticker("PF_ETHUSD", Decimal("3000")),
                    "PF_ZILUSD": _make_futures_ticker("PF_ZILUSD", Decimal("0.02")),
                }
            )
            mock_client.get_futures_balance = AsyncMock(
                return_value={"total": {"USD": 10000}, "free": {"USD": 8000}, "used": {"USD": 2000}}
            )
            mock_client.get_futures_account_info = AsyncMock(return_value={
                "equity": Decimal("10000"), "margin_used": Decimal("2000"),
                "available_margin": Decimal("8000"), "unrealized_pnl": Decimal("0"),
            })
            mock_client.get_open_orders = AsyncMock(return_value=[])
            ksc.return_value.is_active = lambda: False
            ex.return_value.sync_open_orders = AsyncMock()
            ex.return_value.check_order_timeouts = AsyncMock(return_value=0)
            cm.return_value.initialize = AsyncMock()
            cm.return_value.update_candles = AsyncMock()
            # Return enough candles for Stage B (4h >= 250, with fresh timestamps)
            def _get_candles(s, tf):
                if tf == "4h":
                    return [_make_candle(hours_ago=i * 4) for i in range(260)]
                if tf == "15m":
                    return [_make_candle(hours_ago=i * 0.25) for i in range(60)]
                if tf == "1h":
                    return [_make_candle(hours_ago=i) for i in range(60)]
                if tf == "1d":
                    return [_make_candle(hours_ago=i * 24) for i in range(30)]
                return []
            cm.return_value.get_candles = _get_candles
            cm.return_value.flush_pending = AsyncMock()
            cm.return_value.pop_futures_fallback_count = lambda: 0

            minimal_config.exchange.spot_markets = ["BTC/USD", "ETH/USD", "ZIL/USD"]
            engine = LiveTrading(minimal_config)
            engine.active = True
            engine.markets = ["BTC/USD", "ETH/USD", "ZIL/USD"]
            if hasattr(engine, "_startup_sm"):
                engine._startup_sm.advance_to(StartupPhase.SYNCING, reason="test")
                engine._startup_sm.advance_to(StartupPhase.RECONCILING, reason="test")
                engine._startup_sm.advance_to(StartupPhase.READY, reason="test")
            await engine._tick()

    asyncio.run(_run())
