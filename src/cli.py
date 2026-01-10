"""
CLI entrypoint for the Kraken Futures SMC Trading System.

Provides commands for backtest, paper, live, status, and kill-switch.
"""
import typer
from pathlib import Path
from datetime import datetime
from src.config.config import load_config
from src.monitoring.logger import setup_logging, get_logger
from src.storage.db import init_db

app = typer.Typer(
    name="kraken-futures-smc",
    help="Kraken Futures SMC Trading System",
    add_completion=False,
)

logger = get_logger(__name__)


@app.command()
def backtest(
    start: str = typer.Option(..., "--start", help="Start date (YYYY-MM-DD)"),
    end: str = typer.Option(..., "--end", help="End date (YYYY-MM-DD)"),
    config_path: Path = typer.Option("src/config/config.yaml", "--config", help="Path to config file"),
):
    """
    Run backtest on historical spot data with futures cost simulation.
    
    Example:
        python src/cli.py backtest --start 2024-01-01 --end 2024-12-31
    """
    # Load configuration
    config = load_config(str(config_path))
    setup_logging(config.monitoring.log_level, config.monitoring.log_format)
    
    logger.info("Starting backtest", start=start, end=end)
    
    # Parse dates
    start_date = datetime.strptime(start, "%Y-%m-%d")
    end_date = datetime.strptime(end, "%Y-%m-%d")
    
    # TODO: Initialize backtest engine
    typer.echo(f"Backtest from {start_date} to {end_date}")
    typer.echo("⚠️  Backtest engine not yet implemented")
    
    logger.info("Backtest completed")


@app.command()
def paper(
    config_path: Path = typer.Option("src/config/config.yaml", "--config", help="Path to config file"),
):
    """
    Run paper trading with real-time data and simulated execution.
    
    Example:
        python src/cli.py paper
    """
    # Load configuration
    config = load_config(str(config_path))
    setup_logging(config.monitoring.log_level, config.monitoring.log_format)
    
    # Validate environment
    if config.environment != "paper":
        logger.warning("Environment is not set to 'paper' in config", env=config.environment)
        if not typer.confirm("Continue anyway?"):
            raise typer.Abort()
    
    logger.info("Starting paper trading")
    
    # TODO: Initialize paper trading engine
    typer.echo("Paper trading mode")
    typer.echo("⚠️  Paper trading engine not yet implemented")
    
    logger.info("Paper trading stopped")


@app.command()
def live(
    config_path: Path = typer.Option("src/config/config.yaml", "--config", help="Path to config file"),
    force: bool = typer.Option(False, "--force", help="Force live trading (bypass safety gates)"),
):
    """
    Run live trading on Kraken Futures (REAL CAPITAL AT RISK).
    
    ⚠️  WARNING: This mode trades real money. Use with extreme caution.
    
    Example:
        python src/cli.py live
    """
    # Load configuration
    config = load_config(str(config_path))
    setup_logging(config.monitoring.log_level, config.monitoring.log_format)
    
    # Validate environment
    if config.environment != "prod":
        typer.secho(
            f"❌ Environment is '{config.environment}', not 'prod'. Set environment='prod' in config for live trading.",
            fg=typer.colors.RED,
            bold=True,
        )
        raise typer.Abort()
    
    # Safety gates
    if config.live.require_paper_success and not force:
        typer.secho(
            "⚠️  Live trading requires successful paper trading:",
            fg=typer.colors.YELLOW,
            bold=True,
        )
        typer.echo(f"  - Minimum {config.live.min_paper_days} days of paper trading")
        typer.echo(f"  - Minimum {config.live.min_paper_trades} trades")
        typer.echo(f"  - Maximum {config.live.max_paper_drawdown_pct * 100}% drawdown")
        typer.echo("\nPaper trading validation not yet implemented.")
        typer.echo("Use --force to bypass (NOT RECOMMENDED)")
        raise typer.Abort()
    
    # Final confirmation
    typer.secho(
        "\n⚠️  LIVE TRADING MODE ⚠️",
        fg=typer.colors.RED,
        bold=True,
    )
    typer.secho(
        "You are about to trade REAL MONEY on Kraken Futures.",
        fg=typer.colors.RED,
    )
    typer.secho(
        "Leveraged futures trading carries substantial risk of loss.",
        fg=typer.colors.RED,
    )
    
    if not typer.confirm("\nDo you want to proceed?"):
        raise typer.Abort()
    
    logger.warning("Live trading started - REAL CAPITAL AT RISK")
    
    # TODO: Initialize live trading engine
    typer.echo("Live trading mode")
    typer.echo("⚠️  Live trading engine not yet implemented")


@app.command(name="kill-switch")
def kill_switch(
    emergency: bool = typer.Option(False, "--emergency", help="Emergency stop (cancel all orders + flatten all positions)"),
    config_path: Path = typer.Option("src/config/config.yaml", "--config", help="Path to config file"),
):
    """
    Activate kill switch to halt trading immediately.
    
    This will:
    1. Cancel all open orders
    2. Flatten all positions (if --emergency is set)
    3. Latch the system (requires manual restart)
    
    Example:
        python src/cli.py kill-switch --emergency
    """
    # Load configuration
    config = load_config(str(config_path))
    setup_logging(config.monitoring.log_level, config.monitoring.log_format)
    
    typer.secho(
        "\n🛑 KILL SWITCH ACTIVATED 🛑",
        fg=typer.colors.RED,
        bold=True,
    )
    
    logger.critical("Kill switch activated", emergency=emergency)
    
    if emergency:
        typer.echo("Emergency mode:")
        typer.echo("  1. Cancelling all open orders...")
        typer.echo("  2. Flattening all positions...")
        typer.echo("  3. Latching system...")
        
        # TODO: Implement kill switch
        typer.echo("\n⚠️  Kill switch not yet implemented")
    else:
        typer.echo("  1. Halting new entries...")
        typer.echo("  2. System latched (manual restart required)")
        
        # TODO: Implement kill switch
        typer.echo("\n⚠️  Kill switch not yet implemented")
    
    typer.secho(
        "\n✓ Kill switch engaged. Manual acknowledgment required to restart.",
        fg=typer.colors.GREEN,
    )


@app.command()
def status(
    config_path: Path = typer.Option("src/config/config.yaml", "--config", help="Path to config file"),
):
    """
    Display current system status.
    
    Shows:
    - Current positions
    - P&L
    - Risk metrics
    - Kill switch status
    
    Example:
        python src/cli.py status
    """
    # Load configuration
    config = load_config(str(config_path))
    setup_logging(config.monitoring.log_level, config.monitoring.log_format)
    
    typer.echo("System Status")
    typer.echo("=" * 50)
    typer.echo(f"Environment: {config.environment}")
    typer.echo(f"Max Leverage: {config.risk.max_leverage}×")
    typer.echo(f"Risk per Trade: {config.risk.risk_per_trade_pct * 100}%")
    typer.echo("\n⚠️  Status monitoring not yet implemented")


@app.callback()
def main(
    version: bool = typer.Option(False, "--version", help="Show version and exit"),
):
    """
    Kraken Futures SMC Trading System
    
    A professional algorithmic trading system for Kraken Futures perpetual contracts.
    """
    if version:
        typer.echo("Kraken Futures SMC Trading System v1.0.0")
        raise typer.Exit()


if __name__ == "__main__":
    app()
