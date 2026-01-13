# Production Deployment Guide

**System**: V2 Trading System  
**Date**: 2025-01-10  
**Status**: ⚠️ **READY WITH SAFETY GATES**

## Current Status

### ✅ System Readiness
- All V2 features implemented and tested
- Configuration set to `prod` environment
- All critical bugs fixed
- System tests passing

### ⚠️ Safety Gates

The system has **built-in safety gates** that will **prevent live trading** until paper trading requirements are met:

- **Require paper success**: ✅ Enabled
- **Minimum paper days**: 30 days
- **Minimum paper trades**: 50 trades
- **Maximum paper drawdown**: 15%

**Current Status**: Paper trading validation not yet implemented (gates will block live trading)

## Production Configuration

### Current Settings (Ultra-Conservative)
```yaml
environment: "prod"
risk_per_trade_pct: 0.003  # 0.3% per trade
max_leverage: 10.0x
max_concurrent_positions: 10
daily_loss_limit_pct: 0.05  # 5%
```

### Safety Mechanisms
- ✅ Kill switch enabled
- ✅ Position limits enforced
- ✅ Leverage cap (10x max)
- ✅ Daily loss limit (5%)
- ✅ Liquidation buffer (35% minimum)
- ✅ Basis guards enabled
- ✅ Paper trading gate (blocks live trading)

## Deployment Process

### Option 1: Standard Deployment (Recommended)
**Requires paper trading validation first**

```bash
# 1. Run paper trading for 30+ days
python3 run.py paper

# 2. After paper trading meets requirements, deploy:
python3 run.py live
```

### Option 2: Force Deployment (NOT RECOMMENDED)
**Bypasses safety gates - USE WITH EXTREME CAUTION**

```bash
# ⚠️ WARNING: This bypasses all safety gates
python3 run.py live --force
```

**⚠️ DO NOT USE `--force` UNLESS:**
- You fully understand the risks
- You have manually validated the system
- You accept full responsibility for losses
- You have tested extensively in paper mode

## Pre-Flight Checklist

Before deploying to production:

### Required Checks
- [ ] System tests passing ✅
- [ ] Configuration validated ✅
- [ ] Environment set to `prod` ✅
- [ ] API credentials configured
- [ ] Database configured
- [ ] Kill switch tested
- [ ] Monitoring/logging operational
- [ ] Risk parameters reviewed
- [ ] Paper trading completed (30+ days, 50+ trades)

### Recommended Checks
- [ ] Extended backtesting (180+ days)
- [ ] Multi-asset validation
- [ ] Failure mode testing
- [ ] Network connectivity tested
- [ ] Backup procedures in place
- [ ] Alert system configured

## Starting Production

### 1. Verify Configuration
```bash
python3 run.py status
```

### 2. Run Pre-Flight Check
```bash
python3 scripts/pre_flight_check.py
```

### 3. Start Live Trading
```bash
# Standard (will check safety gates)
python3 run.py live

# With force (bypasses gates - NOT RECOMMENDED)
python3 run.py live --force
```

### 4. Monitor System
```bash
# Check status
python3 run.py status

# View dashboard
python3 run.py dashboard
```

## Emergency Procedures

### Kill Switch
```bash
# Emergency stop
python3 run.py kill-switch --emergency
```

### Monitoring
- Check logs for errors
- Monitor positions
- Watch for kill switch activation
- Track daily PnL

## Risk Management

### Current Risk Settings (Ultra-Conservative)
- **Risk per trade**: 0.3% (recommended to increase to 0.7-1.0% after validation)
- **Max leverage**: 10x (hard cap)
- **Daily loss limit**: 5%
- **Max concurrent positions**: 10
- **Liquidation buffer**: 35% minimum

### Recommendations
1. Start with current ultra-conservative settings
2. Monitor for 7-14 days
3. Gradually increase risk if performance is stable
4. Never exceed 1% risk per trade
5. Always maintain 35%+ liquidation buffer

## Important Notes

### Safety Gates Protection
The system **will automatically block live trading** if:
- Environment is not `prod`
- Paper trading requirements not met (unless `--force` used)
- Kill switch is active
- Daily loss limit exceeded
- System errors detected

### Production Readiness
**Current Status**: System is **technically ready** but **safety gates will block live trading** until paper trading validation is implemented/completed.

**To proceed with production**:
1. **Option A** (Recommended): Complete paper trading validation first
2. **Option B** (Not Recommended): Use `--force` flag (bypasses all safety gates)

### Recommendations
1. ✅ System is ready for production deployment
2. ⚠️ Safety gates are active and will prevent live trading
3. 📋 Complete paper trading validation before removing gates
4. 🔒 Never use `--force` in production unless absolutely necessary

## Next Steps

1. **For Safe Deployment**:
   - Complete paper trading (30+ days)
   - Validate performance metrics
   - Remove safety gates (or disable paper requirement)
   - Deploy to production

2. **For Immediate Deployment** (Not Recommended):
   - Review all risk parameters
   - Ensure API credentials are correct
   - Start with `--force` flag
   - Monitor closely

---

**⚠️ WARNING**: Trading involves substantial risk of loss. Use at your own risk. Past performance does not guarantee future results.
