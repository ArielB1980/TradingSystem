# Server Migration Guide

This guide will help you migrate the trading system from your local machine to a production server.

## Overview

The system is designed to be portable and can run on any Linux server. It supports:
- ✅ PostgreSQL or SQLite databases
- ✅ Environment variable configuration
- ✅ Relative paths (no hardcoded paths)
- ✅ Systemd service management (Linux)

## Prerequisites

### Server Requirements
- **OS**: Linux (Ubuntu 20.04+ or similar)
- **Python**: 3.11 or higher
- **RAM**: Minimum 2GB (4GB+ recommended)
- **Disk**: 10GB+ free space
- **Network**: Stable internet connection (low latency to Kraken API)

### Software Dependencies
```bash
# Install Python and pip
sudo apt-get update
sudo apt-get install -y python3 python3-pip python3-venv git

# Install PostgreSQL (optional, if using PostgreSQL instead of SQLite)
sudo apt-get install -y postgresql postgresql-contrib

# Install systemd (usually pre-installed)
systemctl --version
```

## Migration Steps

### 1. Transfer Files to Server

```bash
# On your local machine, create a deployment package
cd /Users/arielbarack/Programming/PT_Cursor/TradingSystem
tar -czf trading-system.tar.gz \
    --exclude='.git' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.env' \
    --exclude='trading.db*' \
    --exclude='logs/*' \
    --exclude='.live_trading.pid' \
    .

# Transfer to server (replace with your server details)
scp trading-system.tar.gz user@your-server.com:/opt/trading-system/

# On server, extract
ssh user@your-server.com
cd /opt/trading-system
tar -xzf trading-system.tar.gz
```

### 2. Set Up Python Environment

```bash
# On server
cd /opt/trading-system

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

### 3. Database Setup

#### Option A: Use SQLite (Simpler, for single-server deployment)
```bash
# SQLite will be created automatically
# No additional setup needed
# Database file: /opt/trading-system/trading.db
```

#### Option B: Use PostgreSQL (Recommended for production)
```bash
# Create PostgreSQL database
sudo -u postgres psql
CREATE DATABASE trading_system;
CREATE USER trading_user WITH PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE trading_system TO trading_user;
\q

# Set environment variable
export DATABASE_URL="postgresql://trading_user:your_secure_password@localhost:5432/trading_system"
```

**Migrate existing SQLite data to PostgreSQL:**
```bash
# On your local machine, export SQLite data
python3 << 'EOF'
import sqlite3
import json
from datetime import datetime

conn = sqlite3.connect('trading.db')
cursor = conn.cursor()

# Export positions
cursor.execute("SELECT * FROM positions")
positions = cursor.fetchall()
with open('positions_export.json', 'w') as f:
    json.dump(positions, f, default=str)

# Export system_events
cursor.execute("SELECT * FROM system_events")
events = cursor.fetchall()
with open('events_export.json', 'w') as f:
    json.dump(events, f, default=str)

# Export candles (sample - adjust as needed)
cursor.execute("SELECT * FROM candles LIMIT 1000")
candles = cursor.fetchall()
with open('candles_export.json', 'w') as f:
    json.dump(candles, f, default=str)

conn.close()
print("Data exported successfully")
EOF

# Transfer export files to server and import
# (You'll need to write an import script based on your schema)
```

### 4. Configure Environment Variables

```bash
# On server, create .env file
cd /opt/trading-system
cat > .env << 'EOF'
# Database
DATABASE_URL=postgresql://trading_user:your_password@localhost:5432/trading_system
# Or for SQLite:
# DATABASE_URL=sqlite:///./trading.db

# Kraken API Credentials (DO NOT COMMIT THESE!)
KRAKEN_API_KEY=your_spot_api_key
KRAKEN_API_SECRET=your_spot_api_secret
KRAKEN_FUTURES_API_KEY=your_futures_api_key
KRAKEN_FUTURES_API_SECRET=your_futures_api_secret

# Environment
ENVIRONMENT=prod

# Optional: Logging
LOG_LEVEL=INFO
EOF

# Secure the .env file
chmod 600 .env
```

### 5. Create Systemd Service

```bash
# Create systemd service file
sudo nano /etc/systemd/system/trading-system.service
```

**Service file content:**
```ini
[Unit]
Description=Kraken Futures Trading System
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=simple
User=trading
Group=trading
WorkingDirectory=/opt/trading-system
Environment="PATH=/opt/trading-system/venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/opt/trading-system/.env
ExecStart=/opt/trading-system/venv/bin/python3 /opt/trading-system/run.py live --force
Restart=always
RestartSec=10
StandardOutput=append:/opt/trading-system/logs/live_trading_stdout.log
StandardError=append:/opt/trading-system/logs/live_trading_stderr.log

# Security
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/trading-system

# Resource limits
LimitNOFILE=65536
MemoryMax=2G

