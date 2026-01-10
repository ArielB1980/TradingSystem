"""
Configuration models for the Kraken Futures SMC Trading System.

Uses Pydantic for validation and type safety.
"""
from typing import List, Literal
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml
from pathlib import Path


class ExchangeConfig(BaseSettings):
    """Exchange configuration."""
    name: str = "kraken"
    spot_markets: List[str] = ["BTC/USD", "ETH/USD"]
    futures_markets: List[str] = ["BTCUSD-PERP", "ETHUSD-PERP"]


class RiskConfig(BaseSettings):
    """Risk management configuration."""
    # Position sizing
    risk_per_trade_pct: float = Field(default=0.005, ge=0.0001, le=0.05)
    max_leverage: float = Field(default=10.0, ge=1.0, le=10.0)
    
    # Liquidation safety
    min_liquidation_buffer_pct: float = Field(default=0.35, ge=0.30, le=0.50)
    
    # Portfolio limits
    max_concurrent_positions: int = Field(default=2, ge=1, le=5)
    daily_loss_limit_pct: float = Field(default=0.02, ge=0.01, le=0.10)
    loss_streak_cooldown: int = Field(default=3, ge=2, le=10)
    
    # Basis guards
    basis_max_pct: float = Field(default=0.0075, ge=0.001, le=0.02)
    basis_max_post_pct: float = Field(default=0.0075, ge=0.001, le=0.02)
    
    # Fee & Funding Assumptions (configurable for accuracy)
    taker_fee_bps: float = Field(default=5.0, ge=1.0, le=10.0)
    maker_fee_bps: float = Field(default=2.0, ge=0.0, le=5.0)
    funding_rate_daily_bps: float = Field(default=10.0, ge=0.0, le=50.0)
    use_live_funding_rate: bool = Field(default=False)  # Future: fetch from API
    
    # Cost-aware validation
    max_fee_funding_rr_distortion_pct: float = Field(default=0.20, ge=0.05, le=0.30)
    rr_distortion_strict_limit_pct: float = Field(default=0.10, ge=0.05, le=0.30)
    tight_stop_threshold_pct: float = Field(default=0.015, ge=0.005, le=0.05)
    funding_cost_threshold_pct: float | None = Field(default=0.02, ge=0.0, le=0.10)

    @field_validator('max_leverage')
    @classmethod
    def validate_leverage(cls, v):
        if v > 10.0:
            raise ValueError("Leverage cap is 10× (hard limit, non-negotiable)")
        return v


class StrategyConfig(BaseSettings):
    """Strategy parameters configuration."""
    # Timeframes
    bias_timeframes: List[str] = ["4h", "1d"]
    execution_timeframes: List[str] = ["15m", "1h"]
    
    # Indicators
    ema_period: int = Field(default=200, ge=50, le=300)
    adx_period: int = Field(default=14, ge=7, le=30)
    adx_threshold: float = Field(default=25.0, ge=15.0, le=40.0)
    atr_period: int = Field(default=14, ge=7, le=30)
    atr_multiplier_stop: float = Field(default=1.5, ge=1.0, le=3.0)
    rsi_period: int = Field(default=14, ge=7, le=30)
    rsi_divergence_enabled: bool = False
    
    # SMC Parameters
    orderblock_lookback: int = Field(default=50, ge=20, le=200)
    ob_entry_mode: Literal["high_low", "mid", "open", "discount"] = Field(default="mid")
    ob_discount_pct: float = Field(default=0.25, ge=0.1, le=0.5)
    fvg_min_size_pct: float = Field(default=0.001, ge=0.0001, le=0.01)
    bos_confirmation_candles: int = Field(default=3, ge=1, le=10)
    require_bos_confirmation: bool = Field(default=False)  # Optional filter for higher quality
    fvg_mitigation_mode: Literal["touched", "partial", "full"] = "touched"
    fvg_partial_fill_pct: float = Field(default=0.5, ge=0.0, le=1.0)


class ExecutionConfig(BaseSettings):
    """Execution settings configuration."""
    # Price conversion
    use_mark_price: bool = True
    
    # Order Structure
    default_order_type: Literal["market", "limit"] = "limit"
    slippage_tolerance_pct: float = Field(default=0.001, ge=0.0001, le=0.01)
    
    # Take Profit Settings
    tp_mode: Literal["structure_with_rr_fallback"] = "structure_with_rr_fallback"
    tp_splits: List[float] = [0.35, 0.35, 0.30]
    rr_fallback_multiples: List[float] = [1.0, 2.0, 3.0]
    
    # Dynamic Management
    break_even_trigger: Literal["tp1_fill"] = "tp1_fill"
    break_even_offset_ticks: int = 2
    
    trailing_enabled: bool = True
    trailing_trigger: Literal["tp1_fill"] = "tp1_fill"
    trailing_type: Literal["atr"] = "atr"
    trailing_atr_period: int = 14
    trailing_atr_mult: float = 2.0
    trailing_update_min_ticks: int = 2
    trail_tighten_after_tp2: bool = False
    trail_atr_mult_after_tp2: float = 1.6
    order_timeout_seconds: int = Field(default=30, ge=10, le=120)
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_backoff_seconds: int = Field(default=2, ge=1, le=10)
    
    # Pyramiding
    pyramiding_enabled: bool = False  # Default: no adding to positions


