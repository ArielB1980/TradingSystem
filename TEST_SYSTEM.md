# System Testing Guide

## Quick Test via Web Endpoint

Visit: **https://tradingbot-2tdzi.ondigitalocean.app/test**

This will run all system tests and return results.

## Test Components

The system test verifies:

1. **Database Connection** ✅
   - Tests PostgreSQL connection
   - Verifies database is accessible

2. **Kraken API Connection** ✅
   - Tests spot market data access
   - Tests futures API (if credentials configured)
   - Verifies API keys are working

3. **Data Acquisition** ✅
   - Fetches candles for test coins
   - Verifies data is being retrieved
   - Checks multiple timeframes

4. **Signal Processing** ✅
   - Tests SMC engine signal generation
   - Verifies all required candles are available
   - Generates a test signal

## Running Tests Locally

```bash
# Activate virtual environment
source venv/bin/activate

# Run tests
python run.py test
```

Or directly:
```bash
python src/test_system.py
```

## Running Tests on Server

### Option 1: Via Web Endpoint
Visit: https://tradingbot-2tdzi.ondigitalocean.app/test

### Option 2: Via App Platform Console
If SSH/console access is available:
```bash
python run.py test
```

### Option 3: Check Runtime Logs
- App Platform → Runtime Logs
- Look for test output or errors

## Expected Output

```
============================================================
TRADING SYSTEM HEALTH CHECK
============================================================
✅ Config loaded from src/config/config.yaml

============================================================
TEST 1: Database Connection
============================================================
✅ Database connected: PostgreSQL 15.x...

============================================================
TEST 2: Kraken API Connection
============================================================
Testing spot market data access...
✅ Spot API working - BTC/USD price: $45000.00
Testing futures API access...
✅ Futures API working - Balance retrieved

============================================================
TEST 3: Data Acquisition (Getting Coin Data)
============================================================
Testing data acquisition for 3 coins...
  Fetching BTC/USD...
    ✅ BTC/USD: Got 10 candles, latest: $45000.00 @ 2026-01-14 13:00:00
  Fetching ETH/USD...
    ✅ ETH/USD: Got 10 candles, latest: $2500.00 @ 2026-01-14 13:00:00
  ...

✅ Data acquisition working - Successfully fetched data for 3/3 coins

============================================================
TEST 4: Signal Processing (SMC Engine)
============================================================
Testing signal generation for BTC/USD...
  Fetching candles...
  ✅ Got candles: 15m=100, 1h=100, 4h=100, 1d=100
  Generating signal...
  ✅ Signal generated:
     Type: no_signal
     Entry: $0
     Stop: $0
     Regime: wide_structure
     Bias: bullish

============================================================
TEST SUMMARY
============================================================
1. Database Connection: ✅ PASS
2. Kraken API Connection: ✅ PASS
3. Data Acquisition: ✅ PASS
4. Signal Processing: ✅ PASS

🎉 All tests passed! System is ready for trading.
```

## Troubleshooting

### Database Connection Fails
- Check `DATABASE_URL` environment variable
- Verify database firewall allows connections
- Check database is running

### API Connection Fails
- Verify API keys are set in environment variables
- Check API keys are valid
- Verify network connectivity

### Data Acquisition Fails
- Check API connection first
- Verify symbols are valid
- Check rate limits aren't exceeded

### Signal Processing Fails
- Ensure data acquisition is working
- Check all required timeframes have data
- Verify config file is valid

## Continuous Monitoring

Set up periodic health checks:
- Monitor `/health` endpoint
- Check runtime logs regularly
- Set up alerts for errors
- Review test results periodically
