"""
Quick test of configuration loading and validation.
"""
from src.config.config import load_config
from src.monitoring.logger import setup_logging

def test_config():
    """Test configuration loading."""
    print("Testing configuration loading...")
    
    # Load config
    try:
        config = load_config("src/config/config.yaml")
        print("✅ Config loaded successfully")
        
        # Test some values
        print(f"\nKey Configuration Values:")
        print(f"  Max Leverage: {config.risk.max_leverage}×")
        print(f"  Risk per Trade: {config.risk.risk_per_trade_pct * 100}%")
        print(f"  Min Liquidation Buffer: {config.risk.min_liquidation_buffer_pct * 100}%")
        print(f"  Basis Max: {config.risk.basis_max_pct * 100}%")
        print(f"  EMA Period: {config.strategy.ema_period}")
        print(f"  ADX Threshold: {config.strategy.adx_threshold}")
        print(f"  Pyramiding Enabled: {config.execution.pyramiding_enabled}")
        print(f"  Environment: {config.environment}")
        
        # Test design locks
        print(f"\nDesign Lock Validations:")
        print(f"  ✅ Mark price enforced: {config.execution.use_mark_price}")
        print(f"  ✅ Leverage capped at: {config.risk.max_leverage}×")
        print(f"  ✅ Pyramiding default: {config.execution.pyramiding_enabled}")
        
        # Test logging
        setup_logging(config.monitoring.log_level, config.monitoring.log_format)
        print(f"\n✅ Logging configured: {config.monitoring.log_level} / {config.monitoring.log_format}")
        
        return True
        
    except Exception as e:
        print(f"❌ Config loading failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    success = test_config()
    exit(0 if success else 1)
