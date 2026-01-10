# Implementation Tasks: Kraken Futures SMC Trading System

## Relevant Files

- `src/config/config.py` - System configuration using Pydantic
- `src/config/config.yaml` - Default configuration file
- `src/domain/models.py` - Domain models (Candle, Signal, OrderIntent, Position, RiskDecision, etc.)
- `src/storage/db.py` - DB engine/session + migrations hook
- `src/storage/repository.py` - Persistence functions (candles, trades, positions, metrics)
- `src/data/data_acquisition.py` - Spot and futures data feed management
- `src/data/kraken_client.py` - Kraken REST API and WebSocket client
- `src/data/orderbook.py` - Futures mark price and best-bid/ask tracking (no strategy logic)
- `src/strategy/smc_engine.py` - SMC signal generation from spot data
- `src/strategy/indicators.py` - Technical indicators (EMA, ADX, ATR, RSI)
- `src/risk/risk_manager.py` - Position sizing and risk validation
- `src/risk/basis_guard.py` - Spot-perp basis monitoring
- `src/execution/executor.py` - Order execution and management
- `src/execution/price_converter.py` - Spot-to-futures price conversion
- `src/execution/futures_adapter.py` - Kraken Futures adapter with spot-to-futures mapping
- `src/reconciliation/reconciler.py` - State reconciliation with exchange
- `src/monitoring/metrics.py` - Real-time metrics and alerting
- `src/monitoring/logger.py` - Structured logging setup
- `src/backtest/backtest_engine.py` - Backtesting on spot data with futures cost simulation
- `src/paper/paper_trading.py` - Paper trading engine
- `src/live/live_trading.py` - Live trading engine with safety gates
- `src/utils/kill_switch.py` - Emergency kill switch with latching
- `src/cli.py` - CLI entrypoint with commands (backtest/paper/live/status/kill-switch)
- `tests/unit/test_smc_engine.py` - SMC logic unit tests
- `tests/unit/test_risk_manager.py` - Risk management unit tests
- `tests/integration/test_kraken_adapter.py` - Kraken Futures integration tests
- `tests/integration/test_price_conversion.py` - Price conversion tests
- `tests/replay/test_deterministic_signals.py` - Replay tests for determinism
- `tests/failure_modes/test_kill_switch.py` - Failure mode tests
- `README.md` - Setup and usage documentation
- `docker-compose.yml` - Docker orchestration
- `requirements.txt` - Python dependencies

### Notes

- Tests live under `tests/` grouped by `unit/`, `integration/`, `replay/`, and `failure_modes/`
- Run tests with `pytest tests/` or `pytest tests/unit/test_specific.py` for specific files
- All configuration should be externalized to `src/config/config.yaml` with Pydantic validation
- No magic constants or environment-dependent logic in code

## Instructions for Completing Tasks

**IMPORTANT:** As you complete each task, you must check it off in this markdown file by changing `- [ ]` to `- [x]`. This helps track progress and ensures you don't skip any steps.

Example:
- `- [ ] 1.1 Read file` → `- [x] 1.1 Read file` (after completing)

Update the file after completing each sub-task, not just after completing an entire parent task.

---

## Design Locks (Non-Negotiable)

These are the architectural invariants that MUST NOT be violated under any circumstances:

- **Strategy consumes spot data only** - No futures prices, funding data, or order book data may be accessed by the strategy engine
- **Execution consumes futures data only** - All orders are placed on futures perpetuals using mark price
- **Mark price is the sole risk reference** - Liquidation, stops, and risk calculations MUST use mark price, never last price
- **Leverage is a cap, not a target** - 10× is the maximum; actual leverage is dynamically determined by stop distance
- **Kill switch is latched** - Once triggered, system cannot auto-resume; manual acknowledgment required
- **No pyramiding by default** - Adding to positions is disabled unless explicitly enabled in config
- **Basis guard is mandatory** - All entries subject to spot-perp divergence validation

---

## Tasks

**Phase 0 — Repository & Branching**

