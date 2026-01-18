# DigitalOcean App Platform Deployment Guide

## Current Setup

Your trading system is deployed on **DigitalOcean App Platform** with 3 services:
- **dashboard** - Streamlit dashboard (port 8080)
- **web** - Health check API (port 8080)
- **worker** - Trading bot (runs `python run.py live --force`)

All services automatically pull from `github.com/ArielB1980/TradingSystem` branch `main`.

## Deployment Status

The regime classification fix has been pushed to `main` (commit `3ab1aec`).

### Auto-Deployment Timeline:
1. ✅ Code pushed to GitHub `main` - **DONE**
2. ⏳ DigitalOcean detects changes - **IN PROGRESS** (1-2 minutes)
3. ⏳ Rebuild Docker images - **PENDING** (3-5 minutes)
4. ⏳ Deploy new containers - **PENDING** (1-2 minutes)
5. ⏳ Health checks pass - **PENDING** (30 seconds)
6. ✅ New code live - **ESTIMATED: 5-10 minutes from push**

## How to Check Deployment Status

### Method 1: DigitalOcean Web Console
1. Go to: https://cloud.digitalocean.com/apps
2. Click on **tradingsystem**
3. Check the **"Activity"** tab for deployment status
4. Look for "Deployment in progress" or "Deployment successful"

### Method 2: Using doctl CLI

```bash
# Install doctl (if not installed)
brew install doctl  # macOS
# or
snap install doctl  # Linux

# Authenticate
doctl auth init

# List apps and get APP_ID
doctl apps list

# Check deployment status
doctl apps get YOUR_APP_ID

# View recent deployments
doctl apps list-deployments YOUR_APP_ID

# Trigger manual deployment (if needed)
doctl apps create-deployment YOUR_APP_ID
```

### Method 3: Check Dashboard Directly
Simply refresh your dashboard in a few minutes and check if the regime distribution has changed.

## Force Manual Deployment

If auto-deployment doesn't trigger:

1. **Via Web Console**:
   - Go to https://cloud.digitalocean.com/apps
   - Select your app
   - Click **"Actions"** → **"Force Rebuild and Deploy"**

2. **Via CLI**:
   ```bash
   doctl apps create-deployment YOUR_APP_ID
   ```

## Verify Deployment

Once deployed, verify the fix is working:

1. **Check Dashboard**: Should now show 3-4 different regimes (tight_smc, wide_structure, consolidation, no_data)

2. **Check Logs**:
   - Go to DigitalOcean console → Your App → **"Runtime Logs"**
   - Select the **"worker"** component
   - Look for log entries with `📊 Market Regime: tight_smc` or similar

3. **Check Database**:
   - New decision traces should have regime field populated with diverse values
   - Query: `SELECT regime, COUNT(*) FROM decision_traces GROUP BY regime;`

## Troubleshooting

### Dashboard still shows old data after 10+ minutes
- Check DigitalOcean console for deployment errors
- Verify the deployment completed successfully
- Check if the worker service restarted
- Force a manual deployment

### Deployment failed
- Check build logs in DigitalOcean console
- Verify `app.yaml` is correct
- Check for Python syntax errors in recent commits

### Worker not restarting
- Check worker logs for errors
- Verify environment variables are set correctly
- Check database connection

## Expected Timeline

**Current Time**: 2026-01-18 20:05 CET
**Code Pushed**: 2026-01-18 19:00 CET
**Expected Deployment**: 2026-01-18 20:10 CET (approximately)

**Wait 5-10 minutes from the time you pushed to main, then check the dashboard.**
