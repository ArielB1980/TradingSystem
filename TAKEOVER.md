# Production Takeover Protocol

## Overview
This protocol stabilizes the 50+ open positions by importing them into the V2 system with mandatory safety guards (Invariant K).

## Implemented Protocol
1. **Snapshot**: Captures single-source-of-truth from exchange.
2. **Classify**: Protected (A) vs Naked (B) vs Chaos (C) vs Duplicate (D).
3. **Resolve**: Cancels conflicting stops (Chaos). Purges stale local state (Duplicate).
4. **Protect**: Enforces Invariant K by checking stops or placing fresh ones (2% conservative default).
5. **Import**: Creates `ManagedPosition` records in `data/positions.db`.

## Instructions

### 1. Preparation
Ensure you have your Kraken Futures API credentials in your `.env` or config:
```
KRAKEN_FUTURES_API_KEY=...
KRAKEN_FUTURES_API_SECRET=...
```

### 2. Run Takeover Script
Run the takeover utility. This is a ONE-OFF operation.
```bash
python -m src.tools.run_takeover
```
*Note: You can dry-run first with `export TAKEOVER_DRY_RUN=true`*

### 3. Switch to Safe Mode Management
After the script completes, restart your main bot with the following safety flags:

```bash
# Block new risk + enable V2 safety
export TRADING_NEW_ENTRIES_ENABLED=false
export TRADING_REVERSALS_ENABLED=false
export TRADING_PARTIALS_ENABLED=false
export TRADING_TRAILING_ENABLED=false
export USE_STATE_MACHINE_V2=true

# Run Main Bot
python -m src.main
```

### 4. Continuous Monitoring
The main bot will now manage the imported positions.
- **Exits**: Will handle stops and TPs (if manually added).
- **Safety**: `ExitTimeoutManager` will escalate hanging exits. `PositionProtectionMonitor` will ensure stops remain valid.