- [ ] 0.0 Create feature branch
  - [ ] 0.1 Create and checkout branch: `git checkout -b feature/kraken-futures-smc-system`

**Phase 1 — Project Setup & Core Infrastructure**

- [ ] 1.0 Project setup & scaffolding
  - [ ] 1.1 Create directory structure (`src/`, `tests/`, `src/config/`, `src/domain/`, `src/storage/`, etc.)
  - [ ] 1.2 Create `requirements.txt` with all dependencies (ccxt, pandas, numpy, pandas-ta, pydantic, pytest, structlog, typer, etc.)
  - [ ] 1.3 Create `docker-compose.yml` with PostgreSQL service (initially can use SQLite for development)
  - [ ] 1.4 Create `.env.example` file for environment variables (API keys, config paths)
  - [ ] 1.5 Create `.gitignore` for Python project
  - [ ] 1.6 Initialize `README.md` with basic project description
  - [ ] 1.7 Create `src/config/config.py` with Pydantic configuration models
  - [ ] 1.8 Create `src/config/config.yaml` with default configuration values
  - [ ] 1.9 Implement configuration validation on startup (fail-fast with clear errors)
  - [ ] 1.10 Create `src/monitoring/logger.py` for structured logging setup (JSON format, all required fields)
  - [ ] 1.11 Create `src/domain/models.py` with domain models (Candle, Signal, OrderIntent, Position, RiskDecision, etc.)
  - [ ] 1.12 Create `src/storage/db.py` with DB engine/session + migrations hook
  - [ ] 1.13 Create `src/storage/repository.py` with persistence functions (candles, trades, positions, metrics)
  - [ ] 1.14 Create `src/cli.py` with CLI entrypoint (use typer or argparse)
  - [ ] 1.15 Implement CLI commands: backtest, paper, live, status, kill-switch

**Exit Criteria:** Configuration loads successfully, validates all parameters, and structured logging outputs valid JSON with all required fields.

**Phase 2 — Data Acquisition Layer (Spot & Futures)**

- [ ] 2.0 Market data ingestion
  - [ ] 2.1 Create `src/data/kraken_client.py` with base REST client for Kraken
  - [ ] 2.2 Implement authentication (API key + secret HMAC signature)
  - [ ] 2.3 Implement token-bucket rate limiter configurable per endpoint group
  - [ ] 2.4 Add REST methods for spot market data (BTC/USD, ETH/USD OHLCV)
  - [ ] 2.5 Add REST methods for futures market data (BTCUSD-PERP, ETHUSD-PERP positions, margin, liquidation price)
  - [ ] 2.6 Implement WebSocket client for real-time spot data feeds
  - [ ] 2.7 Implement WebSocket client for real-time futures data feeds (order updates, fills, margin updates)
  - [ ] 2.8 Add WebSocket reconnection logic with exponential backoff
  - [ ] 2.9 Create `src/data/orderbook.py` for futures mark price and best-bid/ask tracking
  - [ ] 2.10 Mark price MUST be sourced from Kraken Futures mark/index feed (authoritative), not computed from bid/ask
  - [ ] 2.11 Create `src/data/data_acquisition.py` orchestrator for managing spot & futures feeds
  - [ ] 2.12 Implement data validation (no gaps, no duplicate timestamps)
  - [ ] 2.13 Add graceful failure handling (data feed failure → halt entries, manage exits only)
  - [ ] 2.14 Implement data storage via `src/storage/repository.py` to PostgreSQL/SQLite (OHLCV, positions, trades)

**Exit Criteria:** Spot and futures data streams are stable for ≥24h with no gaps, reconnections logged, and mark price correctly tracked.

**Phase 3 — Strategy Engine (SMC on Spot Data)**

**Constraint:** No futures prices, funding data, or order book data may be accessed by the strategy engine.

