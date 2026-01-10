"""
Pre-flight check script for live trading readiness.

Validates all safety gates, configuration, and system health
before allowing live trading to start.
"""
import sys
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timedelta

from src.config.config import load_config
from src.storage.repository import get_all_trades
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


class PreFlightCheck:
    """Validates system readiness for live trading."""
    
    def __init__(self):
        self.config = load_config()
        self.failures = []
        self.warnings = []
    
    def run_all_checks(self) -> bool:
        """Run all pre-flight checks."""
        print("=" * 60)
        print("🚀 LIVE TRADING PRE-FLIGHT CHECK")
        print("=" * 60)
        print()
        
        checks = [
            self.check_configuration,
            self.check_safety_gates,
            self.check_paper_trading_history,
            self.check_api_credentials,
            self.check_system_health,
            self.check_risk_parameters,
        ]
        
        for check in checks:
            check()
        
        # Summary
        print()
        print("=" * 60)
        print("SUMMARY")
        print("=" * 60)
        
        if self.failures:
            print(f"\n❌ FAILED: {len(self.failures)} critical issues")
            for i, failure in enumerate(self.failures, 1):
                print(f"  {i}. {failure}")
            print("\n⚠️  DO NOT PROCEED WITH LIVE TRADING")
            return False
        
        if self.warnings:
            print(f"\n⚠️  WARNINGS: {len(self.warnings)} items need attention")
            for i, warning in enumerate(self.warnings, 1):
                print(f"  {i}. {warning}")
        
        print("\n✅ ALL CRITICAL CHECKS PASSED")
        print("\n🟢 System is ready for live trading (proceed with caution)")
        return True
    
    def check_configuration(self):
        """Validate configuration for live trading."""
        print("📋 Checking Configuration...")
        
        # Environment must be prod for live
        if self.config.environment != "prod":
            self.failures.append(
                f"Environment is '{self.config.environment}', must be 'prod' for live trading"
            )
        else:
            print("  ✅ Environment: prod")
        
        # Live config checks
        if not hasattr(self.config, 'live'):
            self.failures.append("LiveConfig not found in configuration")
        else:
            if not self.config.live.require_paper_success:
                self.warnings.append("Paper trading success not required (risky)")
            else:
                print(f"  ✅ Paper trading requirement: enabled")
        
        print()
    
    def check_safety_gates(self):
        """Validate all safety mechanisms."""
        print("🛡️  Checking Safety Gates...")
        
        # Max leverage check
        if self.config.risk.max_leverage > 10.0:
            self.failures.append(
                f"Max leverage {self.config.risk.max_leverage}× exceeds 10× hard limit"
            )
        elif self.config.risk.max_leverage > 5.0:
            self.warnings.append(
                f"Max leverage {self.config.risk.max_leverage}× is high for first week (recommend ≤5×)"
            )
        else:
            print(f"  ✅ Max leverage: {self.config.risk.max_leverage}×")
        
        # Risk per trade check
        if self.config.risk.risk_per_trade_pct > 0.01:
            self.warnings.append(
                f"Risk per trade {self.config.risk.risk_per_trade_pct:.1%} is high (recommend ≤0.5% for first week)"
            )
        else:
            print(f"  ✅ Risk per trade: {self.config.risk.risk_per_trade_pct:.1%}")
        
        # Position limits
        if self.config.risk.max_concurrent_positions > 3:
            self.warnings.append(
                f"Max positions {self.config.risk.max_concurrent_positions} is high (recommend ≤1 for first day)"
            )
        else:
            print(f"  ✅ Max concurrent positions: {self.config.risk.max_concurrent_positions}")
        
        # Daily loss limit
        if self.config.risk.daily_loss_limit_pct > 0.05:
            self.warnings.append(
                f"Daily loss limit {self.config.risk.daily_loss_limit_pct:.1%} is high (recommend ≤2%)"
            )
        else:
            print(f"  ✅ Daily loss limit: {self.config.risk.daily_loss_limit_pct:.1%}")
        
        # Liquidation buffer
        if self.config.risk.min_liquidation_buffer_pct < 0.40:
            self.failures.append(
                f"Liquidation buffer {self.config.risk.min_liquidation_buffer_pct:.0%} < 40% minimum"
            )
        else:
            print(f"  ✅ Liquidation buffer: {self.config.risk.min_liquidation_buffer_pct:.0%}")
        
        print()
    
    def check_paper_trading_history(self):
        """Validate paper trading performance."""
        print("📊 Checking Paper Trading History...")
        
        # Get trades from last 7 days
        trades = get_all_trades()
        recent_trades = [
            t for t in trades
            if t.exited_at and t.exited_at > datetime.now() - timedelta(days=7)
        ]
        
        min_trades = getattr(self.config.live, 'min_paper_trades', 5) if hasattr(self.config, 'live') else 5
        
        if len(recent_trades) < min_trades:
            self.failures.append(
                f"Only {len(recent_trades)} paper trades in last 7 days (minimum: {min_trades})"
            )
        else:
            print(f"  ✅ Paper trades: {len(recent_trades)} (minimum: {min_trades})")
            
            # Calculate win rate
            wins = sum(1 for t in recent_trades if t.net_pnl > 0)
            win_rate = wins / len(recent_trades) if recent_trades else 0
            
            min_win_rate = getattr(self.config.live, 'min_paper_win_rate', 0.30) if hasattr(self.config, 'live') else 0.30
            
            if win_rate < min_win_rate:
                self.warnings.append(
                    f"Paper win rate {win_rate:.1%} below minimum {min_win_rate:.1%}"
                )
            else:
                print(f"  ✅ Paper win rate: {win_rate:.1%}")
            
            # Calculate total PnL
            total_pnl = sum(t.net_pnl for t in recent_trades)
            if total_pnl < 0:
                self.warnings.append(
                    f"Paper trading PnL is negative: ${total_pnl:,.2f}"
                )
            else:
                print(f"  ✅ Paper PnL: ${total_pnl:,.2f}")
        
        print()
    
    def check_api_credentials(self):
        """Validate API credentials are configured."""
        print("🔐 Checking API Credentials...")
        
        if not self.config.exchange.api_key:
            self.failures.append("API key not configured")
        else:
            print(f"  ✅ API key: {self.config.exchange.api_key[:8]}...")
        
        if not self.config.exchange.api_secret:
            self.failures.append("API secret not configured")
        else:
            print("  ✅ API secret: configured")
        
        if self.config.exchange.use_testnet:
            self.failures.append("Testnet mode enabled (must be disabled for live trading)")
        else:
            print("  ✅ Testnet: disabled")
        
        print()
    
    def check_system_health(self):
        """Check system health indicators."""
        print("💊 Checking System Health...")
        
        # Check if database is accessible
        try:
            get_all_trades()
            print("  ✅ Database: accessible")
        except Exception as e:
            self.failures.append(f"Database error: {str(e)}")
        
        # Check config file exists
        config_path = Path("config.yaml")
        if not config_path.exists():
            self.failures.append("config.yaml not found")
        else:
            print("  ✅ Config file: found")
        
        # Check .env file exists
        env_path = Path(".env")
        if not env_path.exists():
            self.warnings.append(".env file not found (API keys should be in .env)")
        else:
            print("  ✅ .env file: found")
        
        print()
    
    def check_risk_parameters(self):
        """Validate risk parameters are sane."""
        print("⚖️  Checking Risk Parameters...")
        
        # Check for ultra-conservative first-week settings
        is_conservative = (
            self.config.risk.risk_per_trade_pct <= 0.005 and
            self.config.risk.max_concurrent_positions <= 1 and
            self.config.risk.max_leverage <= 5.0
        )
        
        if not is_conservative:
            self.warnings.append(
                "Risk parameters not ultra-conservative for first week "
                "(recommend: 0.5% risk, 1 position, 5× leverage)"
            )
        else:
            print("  ✅ Ultra-conservative mode: enabled")
        
        # Check fee assumptions
        if self.config.risk.taker_fee_bps < 5.0:
            self.warnings.append(
                f"Taker fee {self.config.risk.taker_fee_bps} bps seems low (Kraken default: 5-26 bps)"
            )
        else:
            print(f"  ✅ Taker fee: {self.config.risk.taker_fee_bps} bps")
        
        print()


def main():
    """Run pre-flight check."""
    checker = PreFlightCheck()
    passed = checker.run_all_checks()
    
    if not passed:
        print("\n⛔ LIVE TRADING NOT AUTHORIZED")
        print("Fix all critical issues before proceeding.")
        sys.exit(1)
    
    print("\n" + "=" * 60)
    print("⚠️  FINAL WARNING")
    print("=" * 60)
    print()
    print("Live trading with leverage involves substantial risk of loss.")
    print("Only trade with capital you can afford to lose entirely.")
    print()
    print("Recommended first-week approach:")
    print("  • Start capital: $1,000 - $5,000")
    print("  • Risk per trade: 0.5%")
    print("  • Max positions: 1")
    print("  • Monitor continuously")
    print("  • Keep kill switch ready")
    print()
    print("To proceed with live trading:")
    print("  python3 run.py live --start-capital 1000 --confirm")
    print()
    print("=" * 60)
    
    sys.exit(0)


if __name__ == "__main__":
    main()
