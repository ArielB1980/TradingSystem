# Product Requirements Document: Kraken Futures SMC Trading System

## Introduction/Overview

This document outlines the development of a professional-grade algorithmic trading system for Kraken Futures perpetual contracts (BTCUSD-PERP, ETHUSD-PERP) utilizing Smart Money Concepts (SMC) methodology with leveraged trading. The system addresses the critical challenge of executing directional long/short trades on leveraged perpetuals while maintaining strict risk controls, liquidation safety, and operational reliability.

**Problem Statement:** Trading leveraged futures requires precise risk management, continuous position monitoring, and robust execution logic to prevent liquidation under modeled conditions, duplicate orders, and unintended exposure. Manual trading with leverage is prone to human error, emotional decisions, and inadequate real-time monitoring.

**Goal:** Build a deterministic, fail-safe trading system that can execute SMC-based swing trades on Kraken Futures perpetuals with full lifecycle automation (backtesting → paper trading → live trading), ensuring capital preservation through liquidation-aware risk management and state reconciliation.

### Architecture: Spot Signals, Futures Execution

**CRITICAL:** All market analysis and trading signals are derived from **spot market data** (e.g., BTC/USD, ETH/USD spot prices), but all trade execution occurs on **futures perpetuals** (BTCUSD-PERP, ETHUSD-PERP). This separation ensures:
- Cleaner price action analysis (spot markets have no funding rate distortions)
- Leveraged execution with capital efficiency (futures perpetuals)
- Separation of concerns between signal generation and execution

**Basis & Triggering:** Signals are computed on spot candles. Orders are executed on futures using futures-native prices. The system MUST enforce a configurable basis guard: if `abs(spot_price - perp_mark_price) / spot_price > basis_max` (e.g., 0.25%–0.75%), the trade is rejected or delayed to prevent execution during excessive spot-perp divergence.

**Price Source Policy (Safety-Critical):**
- **Mark price MUST be used for:**
  - Liquidation distance calculations
  - Risk validation
  - Stop-loss and take-profit trigger conditions
  - Basis guard calculations
- **Last traded price MAY be used for:**
  - Informational displays
  - Analytics and reporting only
- **Rationale:** Kraken liquidates on mark price; all safety-critical logic MUST rely on mark price, never last price, to align risk modeling with exchange reality and prevent false triggers from wicks or thin prints.

**Default Price Conversion Method:**
Stop-loss and take-profit levels are computed as **percentage distances** from the spot entry price (derived from spot structure and ATR), then applied to the **futures mark price** at the moment of order placement. This ensures structural integrity from spot analysis while guaranteeing that all risk calculations and order triggers are aligned with futures pricing. This method is robust to small basis fluctuations and cleanly separates structure logic (spot) from execution logic (futures).

**Post-Entry Basis Risk Handling:**
After a position is opened, if spot-to-perpetual basis widens beyond a second configurable threshold (`basis_max_post`, default same as `basis_max`), the system MUST:
- Disallow any position increases or pyramiding
- Allow reduce-only exits only (SL/TP remain active)
- Log a "basis risk state" event
- The position remains managed by existing protective orders but no new exposure is added while basis risk persists

### Leverage Policy

**The system enforces a maximum leverage cap of 10×.** Actual effective leverage is dynamically determined by stop distance and risk constraints and may be lower. The 10× cap is a hard limit, not a target operating leverage. The system's primary goal is capital preservation, not maximizing leverage.

---

## Goals

1. **Liquidation Safety:** Prevent liquidation events caused by system failure through continuous monitoring of exchange-reported liquidation distance (minimum 30-40% buffer) and risk-based position sizing
2. **Operational Reliability:** Achieve 99% uptime with fail-safe behavior (correctness > uptime) and zero unintended exposure
3. **Deterministic Execution:** Ensure same market data produces same trading signals with full audit trails
4. **Progressive Deployment:** Gate live trading behind successful backtesting and paper trading validation
5. **Full State Reconciliation:** Maintain continuous synchronization with exchange truth (positions, orders, margin, funding)
6. **Cost-Aware Performance:** Model maker/taker fees and funding payments in all simulation and live trading
7. **Observability:** Provide complete trade lifecycle logging and real-time risk metrics

---

## User Stories

