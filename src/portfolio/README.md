# Portfolio Auction Allocator

Deterministic auction-based portfolio allocation system that selects the best 50 positions each cycle with hysteresis and cost penalties to prevent churn.

## Overview

The auction allocator implements a clean, deterministic algorithm to manage a portfolio of up to 50 positions. It evaluates both open positions and new candidate signals using a unified value scoring system, then applies constraints and hysteresis rules to prevent excessive trading.

## Key Features

1. **Unified Value Scoring**: Both open positions and new candidates are scored using the same metric
2. **Hysteresis**: Positions can only be replaced if the new candidate is meaningfully better (configurable threshold)
3. **Cost Awareness**: Entry and exit costs are factored into value calculations
4. **Constraints**: Hard limits on margin, positions per cluster, positions per symbol
5. **Anti-Churn**: Limits on trades per cycle, minimum hold times, locked positions

## Usage

```python
from src.portfolio.auction_allocator import (
    AuctionAllocator,
    PortfolioLimits,
    OpenPositionMetadata,
    CandidateSignal,
    create_candidate_signal,
    position_to_open_metadata,
)
from src.domain.models import Position, Signal
from decimal import Decimal

# Initialize allocator
limits = PortfolioLimits(
    max_positions=50,
    max_margin_util=0.90,
    max_per_cluster=12,
    max_per_symbol=1,
)

allocator = AuctionAllocator(
    limits=limits,
    swap_threshold=10.0,  # Minimum score advantage to replace
    min_hold_minutes=15,
    max_trades_per_cycle=5,
    entry_cost=2.0,
    exit_cost=2.0,
)

# Prepare inputs
open_positions_meta = [
    position_to_open_metadata(
        position=position,
        account_equity=equity,
        is_protective_orders_live=True,
    )
    for position in current_positions
]

candidate_signals_list = [
    create_candidate_signal(
        signal=signal,
        required_margin=margin,
        risk_R=stop_distance,
    )
    for signal in new_signals
]

portfolio_state = {
    "available_margin": available_margin,
    "account_equity": equity,
}

# Run auction
plan = allocator.allocate(
    open_positions=open_positions_meta,
    candidate_signals=candidate_signals_list,
    portfolio_state=portfolio_state,
)

# Execute plan
for signal in plan.opens:
    # Open new position
    pass

for symbol in plan.closes:
    # Close position
    pass
```

## Value Scoring

### New Candidates
```
value_new = score - entry_cost_penalty - concentration_penalty - correlation_penalty
```

### Open Positions
```
value_open = entry_score + pnl_bonus + trend_followthrough_bonus
            - time_decay_penalty - concentration_penalty
            - correlation_penalty - exit_cost_penalty
```

## Hysteresis Rule

A candidate can replace an open position only if:
```
value_new >= value_open + SWAP_THRESHOLD
```

This prevents churn by requiring a meaningful advantage before swapping.

## Configuration

Add to `config.yaml`:

```yaml
risk:
  auction_mode_enabled: true
  auction_max_positions: 50
  auction_max_margin_util: 0.90
  auction_max_per_cluster: 12
  auction_max_per_symbol: 1
  auction_swap_threshold: 10.0
  auction_min_hold_minutes: 15
  auction_max_trades_per_cycle: 5
  auction_entry_cost: 2.0
  auction_exit_cost: 2.0
```

## Position Metadata

Positions must store:
- `entry_score`: Signal score at entry
- `cluster`: Cluster identifier (e.g., "tight_smc_ob")
- `initial_stop_distance_pct`: Risk in % at entry
- `margin_used_at_entry`: Margin at entry

These are automatically populated when creating positions from signals.

## Cluster Derivation

Clusters are derived from signal properties:
- `regime` + `setup_type` = cluster
- Examples: "tight_smc_ob", "wide_structure_bos"

This groups similar trades for concentration limits.
