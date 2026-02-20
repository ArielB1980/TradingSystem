# Local Development Environment

This guide explains how to run the trading system locally on your machine for development, testing, and debugging.

## Prerequisites

### Required Software

- **Python 3.11+** (required)
- **PostgreSQL** (required — system uses PostgreSQL only, no SQLite)
- **Git** (required)
- **Make** (usually pre-installed on macOS)

### Installing Python on macOS

```bash
python3 --version   # Needs 3.11+
brew install python@3.11   # If missing
```

### Installing PostgreSQL on macOS

If you already have PostgreSQL running on port 5432, you can use that.
Otherwise, install via Homebrew on port 5433 (avoids conflicts):

```bash
brew install postgresql@17

# Configure to use port 5433 (avoids conflict with existing PG installs)
echo "port = 5433" >> /opt/homebrew/var/postgresql@17/postgresql.conf
echo 'unix_socket_directories = '"'"'/tmp'"'"'' >> /opt/homebrew/var/postgresql@17/postgresql.conf

# Start and create the database
brew services start postgresql@17
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
createdb -p 5433 trading_local

# Verify
psql -p 5433 trading_local -c "SELECT 1;"
```

The DATABASE_URL for `.env.local` is:
```
DATABASE_URL=postgresql://<your-username>@localhost:5433/trading_local
```

---

## Quick Start

### 1. Initial Setup

```bash
# Clone the repository (if not already done)
cd /path/to/TradingSystem-1

# Create virtual environment and install dependencies
make install
```

This will:
- Create a `.venv` virtual environment
- Install all required Python packages
- Take 1-2 minutes to complete

### 2. Configure Environment

```bash
# Create local environment file
make validate
```

This creates `.env.local` from the template. Then edit it to set:
- `DRY_RUN=1` (no real trades — already set)
- `DATABASE_URL=postgresql://<your-username>@localhost:5433/trading_local`
- API keys can be left blank for DRY_RUN=1 smoke tests

### 3. Run Smoke Test

```bash
# Quick 30-second test to verify everything works
make smoke
```

Expected output:
```
✅ SMOKE TEST COMPLETED SUCCESSFULLY
loops_completed: 1
runtime_seconds: 30.2
```

If you see this, your environment is ready! ✅

### 4. Run Locally

```bash
# Run the bot continuously (still in dry-run mode)
make run
```

Press `Ctrl+C` to stop.

---

## Available Commands

| Command | Description |
|---------|-------------|
| `make install` | Create venv and install dependencies |
| `make validate` | Check/create `.env.local` configuration |
| `make smoke` | Run 30-second smoke test |
| `make run` | Run bot continuously (dry-run) |
| `make test` | Run unit tests |
| `make logs` | View real-time logs |
| `make smoke-logs` | View smoke test logs |
| `make status` | Check if bot is running |
| `make clean` | Remove venv and caches |
| `make clean-logs` | Remove log files |

---

## Understanding the Environment

### Directory Structure

```
TradingSystem/
├── .venv/          # Python virtual environment (gitignored)
├── logs/           # Application logs (gitignored)
│   ├── run.log     # Main run logs
│   └── smoke.log   # Smoke test logs
├── .env.local      # Your local secrets (gitignored)
└── .env.local.example  # Template (committed)
```

The database is PostgreSQL (not SQLite). Local default: `trading_local` on port 5433.

### Runner & Risk Config (config.yaml)

Recent additions (2026-02-11):
- **multi_tp.trailing_activation_atr_min**: Guard at TP1; require ATR >= this to activate trailing (0 = no min).
- **risk.max_single_position_margin_pct_equity**: 25% margin per position (1.75x notional at 7x).
- **risk.max_aggregate_margin_pct_equity**: 200% total margin (14x notional at 7x).
- **risk.auction_partial_close_cooldown_seconds**: Skip new opens for N sec after TP1/TP2 partial (0 = disabled).

---

### Environment Variables

Key variables in `.env.local`:

| Variable | Values | Description |
|----------|--------|-------------|
| `DRY_RUN` | `1` or `0` | **1** = Safe mode (default), **0** = Real trading |
| `ENV` | `local`, `dev`, `prod` | Environment type |
| `LOG_LEVEL` | `DEBUG`, `INFO`, etc. | Logging verbosity |
| `RUN_SECONDS` | Number | Run for N seconds then exit (smoke mode) |
| `MAX_LOOPS` | Number | Run for N loops then exit (smoke mode) |

---

## Logs

### Viewing Logs

**Real-time (follow mode):**
```bash
make logs
```

