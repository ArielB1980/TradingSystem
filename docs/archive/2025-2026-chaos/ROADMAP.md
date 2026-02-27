# Development Roadmap

## ✅ Phase 1: Foundation (COMPLETE)
All core infrastructure implemented and tested.

## 🔄 Phase 2: Kraken Futures API Integration (IN PROGRESS)

### Priority 1: Mark Price & Positions ✅
1. **Mark price fetching** — `KrakenClient.get_futures_mark_price()` implemented
2. **Position fetching** — `KrakenClient.get_all_futures_positions()` implemented
3. **Order placement** — FuturesAdapter + ExecutionGateway place orders via Kraken Futures API

### Priority 2: WebSocket Real-Time Feeds
4. **Spot candle streams** (`src/data/data_acquisition.py`)
   - Connect to Kraken spot WebSocket
   - Subscribe to OHLC channels (15m, 1h, 4h, 1d)
   - Buffer and validate candle data
   - Trigger signal generation on candle close

5. **Futures updates** (`src/data/data_acquisition.py`)
   - Connect to Kraken Futures WebSocket
   - Subscribe to mark price updates
   - Subscribe to position/margin updates
   - Subscribe to order fill notifications

## 🔄 Phase 3: Trading Runtimes

### Backtest Engine
1. **Historical data loader**
   - Fetch spot OHLCV from ccxt/Kraken API
   - Store in database via repository
   - Create data replay iterator

2. **Backtesting logic**
   - Replay candles chronologically
   - Generate signals on each candle close
   - Simulate futures fills with slippage/fees/basis
   - Track P&L and performance metrics
   - Generate performance report

### Paper Trading
1. **Real-time data integration**
   - Connect to live WebSocket feeds
   - Signal generation on live candles
   - Simulate order fills with realistic delays

2. **Position simulation**
   - Track simulated positions
   - Calculate unrealized P&L
   - Simulate stop-loss/take-profit triggers

### Live Trading
1. **Safety gates implementation**
   - Check paper trading success metrics
   - Validate API credentials on startup
   - Emergency kill switch integration

2. **Production trading loop**
   - Real signal generation
   - Real order placement via Kraken Futures API
   - Real-time reconciliation
   - Alert on critical events

## 🔄 Phase 4: Testing & Validation

### Test Coverage
- [ ] Increase to 80%+ coverage
- [ ] Integration tests with mocked Kraken API
- [ ] End-to-end tests (backtest → paper → signals)
- [ ] Performance tests (latency, throughput)

### Validation
- [ ] Run determinism tests with replay data
- [ ] Validate indicator calculations against known TA libraries
- [ ] Stress test with high-frequency data
- [ ] Test all failure modes (network, API, data)

## 🔄 Phase 5: Production Readiness

### Operations
- [ ] Docker deployment setup
- [ ] Monitoring dashboards (Grafana/Prometheus)
- [ ] Log aggregation (ELK stack or similar)
- [ ] Alert delivery (Telegram/Slack/Email)

### Documentation
- [ ] API documentation
- [ ] Deployment guide
- [ ] Troubleshooting guide
- [ ] Performance tuning guide

### Security
- [ ] Secure credential storage (HashiCorp Vault or similar)
- [ ] Rate limiting enforcement
- [ ] API key rotation procedure
- [ ] Audit logging

## Quick Start for Next Session

```bash
# 1. Research Kraken Futures API
# Visit: https://docs.futures.kraken.com/

# 2. Test Kraken Futures authentication
# Create test script to verify API credentials work

# 3. Implement mark price fetching
# Edit: src/data/kraken_client.py
# Method: get_futures_mark_price()

# 4. Write integration test
# Create: tests/integration/test_kraken_futures_api.py
# Test against Kraken testnet/demo trading

# 5. Implement simple backtest
# Edit: src/backtest/backtest_engine.py
# Test with 1 month of BTC data
```

## Useful Resources
- Kraken Futures API Docs: https://docs.futures.kraken.com/
- Kraken Spot API Docs: https://docs.kraken.com/rest/
- ccxt Kraken: https://docs.ccxt.com/en/latest/exchange-markets.html#kraken
- SMC Trading References: (add your preferred resources)