### As a Trader:
- I want to backtest SMC strategies on historical spot Kraken data while simulating futures execution costs (fees, funding, slippage, basis) so that I can validate profitability before risking capital
- I want the system to automatically prevent trades that would bring me too close to the exchange-reported liquidation price so that I can prevent liquidation under modeled conditions
- I want to paper trade with real-time data and simulated fills so that I can verify execution logic without risk
- I want automatic stop-loss and take-profit orders placed immediately after entry so that my risk is always defined
- I want a kill switch that immediately cancels all orders and flattens positions in emergencies, with manual restart required
- I want full transparency into why each trade was taken or rejected so that I can audit strategy performance

### As a System Operator:
- I want continuous reconciliation between system state and exchange state so that I never have ghost positions or orders
- I want alerts when margin usage exceeds thresholds or liquidation distance is breached so that I can intervene
- I want environment separation (dev/paper/prod) so that I never accidentally trade live during testing
- I want deterministic replay of historical data so that I can debug issues with precision
- I want automated tests covering SMC logic, exchange integration, and order execution so that changes don't introduce regressions

---

## Functional Requirements

### 1. Data Acquisition Module

**1.1** The system MUST connect to Kraken REST API and WebSocket feeds for real-time market data:
- **Spot markets:** BTC/USD, ETH/USD (for signal generation and analysis)
- **Futures markets:** BTCUSD-PERP, ETHUSD-PERP (for execution, position monitoring, and margin tracking)

**1.2** The system MUST support BTCUSD-PERP and ETHUSD-PERP as primary execution contracts, with architecture allowing expansion to other high-liquidity perpetuals

**1.3** The system MUST fetch and maintain OHLCV data from **spot markets** for multiple timeframes:
- Bias determination: 4H, 1D (BTC/USD, ETH/USD spot)
- Execution signals: 15m, 1H (BTC/USD, ETH/USD spot)
- **Note:** Futures perpetual data is used only for position/margin/funding monitoring, NOT for signal generation

**1.4** The system MUST handle WebSocket connection failures gracefully:
- Automatic reconnection with exponential backoff
- Data feed failure → halt new entries, manage exits only
- Log all connection state changes

**1.5** The system MUST store historical and real-time data in PostgreSQL (initial development may use SQLite)

**1.6** The system MUST validate data integrity (no gaps, no duplicate timestamps) before feeding to strategy logic

### 2. Strategy Definition Module (SMC-Based)

**2.1** The system MUST implement Smart Money Concepts (SMC) methodology with deterministic logic:
- **All analysis performed on spot market data** (BTC/USD, ETH/USD)
- Same input data → same trading signal
- Full reasoning logs for each decision point
- Signals are market-neutral (long/short/hold) and map 1:1 to corresponding futures perpetual contracts

**2.2** The system MUST use the following indicators as filters (not triggers):
- **EMA 200** (higher-timeframe bias on 4H/1D)
- **ADX** (trend strength filter)
- **ATR** (volatility measurement for stop sizing - critical at 10×)
- **RSI divergence** (optional confirmation, never a standalone trigger)

**2.3** The system MUST define explicit entry rules:
- SMC structure identification (order blocks, fair value gaps, break of structure)
- Higher-timeframe bias confirmation (4H/1D)
- Execution timeframe signal (15m/1H)
- Trend strength filter (ADX above threshold)
- Volatility assessment (ATR within acceptable range)

**2.4** The system MUST define explicit exit rules:
- **Stop-loss:** Based on SMC invalidation level + ATR buffer (from spot analysis)
- **Take-profit:** Based on next significant SMC level (liquidity zones from spot analysis)
- **Trailing stops:** Optional, configurable
- **Time-based exits:** Optional, configurable for swing holding period
- **Price conversion:** Stop-loss and take-profit levels are derived from spot structure but MUST be converted into futures order prices using current perp pricing (mark/last) with a configurable conversion method

**2.5** The system MUST reject trades that:
- Would place stop-loss too close to liquidation price
- Occur during extreme volatility regimes (optional filter)
- Occur near funding payment times if funding rate spike detected (optional filter; spike defined as funding rate exceeding configurable threshold, e.g., >0.1% or 3x recent average)

**2.6** The system MUST be configurable via parameters (no hardcoded strategy values):
- Indicator periods and thresholds
- SMC structure detection sensitivity
- Risk percentage per trade
- Leverage cap (default 10×)

### 3. Risk Management Module