**Last 200 lines:**
```bash
tail -n 200 logs/run.log
```

**Search logs:**
```bash
grep "ERROR" logs/run.log
grep "BTC/USD" logs/run.log
```

### Log Format

Logs are in JSON format for structured parsing:
```json
{"event": "Starting run loop", "level": "info", "timestamp": "2026-01-16T17:00:00Z", "dry_run": true}
```

---

## Troubleshooting

### ❌ "CONFIGURATION ERROR: Missing Required Environment Variables"

**Problem:** Required environment variables not set.

**Solution:**
```bash
# Ensure .env.local exists
make validate

# Check contents
cat .env.local

# For dry-run mode, ensure DRY_RUN=1
# For live trading, you need API keys
```

---

### ❌ "ModuleNotFoundError: No module named 'X'"

**Problem:** Dependencies not installed or wrong Python version.

**Solution:**
```bash
# Reinstall dependencies
make clean
make install

# Verify Python version
.venv/bin/python --version  # Should be 3.11+
```

---

### ❌ "DATABASE_URL is required but not set"

**Problem:** No PostgreSQL connection configured.

**Solution:**
```bash
# Ensure PostgreSQL is running
pg_isready -p 5433  # Should say "accepting connections"

# If not running:
brew services start postgresql@17

# Ensure DATABASE_URL is in .env.local:
# DATABASE_URL=postgresql://<your-username>@localhost:5433/trading_local
```

---

### ❌ Smoke test fails with "API error"

**Problem:** Trying to connect to Kraken API without credentials.

**Solution:**
```bash
# Ensure DRY_RUN=1 in .env.local
echo "DRY_RUN=1" >> .env.local

# Or add testnet credentials (safe for testing)
# See API_CREDENTIALS_SETUP.md
```

---

### ❌ "Permission denied" errors

**Problem:** Insufficient file permissions.

**Solution:**
```bash
# Ensure you own the project directory
sudo chown -R $USER:staff /path/to/TradingSystem-1

# Make sure you can write to logs/
mkdir -p logs
chmod 755 logs
```

---

### ❌ Logs not appearing

**Problem:** Logging not configured correctly.

**Solution:**
```bash
# Check if logs directory exists
ls -la logs/

# Manually create if needed
mkdir -p logs

# Run with explicit log level
LOG_LEVEL=DEBUG make smoke
```

---

## Verifying Smoke Test Success

A successful smoke test should:

1. **Exit with code 0**
   ```bash
   make smoke
   echo $?  # Should print: 0
   ```

2. **Show success message**
   ```
   ✅ SMOKE TEST COMPLETED SUCCESSFULLY
   ```

3. **Create log file**
   ```bash
   ls -lh logs/smoke.log
   # Should exist and have content
   ```

4. **Complete within ~30-60 seconds**

---

## Advanced Usage

### Running with Custom Settings

```bash
# Run for 60 seconds
RUN_SECONDS=60 make smoke

# Run with debug logging
LOG_LEVEL=DEBUG make run

# Use different database
DATABASE_URL=postgresql://localhost/test make smoke
```

### Checking if Bot is Running

```bash
# Method 1: Using make
make status

# Method 2: Manual check
ps aux | grep "python run.py"
```

### Stopping a Running Bot

```bash
# Graceful stop (Ctrl+C in terminal)
# Or kill by process ID
pkill -f "python run.py"
```

---

## Production vs Local

| Aspect | Local (This Guide) | Production (Droplet) |
|--------|-------------------|---------------------------|
| Config | `.env.local` file | Injected env vars |
| Database | PostgreSQL (port 5433) | PostgreSQL (port 5432) |
| API Keys | Optional (DRY_RUN=1) | Required |
| Entrypoint | `make run` / `make smoke` | `python -m src.entrypoints.prod_live` |
| Logs | `logs/run.log` | `logs/run.log` via systemd |
| Deploy | N/A | `make deploy` |

**Important:** `.env.local` is NEVER used in production. Production reads from environment variables set on the Droplet. The production entrypoint (`prod_live`) explicitly refuses to load dotenv files.

---

## Next Steps

- ✅ Smoke test passing? You're ready to develop!
- 📖 Read `README.md` for system architecture
- 🔑 Need API keys? See `API_CREDENTIALS_SETUP.md`
- 🚀 Ready for production? See `PRODUCTION_DEPLOYMENT.md`

---

## Getting Help

If you encounter issues not covered here:

1. Check `logs/smoke.log` for detailed error messages
2. Verify Python version: `python3 --version`
3. Ensure all dependencies installed: `make install`
4. Try a clean start: `make clean && make install && make smoke`

