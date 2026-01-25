# Droplet Setup Instructions - Quick Reference

## Server Details
- **IP:** 164.92.129.140
- **SSH Key:** `~/.ssh/trading_system_droplet`
- **User:** `trading` (or `root` for initial setup)

## Current Status

✅ **Completed:**
- Server accessible via SSH
- Python 3.12 installed
- PostgreSQL client installed
- Trading user created
- Virtual environment created
- SSH key added to trading user

⏳ **Next Steps:**

### 1. Clone Repository

Since the repo is private, you need to clone it manually:

**Option A: Clone via SSH (if you have SSH key on GitHub)**
```bash
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140
cd ~
git clone git@github.com:ArielB1980/TradingSystem.git
```

**Option B: Clone via HTTPS (will prompt for credentials)**
```bash
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140
cd ~
git clone https://github.com/ArielB1980/TradingSystem.git
# Enter your GitHub username and personal access token when prompted
```

**Option C: Transfer files from your local machine**
```bash
# On your local machine:
cd /Users/arielbarack/Documents/TradingSystem
tar --exclude='.git' --exclude='__pycache__' --exclude='*.pyc' --exclude='.env' --exclude='logs' -czf trading-system.tar.gz .

# Transfer to server:
scp -i ~/.ssh/trading_system_droplet trading-system.tar.gz trading@164.92.129.140:~/

# On server:
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140
cd ~
tar -xzf trading-system.tar.gz
mv TradingSystem TradingSystem  # If needed
```

### 2. Install Dependencies

```bash
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140
cd ~/TradingSystem
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure Environment

```bash
# Create .env file
nano ~/TradingSystem/.env
```

Add:
```bash
DATABASE_URL=postgresql://dbtradingbot:AVNS_3ZbhLloQP64uLYyhxoe@app-65e2763f-0c06-4d87-a349-ddc49db0abf3-do-user-31978256-0.l.db.ondigitalocean.com:25060/dbtradingbot?sslmode=require

KRAKEN_API_KEY=your_key
KRAKEN_API_SECRET=your_secret
KRAKEN_FUTURES_API_KEY=your_futures_key
KRAKEN_FUTURES_API_SECRET=your_futures_secret

ENVIRONMENT=prod
LOG_LEVEL=INFO
```

```bash
chmod 600 ~/TradingSystem/.env
```

### 4. Grant Database Permissions

```bash
# Connect to database as doadmin (you'll need admin password)
psql "postgresql://doadmin:ADMIN_PASSWORD@app-65e2763f-0c06-4d87-a349-ddc49db0abf3-do-user-31978256-0.l.db.ondigitalocean.com:25060/dbtradingbot?sslmode=require"

# Run SQL:
GRANT CREATE ON SCHEMA public TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO dbtradingbot;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO dbtradingbot;
\q
```

### 5. Initialize Database

```bash
cd ~/TradingSystem
source venv/bin/activate
python migrate_schema.py
```

### 6. Create Systemd Service

```bash
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

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable trading-system.service
sudo systemctl start trading-system.service

# Check status
sudo systemctl status trading-system.service
```

## Quick Commands

```bash
# SSH into server
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140

# View logs
tail -f ~/TradingSystem/logs/trading.log

# Restart service
sudo systemctl restart trading-system.service

# Check service status
sudo systemctl status trading-system.service
```