- [ ] 3.0 SMC signal engine
  - [ ] 3.1 Create `src/strategy/indicators.py` with technical indicator calculations
  - [ ] 3.2 Implement EMA 200 calculation (higher-timeframe bias)
  - [ ] 3.3 Implement ADX calculation (trend strength filter)
  - [ ] 3.4 Implement ATR calculation (volatility measurement for stop sizing)
  - [ ] 3.5 Implement RSI calculation (optional divergence confirmation)
  - [ ] 3.6 Create `src/strategy/smc_engine.py` for SMC signal generation
  - [ ] 3.7 Implement SMC structure detection (order blocks, fair value gaps, break of structure) on spot data
  - [ ] 3.8 Implement higher-timeframe bias logic (4H/1D spot candles)
  - [ ] 3.9 Implement execution timeframe signal logic (15m/1H spot candles)
  - [ ] 3.10 Add entry rule validation (structure + bias + ADX + ATR filters)
  - [ ] 3.11 Implement exit rule logic (stop-loss from SMC invalidation + ATR, take-profit from next SMC level)
  - [ ] 3.12 Add full reasoning logs for each signal decision
  - [ ] 3.13 Ensure deterministic behavior (same input → same signal)
  - [ ] 3.14 Make all parameters configurable (no hardcoded values)

**Exit Criteria:** Strategy generates deterministic signals from spot data only; no futures data is accessed; all signals include full reasoning logs.

**Phase 4 — Risk Management & Basis Guards**

- [ ] 4.0 Risk validation & liquidation safety
  - [ ] 4.1 Create `src/risk/risk_manager.py` for position sizing and risk validation
  - [ ] 4.2 Implement correct position sizing formula: `position_notional = (account_equity × risk_pct) / stop_distance_pct`
  - [ ] 4.3 Calculate margin usage: `margin_used = position_notional / leverage`
  - [ ] 4.4 Implement liquidation distance calculator using exchange-reported values
  - [ ] 4.5 Add liquidation distance formula (directional): Long: `(mark_price - liq_price) / mark_price`, Short: `(liq_price - mark_price) / mark_price` (must be > buffer for both)
  - [ ] 4.6 Enforce minimum liquidation buffer (30-40%, reject trade if too close)
  - [ ] 4.7 Implement effective leverage monitoring: `total_position_notional / account_equity`
  - [ ] 4.8 Add portfolio-level risk limits (max concurrent positions, daily loss limit, loss streak cooldown)
  - [ ] 4.9 Enforce non-negotiable rules (no leverage escalation, no widening stops, no averaging down)
  - [ ] 4.10 Create `src/risk/basis_guard.py` for spot-perp basis monitoring
  - [ ] 4.11 Implement pre-entry basis guard: reject if `abs(spot_price - perp_mark_price) / spot_price > basis_max`
  - [ ] 4.12 Implement post-entry basis risk handling (disallow pyramiding, log basis risk state)
  - [ ] 4.13 Add optional funding cost risk control (block entries if projected funding exceeds threshold)
  - [ ] 4.14 Implement cost tracking (maker/taker fees, funding payments, slippage)
  - [ ] 4.15 Reject trades where estimated fees + funding materially distort R:R beyond a configurable threshold

**Exit Criteria:** All trades pass liquidation buffer checks (30-40%), basis guards enforce divergence limits, and cost-aware validation rejects trades with distorted R:R.

**Phase 5 — Execution Layer (Futures with Price Conversion)**

- [ ] 5.0 Futures execution & order lifecycle
  - [ ] 5.1 Create `src/execution/price_converter.py` for spot-to-futures price conversion
  - [ ] 5.2 Implement percentage-based conversion (spot levels → futures mark price distances)
  - [ ] 5.3 Add example: spot stop 2% below entry → futures stop 2% below futures mark
  - [ ] 5.4 Enforce mark price usage for all conversions (never last price)
  - [ ] 5.5 Create `src/execution/futures_adapter.py` for Kraken Futures order execution
  - [ ] 5.6 Implement spot-to-futures ticker mapping (BTC/USD → BTCUSD-PERP)
  - [ ] 5.7 Add leverage setting per order (max 10× cap)
  - [ ] 5.8 Implement reduce-only order support for SL/TP
  - [ ] 5.9 Create `src/execution/executor.py` for order lifecycle orchestration
  - [ ] 5.10 Implement idempotent order handling (no duplicates, use client_order_id)
  - [ ] 5.11 Add ghost order detection and cleanup
  - [ ] 5.12 Implement SL/TP placement immediately after entry fill confirmation
  - [ ] 5.13 Add order state machine: PENDING → SUBMITTED → FILLED/CANCELLED/REJECTED
  - [ ] 5.14 Implement dry-run capability (validate without sending to exchange)
  - [ ] 5.15 Add exchange error handling (insufficient margin, order rejection, rate limiting, timeouts)
  - [ ] 5.16 Implement retry logic with exponential backoff (max retries defined)
  - [ ] 5.17 Implement pyramiding guard: executor rejects add-to-position unless pyramiding.enabled=true (default: disabled)

