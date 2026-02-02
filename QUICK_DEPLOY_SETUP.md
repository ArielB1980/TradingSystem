# Quick Deployment Setup

## ✅ What's Already Configured

1. ✅ SSH key exists: `~/.ssh/trading_droplet`
2. ✅ Git remote configured: `https://github.com/ArielB1980/TradingSystem.git`
3. ✅ Deployment script created: `scripts/deploy.sh`
4. ✅ Makefile targets added: `make deploy` and `make deploy-quick`
5. ✅ `.env.local` is gitignored (safe for secrets)

## 🚀 Quick Start

### Step 1: Add GitHub Token to `.env.local`

1. **Create GitHub Personal Access Token:**
   - Go to: https://github.com/settings/tokens
   - Click "Generate new token (classic)"
   - Name: "TradingSystem Deployment"
   - Select scope: `repo` (full control)
   - Generate and copy the token

2. **Add to `.env.local`:**
   ```bash
   # If .env.local doesn't exist, create it
   cp .env.local.example .env.local
   
   # Edit .env.local and add your token:
   GITHUB_TOKEN=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
   ```

### Step 2: Test Deployment

```bash
# Quick deployment (skips tests)
make deploy-quick

# Or full deployment (runs tests first)
make deploy
```

## 📋 Deployment Options

### Option 1: Full Deployment (Recommended)
```bash
make deploy
```
- Runs smoke tests
- Commits changes
- Pushes to GitHub
- Deploys to server
- Restarts service

### Option 2: Quick Deployment
```bash
make deploy-quick
```
- Skips tests
- Commits changes
- Pushes to GitHub
- Deploys to server
- Restarts service

### Option 3: Manual Script
```bash
# With custom commit message
./scripts/deploy.sh --message "Fix: Your description"

# Skip tests
./scripts/deploy.sh --skip-tests

# Skip commit (if already pushed)
./scripts/deploy.sh --skip-commit
```

## 🔍 Verify Deployment

After deployment, check logs:

```bash
# Monitor live logs
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'sudo -u trading tail -f /home/trading/TradingSystem/logs/run.log'

# Check service status
ssh -i ~/.ssh/trading_droplet root@207.154.193.121 \
  'systemctl status trading-system.service'
```

## 📚 Full Documentation

See `DEPLOYMENT_WORKFLOW.md` for complete documentation.

## ⚠️ Important Notes

1. **GitHub Token**: The token you provided is already in the format needed. Just add it to `.env.local`
2. **SSH Key**: Already configured at `~/.ssh/trading_droplet`
3. **Server**: Default server is `root@207.154.193.121` (can be overridden in `.env.local`)
4. **Safety**: `.env.local` is gitignored, so your token won't be committed

## 🎯 Next Steps

1. Add `GITHUB_TOKEN` to `.env.local`
2. Test with: `make deploy-quick`
3. Monitor logs to verify deployment
4. Use `make deploy` for production deployments (includes tests)

---

**Ready to deploy?** Just add your GitHub token to `.env.local` and run `make deploy-quick`!
