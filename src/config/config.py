"""
Configuration models for the Kraken Futures SMC Trading System.

Uses Pydantic for validation and type safety.
"""
from typing import List, Literal, Optional, Dict
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import yaml
from pathlib import Path
from decimal import Decimal
from dotenv import load_dotenv
import os

# Load .env first (default)
load_dotenv()

# Load .env.local if it exists (overrides .env)
env_local_path = Path(__file__).parent.parent.parent / ".env.local"
if env_local_path.exists():
    load_dotenv(dotenv_path=env_local_path, override=True)



class ExchangeConfig(BaseSettings):
    """Exchange configuration."""
    name: str = "kraken"
    
    # Market Discovery (for multi-asset expansion)
    use_market_discovery: bool = True
    discovery_refresh_hours: int = 24
    
    # Legacy: Hardcoded markets (used if use_market_discovery=False)
    spot_markets: List[str] = ["BTC/USD", "ETH/USD"]
    futures_markets: List[str] = ["BTCUSD-PERP", "ETHUSD-PERP"]
    
    # Skip OHLCV fetch for these spot symbols (delisted, unsupported, or consistently failing)
    spot_ohlcv_blocklist: List[str] = Field(default_factory=lambda: ["2Z/USD", "ANIME/USD"], description="Excluded from OHLCV")

    # When spot OHLCV unavailable (BadSymbol, no data), use futures OHLCV for signal analysis
    use_futures_ohlcv_fallback: bool = Field(default=True, description="Use futures candles when spot has 0")
    
    # Credentials (loaded from env or yaml)
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    futures_api_key: Optional[str] = None
    futures_api_secret: Optional[str] = None
    use_testnet: bool = False


