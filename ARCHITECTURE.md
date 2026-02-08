# Architecture

## System Overview

Kraken Futures SMC Trading System: an algorithmic trading system that trades cryptocurrency futures on Kraken using Smart Money Concepts (SMC) analysis. It runs 24/7 on a DigitalOcean Droplet as a `systemd` service.

**Entrypoint:** `run.py live` starts the `LiveTrading` runtime, which runs an infinite async loop analyzing ~45 coins every ~60 seconds.

## Directory Structure

```
TradingSystem/
  run.py                          # CLI entrypoint (Typer)
  Makefile                        # Developer workflow targets
  
  src/
    config/                       # Configuration loading and validation
      config.py                   #   Pydantic config models
      config.yaml                 #   Main trading configuration
      safety.yaml                 #   Safety threshold overrides
    
    data/                         # Market data layer
      kraken_client.py            #   Kraken REST + WebSocket client
      data_acquisition.py         #   OHLCV fetching orchestration
      candle_manager.py           #   Candle caching with bar-boundary optimization
      market_discovery.py         #   Spot-to-futures market pairing
      market_registry.py          #   Tier classification, liquidity filtering
      symbol_utils.py             #   Symbol normalization (single source of truth)
      fiat_currencies.py          #   Fiat/stablecoin exclusion lists
    
    domain/                       # Domain models and protocols
      models.py                   #   Candle, Signal, Position, Trade, Order
      protocols.py                #   EventRecorder protocol (dependency inversion)
    
    strategy/                     # Signal generation
      smc_engine.py               #   SMC analysis: bias, structure, entry zones
      fibonacci.py                #   Fibonacci retracement calculations
    
    portfolio/                    # Capital allocation
      auction_allocator.py        #   Deterministic auction for position selection
    
    risk/                         # Risk management
      risk_manager.py             #   Position sizing, margin checks, daily loss
      symbol_cooldown.py          #   Per-symbol loss tracking and cooldowns
    
    execution/                    # Order execution and position management
      execution_gateway.py        #   Order submission, tracking, event routing
      executor.py                 #   Order lifecycle, SL/TP placement
      position_manager_v2.py      #   Position state machine, reconciliation
      position_state_machine.py   #   ManagedPosition states, PositionRegistry
      futures_adapter.py          #   Spot-to-futures symbol mapping
      instrument_specs.py         #   Contract specifications (tick size, lot size)
      production_safety.py        #   ProtectionEnforcer, PositionProtectionMonitor
      production_takeover.py      #   Startup: adopt exchange positions into registry
      equity.py                   #   Effective equity calculation
    
    live/                         # Live trading runtime (decomposed)
      live_trading.py             #   Core loop + tick orchestration
      protection_ops.py           #   SL reconciliation, TP backfill, orphan cleanup
      health_monitor.py           #   Order polling, protection checks, daily summary
      auction_runner.py           #   Auction allocation execution
      exchange_sync.py            #   Position sync, account state, trade history
      signal_handler.py           #   Signal processing (v1/v2 paths)
      coin_processor.py           #   Symbol filtering, universe discovery
      startup_validator.py        #   Startup checks (decision traces)
      maintenance.py              #   Periodic data maintenance
    
    safety/                       # Production safety layer
      invariant_monitor.py        #   Margin, equity, position invariants
      integration.py              #   ProductionHardeningLayer
    
    reconciliation/               # Position reconciliation
      reconciler.py               #   Exchange vs internal state sync
    
    storage/                      # Persistence (PostgreSQL)
      repository.py               #   ORM models, CRUD operations
      maintenance.py              #   Database pruning
    
    monitoring/                   # Observability
      logger.py                   #   Structured logging (structlog)
      alerting.py                 #   Telegram/Discord webhooks
      telegram_bot.py             #   Interactive Telegram commands
    
    utils/                        # Utilities
      kill_switch.py              #   Emergency halt (preserves SL orders)
    
    dashboard/                    # Web dashboard
      static_dashboard.py         #   HTML dashboard served via HTTP
    
    tools/                        # Operational tools
      place_missing_stops.py      #   Manual SL placement for unprotected positions
      audit_open_orders.py        #   Order audit utility
  
  tests/                          # Test suite (330 tests)
    unit/                         #   Unit tests
    integration/                  #   Integration tests
    fixtures/                     #   Test data (golden snapshots)
    conftest.py                   #   Shared fixtures and mocking
  
  scripts/                        # Deployment and operations
    trading-system.service        #   systemd unit file
    real_exchange/                #   Manual exchange interaction scripts
  
  docs/                           # Active documentation
    archive/                      #   Historical docs (114 archived files)
```

## Data Flow