**3.1** The system MUST size positions based on stop distance and risk percentage, independent of leverage:
- Formula: `position_notional = (account_equity × risk_pct) / stop_distance_pct`
- Leverage determines margin usage: `margin_used = position_notional / leverage`
- Leverage affects margin requirements, not risk exposure
- Default risk per trade: 0.25% - 0.5% of account equity
- Maximum leverage cap: 10× (hard limit, actual leverage may be lower based on stop distance)

**3.2** The system MUST calculate and enforce liquidation buffer using exchange-reported values:
- Primary source: Exchange-reported liquidation price (authoritative)
- **Liquidation distance is measured as:** `abs(mark_price - liq_price) / mark_price` (direction-aware), using exchange-reported mark price and liquidation price
- Minimum liquidation distance: 30-40% (e.g., 0.30 to 0.40 in decimal form)
- Trade MUST be rejected if stop-loss placement is too close to exchange-reported liquidation price
- Internal liquidation estimates MUST include safety buffers and are used only for pre-trade validation, never as safety guarantees

**3.3** The system MUST implement portfolio-level risk limits:
- Maximum concurrent positions (configurable, e.g., 2-3)
- Daily loss limit (configurable, e.g., 2% of equity)
- Loss streak cooldown (configurable, e.g., halt after 3 consecutive losses)

**3.4** The system MUST enforce non-negotiable rules:
- No leverage escalation beyond 10×
- No widening of stop-loss after entry
- No averaging down into losing positions
- No position increases after drawdown threshold

**3.5** The system MUST monitor effective leverage in real-time:
- Effective leverage = total position notional / account equity
- Alert when approaching leverage cap

**3.6** The system MUST track and account for all costs:
- Maker/taker fees (Kraken Futures fee schedule)
- Funding payments (perpetual funding rate)
- Slippage estimates (configurable per market condition)

**3.7** The system MAY implement funding cost risk control:
- Block new entries if projected funding cost over expected holding period exceeds a configurable threshold
- This is optional but recommended for leveraged perpetual strategies

### 4. Execution Module

**4.1** The system MUST implement idempotent order handling:
- No duplicate orders from repeated signals
- Order ID tracking and deduplication
- Ghost order detection and cleanup

**4.2** The system MUST place stop-loss and take-profit orders immediately after entry fill confirmation:
- All protective orders MUST be reduce-only
- Protective orders MUST be linked to parent position
- **Price conversion (default method):** SMC levels from spot analysis are computed as percentage distances from spot entry price, then applied to futures mark price at order placement moment (e.g., if spot stop is 2% below spot entry, futures stop is placed 2% below futures mark price)

**4.3** The system MUST support all three deployment modes:

**Backtesting Mode:**
- Historical OHLCV data replay
- Simulated fill prices (configurable: market/limit fill assumptions)
- Full fee and funding cost modeling
- Deterministic results for same input data

**Paper Trading Mode:**
- Real-time data feeds
- Simulated order execution with realistic slippage
- Full fee and funding modeling
- No actual exchange orders

**Live Trading Mode:**
- Real exchange order placement via **Kraken Futures API** (BTCUSD-PERP, ETHUSD-PERP)
- Signals derived from **spot market analysis** (BTC/USD, ETH/USD), executed on corresponding futures perpetuals
- Real fills and position management
- Continuous reconciliation with exchange state
- MUST be gated: no live trading until paper trading meets all thresholds

**4.4** The system MUST implement continuous state reconciliation using a hybrid model:
- **Event-driven reconciliation** (immediate) on:
  - Order fills
  - Order updates (cancelled, rejected, modified)
  - Margin updates
  - Position updates
- **Periodic hard reconciliation** (default: every 15 seconds, configurable):
  - Open positions (size, side, entry price, unrealized PnL, **exchange-reported liquidation price**)
  - Open orders (ID, type, price, quantity, status)
  - Account margin balance
  - Effective leverage
- Reconcile system state with exchange truth
- Alert on discrepancies (ghost positions, missing orders, margin mismatch)

**4.5** The system MUST implement a Kill Switch with latching behavior:
- Triggered by:
  - Manual user command
  - Repeated API/auth errors
  - Margin below critical threshold
  - Liquidation distance breach (based on exchange-reported price)
  - Data feed prolonged failure
- Actions on trigger:
  - Cancel ALL open orders immediately
  - Flatten ALL positions at market (force close)
  - Log kill switch activation with full context
  - **Latch system in stopped state** (cannot auto-resume)
