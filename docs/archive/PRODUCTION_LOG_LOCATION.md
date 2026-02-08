# Finding Logs on Production Server

Since you're already on the server (`root@ubuntu-s-2vcpu-2gb-fra1-01`), run these commands:

## Find the Trading System Directory

```bash
# Check common locations
ls -la /home/*/TradingSystem 2>/dev/null
ls -la /opt/TradingSystem 2>/dev/null
ls -la /root/TradingSystem 2>/dev/null

# Or search for it
find /home -name "TradingSystem" -type d 2>/dev/null
find /opt -name "TradingSystem" -type d 2>/dev/null
find /root -name "TradingSystem" -type d 2>/dev/null
```

## Find Running Processes

```bash
# Check if trading system is running
ps aux | grep -E "run.py|python.*live|trading" | grep -v grep

# Check systemd services
systemctl list-units | grep -E "trading|run"
systemctl status trading* 2>/dev/null
```

## Find Log Files

```bash
# Search for log files
find /home -name "run.log" -type f 2>/dev/null
find /opt -name "run.log" -type f 2>/dev/null
find /root -name "*.log" -type f 2>/dev/null

# Check common log locations
ls -la /var/log/trading* 2>/dev/null
ls -la /var/log/*trading* 2>/dev/null
```

## Once You Find the Directory

```bash
# Navigate to the directory (replace with actual path)
cd /path/to/TradingSystem

# Check logs
tail -f logs/run.log | grep -E "Futures symbol not found|Signal skipped|Signal generated"

# Or if logs are elsewhere
tail -f /path/to/logs/run.log | grep -E "Futures symbol not found|Signal skipped|Signal generated"
```

## Check Service Status

If it's running as a systemd service:

```bash
# List all services
systemctl list-units --type=service | grep -i trading

# Check status
systemctl status trading-system
# or
systemctl status trading-bot
# or
systemctl status run-trading
```

## Quick Check Script

Run this to find everything:

```bash
echo "=== Finding Trading System ==="
echo "Processes:"
ps aux | grep -E "run.py|python.*live" | grep -v grep
echo ""
echo "Directories:"
find /home /opt /root -name "TradingSystem" -type d 2>/dev/null | head -5
echo ""
echo "Log files:"
find /home /opt /root /var/log -name "run.log" -type f 2>/dev/null | head -5
echo ""
echo "Systemd services:"
systemctl list-units --type=service | grep -i trading
```
