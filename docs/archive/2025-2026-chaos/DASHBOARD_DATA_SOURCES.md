# Dashboard Data Sources & Freshness

## Overview

This document describes all dashboard fields, their data sources, and freshness guarantees.

## Data Freshness Guarantees

### DECISION_TRACE Events
- **Frequency**: Every 3 minutes (180 seconds) per coin
- **Guarantee**: All coins get DECISION_TRACE events logged, even on errors
- **Status Types**:
  - `active`: Normal processing, has valid data
  - `monitoring`: Insufficient candles (< 50), but still tracking
  - `error`: Processing error occurred
  - `circuit_breaker_open`: Coin temporarily disabled due to repeated failures
  - `no_price`: No valid price data available
  - `zero_price`: Price is zero (invalid)
  - `fetch_error`: Failed to fetch ticker data

### Position Updates
- **Frequency**: Every tick (~60 seconds)
- **Source**: Exchange API → Database (PositionModel)
- **Fields Updated**: `current_mark_price`, `unrealized_pnl`, `size`

## Dashboard Fields

### Coin Table Fields

| Field | Source | Data Type | Freshness | Notes |
|-------|--------|-----------|-----------|-------|
| **Status** | Calculated from `last_update` | Emoji (🟢🟡🔴) | Real-time | Active < 1h, Stale < 6h, Dead > 6h |
| **Symbol** | Config/Coin Universe | String | Static | From monitored symbols list |
| **Price** | `DECISION_TRACE.details.spot_price` | Float | ~3 min | Shows "N/A" if 0 or missing |
| **24h %** | Calculated from candles | Float | ~3 min | Uses 1h/15m/4h candles, falls back gracefully |
| **Signal** | `DECISION_TRACE.details.signal` | String | ~3 min | LONG/SHORT/NO_SIGNAL |
| **Regime** | `DECISION_TRACE.details.regime` | String | ~3 min | tight_range/wide_structure/trending/unknown |
| **Bias** | `DECISION_TRACE.details.bias` | String | ~3 min | bullish/bearish/neutral |
| **Quality** | Calculated from `score_breakdown` | Float (0-100) | ~3 min | Sum of score components normalized |
| **SMC** | `DECISION_TRACE.details.score_breakdown.smc` | Float | ~3 min | SMC component score |
| **Fib** | `DECISION_TRACE.details.score_breakdown.fib` | Float | ~3 min | Fibonacci component score |
| **HTF** | `DECISION_TRACE.details.score_breakdown.htf` | Float | ~3 min | Higher timeframe component score |
| **ADX** | `DECISION_TRACE.details.adx` | Float | ~3 min | ADX indicator value |
| **ATR** | `DECISION_TRACE.details.atr` | Float | ~3 min | ATR indicator value |
| **EMA200** | `DECISION_TRACE.details.ema200_slope` | String | ~3 min | bullish/bearish/flat |
| **Last Update** | `DECISION_TRACE.timestamp` | Time delta | Real-time | Shows seconds/minutes/hours ago |

### Position Table Fields