- **Manual acknowledgment required to restart trading** (prevents oscillation in unstable conditions)

**4.6** The system MUST implement dry-run capability:
- Every order creation path MUST support dry-run mode
- Dry-run: validate order parameters without sending to exchange
- Used in tests and pre-deployment validation

**4.7** The system MUST handle exchange errors gracefully:
- Insufficient margin → reject trade, log error, alert
- Order rejection → log with full context, do not retry blindly
- Rate limiting → respect exchange limits, implement backoff
- Timeout errors → retry with exponential backoff (max retries defined)

### 5. Observability & Monitoring

**5.1** The system MUST log full trade lifecycle:
- Market snapshot at signal time (price, indicators, SMC structures)
- Signal generation (entry/exit/hold decision + reasoning)
- Order creation (parameters, dry-run validation)
- Order submission (exchange response, order ID)
- Fill confirmation (executed price, quantity, fees)
- Position management (stops placed, PnL tracking)
- Exit execution (reason, final PnL, fees, funding)

**5.2** The system MUST provide real-time metrics dashboard:
- Current positions (side, size, entry, unrealized PnL, liquidation distance)
- Effective leverage
- Margin usage (used / available)
- Daily PnL (realized + unrealized)
- Win rate, average win/loss, Sharpe ratio (rolling window)
- Fee and funding costs (cumulative)

**5.3** The system MUST implement alerting for critical events:
- Margin usage > threshold (e.g., 70%)
- Liquidation distance < minimum buffer
- Repeated order rejections (> 3 in 5 minutes)
- Kill switch activation
- Data feed disconnection > threshold duration
- Daily loss limit reached

**5.4** The system MUST store logs in structured format (JSON) with:
- Timestamp (ISO 8601, UTC)
- Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
- Component (data, strategy, risk, execution, reconciliation)
- Event type
- Full context (market state, orders, positions)

### 6. Configuration & Environment Management

**6.1** The system MUST support environment separation:
- **dev:** Development, no exchange connection, synthetic data
- **paper:** Paper trading, real data feeds, simulated execution
- **prod:** Live trading, real exchange orders

**6.2** The system MUST use configuration files (YAML or JSON) for all parameters:
- Exchange credentials (API key, secret) via environment variables
- Risk limits (leverage cap, risk %, daily loss limit, concurrent positions)
- Strategy parameters (indicator periods, SMC thresholds)
- Execution settings (order types, slippage assumptions, timeout values)
- Alert thresholds (margin, liquidation buffer, error rates)

**6.3** The system MUST validate configuration on startup:
- All required parameters present
- Values within acceptable ranges (e.g., leverage ≤ 10×)
- Credentials valid (test API connection in non-prod)
- Fail fast with clear error messages if invalid

**6.4** The system MUST use Pydantic for configuration validation and type safety

### 7. Testing & Quality Assurance

**7.1** The system MUST include unit tests for:
- SMC logic (indicator calculations, signal generation)
- Risk calculations (position sizing, liquidation distance)
- Order parameter generation
- State reconciliation logic

**7.2** The system MUST include integration tests for:
- Kraken Futures adapter (API calls, WebSocket handling)
- Database operations (read/write OHLCV, positions, trades)
- Full strategy pipeline (data → signal → order → execution)

**7.3** The system MUST include replay tests:
- Historical candle data → deterministic signal generation
- Simulated fills → position state evolution
- Verify: same data + same config = same results

**7.4** The system MUST achieve minimum test coverage:
- Core strategy logic: 90%+
- Risk management: 95%+
- Execution module: 80%+
- Overall: 80%+

---

## Non-Goals (Out of Scope)

**NG-1:** Multi-exchange support (Kraken Futures only for v1)

**NG-2:** High-frequency trading or market making (swing trading only)

**NG-3:** Machine learning or AI-based signal generation (deterministic SMC only)

**NG-4:** Mobile app or web UI (CLI and metrics dashboard only)

**NG-5:** Social trading or signal sharing features

**NG-6:** Support for spot markets (futures perpetuals only)

**NG-7:** Automated parameter optimization (manual backtesting and tuning)

**NG-8:** Multi-account management (single account per instance)

**NG-9:** Advanced order types beyond market/limit/stop (SL/TP sufficient)

**NG-10:** Arbitrage or cross-market strategies (single-market directional only)

---

## Design Considerations

### Architecture Patterns

