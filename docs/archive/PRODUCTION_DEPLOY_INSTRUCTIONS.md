# Deploy Fix to Production Server

## Problem Identified

Production logs show orders are failing:
```
"Failed to submit entry order"
"Instrument specs for AUD/USD:USD not found"
```

This is the **same symbol format issue** we fixed locally. The fix is in commit `9780f2d` but production hasn't pulled it yet.

## Quick Deploy Commands

Run these on the production server:

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
sudo systemctl restart trading-system.service

# 6. Verify it's running
sudo systemctl status trading-system.service

# 7. Monitor logs for successful orders
sudo -u trading tail -f /home/trading/TradingSystem/logs/run.log | grep -E "Entry order submitted|Failed to submit|Instrument specs"
```

## What the Fix Does

The fix in `src/execution/futures_adapter.py` now tries multiple symbol formats when looking up instruments:
1. `PF_AUDUSD` (Kraken native)
2. `AUDUSD` (without prefix)
3. `AUD/USD:USD` (CCXT unified format)

This should resolve the "Instrument specs not found" errors.

## Expected Result

After deployment, you should see:
- ✅ "Entry order submitted" messages
- ✅ "Protective SL placed" messages
- ✅ "TP ladder placed" messages
- ❌ No more "Instrument specs for X/USD:USD not found" errors

## Verify Deployment

```bash
# Check recent order placement
sudo -u trading tail -n 5000 /home/trading/TradingSystem/logs/run.log | grep -E "Entry order submitted|Failed to submit" | tail -n 10
```