**Exit Criteria:** Orders execute on futures using mark price for all safety-critical operations; SL/TP placed immediately after fills; pyramiding disabled by default.

**Phase 6 — State Reconciliation & Monitoring**

- [ ] 6.0 Reconciliation, metrics & alerting
  - [ ] 6.1 Create `src/reconciliation/reconciler.py` for state reconciliation
  - [ ] 6.2 Implement event-driven reconciliation (immediate on fills, order updates, margin updates, position updates)
  - [ ] 6.3 Implement periodic hard reconciliation (default every 15 seconds, configurable)
  - [ ] 6.4 Fetch and reconcile: positions (size, side, entry, PnL, liquidation price)
  - [ ] 6.5 Fetch and reconcile: open orders (ID, type, price, quantity, status)
  - [ ] 6.6 Fetch and reconcile: margin balance and effective leverage
  - [ ] 6.7 Alert on discrepancies (ghost positions, missing orders, margin mismatch)
  - [ ] 6.8 Update system state to match exchange truth
  - [ ] 6.9 Create `src/monitoring/metrics.py` for real-time metrics collection
  - [ ] 6.10 Track: current positions, effective leverage, margin usage, daily PnL, liquidation distance
  - [ ] 6.11 Track: win rate, average win/loss, profit factor, fee/funding costs
  - [ ] 6.12 Create `src/monitoring/alerting.py` for critical event alerts
  - [ ] 6.13 Alert on: margin usage > threshold, liquidation distance < buffer, repeated order rejections
  - [ ] 6.14 Alert on: kill switch activation, data feed disconnection, daily loss limit reached
  - [ ] 6.15 Implement full trade lifecycle logging (market snapshot → signal → order → fill → position → exit)
  - [ ] 6.16 Create `src/utils/kill_switch.py` with latching emergency stop
  - [ ] 6.17 Implement kill switch triggers (manual command, API errors, margin critical, liquidation breach, data failure)
  - [ ] 6.18 Implement kill switch actions (cancel all orders, flatten all positions, latch system)
  - [ ] 6.19 Require manual acknowledgment to restart trading after kill switch

**Exit Criteria:** Reconciliation detects and alerts on all discrepancies; kill switch triggers correctly and latches; all alerts fire within 10 seconds.

**Phase 7 — Backtesting, Paper Trading & Live Trading**

- [ ] 7.0 Trading runtimes
  - [ ] 7.1 Create `src/backtest/backtest_engine.py` for backtesting
  - [ ] 7.2 Implement historical spot OHLCV data replay
  - [ ] 7.3 Simulate futures execution with configurable fill assumptions (market/limit)
  - [ ] 7.4 Model maker/taker fees based on Kraken fee schedule
  - [ ] 7.5 Model funding payments using historical rates
  - [ ] 7.6 Model slippage and spot-perp basis (static or stochastic, configurable)
  - [ ] 7.7 Ensure deterministic results (same data + config → same PnL)
  - [ ] 7.8 Generate backtest report (expectancy, max DD, profit factor, win rate, Sharpe)
  - [ ] 7.9 Create `src/paper/paper_trading.py` for paper trading runtime
  - [ ] 7.10 Connect to real-time spot and futures data feeds
  - [ ] 7.11 Simulate order execution with realistic slippage
  - [ ] 7.12 Track simulated positions with full fee and funding modeling
  - [ ] 7.13 Run full reconciliation logic (as if live, but simulated)
  - [ ] 7.14 Validate: slippage within tolerance, zero missed signals, no margin warnings
  - [ ] 7.15 Create `src/live/live_trading.py` for live trading runtime
  - [ ] 7.16 Implement safety gates (no live trading until paper trading meets thresholds)
  - [ ] 7.17 Connect to Kraken Futures API for real order execution
  - [ ] 7.18 Enforce all risk limits and basis guards
  - [ ] 7.19 Run full reconciliation continuously
  - [ ] 7.20 Integrate kill switch for emergency stops
  - [ ] 7.21 Add environment separation (dev/paper/prod configs)

