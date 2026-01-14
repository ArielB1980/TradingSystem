# Hetzner Server Deployment Guide (DEPRECATED - Now using DigitalOcean)

**Note:** This guide is deprecated. The system now uses DigitalOcean App Platform. See `APP_PLATFORM_SETUP.md` or `DIGITALOCEAN_DEPLOYMENT.md` for current deployment instructions.

**System Analysis:**
- **Runtime**: Bare Python (not Dockerized)
- **Bots**: Single instance, multi-asset orchestrator
- **Scale**: ~249 coins monitored, up to 10 concurrent positions
- **Frequency**: 1-minute main loop, parallel processing (20 coins concurrent)
- **Database**: Currently SQLite, PostgreSQL available

## Recommended Deployment Pattern

### ✅ Option A: PostgreSQL (Local) - RECOMMENDED

**Why PostgreSQL over SQLite for your workload:**

With your profile:
- **1-minute loop** with frequent writes (ticks, signals, fills, state updates)
- **Parallel processing** (20 coins concurrent)
- **249 coins** monitored
- **Long-running daemon** with continuous writes

SQLite can hit **"database is locked"** errors and write contention under concurrent writes. PostgreSQL handles concurrent writes cleanly and is more reliable for this workload.

**Server Specs:**
- **CPU**: 2 vCPU (CPX21 minimum, CPX31 recommended)
- **RAM**: 4GB minimum (8GB recommended for 249 coins + PostgreSQL)
- **Disk**: 40GB SSD (20GB for system, 20GB for PostgreSQL data)
- **Location**: Choose closest to Kraken API (likely EU/US)

### Folder Layout

```
/opt/trading-system/
├── app/                          # Application code
│   ├── src/
│   ├── run.py
│   ├── system_watchdog.py
│   ├── requirements.txt
│   └── pyproject.toml
├── data/                         # Persistent data (optional cache files)
│   └── discovered_markets.json   # Market discovery cache
├── logs/                         # Logs directory
│   ├── live_trading_stdout.log
│   ├── live_trading_stderr.log
│   └── watchdog.log
├── venv/                         # Python virtual environment
└── .env                          # Environment variables (chmod 600)
```

**PostgreSQL data**: Stored in `/var/lib/postgresql/15/main/` (managed by PostgreSQL)

### Setup Steps

#### 1. Initial Server Setup

```bash
# Update system
sudo apt-get update
sudo apt-get upgrade -y

# Install PostgreSQL and Python
sudo apt-get install -y postgresql postgresql-contrib python3.11 python3.11-venv python3-pip git
```

#### 2. Configure PostgreSQL

```bash
# Switch to postgres user
sudo -u postgres psql

# Create database and user
CREATE DATABASE kraken_futures_trading;
CREATE USER trading_user WITH PASSWORD 'your_secure_password_here';
GRANT ALL PRIVILEGES ON DATABASE kraken_futures_trading TO trading_user;

# For PostgreSQL 15+, also grant schema privileges
\c kraken_futures_trading
GRANT ALL ON SCHEMA public TO trading_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO trading_user;

# Exit
\q
```

#### 3. Configure PostgreSQL for Local Access

```bash
# Edit PostgreSQL config (only allow localhost connections)
sudo nano /etc/postgresql/15/main/pg_hba.conf

# Ensure this line exists (should be default):
local   all             all                                     peer
host    all             all             127.0.0.1/32            scram-sha-256

# Restart PostgreSQL
sudo systemctl restart postgresql
sudo systemctl enable postgresql
```

#### 4. Deploy Application

```bash
# Create app directory
sudo mkdir -p /opt/trading-system/app
sudo chown $USER:$USER /opt/trading-system/app

# Clone/copy application
cd /opt/trading-system/app
# (transfer files via scp/rsync/git)

# Create logs directory
sudo mkdir -p /opt/trading-system/logs
sudo chown $USER:$USER /opt/trading-system/logs
```

#### 5. Python Environment

```bash
cd /opt/trading-system/app

# Create venv
python3.11 -m venv /opt/trading-system/venv
source /opt/trading-system/venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

#### 6. Configure Environment

```bash
cd /opt/trading-system/app

# Create .env file
cat > .env << 'EOF'
# Database (PostgreSQL - local)
DATABASE_URL=postgresql://trading_user:your_secure_password_here@localhost:5432/kraken_futures_trading

# Kraken API Credentials
KRAKEN_API_KEY=your_spot_api_key
KRAKEN_API_SECRET=your_spot_api_secret
KRAKEN_FUTURES_API_KEY=your_futures_api_key
KRAKEN_FUTURES_API_SECRET=your_futures_api_secret

# Environment
ENVIRONMENT=prod

# Logging
LOG_LEVEL=INFO
EOF

chmod 600 .env
```

**Important**: Replace `your_secure_password_here` with the password you set in step 2.

#### 7. Initialize Database Schema

```bash
cd /opt/trading-system/app
source /opt/trading-system/venv/bin/activate