| Field | Source | Data Type | Freshness | Notes |
|-------|--------|-----------|-----------|-------|
| **Symbol** | `PositionModel.symbol` | String | ~60s | Converted from futures to spot format |
| **Side** | `PositionModel.side` | String | ~60s | LONG/SHORT |
| **Size** | `PositionModel.size_notional` | Float | ~60s | USD notional value |
| **Leverage** | `PositionModel.leverage` | Float | ~60s | Leverage multiplier |
| **Entry** | `PositionModel.entry_price` | Float | Static | Entry price (doesn't change) |
| **Current** | `PositionModel.current_mark_price` | Float | ~60s | Updated every tick |
| **Change %** | Calculated from entry/current | Float | ~60s | Percentage change from entry |
| **PnL** | `PositionModel.unrealized_pnl` | Float | ~60s | Unrealized profit/loss |
| **Stop Loss** | `PositionModel.initial_stop_price` | Float | Static | Initial stop price |
| **TP Targets** | `PositionModel.tp1_price`, `tp2_price`, `final_target_price` | Float | Static | Take profit targets |
| **Liquidation** | `PositionModel.liquidation_price` | Float | ~60s | May update with leverage changes |
| **Opened** | `PositionModel.opened_at` | DateTime | Static | Opening timestamp |
| **Holding** | Calculated from `opened_at` | Time delta | Real-time | Time since opening |
| **Margin** | `PositionModel.margin_used` | Float | ~60s | Margin used for position |

### Sidebar Metrics

| Field | Source | Data Type | Freshness | Notes |
|-------|--------|-----------|-----------|-------|
| **30d PnL** | `calculate_performance_metrics()` | Float | ~10s cache | From closed trades |
| **Win Rate** | `calculate_performance_metrics()` | Float | ~10s cache | From closed trades |
| **Sharpe Ratio** | `calculate_performance_metrics()` | Float | ~10s cache | Calculated metric |
| **Max Drawdown** | `calculate_performance_metrics()` | Float | ~10s cache | Calculated metric |
| **Kill Switch** | `KillSwitch.get_status()` | Boolean | Real-time | System kill switch status |

## Data Flow

### Coin Data Flow
```
Live Trading Tick (60s)
  ↓
Process Coin (parallel, 20 concurrent)
  ↓
Fetch Price Data (bulk or individual)
  ↓
Update Candles (throttled per timeframe)
  ↓
Generate Signal (SMC Engine)
  ↓
Log DECISION_TRACE Event (every 3 min)
  ↓
Database (system_events table)
  ↓
Dashboard Loader (get_latest_traces)
  ↓
Dashboard Display (cached 10s)
```

### Position Data Flow
```
Live Trading Tick (60s)
  ↓
Sync Positions from Exchange API
  ↓
Update PositionModel in Database
  ↓
Dashboard Loader (load_active_positions)
  ↓
Dashboard Display (cached 10s)
```

## Freshness Status Indicators

### Coin Status
- **🟢 Active**: Last update < 1 hour ago
- **🟡 Stale**: Last update 1-6 hours ago
- **🔴 Dead**: Last update > 6 hours ago OR no data

### Position Status
- **Current Price**: Updated every tick (~60s)
- **PnL**: Updated every tick (~60s)
- **Stop Loss/TP**: Static (set at entry)

## Error Handling

### Missing Data
- **No DECISION_TRACE**: Coin shows as "dead" status, all fields default to 0/N/A
- **No Price**: Price shows "N/A", 24h % shows "N/A"
- **No Candles**: 24h % defaults to 0.0%
- **No Position Data**: Position table shows "No open positions"

### Invalid Data
- **Zero Price**: Handled gracefully, shows "N/A"
- **Missing Score Breakdown**: Defaults to empty dict, scores show 0
- **Invalid Timestamps**: Defaults to current time or datetime.min

## Cache Strategy

- **Dashboard Data**: 10 second cache (`@st.cache_data(ttl=10)`)
- **Positions**: 10 second cache
- **Performance Metrics**: 10 second cache
- **Recent Signals**: 10 second cache

## Monitoring

### Freshness Checks
- Average freshness displayed in status bar
- Individual coin freshness in "Last Update" column
- Status emoji indicates overall freshness

### Alerts
- Coins with "dead" status (> 6h old) should be investigated
- Positions with stale prices (> 5 min old) indicate sync issues
- Missing DECISION_TRACE events indicate processing failures

## Troubleshooting

### All Coins Show "Dead"
- Check if live trading is running
- Check database connection
- Check if DECISION_TRACE events are being logged

### Prices Show "N/A"
- Check API connectivity
- Check if symbol is valid
- Check if price data is in DECISION_TRACE details

### Positions Not Updating
- Check position sync in live trading tick
- Check database write permissions
- Check if positions exist on exchange

### 24h Change Shows 0.00%
- Check if candles exist in database
- Check candle timeframe availability
- May be normal for new coins without history
