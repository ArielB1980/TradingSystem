# Kraken Futures SMC Trading System

A production-grade algorithmic trading system for Kraken Futures perpetual contracts using Smart Money Concepts (SMC) methodology. Deployed on DigitalOcean via systemd.

## Overview

This system trades ~27 Kraken Futures perpetuals with:
- **Spot signals, futures execution** architecture (SMC strategy on spot data, orders on futures)
- **10× leverage cap** with liquidation-aware risk management
- **Production hardening:** Invariant monitor, circuit breaker, kill switch, position reconciliation
- **Fail-safe design:** Startup state machine, atomic stop replacement, write-ahead intent log

## Quick Start

```bash
# Setup
make venv          # Create .venv and install dependencies
cp .env.local.example .env.local   # Configure API keys

# Run
make smoke         # 30-second smoke test (validates full startup)
make run           # Start live trading (loads .env.local)
make logs          # Tail recent logs
```

### Production Runtime

Live trading runs via: `python -m src.entrypoints.prod_live` → `LiveTrading`

See [docs/PRODUCTION_RUNTIME.md](docs/PRODUCTION_RUNTIME.md) for canonical entrypoints.

## Project Structure

```
TradingSystem/
├── src/
│   ├── config/          # Configuration (Pydantic + YAML + safety.yaml)
│   ├── domain/          # Domain models (Candle, Signal, Position)
│   ├── storage/         # Database layer (PostgreSQL + SQLite)
│   ├── data/            # Data acquisition + KrakenClient
│   ├── strategy/        # SMC signal generation (spot data)
│   ├── risk/            # Risk management, basis guards, auction mode
│   ├── execution/       # Order execution, state machine, gateway
│   ├── runtime/         # Startup state machine (P2.3)
│   ├── safety/          # Invariant monitor, hardening layer
│   ├── reconciliation/  # Exchange state reconciliation
│   ├── monitoring/      # Metrics, logging, Telegram alerts
│   ├── live/            # Live trading runtime
│   ├── utils/           # Kill switch, circuit breaker, helpers
│   ├── tools/           # Promoted operational tools (--dry-run default)
│   └── entrypoints/     # prod_live.py (canonical entry)
├── tests/
│   ├── unit/            # Unit tests
│   └── integration/     # Lifecycle integration tests
├── scripts/             # Deployment + operational scripts
│   └── archive/         # Archived debug scripts
├── docs/                # Documentation
└── Makefile
```

## Testing

```bash
make test          # Unit tests
make smoke         # 30-second smoke test
make pre-deploy    # Full pre-deployment validation
```

## Documentation

| Document | Purpose |
|----------|---------|
| [README.md](README.md) | This file — quickstart and overview |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design, data flow, components |
| [docs/OPERATIONS.md](docs/OPERATIONS.md) | Runbooks: deploy, recovery, tools, log patterns |
| [FORAI.md](FORAI.md) | Institutional memory: lessons learned, bugs fixed |
| [docs/PRODUCTION_RUNTIME.md](docs/PRODUCTION_RUNTIME.md) | Canonical entrypoints and safety requirements |
| [docs/PHASE2_PROFITABILITY_PLAN.md](docs/PHASE2_PROFITABILITY_PLAN.md) | Post-deploy profitability optimization roadmap and experiment cadence |

## Safety

- **Invariant Monitor:** Hard limits on equity drawdown, margin utilization, concurrent positions
- **Circuit Breaker:** API-level protection against exchange outages (P2.1)
- **Kill Switch:** Emergency halt preserving stop-loss orders
- **Startup State Machine:** INITIALIZING → SYNCING → RECONCILING → READY (P2.3)
- **Exception Hierarchy:** OperationalError (retry) / DataError (skip) / InvariantError (halt) (P2.2)
- **Atomic Stop Replacement:** New stop acknowledged before old cancelled

## License

[Add your license here]

## Disclaimer

This software is for educational purposes. Trading cryptocurrencies and leveraged futures involves substantial risk. Past performance does not guarantee future results. Use at your own risk.
