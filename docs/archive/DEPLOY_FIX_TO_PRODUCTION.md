# Deploy Symbol Format Fix to Production

## Issue Found

Production logs show:
```
"Failed to submit entry order"
"Instrument specs for AUD/USD:USD not found"
"Instrument specs for BRETT/USD:USD not found"
"Instrument specs for ONE/USD:USD not found"
```

This is the same issue we fixed locally - the instrument lookup is using the wrong symbol format.

## Fix Status

✅ **Fix is committed and pushed to GitHub** (commit `9780f2d`)
- File: `src/execution/futures_adapter.py`
- Fix: Added fallback logic to try multiple symbol formats when looking up instruments

## Deployment Steps

### Option 1: Pull Latest Code and Restart Service

```bash
# SSH to production server
ssh root@your-server

# Switch to trading user
su - trading

# Navigate to TradingSystem
cd TradingSystem

# Pull latest code
git pull origin main

# Restart the service (as root)
exit
sudo systemctl restart trading-system.service

# Verify it's running
sudo systemctl status trading-system.service
```

### Option 2: Manual File Update

If git pull doesn't work, manually update the file:

```bash
# On production server
cd /home/trading/TradingSystem

# Backup current file
cp src/execution/futures_adapter.py src/execution/futures_adapter.py.backup

# Update the file with the fix (lines 155-185)
# Copy the fixed code from the GitHub commit
```

## Verify Fix

After deployment, monitor logs:

```bash
tail -f logs/run.log | grep -E "Entry order submitted|Failed to submit entry order|Instrument specs"
```

You should see:
- ✅ "Entry order submitted" (success)
- ❌ No more "Instrument specs for X/USD:USD not found" errors

## Expected Behavior After Fix

1. Signals generated ✅ (already working)
2. Auction approves trades ✅ (already working)
3. **Orders placed successfully** ✅ (will work after fix)
4. Stop loss and TP ladder placed ✅ (will work after fix)
