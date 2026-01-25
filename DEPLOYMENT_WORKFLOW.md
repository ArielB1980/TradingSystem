# Deployment Workflow

## Standard Deployment Process

All changes must follow this workflow:

1. **Make changes locally**
2. **Run pre-deployment tests**
3. **Commit and push to GitHub**
4. **Deploy to server**

## Step-by-Step

### 1. Make Changes Locally

Edit files in your local workspace. Test changes manually if needed.

### 2. Run Pre-Deployment Tests

```bash
make pre-deploy
```

This runs:
- **Smoke test** (30 seconds) - Quick validation
- **Integration test** (5 minutes) - Full code path testing

**⚠️ Do not skip this step!** Pre-deployment tests catch bugs before they reach production.

### 3. Commit and Push to GitHub

```bash
git add <files>
git commit -m "Descriptive commit message"
git push origin main
```

### 4. Deploy to Droplet

**Option A: Use deployment script (recommended)**
```bash
./scripts/deploy_to_droplet.sh
```

Or with custom parameters:
```bash
./scripts/deploy_to_droplet.sh ~/.ssh/trading_system_droplet 164.92.129.140 trading
```

**Option B: Manual deployment**
```bash
# Transfer files
tar --exclude='.git' --exclude='.venv' --exclude='logs' -czf /tmp/trading-system.tar.gz .
scp -i ~/.ssh/trading_system_droplet /tmp/trading-system.tar.gz trading@164.92.129.140:~/TradingSystem/

# Extract and restart
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140 "cd ~/TradingSystem && tar -xzf trading-system.tar.gz && rm trading-system.tar.gz"
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140 "systemctl restart trading-system.service"
```

## Quick Reference

```bash
# Full workflow
make pre-deploy && git push origin main && ./scripts/deploy_to_droplet.sh

# Check deployment status
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140 "systemctl status trading-system.service"

# View logs
ssh -i ~/.ssh/trading_system_droplet trading@164.92.129.140 "tail -f ~/TradingSystem/logs/trading.log"
```

## Important Notes

- **Always run `make pre-deploy` before deploying**
- **Never push directly to main without testing**
- **Monitor logs after deployment**
- **Verify service is running after restart**
