# Deployment Workflow Guide

This guide explains how to deploy code changes from your local machine to the production server using SSH and GitHub.

## Overview

The deployment process:
1. **Local Development**: Make changes, test locally
2. **GitHub Push**: Commit and push to GitHub (using GitHub token)
3. **Server Deployment**: SSH to production server, pull latest code, restart service

## Prerequisites

### 1. SSH Key Setup

Your SSH key should already be configured. Verify:

```bash
ls -la ~/.ssh/trading_system_droplet
```

If the key doesn't exist, you'll need to:
1. Generate or add the SSH key
2. Ensure it's authorized on the server

### 2. GitHub Token Setup

1. Create a GitHub Personal Access Token:
   - Go to: https://github.com/settings/tokens
   - Click "Generate new token (classic)"
   - Name: "TradingSystem Deployment"
   - Scopes: Select `repo` (full control of private repositories)
   - Generate and copy the token

2. Add to `.env.local`:
   ```bash
   # Copy from template if needed
   cp .env.local.example .env.local
   
   # Edit .env.local and add:
   GITHUB_TOKEN=your_github_personal_access_token_here
   ```

### 3. Server Configuration

Default server settings (can be overridden in `.env.local`):

```bash
DEPLOY_SERVER=root@164.92.129.140
DEPLOY_SSH_KEY=~/.ssh/trading_system_droplet
DEPLOY_TRADING_USER=trading
DEPLOY_TRADING_DIR=/home/trading/TradingSystem
DEPLOY_SERVICE_NAME=trading-system.service
```

## Deployment Methods

### Method 1: Full Deployment (Recommended)

Runs tests, commits, pushes, and deploys:

```bash
make deploy
```

**What it does:**
1. ✅ Runs smoke tests (30 seconds)
2. ✅ Commits all changes
3. ✅ Pushes to GitHub
4. ✅ Deploys to production server
5. ✅ Restarts service
6. ✅ Verifies deployment

### Method 2: Quick Deployment

Skips tests, commits, pushes, and deploys:

```bash
make deploy-quick
```

**Use when:**
- You've already tested locally
- Making a quick fix
- Need to deploy urgently

### Method 3: Manual Deployment Script

Use the script directly for more control:

```bash
# Full deployment with tests
./scripts/deploy.sh

# Skip tests
./scripts/deploy.sh --skip-tests

# Skip commit (if already pushed)
./scripts/deploy.sh --skip-commit

# Custom commit message
./scripts/deploy.sh --message "Fix: Instrument specs issue"

# Force push (use with caution)
./scripts/deploy.sh --force
```

## Step-by-Step Workflow

### 1. Make Changes Locally

```bash
# Make your code changes
vim src/some_file.py

# Test locally
make smoke
```

### 2. Deploy

```bash
# Option A: Full deployment (recommended)
make deploy

# Option B: Quick deployment
make deploy-quick

# Option C: Manual with custom message
./scripts/deploy.sh --message "Your commit message"
```

### 3. Monitor Deployment

After deployment, monitor the logs:

```bash
# SSH to server and tail logs
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140 \
  'sudo -u trading tail -f /home/trading/TradingSystem/logs/run.log'

# Or check service status
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140 \
  'systemctl status trading-system.service'
```

## Troubleshooting

### ❌ "SSH key not found"

**Problem:** SSH key doesn't exist or wrong path.

**Solution:**
```bash
# Check if key exists
ls -la ~/.ssh/trading_system_droplet

# If missing, you need to set it up or update DEPLOY_SSH_KEY in .env.local
```

### ❌ "Failed to connect to server via SSH"

**Problem:** Can't SSH to the server.

**Solution:**
```bash
# Test SSH connection manually
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140 "echo 'test'"

# Check key permissions
chmod 600 ~/.ssh/trading_system_droplet

# Verify server is accessible
ping 164.92.129.140
```

### ❌ "GITHUB_TOKEN not set"

**Problem:** GitHub token missing.

**Solution:**
1. Create token at https://github.com/settings/tokens
2. Add to `.env.local`:
   ```bash
   GITHUB_TOKEN=your_token_here
   ```

### ❌ "Failed to push to GitHub"

**Problem:** Can't push to GitHub.

**Solutions:**
- Check if token has correct permissions (`repo` scope)
- Verify you have write access to the repository
- Check if branch is protected (may need to use `--force` with caution)

### ❌ "Service is not active"

**Problem:** Service didn't start after deployment.

**Solution:**
```bash
# Check service logs
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140 \
  'journalctl -u trading-system.service -n 50 --no-pager'

# Check application logs
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140 \
  'sudo -u trading tail -n 50 /home/trading/TradingSystem/logs/run.log'
```

### ❌ "Smoke tests failed"

**Problem:** Pre-deployment tests are failing.

**Solution:**
```bash
# Run tests manually to see errors
make smoke

# Fix issues, then retry deployment
# Or use --skip-tests if you're confident (not recommended)
```

## Advanced Usage

### Custom Server Configuration

Override defaults in `.env.local`:

```bash
DEPLOY_SERVER=user@different-server.com
DEPLOY_SSH_KEY=~/.ssh/custom_key
DEPLOY_TRADING_USER=custom_user
DEPLOY_TRADING_DIR=/custom/path
DEPLOY_SERVICE_NAME=custom-service.service
```

### Deployment Without Committing

If you've already committed and pushed manually:

```bash
./scripts/deploy.sh --skip-commit
```

### Force Push (Use with Caution)

Only use if you know what you're doing:

```bash
./scripts/deploy.sh --force
```

⚠️ **Warning:** Force push can overwrite remote history. Only use when necessary.

## Security Best Practices

1. **Never commit `.env.local`** - It contains secrets
2. **Rotate GitHub tokens periodically**
3. **Use SSH keys with passphrases** (optional but recommended)
4. **Limit GitHub token scope** - Only grant `repo` access if needed
5. **Review changes before deploying** - Don't deploy untested code

## Quick Reference

```bash
# Full deployment (tests + commit + push + deploy)
make deploy

# Quick deployment (skip tests)
make deploy-quick

# Manual deployment with options
./scripts/deploy.sh [--skip-tests] [--skip-commit] [--message "msg"] [--force]

# Monitor logs
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140 \
  'sudo -u trading tail -f /home/trading/TradingSystem/logs/run.log'

# Check service status
ssh -i ~/.ssh/trading_system_droplet root@164.92.129.140 \
  'systemctl status trading-system.service'
```

## Next Steps

After deployment:
1. ✅ Monitor logs for errors
2. ✅ Verify service is running
3. ✅ Check dashboard (if applicable)
4. ✅ Verify trading activity (if live)

---

**Questions or Issues?** Check the logs first, then review the troubleshooting section above.
