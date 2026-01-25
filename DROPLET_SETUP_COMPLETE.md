# Droplet Setup Complete ✅

## Summary

The trading system has been successfully deployed to the DigitalOcean Droplet with a local PostgreSQL database.

## Server Details

- **IP Address:** 164.92.129.140
- **SSH Key:** `~/.ssh/trading_system_droplet`
- **User:** `trading`
- **Database:** PostgreSQL 16 (local, on Droplet)

## What Was Installed

### 1. PostgreSQL Database
- ✅ PostgreSQL 16 installed and running
- ✅ Database `dbtradingbot` created
- ✅ User `dbtradingbot` created with full permissions
- ✅ All tables created via migration script
- ✅ Database configured for local access only

### 2. Application
- ✅ All application files deployed
- ✅ Python 3.12 virtual environment created
- ✅ All dependencies installed
- ✅ Environment variables configured (`.env.local`)

### 3. System Service
- ✅ Systemd service `trading-system.service` created
- ✅ Service enabled to start on boot
- ✅ Service currently running

## Database Connection

**Connection String:**
```
postgresql://dbtradingbot:AVNS_3ZbhLloQP64uLYyhxoe@localhost:5432/dbtradingbot
```

**Location:** Stored in `/home/trading/TradingSystem/.env.local`

## Service Management

### Check Status
```bash
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140
sudo systemctl status trading-system.service
```

### View Logs
```bash
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140
tail -f ~/TradingSystem/logs/trading.log
tail -f ~/TradingSystem/logs/trading-error.log
```

### Restart Service
```bash
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140
sudo systemctl restart trading-system.service
```

### Stop Service
```bash
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140
sudo systemctl stop trading-system.service
```

## Database Management

### Connect to Database
```bash
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140
psql -U dbtradingbot -d dbtradingbot
# Password: AVNS_3ZbhLloQP64uLYyhxoe
```

### Backup Database
```bash
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140
pg_dump -U dbtradingbot dbtradingbot > backup_$(date +%Y%m%d).sql
```

### Restore Database
```bash
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140
psql -U dbtradingbot dbtradingbot < backup_YYYYMMDD.sql
```

## File Locations

- **Application:** `/home/trading/TradingSystem/`
- **Logs:** `/home/trading/TradingSystem/logs/`
- **Environment:** `/home/trading/TradingSystem/.env.local`
- **Database Data:** `/var/lib/postgresql/16/main/`
- **Service Config:** `/etc/systemd/system/trading-system.service`

## Next Steps

1. **Add API Keys:** Update `.env.local` with your Kraken API credentials:
   ```bash
   ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140
   nano ~/TradingSystem/.env.local
   # Add:
   # KRAKEN_API_KEY=your_key
   # KRAKEN_API_SECRET=your_secret
   # KRAKEN_FUTURES_API_KEY=your_futures_key
   # KRAKEN_FUTURES_API_SECRET=your_futures_secret
   ```

2. **Restart Service:** After adding API keys:
   ```bash
   sudo systemctl restart trading-system.service
   ```

3. **Monitor Logs:** Watch for any errors or issues:
   ```bash
   tail -f ~/TradingSystem/logs/trading.log
   ```

## Troubleshooting

### Service Not Starting
```bash
# Check service status
sudo systemctl status trading-system.service

# Check error logs
tail -50 ~/TradingSystem/logs/trading-error.log
```

### Database Connection Issues
```bash
# Test database connection
psql -U dbtradingbot -d dbtradingbot -c "SELECT version();"

# Check PostgreSQL status
sudo systemctl status postgresql
```

### Permission Issues
```bash
# Ensure trading user owns the application directory
sudo chown -R trading:trading /home/trading/TradingSystem
```

## Security Notes

- Database is only accessible from localhost (127.0.0.1)
- `.env.local` file has restricted permissions (600)
- Service runs as non-root user (`trading`)
- SSH key authentication required for access

## Backup Recommendations

1. **Regular Database Backups:**
   - Set up a cron job to backup the database daily
   - Store backups in a separate location

2. **Configuration Backups:**
   - Backup `.env.local` (without committing secrets)
   - Backup service configuration files
