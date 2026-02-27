# DigitalOcean Deployment Guide

This guide provides deployment instructions for DigitalOcean Droplets.

## Quick Start

1. **Create Droplet**
   - Ubuntu 22.04 LTS
   - Minimum: 2GB RAM, 1 vCPU (recommend 4GB+ for 249 coins)
   - Add SSH key during creation

2. **Initial Setup** (as root)
   ```bash
   # Update system
   apt update && apt upgrade -y
   
   # Create trading user
   useradd -m -s /bin/bash trading
   usermod -aG sudo trading
   
   # Switch to trading user
   su - trading
   ```

3. **Clone from GitHub**
   ```bash
   cd ~
   git clone <your-repo-url> TradingSystem
   cd TradingSystem
   ```

4. **Install Dependencies**
   ```bash
   # Python 3.10+
   sudo apt install -y python3 python3-pip python3-venv postgresql postgresql-contrib
   
   # Create virtual environment
   python3 -m venv venv
   source venv/bin/activate
   
   # Install Python packages
   pip install -r requirements.txt
   ```

5. **Database Setup** (PostgreSQL)
   ```bash
   # Create database and user
   sudo -u postgres psql
   CREATE DATABASE trading_system;
   CREATE USER trading_user WITH PASSWORD 'your_secure_password';
   GRANT ALL PRIVILEGES ON DATABASE trading_system TO trading_user;
   \q
   ```

6. **Configuration**
   ```bash
   # Copy and edit config
   cp .env.example .env
   nano .env
   
   # Set database URL
   DATABASE_URL=postgresql://trading_user:your_secure_password@localhost/trading_system
   ```

7. **Initialize Database**
   ```bash
   source venv/bin/activate
   python run.py migrate  # If you have migrations
   # Or create tables manually
   ```

8. **Systemd Service** (auto-start on boot)
   ```bash
   sudo cp scripts/trading-system.service /etc/systemd/system/
   sudo nano /etc/systemd/system/trading-system.service
   
   # Update paths in service file:
   # - WorkingDirectory=/home/trading/TradingSystem
   # - ExecStart=/home/trading/TradingSystem/venv/bin/python run.py live
   # - User=trading
   
   sudo systemctl daemon-reload
   sudo systemctl enable trading-system
   sudo systemctl start trading-system
   sudo systemctl status trading-system
   ```

9. **Dashboard** (optional, separate service)
   ```bash
   sudo cp scripts/dashboard.service /etc/systemd/system/  # Create this if needed
   sudo systemctl enable dashboard
   sudo systemctl start dashboard
   ```

## Key Features

- **Same deployment pattern**: Ubuntu + systemd + PostgreSQL
- **Same folder structure**: `/home/trading/TradingSystem`
- **Same service management**: systemd (not launchd)
- **Firewall**: Use `ufw` instead of `firewalld`
  ```bash
  sudo ufw allow 22/tcp
  sudo ufw allow 8000/tcp  # Dashboard (if exposed)
  sudo ufw enable
  ```

## Monitoring

```bash
# Check logs
sudo journalctl -u trading-system -f

# Check status
sudo systemctl status trading-system

# Restart
sudo systemctl restart trading-system
```

## Security

- Use SSH keys (disable password auth)
- Keep system updated: `sudo apt update && sudo apt upgrade`
- Use strong PostgreSQL password
- Consider fail2ban for SSH protection
- Use firewall (ufw)

## Backup

```bash
# Database backup
pg_dump -U trading_user trading_system > backup_$(date +%Y%m%d).sql

# Restore
psql -U trading_user trading_system < backup_YYYYMMDD.sql
```

## Troubleshooting

- **Service won't start**: Check logs with `journalctl -u trading-system`
- **Database connection**: Verify `.env` DATABASE_URL
- **Permissions**: Ensure trading user owns project directory
- **Port conflicts**: Check with `sudo netstat -tulpn`