**Exit Criteria:** Backtests run deterministically with basis modeling; paper trading runtime supports continuous operation + logging + reconciliation + simulated fills; live trading gates enforced.

**Phase 8 — Testing, Validation & Documentation**

- [ ] 8.0 Testing, replay validation & docs
  - [ ] 8.1 Create `tests/unit/test_smc_engine.py` for SMC logic unit tests
  - [ ] 8.2 Test indicator calculations (EMA, ADX, ATR, RSI) against known values
  - [ ] 8.3 Test SMC structure detection (OB, FVG, BOS) with synthetic data
  - [ ] 8.4 Test signal generation determinism (same input → same output)
  - [ ] 8.5 Create `tests/unit/test_risk_manager.py` for risk management unit tests
  - [ ] 8.6 Test position sizing formula correctness
  - [ ] 8.7 Test liquidation distance calculations
  - [ ] 8.8 Test basis guard logic (pre-entry and post-entry)
  - [ ] 8.9 Test portfolio-level risk limits enforcement
  - [ ] 8.10 Create `tests/integration/test_kraken_adapter.py` for Kraken integration tests (mocked)
  - [ ] 8.11 Test REST API calls (account info, positions, margin, liquidation price)
  - [ ] 8.12 Test WebSocket handling (connection, reconnection, message parsing)
  - [ ] 8.13 Test order placement and lifecycle
  - [ ] 8.14 Create `tests/integration/test_price_conversion.py` for price conversion tests
  - [ ] 8.15 Test spot-to-futures percentage-based conversion
  - [ ] 8.16 Test mark price usage (never last price for safety-critical)
  - [ ] 8.17 Create `tests/replay/test_deterministic_signals.py` for replay tests
  - [ ] 8.18 Load historical spot candle data
  - [ ] 8.19 Replay through strategy engine
  - [ ] 8.20 Verify: same data + config = same signals (100% determinism)
  - [ ] 8.21 Create `tests/failure_modes/test_kill_switch.py` for failure-mode tests
  - [ ] 8.22 Test kill switch activation under API failure scenarios
  - [ ] 8.23 Test system behavior during data feed drop mid-position
  - [ ] 8.24 Test reconciliation mismatch detection and resolution
  - [ ] 8.25 Achieve minimum test coverage (core logic 90%+, risk 95%+, execution 80%+, overall 80%+)
  - [ ] 8.26 Add CI command: `pytest --cov=src --cov-fail-under=80` to enforce coverage
  - [ ] 8.27 Update `README.md` with setup instructions
  - [ ] 8.28 Add configuration guide (config.yaml parameters, environment variables)
  - [ ] 8.29 Add usage examples (backtest, paper, live commands)
  - [ ] 8.30 Document architecture (spot signals → futures execution, basis guards, price conversion)
  - [ ] 8.31 Add operational guide (kill switch, monitoring, alerts)
  - [ ] 8.32 Create example configuration files for different risk profiles

**Exit Criteria:** All tests pass with 80%+ coverage; failure-mode tests validate kill switch and reconciliation behavior; documentation complete with examples.

---

**Phase 1 Complete:** High-level parent tasks generated based on PRD requirements.

**Next Step:** Respond with "Go" to generate detailed sub-tasks for each parent task.
