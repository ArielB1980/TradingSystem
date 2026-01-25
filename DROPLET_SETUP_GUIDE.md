# DigitalOcean Droplet Setup Guide

## Recommended Architecture

**Droplet + Separate Managed Database**

- ✅ Full control and SSH access
- ✅ Direct database access (solves permission issues)
- ✅ Better for trading systems
- ✅ Easier debugging and maintenance

## Step 1: Create Managed Database

1. **DigitalOcean Dashboard → Databases → Create Database**
   - Choose: **PostgreSQL** (latest version)
   - Plan: **Basic** or **Professional** (based on needs)
   - Region: **NYC1** (or closest to you)
   - Database name: `dbtradingbot` (or your choice)

2. **After creation:**
   - Go to database → **Connection Details**
   - Copy the connection string
   - **Important:** Note the `doadmin` credentials (admin user)

3. **Grant permissions immediately:**
   - Use the database console or connect via psql
   - Run:
   ```sql
   GRANT CREATE ON SCHEMA public TO dbtradingbot;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;
   ```

## Step 2: Create Droplet

1. **DigitalOcean Dashboard → Droplets → Create Droplet**
   - **Image:** Ubuntu 22.04 LTS
   - **Plan:** 
     - Minimum: **Basic** - 2GB RAM / 1 vCPU ($12/mo)
     - Recommended: **Basic** - 4GB RAM / 2 vCPU ($24/mo)
   - **Region:** Same as database (NYC1)
   - **Authentication:** Add your SSH key
   - **Hostname:** `trading-system` (or your choice)

2. **After creation:**
   - Note the IP address
   - SSH in: `ssh root@YOUR_DROPLET_IP`

## Step 3: Initial Server Setup

```bash
# Update system
apt update && apt upgrade -y

# Install required packages
apt install -y python3.11 python3.11-venv python3-pip git postgresql-client curl

# Create trading user (non-root)
useradd -m -s /bin/bash trading
usermod -aG sudo trading

# Switch to trading user
su - trading
```

## Step 4: Deploy Application

```bash
# Clone repository
cd ~
git clone https://github.com/ArielB1980/TradingSystem.git
cd TradingSystem

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

## Step 5: Configure Environment

```bash
# Create .env file
nano .env
```

Add:
```bash
# Database (use your managed database connection string)
DATABASE_URL=postgresql://dbtradingbot:password@host:port/dbtradingbot?sslmode=require

# Exchange Credentials
KRAKEN_API_KEY=your_key
KRAKEN_API_SECRET=your_secret
KRAKEN_FUTURES_API_KEY=your_futures_key
KRAKEN_FUTURES_API_SECRET=your_futures_secret

# Environment
ENVIRONMENT=prod
LOG_LEVEL=INFO
```

```bash
# Secure the file
chmod 600 .env
```

## Step 6: Grant Database Permissions

```bash
# Install psql if not already installed
sudo apt install -y postgresql-client

# Connect as doadmin (use admin connection string from database)
psql "postgresql://doadmin:ADMIN_PASSWORD@host:port/dbtradingbot?sslmode=require"

# Run SQL commands:
GRANT CREATE ON SCHEMA public TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;
\q
```

## Step 7: Initialize Database

```bash
cd ~/TradingSystem
source venv/bin/activate

# Run migration
python migrate_schema.py

# Verify tables were created
python -c "
from src.storage.db import get_db
from sqlalchemy import inspect
db = get_db()
inspector = inspect(db.engine)
tables = inspector.get_table_names()
print('Tables:', tables)
"
```

## Step 8: Create Systemd Service

```bash
# Create service file
sudo nano /etc/systemd/system/trading-system.service
```

Paste:
```ini
[Unit]
Description=Trading System Live Trading
After=network.target

[Service]
Type=simple
User=trading
WorkingDirectory=/home/trading/TradingSystem
Environment="PATH=/home/trading/TradingSystem/venv/bin"
ExecStart=/home/trading/TradingSystem/venv/bin/python run.py live --force --with-health
Restart=always
RestartSec=10
StandardOutput=append:/home/trading/TradingSystem/logs/trading.log
StandardError=append:/home/trading/TradingSystem/logs/trading-error.log

[Install]
WantedBy=multi-user.target
```

```bash
# Create logs directory
mkdir -p ~/TradingSystem/logs

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable trading-system.service
sudo systemctl start trading-system.service

# Check status
sudo systemctl status trading-system.service

# View logs
tail -f ~/TradingSystem/logs/trading.log
```

## Step 9: Set Up Firewall (Optional but Recommended)

```bash
# Allow SSH
sudo ufw allow 22/tcp

# Allow health check port (if using)
sudo ufw allow 8080/tcp

# Enable firewall
sudo ufw enable
```

## Step 10: Verify Everything Works

```bash
# Check service is running
sudo systemctl status trading-system.service

# Check database connection
python -c "from src.storage.db import get_db; db = get_db(); print('✅ Database connected')"

# Check logs
tail -f ~/TradingSystem/logs/trading.log
```

## Advantages Over App Platform

✅ **Full SSH Access** - Can run any commands, scripts, debug issues
✅ **Direct Database Access** - Connect as doadmin via psql, grant permissions easily
✅ **Better Control** - Install any tools, configure anything
✅ **Easier Debugging** - Direct access to logs, processes, files
✅ **Cost Effective** - $12-24/mo for Droplet + separate database cost
✅ **No UI Limitations** - Don't need to hunt for database console

## Maintenance

```bash
# View logs
tail -f ~/TradingSystem/logs/trading.log

# Restart service
sudo systemctl restart trading-system.service

# Update code
cd ~/TradingSystem
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart trading-system.service
```

## Backup Strategy

- **Database:** Managed database has automatic backups
- **Code:** Git repository (already backed up)
- **Logs:** Consider log rotation or external backup

## Security

- Use SSH keys (not passwords)
- Keep system updated: `apt update && apt upgrade`
- Use firewall (ufw)
- Keep `.env` file secure (chmod 600)
- Don't expose database publicly (use managed database with trusted sources)