```
                            ┌─────────────────────────┐
                            │     Kraken Exchange      │
                            │  (REST API + WebSocket)  │
                            └───────────┬─────────────┘
                                        │
                    ┌───────────────────┼───────────────────┐
                    ▼                   ▼                   ▼
            ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
            │ MarketDiscovery│ │DataAcquisition│ │  KrakenClient │
            │ (spot/futures  │ │  (OHLCV data) │ │(orders, pos.) │
            │   pairing)     │ │               │ │               │
            └───────┬────────┘ └──────┬────────┘ └───────┬───────┘
                    │                 │                   │
                    ▼                 ▼                   │
            ┌──────────────┐  ┌──────────────┐           │
            │MarketRegistry │  │CandleManager │           │
            │(tier classify)│  │(bar caching) │           │
            └───────┬───────┘ └──────┬────────┘           │
                    │                │                    │
                    └───────┬────────┘                    │
                            ▼                             │
                    ┌──────────────┐                      │
                    │  SMC Engine   │                      │
                    │(signal gen.)  │                      │
                    └───────┬───────┘                      │
                            │ signals                     │
                            ▼                             │
                    ┌──────────────┐                      │
                    │   Auction    │                      │
                    │  Allocator   │                      │
                    └───────┬───────┘                      │
                            │ selected                    │
                            ▼                             │
                    ┌──────────────┐                      │
                    │ Risk Manager │                      │
                    │  (sizing)    │                      │
                    └───────┬───────┘                      │
                            │ validated                   │
                            ▼                             │
                    ┌──────────────┐      ┌──────────────┐│
                    │  Execution   │─────▶│  Position    ││
                    │   Gateway    │      │  Registry    ││
                    └───────┬───────┘      └──────────────┘│
                            │ orders                      │
                            ▼                             │
                    ┌──────────────┐                      │
                    │   Executor   │──────────────────────┘
                    │ (SL/TP/entry)│     place orders
                    └──────────────┘
```

## Database Architecture

### PostgreSQL (Production)
**Connection:** `DATABASE_URL` environment variable.
**ORM:** SQLAlchemy with connection pooling.

| Table | Purpose | Key Indexes |
|-------|---------|-------------|
| `candles` | OHLCV price data (15m, 1h, 4h, 1d) | `(symbol, timeframe, timestamp)` |
| `trades` | Completed trade history with P&L | `(symbol, entered_at)`, `(symbol, exited_at)` |
| `positions` | Current open position state | Primary key: `symbol` |
| `system_events` | Audit trail (signals, decisions, alerts) | `(event_type, timestamp)`, `(decision_id)` |
| `account_state` | Balance/equity snapshots over time | `(timestamp)` |

### SQLite (Position State Machine)
**Location:** `data/positions.db`
**Purpose:** WAL-journaled persistence for `PositionRegistry` (the V2 position state machine). Survives process restarts.

## Deployment

### Target: DigitalOcean Droplet

```
Server: 207.154.193.121
SSH Key: ~/.ssh/trading_droplet
User: root (SSH) -> sudo -u trading (commands)
Code: /home/trading/TradingSystem
Venv: /home/trading/TradingSystem/venv
Logs: /home/trading/TradingSystem/logs/run.log
```

### systemd Service (`scripts/trading-system.service`)

```ini
[Service]
User=trading
WorkingDirectory=/home/trading/TradingSystem
EnvironmentFile=/home/trading/TradingSystem/.env.production
ExecStartPre=/home/trading/TradingSystem/venv/bin/python migrate_schema.py
ExecStart=/home/trading/TradingSystem/venv/bin/python run.py live
Restart=always
RestartSec=30
MemoryMax=1G
LimitNOFILE=65536
TimeoutStopSec=60
Wants=postgresql.service
```

### Deploy Sequence
```bash
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  "cd /home/trading/TradingSystem && sudo -u trading git pull && systemctl restart trading-bot.service"
```

## Safety Architecture

The system has multiple layers of protection:

1. **Pre-trade gates** (RiskManager): Position sizing caps, margin checks, daily loss limit, aggregate notional cap
2. **Invariant monitor** (InvariantMonitor): Continuous checks for margin utilization, equity drawdown, position count
3. **Kill switch** (KillSwitch): Emergency halt that preserves stop-loss orders, cancels entries and TPs
4. **Protection monitor** (PositionProtectionMonitor): Background loop detecting naked positions with escalation policy
5. **Auto-recovery**: Margin-critical halts auto-clear when margin drops below 85% (max 2x/day, 5min cooldown)

### Threshold Relationships (Critical)
```
auction_max_margin_util (0.90) < max_margin_utilization_pct (0.92) < 1.0
degraded_margin_utilization_pct (0.85) <= auction_max_margin_util (0.90)
```

## Key Design Decisions

1. **Delegate pattern for live_trading.py decomposition**: Extracted functions receive `lt: "LiveTrading"` to access shared state, rather than passing 10+ individual dependencies. This minimizes regression risk while achieving clean module boundaries.

2. **EventRecorder protocol**: `SMCEngine` and `RiskManager` accept event recorders via constructor injection, enabling clean unit testing without module-level mocking.

3. **symbol_utils.py as single source of truth**: All symbol normalization goes through `src/data/symbol_utils.py`. Six functions cover every conversion pattern: position matching, base extraction, PF-to-unified, futures candidates, symbol comparison, and position side detection.

4. **Persistent aiohttp session**: `KrakenClient` uses a single `aiohttp.ClientSession` with connection pooling instead of creating per-request sessions, reducing TCP/TLS overhead on every API call.

5. **SQLAlchemy pool over raw psycopg2**: Hot-path DB queries use the SQLAlchemy connection pool with TTL caching, avoiding connection churn and enabling consistent transaction management.
