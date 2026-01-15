# Local Development Environment

This guide explains how to run the trading system locally on your machine for development and testing.

## Prerequisites

- **Python 3.11+**
- **Git**
- **Make**

## Quick Start

1. **Setup Environment**
   ```bash
   make install
   ```
   This creates a `.venv` virtual environment and installs all dependencies.

2. **Configure Local Environment**
   Copy the example file:
   ```bash
   cp .env.local.example .env.local
   ```
   Edit `.env.local` if you need to add specific API keys. By default, it runs in **DRY RUN** mode with a local SQLite database.

3. **Smoke Test**
   Run a quick 30-second verification loop to ensure everything initializes correctly:
   ```bash
   make smoke
   ```
   This will:
   - Load `.env.local`
   - Create/Use `.local/bot.db` (SQLite)
   - Initialize Kraken Client
   - Run the trading loop for 30 seconds
   - Exit cleanly

4. **Run Locally**
   To run the bot continuously (still inside `DRY_RUN`):
   ```bash
   make run
   ```

## Logs

Logs are output to both console and files in the `logs/` directory.

- **Run Logs:** `logs/run.log`
- **Smoke Logs:** `logs/smoke.log`

View real-time logs:
```bash
make logs
# or
make smoke-logs
```

## Directory Structure

- `.venv/`: Python virtual environment (ignored by git)
- `.local/`: Local data directory (DBs) (ignored by git)
- `logs/`: Application logs (ignored by git)
- `.env.local`: Local secrets (ignored by git)

## Troubleshooting

**Missing Dependencies:**
Run `make install` to ensure everything is up to date.

**Database Locked:**
If `make run` fails with DB errors, ensure no other instance is running. You can delete `.local/bot.db` to start fresh.

**Permissions:**
Ensure you have write permissions to the project directory.