class RiskConfig(BaseSettings):
    """Risk management configuration."""
    # Position sizing
    risk_per_trade_pct: float = Field(default=0.005, ge=0.0001, le=0.05)
    max_leverage: float = Field(default=10.0, ge=1.0, le=10.0)
    target_leverage: float = Field(default=7.0, ge=1.0, le=10.0)  # Actual leverage to use

    # Sizing Method: fixed, kelly, volatility, kelly_volatility, leverage_based
    sizing_method: Literal["fixed", "kelly", "volatility", "kelly_volatility", "leverage_based"] = "fixed"
    
    # Kelly Criterion Settings
    kelly_win_prob: float = Field(default=0.55, ge=0.1, le=0.9)
    kelly_win_loss_ratio: float = Field(default=2.0, ge=1.0)
    kelly_max_fraction: float = Field(default=0.25, ge=0.01, le=1.0) # Cap Kelly (Quarter Kelly usually safe)
    
    # Volatility Sizing Settings
    vol_sizing_atr_threshold_high: float = Field(default=1.5, ge=1.0) # Reduce size if ATR > 1.5x Avg
    vol_sizing_atr_threshold_low: float = Field(default=0.5, le=1.0) # Increase size if ATR < 0.5x Avg
    vol_sizing_high_vol_penalty: float = Field(default=0.30, le=0.9) # -30% size
    vol_sizing_low_vol_boost: float = Field(default=0.20, le=0.5) # +20% size
    
    # Position size caps
    max_position_size_usd: float = Field(default=100000.0, ge=1000.0, le=1000000.0)  # Max notional position
    max_risk_per_trade_entry_pct: float = Field(default=0.02, ge=0.001, le=0.10)  # Max risk per trade for Kelly
    
    # Liquidation safety
    min_liquidation_buffer_pct: float = Field(default=0.35, ge=0.30, le=0.50)
    
    # Portfolio limits
    max_concurrent_positions: int = Field(default=2, ge=1, le=100)
    daily_loss_limit_pct: float = Field(default=0.02, ge=0.01, le=0.10)
    
    # Auction mode portfolio limits
    auction_mode_enabled: bool = Field(default=False, description="Enable auction-based portfolio allocation")
    auction_max_positions: int = Field(default=50, ge=1, le=100)
    auction_max_margin_util: float = Field(default=0.90, ge=0.50, le=0.95)
    auction_max_per_cluster: int = Field(default=12, ge=1, le=50)
    auction_max_per_symbol: int = Field(default=1, ge=1, le=5)
    auction_swap_threshold: float = Field(default=10.0, ge=0.0, le=50.0)
    auction_min_hold_minutes: int = Field(default=15, ge=0, le=60)
    auction_max_trades_per_cycle: int = Field(default=5, ge=1, le=20)
    auction_max_new_opens_per_cycle: int = Field(default=5, ge=1, le=20)
    auction_max_closes_per_cycle: int = Field(default=5, ge=1, le=20)
    auction_entry_cost: float = Field(default=2.0, ge=0.0, le=10.0)
    auction_exit_cost: float = Field(default=2.0, ge=0.0, le=10.0)
    
    # Loss streak protection (time-based, not permanent block)
    loss_streak_cooldown: int = Field(default=3, ge=2, le=10)  # Trigger threshold
    loss_streak_pause_minutes: int = Field(default=240, ge=60, le=720)  # Pause duration (4h default)
    loss_streak_min_loss_bps: float = Field(default=20.0, ge=5.0, le=100.0)  # Only count losses > X bps
    
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
    
    # Regime-specific settings (NEW for dual-regime strategy)
    # Tight-stop SMC regime (OB/FVG): 0.4-1.0% stops
    tight_smc_cost_cap_bps: float = Field(default=25.0, ge=10.0, le=50.0)  # Absolute cost cap
    tight_smc_min_rr_multiple: float = Field(default=2.0, ge=1.5, le=5.0)  # Min R:R required (reduced to 2.0)
    tight_smc_avg_hold_hours: float = Field(default=6.0, ge=1.0, le=24.0)  # For funding calc
    
    # Wide-stop structure regime (BOS/TREND): 1.5-3.0% stops
    wide_structure_max_distortion_pct: float = Field(default=0.15, ge=0.10, le=0.25)  # R:R distortion
    wide_structure_avg_hold_hours: float = Field(default=36.0, ge=12.0, le=72.0)  # For funding calc

    # Loss streak cooldown (Regime-Aware)
    loss_streak_cooldown_tight: int = Field(default=3, ge=2, le=10)
    loss_streak_cooldown_wide: int = Field(default=5, ge=2, le=10) # 4-5 losses
    loss_streak_pause_minutes_tight: int = Field(default=120, ge=30, le=300) # 120 minutes
    loss_streak_pause_minutes_wide: int = Field(default=90, ge=30, le=300) # 90 minutes


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
    adx_threshold: float = Field(default=20.0, ge=10.0, le=40.0)
    atr_period: int = Field(default=14, ge=7, le=30)
    
    # Stop buffering (Regime specific ranges)
    # tight_smc: 0.3-0.6 ATR
    # wide_structure: 1.0-1.2 ATR
    tight_smc_atr_stop_min: float = Field(default=0.3, ge=0.1, le=1.0)
    tight_smc_atr_stop_max: float = Field(default=0.6, ge=0.1, le=1.0)
    wide_structure_atr_stop_min: float = Field(default=1.0, ge=0.5, le=2.0)
    wide_structure_atr_stop_max: float = Field(default=1.2, ge=0.5, le=2.0)
    
    # Legacy fallbacks
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

    # Bias Logic
    ema_neutral_zone_bps: float = Field(default=10.0, ge=0.0, le=100.0)
    
    # Scoring Gates
    min_score_tight_smc_aligned: float = Field(default=75.0, ge=0.0, le=100.0)
    min_score_tight_smc_neutral: float = Field(default=80.0, ge=0.0, le=100.0)
    min_score_wide_structure_aligned: float = Field(default=70.0, ge=0.0, le=100.0)
    min_score_wide_structure_neutral: float = Field(default=75.0, ge=0.0, le=100.0)
    
    # Fib Enforcement
    fib_proximity_bps: float = Field(default=20.0, ge=0.0, le=100.0) # 0.2%


    # Market Structure Change Confirmation
    require_ms_change_confirmation: bool = Field(default=True)
    ms_confirmation_candles: int = Field(default=3, ge=1, le=10)
    ms_reconfirmation_candles: int = Field(default=2, ge=1, le=10)
    
    # Adaptive Strategy Logic
    adaptive_enabled: bool = True
    atr_confirmation_threshold_high: float = Field(default=1.5, ge=1.0) # > 1.5x avg ATR -> High Vol
    atr_confirmation_threshold_low: float = Field(default=0.5, le=1.0) # < 0.5x avg ATR -> Low Vol
    base_confirmation_candles: int = 3
    max_confirmation_candles: int = 5
    min_confirmation_candles: int = 2
    
    # RSI Divergence
    rsi_divergence_check: bool = True
    rsi_divergence_lookback: int = 20

    # Exits
    abandon_ship_enabled: bool = True
    time_based_exit_bars: int = Field(default=20, ge=5) # Bars to hold without TP before exit

    # Entry Zone Tolerance (for relaxed reconfirmation)
    # Allows entries when price is "near" OB/FVG zone, not strictly inside
    entry_zone_tolerance_pct: float = Field(default=0.015, ge=0.005, le=0.05)  # 1.5% buffer
    entry_zone_tolerance_adaptive: bool = Field(default=True)  # Scale with ATR
    entry_zone_tolerance_atr_mult: float = Field(default=0.3, ge=0.1, le=1.0)  # ATR multiplier
    entry_zone_tolerance_score_penalty: int = Field(default=-5, ge=-15, le=0)  # Score adjustment for tolerance entries
    
    # Skip Reconfirmation in Trending Markets
    # When True, enters immediately after MSS confirmation without waiting for retrace
    skip_reconfirmation_in_trends: bool = Field(default=True)  # Default True for trending markets




