## Real-exchange scripts (manual only)

These scripts talk to real exchange endpoints. They are **not** part of the automated test suite.

### Safety gating

- **Read-only checks** (balances, markets, positions, open orders):
  - Require `RUN_REAL_EXCHANGE_TESTS=1`
- **Order placement** (even “safe” far-from-market orders):
  - Require `RUN_REAL_EXCHANGE_TESTS=1`
  - Require `RUN_REAL_EXCHANGE_ORDERS=1`
  - Require `CONFIRM_LIVE=YES`

### Required environment variables

- **Spot**:
  - `KRAKEN_API_KEY`
  - `KRAKEN_API_SECRET`
- **Futures**:
  - `KRAKEN_FUTURES_API_KEY`
  - `KRAKEN_FUTURES_API_SECRET`

### Examples

```bash
# Read-only connectivity checks
RUN_REAL_EXCHANGE_TESTS=1 python3 scripts/real_exchange/kraken_readonly_check.py

# Explicitly allow placing and canceling a safe test order
RUN_REAL_EXCHANGE_TESTS=1 RUN_REAL_EXCHANGE_ORDERS=1 CONFIRM_LIVE=YES \
  python3 scripts/real_exchange/kraken_futures_safe_order_roundtrip.py
```

