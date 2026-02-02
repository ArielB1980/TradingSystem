# Server Log Review — Correct Operations

Use this after deploy to confirm the production server is running correctly.

## 1. Quick review script (recommended)

From repo root:

```bash
./scripts/review_server_logs.sh
# Or last 5000 lines:
./scripts/review_server_logs.sh --tail 5000
```

The script SSHs to the server, tails `logs/run.log`, and reports:

- **Go-live gates:** `test_db` (0), `MagicMock` (0), `INVARIANT VIOLATION` (near 0)
- **DATABASE_CONNECTION_INIT:** At least one line with host/port/database/user (no password)
- **Recent errors / critical** and **startup / auction** lines

## 2. Manual commands

### Live tail

```bash
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -f /home/trading/TradingSystem/logs/run.log'
```

### Last N lines

```bash
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -n 2000 /home/trading/TradingSystem/logs/run.log'
```

### Check for fixed issues (should be 0)

```bash
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -n 5000 /home/trading/TradingSystem/logs/run.log' \
  | grep -c "test_db" || true

ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -n 5000 /home/trading/TradingSystem/logs/run.log' \
  | grep -c "MagicMock" || true

ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -n 5000 /home/trading/TradingSystem/logs/run.log' \
  | grep -c "INVARIANT VIOLATION" || true
```

### Verify database in use

```bash
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -n 5000 /home/trading/TradingSystem/logs/run.log' \
  | grep "DATABASE_CONNECTION_INIT"
```

You should see one line per process start with `host`, `port`, `database`, `user` (no password).

### Auction and execution

```bash
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -n 3000 /home/trading/TradingSystem/logs/run.log' \
  | grep -E "Auction plan generated|Auction allocation executed|Entry order placed|Auction: Opened position|TRADING PAUSED|AUCTION_END"
```

## 3. What “correct” looks like

| Check | Expected |
|-------|----------|
| `test_db` | 0 mentions |
| `MagicMock` | 0 mentions |
| `INVARIANT VIOLATION` | 0 or rare (duplicate register idempotent) |
| `DATABASE_CONNECTION_INIT` | ≥1 after restart, with correct host/database |
| `Logging initialized` | Present after each restart |
| `Live trading started` / `STARTING LIVE TRADING` | Present after each restart |
| Errors | Occasional data/API errors OK; no repeated DB or mock errors |

## 4. Service status

```bash
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 'systemctl status trading-system.service'
```

Expect: `Active: active (running)`.
