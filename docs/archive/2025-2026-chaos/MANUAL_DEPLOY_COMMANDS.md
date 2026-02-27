# Manual Deployment Commands

Since SSH authentication needs to be configured, run these commands **directly on the production server**:

## Quick Deploy (Run on Server)

```bash
# 1. Switch to trading user
su - trading

# 2. Navigate to TradingSystem
cd TradingSystem

# 3. Pull latest code from GitHub
git pull origin main

# 4. Exit back to root
exit

# 5. Restart the service
systemctl restart trading-system.service

# 6. Verify it's running
systemctl status trading-system.service
```

## Verify Deployment

```bash
# Check service is running
systemctl status trading-system.service

# Monitor logs for successful orders
sudo -u trading tail -f /home/trading/TradingSystem/logs/run.log | grep -E "Entry order submitted|Failed to submit|Instrument specs"
```

## What to Expect

After deployment, you should see:
- ✅ "Entry order submitted" messages (orders working)
- ✅ "Protective SL placed" messages (stop loss working)  
- ✅ "TP ladder placed" messages (take profit working)
- ❌ No more "Instrument specs for X/USD:USD not found" errors

## If Git Pull Fails

If `git pull` fails, check:
1. Git is configured: `git config --list`
2. Remote is set: `git remote -v`
3. You have network access: `ping github.com`

If needed, manually update the file:
```bash
# On server, edit the file
nano /home/trading/TradingSystem/src/execution/futures_adapter.py

# Update lines 155-188 with the fix from commit 9780f2d
# Then restart the service
```
