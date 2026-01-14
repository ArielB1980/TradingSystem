# Quick Start: DigitalOcean Deployment

## TL;DR - PostgreSQL Setup

```bash
# 1. Install PostgreSQL
sudo apt-get update
sudo apt-get install -y postgresql postgresql-contrib python3.11 python3.11-venv

# 2. Create database
sudo -u postgres psql
CREATE DATABASE kraken_futures_trading;
CREATE USER trading_user WITH PASSWORD 'your_password';
GRANT ALL PRIVILEGES ON DATABASE kraken_futures_trading TO trading_user;
\c kraken_futures_trading
GRANT ALL ON SCHEMA public TO trading_user;
\q

# 3. Deploy app
sudo mkdir -p /opt/trading-system/app
# (copy your app files here)

# 4. Setup Python
cd /opt/trading-system/app
python3.11 -m venv /opt/trading-system/venv
source /opt/trading-system/venv/bin/activate
pip install -r requirements.txt

# 5. Configure .env
cat > .env << EOF
DATABASE_URL=postgresql://trading_user:your_password@localhost:5432/kraken_futures_trading
KRAKEN_API_KEY=your_key
KRAKEN_API_SECRET=your_secret
KRAKEN_FUTURES_API_KEY=your_key
KRAKEN_FUTURES_API_SECRET=your_secret
ENVIRONMENT=prod
EOF
chmod 600 .env

# 6. Initialize database
python3 -c "from src.storage.db import init_db; import os; init_db(os.getenv('DATABASE_URL'))"

# 7. Create systemd service
sudo nano /etc/systemd/system/trading-system.service
# (paste service file from HETZNER_DEPLOYMENT.md)

# 8. Start service
sudo useradd -r -s /bin/bash trading
sudo chown -R trading:trading /opt/trading-system
sudo systemctl daemon-reload
sudo systemctl enable trading-system.service
sudo systemctl start trading-system.service
```

## Why PostgreSQL?

Your workload:
- ✅ 249 coins monitored
- ✅ 1-minute loop
- ✅ Parallel processing (20 concurrent)
- ✅ Frequent writes (ticks, signals, fills)

**SQLite will hit write-locks** → Use PostgreSQL

## Server Specs

- **Minimum**: CPX21 (2 vCPU, 4GB RAM) - €5.83/month
- **Recommended**: CPX31 (2 vCPU, 8GB RAM) - €11.83/month

## Key Files

- **Full Guide**: `DIGITALOCEAN_DEPLOYMENT.md` or `APP_PLATFORM_SETUP.md`
- **Service File**: `/etc/systemd/system/trading-system.service`
- **Logs**: `/opt/trading-system/logs/`
- **Database**: PostgreSQL on localhost:5432

## Quick Checks

```bash
# Service status
sudo systemctl status trading-system.service

# Logs
tail -f /opt/trading-system/logs/live_trading_stdout.log

# Database connection
python3 -c "from src.storage.db import get_db; print(get_db().database_url)"

# PostgreSQL status
sudo systemctl status postgresql
```

## Troubleshooting

**Service won't start?**
```bash
sudo journalctl -u trading-system.service -n 50
```

**Database connection failed?**
```bash
sudo systemctl status postgresql
sudo -u postgres psql -d kraken_futures_trading
```

See `DIGITALOCEAN_DEPLOYMENT.md` or `APP_PLATFORM_SETUP.md` for full details.