# Test connection and create tables
python3 -c "from src.storage.db import init_db; import os; init_db(os.getenv('DATABASE_URL'))"
```

#### 8. Create Systemd Service

```bash
sudo nano /etc/systemd/system/trading-system.service
```

**Service file:**
```ini
[Unit]
Description=Kraken Futures Trading System
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=trading
Group=trading
WorkingDirectory=/opt/trading-system/app
Environment="PATH=/opt/trading-system/venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/opt/trading-system/app/.env
ExecStart=/opt/trading-system/venv/bin/python3 /opt/trading-system/app/run.py live --force
Restart=always
RestartSec=10
StandardOutput=append:/opt/trading-system/logs/live_trading_stdout.log
StandardError=append:/opt/trading-system/logs/live_trading_stderr.log

# Security
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/trading-system/app /opt/trading-system/logs

# Resource limits
LimitNOFILE=65536
MemoryMax=4G

[Install]
WantedBy=multi-user.target
```

#### 9. Create User and Permissions

```bash
# Create dedicated user
sudo useradd -r -s /bin/bash -d /opt/trading-system/app trading

# Set ownership
sudo chown -R trading:trading /opt/trading-system/app
sudo chown -R trading:trading /opt/trading-system/logs

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable trading-system.service
sudo systemctl start trading-system.service
```

#### 10. Watchdog Service (Optional but Recommended)

```bash
sudo nano /etc/systemd/system/trading-watchdog.service
```

**Watchdog service:**
```ini
[Unit]
Description=Trading System Watchdog
After=network.target trading-system.service
Requires=trading-system.service

[Service]
Type=simple
User=trading
Group=trading
WorkingDirectory=/opt/trading-system/app
Environment="PATH=/opt/trading-system/venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/opt/trading-system/app/.env
ExecStart=/opt/trading-system/venv/bin/python3 /opt/trading-system/app/system_watchdog.py
Restart=always
RestartSec=30
StandardOutput=append:/opt/trading-system/logs/watchdog.log
StandardError=append:/opt/trading-system/logs/watchdog.log

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-watchdog.service
sudo systemctl start trading-watchdog.service
```

---

### Option B: SQLite (Alternative - Not Recommended for Your Workload)

**Why SQLite may cause issues:**
- ❌ Write-lock contention under concurrent writes
- ❌ "database is locked" errors with parallel processing
- ❌ Performance degradation with frequent writes
- ✅ Simpler setup (no PostgreSQL)
- ✅ Lower resource usage

**Only use SQLite if:**
- You're testing/debugging
- You have very low write frequency (< 10 writes/min)
- You're running single-threaded (no parallelism)

**If you must use SQLite:**

```bash
# In .env, use absolute path:
DATABASE_URL=sqlite:////opt/trading-system/data/trading.db

# Create data directory
sudo mkdir -p /opt/trading-system/data
sudo chown trading:trading /opt/trading-system/data
```

**Note**: With 249 coins, parallel processing, and 1-minute loops, you'll likely hit SQLite write locks. PostgreSQL is strongly recommended.

---

## Verification Checklist

After deployment:

```bash
# 1. Check PostgreSQL is running
sudo systemctl status postgresql

# 2. Check service status
sudo systemctl status trading-system.service

# 3. Check logs
tail -f /opt/trading-system/logs/live_trading_stdout.log

# 4. Verify database connection
cd /opt/trading-system/app
source /opt/trading-system/venv/bin/activate
python3 -c "from src.storage.db import get_db; db = get_db(); print('Database:', db.database_url)"

# 5. Check PostgreSQL tables
sudo -u postgres psql -d kraken_futures_trading -c "\dt"

# 6. Verify auto-start on reboot
sudo reboot
# After reboot, check:
sudo systemctl status postgresql
sudo systemctl status trading-system.service
```

## Backup Strategy

### PostgreSQL Backup

```bash
# Create backup script
cat > /opt/trading-system/backup.sh << 'EOF'
#!/bin/bash
BACKUP_DIR="/opt/trading-system/backups"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR

# Backup PostgreSQL database
sudo -u postgres pg_dump -Fc kraken_futures_trading > $BACKUP_DIR/db_backup_${DATE}.dump

# Keep last 7 days
find $BACKUP_DIR -name "db_backup_*.dump" -mtime +7 -delete

# Optional: Upload to Hetzner Storage Box or S3
# rsync -avz $BACKUP_DIR/ user@storage-box:/backups/trading-system/
EOF

chmod +x /opt/trading-system/backup.sh

# Add to crontab (daily at 2 AM)
(crontab -l 2>/dev/null; echo "0 2 * * * /opt/trading-system/backup.sh") | crontab -
```

### Restore from Backup

```bash
# Restore PostgreSQL backup
sudo -u postgres pg_restore -d kraken_futures_trading /opt/trading-system/backups/db_backup_YYYYMMDD_HHMMSS.dump
```

## Monitoring

### Log Rotation

```bash
sudo nano /etc/logrotate.d/trading-system
```

```
/opt/trading-system/logs/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 0644 trading trading
    sharedscripts
    postrotate
        systemctl reload trading-system.service > /dev/null 2>&1 || true
    endscript
}
```

### Health Checks

```bash
# Check if process is running
ps aux | grep "run.py live"