class AssetConfig(BaseSettings):
    """Asset selection and filtering configuration."""
    mode: Literal["auto", "whitelist", "blacklist"] = Field(default="auto")
    whitelist: List[str] = Field(default_factory=list)  # e.g., ["BTC/USD", "ETH/USD"]
    blacklist: List[str] = Field(default_factory=list)  # e.g., ["DOGE/USD"]
    
    @field_validator('mode')
    @classmethod
    def validate_mode(cls, v):
        if v not in ["auto", "whitelist", "blacklist"]:
            raise ValueError(f"Invalid mode: {v}. Must be auto, whitelist, or blacklist")
        return v


class CoinUniverseConfig(BaseSettings):
    """Coin universe configuration (V2)."""
    enabled: bool = True
    min_spot_volume_24h: Decimal = Field(default=Decimal("5000000"))
    liquidity_tiers: Dict[str, List[str]] = Field(default_factory=lambda: {"A": ["BTC/USD"], "B": [], "C": []})
    tier_max_leverage: Dict[str, float] = Field(default_factory=lambda: {"A": 10.0, "B": 5.0, "C": 2.0}) # Global cap still applies

class LiquidityFilters(BaseSettings):
    """Market eligibility filters."""
    min_spot_volume_usd_24h: Decimal = Field(default=Decimal("5000000"))  # $5M minimum
    min_futures_open_interest: Optional[Decimal] = None
    max_spread_pct: Decimal = Field(default=Decimal("0.0005"))  # 0.05%
    min_price_usd: Decimal = Field(default=Decimal("0.01"))  # Avoid dust coins


class MultiTPConfig(BaseSettings):
    """Multi-TP configuration (YAML multi_tp section). When enabled, overrides execution TP splits and RR multiples."""
    enabled: bool = False
    tp1_r_multiple: float = Field(default=1.0, ge=0.5, le=5.0)
    tp1_close_pct: float = Field(default=0.40, ge=0.1, le=0.6)
    tp2_r_multiple: float = Field(default=2.5, ge=1.0, le=10.0)
    tp2_close_pct: float = Field(default=0.40, ge=0.1, le=0.6)
    runner_pct: float = Field(default=0.20, ge=0.05, le=0.5)
    move_sl_to_be_after_tp1: bool = True
    trailing_stop_enabled: bool = True
    trailing_stop_atr_multiplier: float = Field(default=1.5, ge=1.0, le=3.0)


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
    order_timeout_seconds: int = Field(default=120, ge=10, le=300)
    order_price_invalidation_pct: float = Field(default=0.03, ge=0.01, le=0.10)  # Cancel if price moves X% away
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
    slack_webhook_url: Optional[str] = None
    discord_webhook_url: Optional[str] = None


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


class SystemConfig(BaseSettings):
    """System metadata."""
    name: str = "Trading System"
    version: str = "3.0.0"
    dry_run: bool = False  # If True, no real orders are placed


