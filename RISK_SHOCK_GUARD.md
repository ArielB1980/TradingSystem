# ShockGuard: Wick/Flash Move Protection

## Overview

ShockGuard is a risk management mechanism that protects the trading system from crypto wicks and flash moves. It detects extreme volatility and automatically pauses new entries while reducing exposure on positions with threatened liquidation buffers.

## Configuration

All ShockGuard settings are in `RiskConfig`:

```yaml
risk:
  shock_guard_enabled: true
  shock_move_pct: 0.025          # 1-minute move threshold (2.5%)
  shock_range_pct: 0.04           # 1-minute range threshold (4.0%)
  basis_shock_pct: 0.015          # Basis divergence threshold (1.5%)
  shock_cooldown_minutes: 30     # Cooldown after shock (minutes)
  emergency_buffer_pct: 0.10      # Liquidation buffer for CLOSE (10%)
  trim_buffer_pct: 0.18           # Liquidation buffer for TRIM (18%)
  shock_marketwide_count: 3       # Symbols needed for market-wide shock
  shock_marketwide_window_sec: 60 # Window for market-wide detection (seconds)
```

## Detection Mechanisms

ShockGuard triggers SHOCK_MODE when ANY of these conditions are met:

1. **1-Minute Price Move**: `abs(mark_price / prev_mark_price - 1) > shock_move_pct`
   - Default: 2.5% move in 1 minute

2. **1-Minute Range Spike**: `(high - low) / mid > shock_range_pct` (if 1m candles available)
   - Default: 4.0% range in 1 minute

3. **Basis Spike**: `abs(perp_mark / spot_price - 1) > basis_shock_pct`
   - Default: 1.5% basis divergence

4. **Market-Wide Shock**: `>= shock_marketwide_count` symbols trigger within `shock_marketwide_window_sec`
   - Default: 3 symbols within 60 seconds

## Response Actions

When SHOCK_MODE is active:

### Entry Pausing
- New entries are paused for `shock_cooldown_minutes` (default: 30 minutes)
- Signal generation continues but entries are skipped
- Trading resumes automatically after cooldown

### Exposure Reduction
For each open position with known liquidation price:

1. **CLOSE** (Emergency): If liquidation buffer < `emergency_buffer_pct` (10%)
   - Position is closed entirely via reduce-only market order

2. **TRIM** (Warning): If liquidation buffer < `trim_buffer_pct` (18%)
   - Position size is reduced by 50% via reduce-only market order

3. **HOLD**: If buffer >= `trim_buffer_pct` (18%)
   - No action taken

Liquidation buffer calculation:
- **Long positions**: `(mark_price - liquidation_price) / mark_price`
- **Short positions**: `(liquidation_price - mark_price) / mark_price`

## Integration

ShockGuard is integrated into the main trading loop:

1. **After fetching tickers**: Mark prices are updated in ShockGuard
2. **Shock evaluation**: Conditions are checked each tick
3. **Entry pause check**: Before signal generation, entries are paused if shock active
4. **Exposure reduction**: Once per tick, positions are evaluated and actions executed

## Logging

ShockGuard logs:
- **CRITICAL**: When shock is detected (with reasons and symbols)
- **WARNING**: When positions are closed/trimmed (with buffer %)
- **INFO**: When cooldown expires and trading resumes

## Example Scenario

1. Market flash crash: BTC drops 3% in 1 minute
2. ShockGuard detects: `3% > 2.5% threshold` → SHOCK_MODE activated
3. Entries paused: New signals are generated but not executed
4. Exposure check: Position with 8% liquidation buffer → CLOSE action
5. After 30 minutes: Cooldown expires, trading resumes

## Auction Execution Changes

### Refreshed Margin After Closes

The auction allocation system now:
1. Executes planned closes first
2. **Refreshes account margin** after closes complete
3. Uses refreshed margin for open execution (not stale pre-close margin)
4. Passes `notional_override` and `skip_margin_check` to prevent re-rejection

This ensures that when auction closes positions to free margin, the opens can actually execute in the same cycle.

### Override Parameters

`_handle_signal()` now supports:
- `available_margin_override`: Use specific margin value (from refreshed state)
- `notional_override`: Use pre-computed position notional (from auction sizing)
- `skip_margin_check`: Bypass margin validation (auction already validated)

## Futures Symbol Normalization

### Multiple Format Support

`get_futures_tickers_bulk()` now returns tickers keyed by multiple formats:
- Original raw symbol: `PI_THETAUSD`
- PF_ format: `PF_THETAUSD`
- CCXT unified: `THETA/USD:USD`
- BASE/USD: `THETA/USD`

This ensures mapping works regardless of which format is used.

### Improved Mapping

`FuturesAdapter.map_spot_to_futures()` now:
1. Checks discovery override first
2. Looks up in futures tickers for best executable symbol
3. Prefers CCXT unified format, then PF_, then raw
4. Falls back to `PF_{BASE}USD` if nothing found

This fixes issues where LUNA2 and THETA had futures markets but mapping failed.

## Signal Data Source

Signals are generated from **SPOT candles** by default. Futures OHLCV is only used as fallback when:
- Spot OHLCV is unavailable
- `use_futures_ohlcv_fallback` config is enabled

Rate-limited logging indicates which data source was used per symbol.