[Install]
WantedBy=multi-user.target
```

**Create user and set permissions:**
```bash
# Create dedicated user
sudo useradd -r -s /bin/bash -d /opt/trading-system trading
sudo chown -R trading:trading /opt/trading-system

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable trading-system.service
sudo systemctl start trading-system.service
```

### 6. Set Up Watchdog (Optional but Recommended)

```bash
# Create watchdog service
sudo nano /etc/systemd/system/trading-watchdog.service
```

**Watchdog service file:**
```ini
[Unit]
Description=Trading System Watchdog
After=network.target trading-system.service
Requires=trading-system.service

[Service]
Type=simple
User=trading
Group=trading
WorkingDirectory=/opt/trading-system
Environment="PATH=/opt/trading-system/venv/bin:/usr/local/bin:/usr/bin:/bin"
EnvironmentFile=/opt/trading-system/.env
ExecStart=/opt/trading-system/venv/bin/python3 /opt/trading-system/system_watchdog.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable trading-watchdog.service
sudo systemctl start trading-watchdog.service
```

### 7. Set Up Log Rotation

```bash
# Create logrotate config
sudo nano /etc/logrotate.d/trading-system
```

**Logrotate config:**
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

### 8. Firewall Configuration

```bash
# Allow outbound connections (Kraken API)
# Usually no inbound rules needed unless running dashboard

# Check firewall status
sudo ufw status

# If firewall is active, ensure outbound is allowed
sudo ufw default allow outgoing
```

### 9. Verify Installation

```bash
# Check service status
sudo systemctl status trading-system.service

# View logs
sudo journalctl -u trading-system.service -f
# Or
tail -f /opt/trading-system/logs/live_trading_stdout.log

# Test database connection
cd /opt/trading-system
source venv/bin/activate
python3 -c "from src.storage.db import get_db; db = get_db(); print('Database connected:', db.database_url)"
```

## Post-Migration Checklist

- [ ] System starts successfully
- [ ] Database connection works
- [ ] API credentials are valid
- [ ] Logs are being written
- [ ] Service auto-restarts on failure
- [ ] Watchdog is monitoring (if enabled)
- [ ] Log rotation is working
- [ ] Disk space is sufficient
- [ ] Network connectivity is stable
- [ ] Monitoring/alerting is set up

## Monitoring

### Check System Status
```bash
# Service status
sudo systemctl status trading-system.service

# View recent logs
sudo journalctl -u trading-system.service -n 100

# Check if process is running
ps aux | grep "run.py live"

# Check database
sudo -u postgres psql -d trading_system -c "SELECT COUNT(*) FROM positions;"
```

### Set Up Alerts (Optional)

Consider setting up:
- **Email alerts** for critical errors
- **SMS alerts** for kill switch activation
- **Monitoring dashboard** (Grafana/Prometheus)
- **Uptime monitoring** (UptimeRobot, Pingdom)

## Troubleshooting

### Service Won't Start
```bash
# Check logs
sudo journalctl -u trading-system.service -n 50

# Check permissions
ls -la /opt/trading-system

# Test manually
cd /opt/trading-system
source venv/bin/activate
python3 run.py live --force
```

### Database Connection Issues
```bash
# Test PostgreSQL connection
psql -U trading_user -d trading_system -h localhost

# Check PostgreSQL is running
sudo systemctl status postgresql

# Check environment variable
echo $DATABASE_URL
```

### Permission Issues
```bash
# Fix ownership
sudo chown -R trading:trading /opt/trading-system

# Fix permissions
sudo chmod 600 /opt/trading-system/.env
sudo chmod 755 /opt/trading-system/scripts/*.sh
```

## Security Considerations

1. **API Credentials**: Store in `.env` file with `chmod 600`
2. **Database**: Use strong passwords, limit access
3. **User Account**: Run as non-root user (`trading`)
4. **Firewall**: Only allow necessary outbound connections
5. **Updates**: Keep system and Python packages updated
6. **Backups**: Regular database backups
7. **Monitoring**: Set up alerts for suspicious activity

## Backup Strategy

### Database Backups
```bash
# PostgreSQL backup script
#!/bin/bash
BACKUP_DIR="/opt/backups/trading-system"
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR
pg_dump -U trading_user trading_system > $BACKUP_DIR/db_backup_$DATE.sql
# Keep last 30 days
find $BACKUP_DIR -name "db_backup_*.sql" -mtime +30 -delete
```

### Configuration Backups
```bash
# Backup config and .env (encrypted)
tar -czf config_backup.tar.gz src/config/ .env
# Store securely off-server
```

## Rollback Plan

If migration fails:
1. Stop service: `sudo systemctl stop trading-system.service`
2. Restore database from backup
3. Fix issues
4. Restart service: `sudo systemctl start trading-system.service`

## Next Steps

After successful migration:
1. Monitor system for 24-48 hours
2. Verify all positions are tracked correctly
3. Check that signals are being generated
4. Ensure stop losses are being placed
5. Review logs for any errors
6. Set up regular backups
7. Configure monitoring/alerting

## Support

For issues during migration:
- Check logs: `/opt/trading-system/logs/`
- Review systemd logs: `sudo journalctl -u trading-system.service`
- Verify configuration: `python3 run.py status`
- Test database: Check connection and schema

---

**⚠️ Important**: Always test the migration in a staging environment first before deploying to production!