class Config(BaseSettings):
    """Main configuration class."""
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )
    
    system: SystemConfig = Field(default_factory=SystemConfig)
    exchange: ExchangeConfig
    risk: RiskConfig
    strategy: StrategyConfig
    assets: AssetConfig = Field(default_factory=AssetConfig)  # NEW
    coin_universe: CoinUniverseConfig = Field(default_factory=CoinUniverseConfig) # NEW
    liquidity_filters: LiquidityFilters = Field(default_factory=LiquidityFilters)  # NEW
    execution: ExecutionConfig
    multi_tp: Optional[MultiTPConfig] = None
    data: DataConfig
    reconciliation: ReconciliationConfig
    monitoring: MonitoringConfig
    backtest: BacktestConfig
    paper: PaperConfig
    live: LiveConfig
    environment: Literal["dev", "paper", "prod"] = "prod"

    @classmethod
    def from_yaml(cls, yaml_path: str | Path) -> "Config":
        """Load configuration from YAML file."""
        yaml_path = Path(yaml_path)
        if not yaml_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {yaml_path}")
        
        with open(yaml_path, "r") as f:
            raw_content = f.read()
            
        # Expand environment variables
        import os
        import re
        
        # Regex to find ${VAR} or $VAR
        pattern = re.compile(r'\$\{([^}]+)\}|\$([a-zA-Z_][a-zA-Z0-9_]*)')
        
        def replace_match(match):
            var_name = match.group(1) or match.group(2)
            return os.environ.get(var_name, match.group(0))  # Return original if not found
            
        expanded_content = pattern.sub(replace_match, raw_content)
        config_dict = yaml.safe_load(expanded_content)
            
        import os
        if "ENVIRONMENT" in os.environ:
            config_dict["environment"] = os.environ["ENVIRONMENT"]
            
        # Default local DB if not provided and not in prod
        # DATABASE_URL is required (PostgreSQL only)
        if not config_dict.get("data", {}).get("database_url"):
            import os
            db_url = os.getenv("DATABASE_URL")
            if not db_url:
                raise ValueError(
                    "DATABASE_URL is required. Set it in your environment or .env.local file. "
                    "Example: postgresql://user@localhost/tradingsystem"
                )
            if "data" not in config_dict:
                config_dict["data"] = {}
            config_dict["data"]["database_url"] = db_url

        # Force dry_run if not explicitly set in local/dev
        if config_dict.get("environment") != "prod":
            if "system" not in config_dict:
                config_dict["system"] = {}
            if "dry_run" not in config_dict["system"]:
                config_dict["system"]["dry_run"] = True

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


def validate_required_env_vars() -> None:
    """
    Validate that required environment variables are set.
    
    In production (DigitalOcean), RUN_TIME secrets may not be immediately
    available at startup. This validation logs warnings but doesn't fail
    if we're in a deployment environment where secrets are injected later.
    
    Raises:
        ValueError: If required environment variables are missing and we're
                    not in a deployment environment where they should be injected
    """
    import os
    
    env = os.getenv("ENV", os.getenv("ENVIRONMENT", "prod"))
    dry_run = os.getenv("DRY_RUN", os.getenv("SYSTEM_DRY_RUN", "0"))
    
    # Convert dry_run to boolean
    is_dry_run = dry_run in ("1", "true", "True", "TRUE")
    
    missing_vars = []
    
    # Database is always required (but has defaults in local mode)
    if not is_dry_run and env == "prod":
        # Production mode - check for required vars
        if not os.getenv("DATABASE_URL"):
            missing_vars.append("DATABASE_URL")
        
        # API keys required for live trading
        if not os.getenv("KRAKEN_FUTURES_API_KEY"):
            missing_vars.append("KRAKEN_FUTURES_API_KEY")
        if not os.getenv("KRAKEN_FUTURES_API_SECRET"):
            missing_vars.append("KRAKEN_FUTURES_API_SECRET")
    
    if missing_vars:
        # Check if we're in DigitalOcean App Platform
        # In DO, RUN_TIME secrets are injected but may have timing issues
        # We'll log a warning but allow the app to continue - the actual
        # operations will fail later with clearer errors if secrets are truly missing
        is_do_platform = os.getenv("DIGITALOCEAN_APP_ID") or os.path.exists("/workspace")
        
        if is_do_platform:
            # In DigitalOcean, secrets are injected at runtime
            # Log warning but don't fail - let the actual operations fail with clearer errors
            from src.monitoring.logger import get_logger
            logger = get_logger(__name__)
            logger.warning(
                "Some required environment variables are not yet available at startup",
                missing_vars=missing_vars,
                note="In DigitalOcean App Platform, RUN_TIME secrets may be injected after startup. "
                     "The application will continue, but operations requiring these secrets will fail with clearer errors."
            )
            # Don't raise - allow app to start and fail later with better context
            return
        
        # Not in DO platform - fail fast with clear error
        error_msg = f"""
╔══════════════════════════════════════════════════════════════╗
║  CONFIGURATION ERROR: Missing Required Environment Variables ║
╚══════════════════════════════════════════════════════════════╝

The following required environment variables are not set:
{chr(10).join(f"  ❌ {var}" for var in missing_vars)}

Current environment:
  ENV: {env}
  DRY_RUN: {dry_run}

To fix this:
  1. For local development:
     - Copy .env.local.example to .env.local
     - Set DRY_RUN=1 for safe testing
     - Run: make smoke

  2. For production:
     - Set all required environment variables in DigitalOcean App Platform
     - Ensure DRY_RUN=0 or unset
     - Ensure DATABASE_URL points to production database

See LOCAL_DEV.md for detailed setup instructions.
"""
        raise ValueError(error_msg)


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
    # Validate environment variables first
    validate_required_env_vars()
    
    if config_path is None:
        config_path = Path(__file__).parent / "config.yaml"
    
    config = Config.from_yaml(config_path)
    config.validate_config()
    
    return config
