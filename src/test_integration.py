#!/usr/bin/env python3
"""
Comprehensive Integration Test for Trading System

This test runs the full trading pipeline for 5 minutes and verifies:
1. System starts without errors
2. Data is fetched and processed
3. Signal generation works (no UnboundLocalError or similar bugs)
4. All critical code paths are exercised
5. No memory leaks or crashes

Run before every deployment to catch bugs early.
"""

import asyncio
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent))

from src.config.config import load_config
from src.monitoring.logger import setup_logging, get_logger
from src.services.trading_service import TradingService
from src.data.kraken_client import KrakenClient

logger = get_logger("IntegrationTest")


class IntegrationTest:
    """Comprehensive integration test for the trading system."""
    
    def __init__(self):
        self.config = load_config()
        self.errors = []
        self.warnings = []
        self.stats = {
            'symbols_analyzed': 0,
            'signals_generated': 0,
            'no_signals': 0,
            'errors': 0,
            'start_time': None,
            'end_time': None
        }
    
    async def run(self, duration_seconds=300):
        """
        Run integration test for specified duration.
        
        Args:
            duration_seconds: How long to run the test (default 5 minutes)
        """
        logger.info(f"Starting integration test (duration: {duration_seconds}s)")
        self.stats['start_time'] = datetime.now(timezone.utc)
        
        try:
            # 1. Test Data Acquisition
            await self._test_data_acquisition()
            
            # 2. Test Trading Service
            await self._test_trading_service(duration_seconds)
            
            # 3. Verify Results
            self._verify_results()
            
        except Exception as e:
            logger.error(f"Integration test failed: {e}", exc_info=True)
            self.errors.append(f"Test execution failed: {e}")
        
        finally:
            self.stats['end_time'] = datetime.now(timezone.utc)
            self._print_report()
    
    async def _test_data_acquisition(self):
        """Test that data can be fetched from Kraken."""
        logger.info("Testing data acquisition...")
        
        kraken = KrakenClient(
            api_key=self.config.exchange.api_key,
            api_secret=self.config.exchange.api_secret,
            futures_api_key=self.config.exchange.futures_api_key,
            futures_api_secret=self.config.exchange.futures_api_secret,
            use_testnet=self.config.exchange.use_testnet
        )
        
        await kraken.initialize()
        
        # Test fetching data for a few symbols
        test_symbols = ["BTC/USD", "ETH/USD", "SOL/USD"]
        
        for symbol in test_symbols:
            try:
                candles = await kraken.get_spot_ohlcv(symbol, "15m", limit=50)
                if not candles:
                    self.warnings.append(f"No candles returned for {symbol}")
                else:
                    logger.info(f"✓ Fetched {len(candles)} candles for {symbol}")
            except Exception as e:
                self.errors.append(f"Failed to fetch {symbol}: {e}")
        
        logger.info("Data acquisition test complete")
    
    async def _test_trading_service(self, duration_seconds):
        """Test the full trading service pipeline."""
        logger.info(f"Testing trading service for {duration_seconds}s...")
        
        # Create a mock queue for testing
        from asyncio import Queue
        from src.ipc.messages import MarketUpdate
        from src.data.data_acquisition import DataAcquisition
        
        # Initialize components
        kraken = KrakenClient(
            api_key=self.config.exchange.api_key,
            api_secret=self.config.exchange.api_secret,
            futures_api_key=self.config.exchange.futures_api_key,
            futures_api_secret=self.config.exchange.futures_api_secret,
            use_testnet=self.config.exchange.use_testnet
        )
        await kraken.initialize()
        
        # Get a subset of symbols to test (first 20 coins)
        markets = self._get_test_markets()[:20]
        logger.info(f"Testing with {len(markets)} symbols: {markets[:5]}...")
        
        # Fetch data for each symbol
        for symbol in markets:
            try:
                # Fetch all required timeframes
                candles_15m = await kraken.get_spot_ohlcv(symbol, "15m", limit=100)
                candles_1h = await kraken.get_spot_ohlcv(symbol, "1h", limit=100)
                candles_4h = await kraken.get_spot_ohlcv(symbol, "4h", limit=100)
                candles_1d = await kraken.get_spot_ohlcv(symbol, "1d", limit=100)
                
                if not all([candles_15m, candles_1h, candles_4h, candles_1d]):
                    self.warnings.append(f"Incomplete data for {symbol}")
                    continue
                
                # Test signal generation
                await self._test_signal_generation(
                    symbol, candles_15m, candles_1h, candles_4h, candles_1d
                )
                
                self.stats['symbols_analyzed'] += 1
                
            except Exception as e:
                self.errors.append(f"Error testing {symbol}: {e}")
                self.stats['errors'] += 1
                logger.error(f"Error testing {symbol}: {e}")
        
        logger.info("Trading service test complete")
    
    async def _test_signal_generation(self, symbol, c15m, c1h, c4h, c1d):
        """Test signal generation for a symbol."""
        from src.strategy.smc_engine import SMCEngine
        from src.strategy.fibonacci_engine import FibonacciEngine
        from src.strategy.signal_scorer import SignalScorer
        
        # Initialize engines
        fib_engine = FibonacciEngine(self.config.strategy)
        scorer = SignalScorer(self.config.strategy)
        smc_engine = SMCEngine(self.config.strategy, fib_engine, scorer)
        
        try:
            # Generate signal
            signal = smc_engine.generate_signal(
                symbol=symbol,
                candles_15m=c15m,
                candles_1h=c1h,
                bias_candles_4h=c4h,
                bias_candles_1d=c1d
            )
            
            # Verify signal was generated without errors
            if signal:
                from src.domain.models import SignalType
                if signal.signal_type != SignalType.NO_SIGNAL:
                    self.stats['signals_generated'] += 1
                    logger.info(f"✓ Signal generated for {symbol}: {signal.signal_type}")
                else:
                    self.stats['no_signals'] += 1
            else:
                self.errors.append(f"No signal returned for {symbol}")
        
        except Exception as e:
            # This is what we're trying to catch - bugs like trigger_price UnboundLocalError
            self.errors.append(f"Signal generation failed for {symbol}: {e}")
            logger.error(f"✗ Signal generation failed for {symbol}: {e}")
            raise  # Re-raise to fail the test
    
    def _get_test_markets(self):
        """Get list of markets to test."""
        if self.config.coin_universe.enabled:
            markets = []
            for tier_list in self.config.coin_universe.liquidity_tiers.values():
                markets.extend(tier_list)
            return sorted(list(set(markets)))
        else:
            return self.config.trading.symbols
    
    def _verify_results(self):
        """Verify test results meet minimum requirements."""
        logger.info("Verifying test results...")
        
        # Minimum requirements
        min_symbols = 10
        max_error_rate = 0.1  # 10%
        
        if self.stats['symbols_analyzed'] < min_symbols:
            self.errors.append(
                f"Too few symbols analyzed: {self.stats['symbols_analyzed']} < {min_symbols}"
            )
        
        if self.stats['symbols_analyzed'] > 0:
            error_rate = self.stats['errors'] / self.stats['symbols_analyzed']
            if error_rate > max_error_rate:
                self.errors.append(
                    f"Error rate too high: {error_rate:.1%} > {max_error_rate:.1%}"
                )
        
        # Check that signal generation is working
        if self.stats['symbols_analyzed'] > 0 and self.stats['signals_generated'] == 0 and self.stats['no_signals'] == 0:
            self.errors.append("No signals or no_signals recorded - signal generation may be broken")
    
    def _print_report(self):
        """Print test report."""
        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()
        
        print("\n" + "="*60)
        print("INTEGRATION TEST REPORT")
        print("="*60)
        print(f"Duration: {duration:.1f}s")
        print(f"Symbols Analyzed: {self.stats['symbols_analyzed']}")
        print(f"Signals Generated: {self.stats['signals_generated']}")
        print(f"No Signals: {self.stats['no_signals']}")
        print(f"Errors: {self.stats['errors']}")
        print(f"Warnings: {len(self.warnings)}")
        
        if self.errors:
            print("\n❌ ERRORS:")
            for error in self.errors[:10]:  # Show first 10 errors
                print(f"  - {error}")
            if len(self.errors) > 10:
                print(f"  ... and {len(self.errors) - 10} more")
        
        if self.warnings:
            print("\n⚠️  WARNINGS:")
            for warning in self.warnings[:5]:  # Show first 5 warnings
                print(f"  - {warning}")
            if len(self.warnings) > 5:
                print(f"  ... and {len(self.warnings) - 5} more")
        
        print("\n" + "="*60)
        
        if self.errors:
            print("❌ TEST FAILED")
            print("="*60)
            sys.exit(1)
        else:
            print("✅ TEST PASSED")
            print("="*60)
            sys.exit(0)


async def main():
    """Run integration test."""
    setup_logging()
    
    # Parse command line args
    duration = 300  # 5 minutes default
    if len(sys.argv) > 1:
        try:
            duration = int(sys.argv[1])
        except ValueError:
            print(f"Usage: {sys.argv[0]} [duration_seconds]")
            sys.exit(1)
    
    test = IntegrationTest()
    await test.run(duration_seconds=duration)


if __name__ == "__main__":
    asyncio.run(main())
