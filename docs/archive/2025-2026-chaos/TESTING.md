# Pre-Deployment Testing Protocol

## MANDATORY: Run Before Every Push to Main

**CRITICAL RULE**: Never push to `main` without running pre-deployment tests.

```bash
make pre-deploy
```

This runs:
1. **Smoke test** (30s) - Verifies system starts without crashes
2. **Integration test** (5 mins) - Tests signal generation for 20+ symbols

---

## Why This Matters

**The `trigger_price` bug** (2026-01-18) was a perfect example of what happens when we skip testing:
- Bug existed in production for days
- Caused 100% analysis failure (0 signals generated)
- Only discovered during manual verification
- Could have been caught in 5 minutes with integration testing

---

## Testing Workflow

### Before Pushing to Main:
```bash
# 1. Make your changes
git add .
git commit -m "your changes"

# 2. Run pre-deployment tests (MANDATORY)
make pre-deploy

# 3. Only if tests pass, push to main
git push origin main
```

### If Tests Fail:
1. **DO NOT push to main**
2. Fix the issue
3. Re-run `make pre-deploy`
4. Only push when tests pass

---

## Test Descriptions

### Smoke Test (`make smoke`)
- **Duration**: 30 seconds
- **Purpose**: Verify system starts without immediate crashes
- **Coverage**: Basic startup, data acquisition, system initialization
- **When to use**: Quick sanity check during development

### Integration Test (`make integration`)
- **Duration**: 5 minutes
- **Purpose**: Test full trading pipeline end-to-end
- **Coverage**: 
  - Data fetching for 20+ symbols
  - Signal generation for all timeframes
  - All code paths (signal/no_signal)
  - Error handling
- **When to use**: Before every deployment

### Pre-Deploy (`make pre-deploy`)
- **Duration**: 5.5 minutes
- **Purpose**: Complete pre-deployment validation
- **Coverage**: Smoke + Integration
- **When to use**: **ALWAYS before pushing to main**

---

## What the Integration Test Catches

✅ **Bugs like `trigger_price` UnboundLocalError**  
✅ **Signal generation failures**  
✅ **Data acquisition issues**  
✅ **Missing imports**  
✅ **Type errors**  
✅ **Logic errors in signal generation**  
✅ **Crashes in any code path**  

---

## Deployment Checklist

- [ ] Code changes committed locally
- [ ] `make pre-deploy` runs successfully
- [ ] All tests pass (smoke + integration)
- [ ] No errors in logs/integration.log
- [ ] Push to main
- [ ] Monitor production logs for 5 minutes after deployment

---

## Emergency Bypass (Use Sparingly)

If you **absolutely must** push without testing (e.g., critical hotfix):

1. Document why in commit message
2. Create a GitHub issue to run tests ASAP
3. Monitor production closely
4. Run tests immediately after deployment

**This should be rare** - most "emergencies" can wait 5 minutes for testing.

---

## Future Improvements

- [ ] Add unit tests for critical functions
- [ ] Add GitHub Actions CI/CD pipeline
- [ ] Add pre-commit hooks
- [ ] Add production monitoring/alerts
- [ ] Increase integration test coverage to 50+ symbols

---

## Questions?

If tests are failing and you're not sure why:
1. Check `logs/integration.log` for details
2. Run `make integration` again to reproduce
3. Fix the issue before pushing

**Remember**: 5 minutes of testing now saves hours of debugging in production.