# Check PostgreSQL status
sudo systemctl status postgresql

# Check database size
sudo -u postgres psql -d kraken_futures_trading -c "SELECT pg_size_pretty(pg_database_size('kraken_futures_trading'));"

# Check active connections
sudo -u postgres psql -d kraken_futures_trading -c "SELECT count(*) FROM pg_stat_activity WHERE datname = 'kraken_futures_trading';"

# Check service uptime
systemctl status trading-system.service | grep Active
```

## Troubleshooting

### Service Won't Start

```bash
# Check logs
sudo journalctl -u trading-system.service -n 50

# Check PostgreSQL logs
sudo tail -f /var/log/postgresql/postgresql-15-main.log

# Check permissions
ls -la /opt/trading-system/app
ls -la /opt/trading-system/logs

# Test database connection manually
cd /opt/trading-system/app
source /opt/trading-system/venv/bin/activate
python3 -c "from src.storage.db import get_db; db = get_db(); print('Connected:', db.database_url)"

# Test manually
python3 run.py live --force
```

### PostgreSQL Connection Issues

```bash
# Check PostgreSQL is running
sudo systemctl status postgresql

# Test connection as postgres user
sudo -u postgres psql -d kraken_futures_trading

# Check pg_hba.conf
sudo cat /etc/postgresql/15/main/pg_hba.conf | grep -v "^#"

# Check PostgreSQL is listening
sudo netstat -tlnp | grep 5432
# Should show: 127.0.0.1:5432

# Restart PostgreSQL
sudo systemctl restart postgresql
```

### Database Lock Issues (PostgreSQL)

PostgreSQL handles concurrent writes well, but if you see lock issues:

```bash
# Check for blocking queries
sudo -u postgres psql -d kraken_futures_trading -c "SELECT * FROM pg_locks WHERE NOT granted;"

# Check active queries
sudo -u postgres psql -d kraken_futures_trading -c "SELECT pid, state, query FROM pg_stat_activity WHERE datname = 'kraken_futures_trading';"
```

## PostgreSQL Tuning (Optional)

For better performance with your workload:

```bash
sudo nano /etc/postgresql/15/main/postgresql.conf
```

**Recommended settings:**
```conf
# Memory (adjust based on available RAM)
shared_buffers = 256MB          # 25% of RAM for 1GB, 10% for 4GB+
effective_cache_size = 1GB      # 50-75% of total RAM
work_mem = 16MB                 # Per-operation memory

# Connections
max_connections = 100            # Sufficient for single instance

# Write performance
wal_buffers = 16MB
checkpoint_completion_target = 0.9
```

```bash
# Restart PostgreSQL
sudo systemctl restart postgresql
```

## Security Hardening

1. **Firewall**: Only allow SSH (port 22)
   ```bash
   sudo ufw default deny incoming
   sudo ufw default allow outgoing
   sudo ufw allow 22/tcp
   sudo ufw enable
   ```

2. **SSH**: Disable password auth, use keys only
   ```bash
   sudo nano /etc/ssh/sshd_config
   # Set: PasswordAuthentication no
   sudo systemctl restart sshd
   ```

3. **File Permissions**: Secure .env
   ```bash
   chmod 600 /opt/trading-system/app/.env
   ```

4. **PostgreSQL**: Only localhost access (already configured)
   ```bash
   # Verify pg_hba.conf only allows localhost
   sudo cat /etc/postgresql/15/main/pg_hba.conf | grep -v "^#"
   ```

5. **User Isolation**: Run as non-root (`trading` user)

## Cost Estimate (Hetzner)

- **CPX21** (2 vCPU, 4GB RAM, 80GB SSD): ~€5.83/month
- **CPX31** (2 vCPU, 8GB RAM, 160GB SSD): ~€11.83/month (recommended for 249 coins + PostgreSQL)
- **Total**: ~€6-12/month

---

## Summary

**Recommended Setup:**
- ✅ **PostgreSQL (local)** - handles concurrent writes reliably
- ✅ **Single systemd service** for main app
- ✅ **PostgreSQL service** (auto-starts on boot)
- ✅ **Optional watchdog** for auto-restart
- ✅ **Daily backups** via pg_dump
- ✅ **Log rotation** via logrotate

**Why PostgreSQL over SQLite:**
- ✅ **No write-lock issues** - handles concurrent writes cleanly
- ✅ **Better performance** under parallel processing
- ✅ **More reliable** for long-running daemons
- ✅ **Better for 249 coins** with frequent writes
- ✅ **Production-ready** - battle-tested for concurrent workloads

**When SQLite might work:**
- Very low write frequency (< 10 writes/min)
- Single-threaded processing (no parallelism)
- Testing/debugging only
- Resource-constrained environment (< 1GB RAM)