class DataConfig(BaseSettings):
    """Data acquisition configuration."""
    # WebSocket settings
    ws_reconnect_max_retries: int = Field(default=10, ge=3, le=50)
    ws_reconnect_backoff_seconds: int = Field(default=5, ge=1, le=30)
    
    # Data validation
    max_gap_seconds: int = Field(default=60, ge=10, le=300)
    
    # Storage
    database_url: str


class ReconciliationConfig(BaseSettings):
    """Reconciliation configuration."""
    periodic_interval_seconds: int = Field(default=15, ge=5, le=60)


class MonitoringConfig(BaseSettings):
    """Monitoring and alerting configuration."""
    # Logging
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "text"] = "json"
    
    # Alerts
    alert_margin_usage_threshold_pct: float = Field(default=0.70, ge=0.50, le=0.90)
    alert_liquidation_buffer_threshold_pct: float = Field(default=0.35, ge=0.30, le=0.50)
    alert_repeated_rejections_count: int = Field(default=3, ge=2, le=10)
    alert_repeated_rejections_window_seconds: int = Field(default=300, ge=60, le=600)
    
    # Alert delivery
    alert_methods: List[str] = ["log"]


class BacktestConfig(BaseSettings):
    """Backtesting configuration."""
    # Starting capital
    starting_equity: float = Field(default=10000.0, ge=1000.0, le=1000000.0)
    
    # Fill assumptions
    assume_maker_fills: bool = True
    slippage_bps: float = Field(default=2.0, ge=0.0, le=10.0)
    
    # Cost modeling
    maker_fee_bps: float = Field(default=2.0, ge=0.0, le=10.0)
    taker_fee_bps: float = Field(default=5.0, ge=0.0, le=20.0)
    
    # Basis modeling
    basis_model: Literal["static", "stochastic"] = "static"
    static_basis_bps: float = Field(default=5.0, ge=0.0, le=50.0)


class PaperConfig(BaseSettings):
    """Paper trading configuration."""
    simulate_realistic_slippage: bool = True
    simulate_fill_delays_ms: int = Field(default=100, ge=0, le=1000)


class LiveConfig(BaseSettings):
    """Live trading configuration and safety gates."""
    require_paper_success: bool = True
    min_paper_days: int = Field(default=30, ge=7, le=90)
    min_paper_trades: int = Field(default=50, ge=10, le=200)
    max_paper_drawdown_pct: float = Field(default=0.15, ge=0.10, le=0.30)


class Config(BaseSettings):
    """Main configuration class."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )
    
    exchange: ExchangeConfig
    risk: RiskConfig
    strategy: StrategyConfig
    execution: ExecutionConfig
    data: DataConfig
    reconciliation: ReconciliationConfig
    monitoring: MonitoringConfig
    backtest: BacktestConfig
    paper: PaperConfig
    live: LiveConfig
    environment: Literal["dev", "paper", "prod"] = "dev"

    @classmethod
    def from_yaml(cls, yaml_path: str | Path) -> "Config":
        """Load configuration from YAML file."""
        yaml_path = Path(yaml_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {yaml_path}")
        
        with open(yaml_path, "r") as f:
            config_dict = yaml.safe_load(f)
            
        import os
        if "ENVIRONMENT" in os.environ:
            config_dict["environment"] = os.environ["ENVIRONMENT"]
        
        return cls(**config_dict)

    def validate_config(self) -> None:
        """Perform additional validation checks."""
        # Design lock: Mark price must be used
        if not self.execution.use_mark_price:
            raise ValueError("DESIGN LOCK VIOLATION: Mark price MUST be used for all safety-critical operations")
        
        # Design lock: Leverage cap enforcement
        if self.risk.max_leverage > 10.0:
            raise ValueError("DESIGN LOCK VIOLATION: Leverage cap is 10× (hard limit)")
        
        # Design lock: Pyramiding default
        if self.environment == "prod" and self.execution.pyramiding_enabled:
            raise Warning("Pyramiding is enabled in production - ensure this is intentional")
        
        # Validate basis guards are set
        if self.risk.basis_max_pct <= 0:
            raise ValueError("Basis guard must be configured (basis_max_pct > 0)")


def load_config(config_path: str | None = None) -> Config:
    """
    Load and validate configuration.
    
    Args:
        config_path: Path to config.yaml file. If None, uses src/config/config.yaml
    
    Returns:
        Validated Config object
    
    Raises:
        FileNotFoundError: If config file not found
        ValueError: If configuration validation fails
    """
    if config_path is None:
        config_path = Path(__file__).parent / "config.yaml"
    
    config = Config.from_yaml(config_path)
    config.validate_config()
    
    return config