**Modular Design:**
- Separate modules: DataAcquisition, Strategy, RiskManager, Executor, Reconciler, Monitor
- Each module has clear interface and single responsibility
- Dependency injection for testability

**Event-Driven:**
- Market data updates trigger strategy evaluation
- Strategy signals trigger risk checks
- Risk-approved signals trigger execution
- Fill confirmations trigger reconciliation

**Fail-Safe Design:**
- Correctness > uptime (halt on uncertainty)
- Explicit error handling, no silent failures
- Idempotent operations (safe to retry)
- State machine for order lifecycle

### User Interface

**CLI Commands:**
- `backtest --config path/to/config.yaml --start 2024-01-01 --end 2024-12-31`
- `paper --config path/to/config.yaml`
- `live --config path/to/config.yaml` (requires confirmation)
- `kill-switch --emergency` (immediate stop)
- `status` (current positions, metrics)

**Metrics Dashboard:**
- Real-time web dashboard (optional, e.g., Streamlit or Grafana)
- Display: positions, PnL, leverage, alerts, recent trades

---

## Technical Considerations

### Technology Stack

**Language:** Python 3.11+

**Core Libraries:**
- `ccxt` - Kraken Futures API integration
- `pandas` - Data manipulation
- `numpy` - Numerical calculations
- `pandas-ta` or `ta-lib` - Technical indicators
- `pydantic` - Configuration validation
- `asyncio` - Async WebSocket handling

**Database:**
- PostgreSQL (production)
- SQLite (initial development, local testing)

**Infrastructure:**
- Docker - Containerization
- Docker Compose - Multi-container orchestration (app + database)

**Testing:**
- `pytest` - Test framework
- `pytest-asyncio` - Async test support
- `pytest-cov` - Coverage reporting

**Logging:**
- `structlog` - Structured logging (JSON output)

### Exchange Integration

**Kraken Futures API:**
- REST API: Account info, order placement, position queries
- WebSocket: Real-time market data (orderbook, trades, OHLCV)
- Authentication: API key + secret (HMAC signature)

**Futures-Specific Adapter Requirements:**
- Leverage setting per order
- Margin calculation (initial margin, maintenance margin)
- Liquidation price calculation
- Funding rate tracking
- Reduce-only order support
- **Spot-to-futures mapping:** Translate spot ticker signals (BTC/USD) to futures contracts (BTCUSD-PERP)

**Rate Limits:**
- Respect Kraken's rate limits (public: 1 req/sec, private: varies by endpoint)
- Implement request queue with rate limiting
- Backoff on 429 responses

### State Management

**System State:**
- In-memory: Current positions, open orders, active signals
- Persistent: Trade history, OHLCV data, logs, metrics

**Exchange Truth Reconciliation:**
- **Event-driven reconciliation** (immediate) on:
  - Order fills
  - Order updates (cancelled, rejected, modified)
  - Margin updates
  - Position updates
- **Periodic hard reconciliation** (default: every 15 seconds, configurable):
  - Fetch positions from exchange (size, side, entry, unrealized PnL, liquidation price)
  - Fetch open orders from exchange (ID, type, price, quantity, status)
  - Fetch margin balance and effective leverage from exchange
  - Compare with system state
  - Alert on discrepancies, update system state

**Idempotency:**
- Track order submission attempts (prevent duplicates)
- Use client_order_id for deduplication
- Stateful order lifecycle: PENDING → SUBMITTED → FILLED/CANCELLED/REJECTED

### Margin & Leverage Management

**Margin Calculation:**
- Initial margin = position_notional / leverage
- Maintenance margin = position_notional / liquidation_leverage (Kraken-specific)
- Available margin = account_equity - used_margin

**Liquidation Price Handling:**
- **The system MUST treat exchange-reported liquidation price as authoritative**
- Liquidation price obtained via Kraken Futures API position endpoints
- Internal liquidation estimates are approximate and conservative, accounting for:
  - Maintenance margin tiers (exchange-specific)
  - Funding payment accrual
  - Unrealized PnL fluctuations
  - Fee deductions
- **Any internal calculation MUST include safety buffers and is used only for pre-trade rejection, never for safety guarantees**
- Buffer enforcement: reject trade if stop_loss distance to exchange-reported liquidation price < minimum threshold (30-40%)

**Leverage Control:**
- Fixed 10× cap (configurable but defaults to 10×)
- Leverage set per order
- Continuous monitoring of effective leverage
- Alert if effective leverage > 9× (approaching cap)

