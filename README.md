# Kraken Futures SMC Trading System

A professional-grade algorithmic trading system for Kraken Futures perpetual contracts utilizing Smart Money Concepts (SMC) methodology.

## Overview

This system executes directional long/short trades on Kraken Futures (BTCUSD-PERP, ETHUSD-PERP) with:
- **Spot signals, futures execution** architecture
- **10× leverage cap** with liquidation-aware risk management
- **Progressive deployment:** Backtesting → Paper Trading → Live Trading
- **Fail-safe design:** Kill switch, state reconciliation, basis guards

## Key Features

### Architecture
- Strategy analyzes **spot market data** (BTC/USD, ETH/USD)
- Execution on **futures perpetuals** (BTCUSD-PERP, ETHUSD-PERP)
- Mark price for all safety-critical operations
- Spot-perp basis guards (pre-entry and post-entry)

### Risk Management
- Position sizing independent of leverage
- Exchange-reported liquidation distance (30-40% minimum buffer)
- Cost-aware validation (fees + funding)
- No pyramiding by default

### Trading Modes
- **Backtest:** Spot data with futures cost simulation
- **Paper:** Real-time data, simulated execution
- **Live:** Real Kraken Futures orders (gated by paper trading success)

## Quick Start

### Prerequisites
- Python 3.11+
- Docker (for PostgreSQL)
- Kraken Futures API credentials

### Installation

```bash
# Clone repository
git clone <repository-url>
cd ProjectTrading

# Create virtual environment
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set up environment variables
cp .env.example .env
# Edit .env with your Kraken API credentials

# Start PostgreSQL
docker-compose up -d
```

### Configuration

Edit `src/config/config.yaml` to customize:
- Risk parameters (leverage cap, risk per trade, daily loss limit)
- Strategy parameters (indicator periods, SMC thresholds)
- Execution settings (slippage, basis thresholds)
- Alert thresholds

See [Configuration Guide](docs/configuration.md) for details (coming soon).

### Production runtime

Live trading in production runs **`run.py live`** → `LiveTrading` (`src/live/live_trading.py`). The `main.py` (DataService + TradingService) path is an alternative architecture and is **not** used in production. See [docs/PRODUCTION_RUNTIME.md](docs/PRODUCTION_RUNTIME.md).

## Usage

### Backtesting

```bash
python src/cli.py backtest --start 2024-01-01 --end 2024-12-31
```

### Paper Trading

```bash
python src/cli.py paper
```

### Live Trading

```bash
# WARNING: Real capital at risk
# Only run after paper trading meets success thresholds
python src/cli.py live
```

### Emergency Stop

```bash
python src/cli.py kill-switch --emergency
```

### Check Status

```bash
python src/cli.py status
```

## Project Structure

```
ProjectTrading/
├── src/
│   ├── config/          # Configuration (Pydantic models + YAML)
│   ├── domain/          # Domain models (Candle, Signal, Position, etc.)
│   ├── storage/         # Database layer
│   ├── data/            # Data acquisition (spot & futures feeds)
│   ├─ strategy/        # SMC signal generation (spot data only)
│   ├── risk/            # Risk management & basis guards
│   ├── execution/       # Futures order execution
│   ├── reconciliation/  # State reconciliation with exchange
│   ├── monitoring/      # Metrics, logging, alerting
│   ├── backtest/        # Backtesting engine
│   ├── paper/           # Paper trading runtime
│   ├── live/            # Live trading runtime
│   ├── utils/           # Kill switch, helpers
│   └── cli.py           # CLI entrypoint
├── tests/               # Test suites
├── tasks/               # PRD and implementation tasks
├── requirements.txt
├── docker-compose.yml
└── README.md
```

## Testing

### Pre-Deployment Testing (MANDATORY)

**Before pushing to main**, always run:

```bash
make pre-deploy
```

This runs:
1. **Smoke test** (30s) - Verifies system starts
2. **Integration test** (5 mins) - Tests signal generation for 20+ symbols

See [TESTING.md](TESTING.md) for full protocol.

### Quick Tests

```bash
# Smoke test only (30s)
make smoke

# Integration test only (5 mins)
make integration

# Unit tests (when available)
make test
```

### Why This Matters

The integration test catches bugs like:
- `trigger_price` UnboundLocalError (caught 2026-01-18)
- Signal generation failures
- Data acquisition issues
- Type errors and logic bugs

**Never skip `make pre-deploy` before pushing to main.**

## Documentation

- [PRD](tasks/prd-kraken-futures-smc-trading.md) - Product Requirements Document
- [Tasks](tasks/tasks-kraken-futures-smc-trading.md) - Implementation Task List
- Configuration Guide (coming soon)
- Operational Guide (coming soon)

## Safety & Compliance

- **Testnet first:** Always validate on Kraken Testnet before live trading
- **Paper trading gate:** Live trading disabled until paper trading meets thresholds
- **Kill switch:** Latched emergency stop (manual restart required)
- **State reconciliation:** Continuous sync with exchange truth
- **Liquidation safety:** 30-40% minimum buffer from liquidation price

## License

[Add your license here]

## Disclaimer

This software is for educational purposes. Trading cryptocurrencies and leveraged futures involves substantial risk. Past performance does not guarantee future results. Use at your own risk.