### Funding Payments

**Funding Rate:**
- Perpetual contracts charge/pay funding every 8 hours (Kraken-specific schedule)
- Rate fetched from exchange API
- Applied to position notional: `funding_payment = position_notional * funding_rate`

**Modeling:**
- Backtest: Estimate funding using historical rates
- Paper trading: Use current funding rate projections
- Live trading: Actual funding deducted from margin

---

## Success Metrics

### Priority 1: Reliability & Liquidation Safety (Gate for live trading)

✅ **Zero liquidation events caused by system failure** during paper trading (minimum 30 days)

✅ **Zero ghost positions or duplicate orders** (100% reconciliation accuracy)

✅ **Liquidation buffer (based on exchange-reported price) maintained >30%** in all positions (99.9% compliance)

✅ **Kill switch activates correctly and latches** in simulated failure scenarios (100% tests pass)

✅ **System uptime 99%+** (excluding intentional halts for data issues; correctness prioritized over uptime)

### Priority 2: Functional Correctness (Gate for live trading)

✅ **Deterministic backtesting:** Same data + config → identical results (100%)

✅ **Order correctness:** SL/TP placed within <1 second of fill** (95%+ in paper trading)

✅ **Risk enforcement:** No trades exceed configured risk % (100% compliance)

✅ **Fee & funding modeling:** Live PnL within 2% of projected PnL (paper trading validation)

### Priority 3: Performance Validation (Goal, not gate)

📊 **Backtesting (2+ years of data):**
- **Positive expectancy per trade** (primary metric)
- **Maximum drawdown < 20%** (leveraged basis)
- **Profit factor > 1.5** (gross profit / gross loss)
- Win rate 40%+ (with positive risk/reward ratio)
- Sharpe ratio > 1.0 (secondary diagnostic metric; less reliable for leveraged futures)

📊 **Paper trading (30+ days):**
- Slippage within 0.1% of mid on average
- Zero missed signals (100% signal → order translation)
- No margin warnings or liquidation proximity alerts

### Priority 4: Observability (Operational requirement)

📝 **Full audit trail:** Every trade decision logged with market context (100%)

📊 **Real-time metrics:** Dashboard shows positions, PnL, leverage, alerts (live)

🔔 **Alerting:** Critical events trigger alerts within 10 seconds (95%+)

### Priority 5: Testing Coverage (Quality gate)

🧪 **Unit test coverage:** Core logic >90%, overall >80%

🧪 **Integration tests:** Kraken API adapter fully tested (mocked exchange)

🧪 **Replay tests:** Historical data produces expected signals (deterministic)

---

## Open Questions

**Q1:** Should the system support multiple SMC strategy variants (e.g., orderblock-only vs. FVG-focused), or start with a single canonical SMC implementation?

**Q2:** What is the preferred method for kill switch activation? (CLI command, web endpoint, hardware button, multiple methods?)

**Q3:** Should the metrics dashboard be a separate service (e.g., Grafana + InfluxDB) or integrated into the Python app (e.g., Streamlit)?

**Q4:** What is the exact daily loss limit threshold? (e.g., 2% of starting equity, 2% of current equity, fixed dollar amount?)

**Q5:** Should the system support manual trade intervention (e.g., manually close position via CLI while system running), or fully automated-only?

**Q6:** What is the preferred alert delivery mechanism? (Email, SMS, Telegram bot, Slack webhook, multiple?)

**Q7:** Should backtesting support walk-forward optimization, or manual parameter tuning only?

**Q8:** What is the deployment target? (Local machine, VPS, cloud VM, Kubernetes cluster?)

**Q9:** Should the system support graceful shutdown (close positions before exit) or leave positions open for manual management?

**Q10:** What is the preferred approach for SMC structure detection? (Manual levels configuration, automated pattern recognition, hybrid?)

---

## Next Steps

1. **Review this PRD** - Confirm all requirements, goals, and non-goals align with expectations
2. **Address open questions** - Clarify outstanding design decisions
3. **Generate task list** - Break down implementation into phased tasks (following generate-tasks.md protocol)
4. **Development** - Implement system following the task list, with continuous testing and validation

---

**Document Version:** 1.0  
**Last Updated:** 2026-01-10  
**Target Audience:** Junior to mid-level Python developers familiar with trading concepts
