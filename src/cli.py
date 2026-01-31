"""
CLI entrypoint for the Kraken Futures SMC Trading System.

Provides commands for backtest, paper, live, status, and kill-switch.
"""
import os
import typer
from typing import Optional
from pathlib import Path
from datetime import datetime
from decimal import Decimal
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
    symbol: str = typer.Option("BTC/USD", "--symbol", help="Symbol to backtest"),
    config_path: Path = typer.Option("src/config/config.yaml", "--config", help="Path to config file"),
):
    """
    Run backtest on historical spot data with futures cost simulation.
    
    Example:
        python src/cli.py backtest --start 2024-01-01 --end 2024-12-31 --symbol ETH/USD
    """
    # Load configuration
    config = load_config(str(config_path))
    setup_logging(config.monitoring.log_level, config.monitoring.log_format)
    
    logger.info("Starting backtest", start=start, end=end, symbol=symbol)
    
    # Parse dates
    from datetime import timezone
    start_date = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_date = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    
    # Initialize components
    logger.info("Initializing backtest components...")
    
    # Imports here to avoid circular dependencies at top level if any
    import asyncio
    from src.data.kraken_client import KrakenClient
    from src.backtest.backtest_engine import BacktestEngine
    
    async def run_backtest():
        # Initialize client (testnet=False for backtest data usually, or True if strict)
        # Using real API for data execution
        client = KrakenClient(
            api_key=config.exchange.api_key if hasattr(config.exchange, "api_key") else "",
            api_secret=config.exchange.api_secret if hasattr(config.exchange, "api_secret") else "",
            use_testnet=False # Data comes from mainnet usually
        )
        
        try:
            # Create engine with symbol
            engine = BacktestEngine(config, symbol=symbol)
            engine.set_client(client)
            
            # Run simulation
            metrics = await engine.run(start_date, end_date)
            
            # Calculate final metrics
            end_equity = metrics.equity_curve[-1] if metrics.equity_curve else Decimal(str(config.backtest.starting_equity))
            total_return_pct = (metrics.total_pnl / Decimal(str(config.backtest.starting_equity))) * 100
            
            # Output results
            typer.echo("\n" + "="*60)
            typer.echo(f"BACKTEST RESULTS: {symbol}")
            typer.echo("="*60)
            typer.echo(f"Period:        {start_date.date()} to {end_date.date()}")
            typer.echo(f"Start Equity:  ${config.backtest.starting_equity:,.2f}")
            typer.echo(f"End Equity:    ${end_equity:,.2f}")
            typer.echo(f"PnL:           ${metrics.total_pnl:,.2f} ({total_return_pct:.2f}%)")
            typer.echo(f"Max Drawdown:  {metrics.max_drawdown:.2%}")
            typer.echo(f"Trades:        {metrics.total_trades} ({metrics.winning_trades}W-{metrics.losing_trades}L)")
            typer.echo(f"Win Rate:      {metrics.win_rate:.1f}%")
            typer.echo("="*60 + "\n")
            
        finally:
            await client.close()

    # Run async loop
    asyncio.run(run_backtest())
    
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
    
    import asyncio
    from src.paper.paper_trading import PaperTrading
    
    async def run_paper():
        engine = PaperTrading(config)
        await engine.run()
        
    try:
        asyncio.run(run_paper())
    except KeyboardInterrupt:
        logger.info("Paper trading stopped by user")
    except Exception as e:
        logger.error("Paper trading failed", error=str(e))
        raise typer.Exit(1)


@app.command()
def live(
    config_path: Path = typer.Option("src/config/config.yaml", "--config", help="Path to config file"),
    force: bool = typer.Option(False, "--force", help="Force live trading (bypass safety gates)"),
    with_health: bool = typer.Option(False, "--with-health", help="Start minimal HTTP health server on PORT/8080 (for App Platform worker readiness)"),
    log_file: Optional[Path] = typer.Option(None, "--log-file", help="Path to log file"),
):
    """
    Run live trading on Kraken Futures (REAL CAPITAL AT RISK).
    
    ⚠️  WARNING: This mode trades real money. Use with extreme caution.
    
    Example:
        python src/cli.py live
    """
    # Load configuration with error handling
    try:
        config = load_config(str(config_path))
    except Exception as e:
        import sys
        import traceback
        print("=" * 80, file=sys.stderr)
        print("CRITICAL ERROR - Failed to load configuration", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        print(f"Type: {type(e).__name__}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        raise typer.Exit(1)
    
    # Setup logging (may fail if config is invalid)
    try:
        setup_logging(config.monitoring.log_level, config.monitoring.log_format, log_file=str(log_file) if log_file else None)
    except Exception as e:
        import sys
        import traceback
        print("=" * 80, file=sys.stderr)
        print("CRITICAL ERROR - Failed to setup logging", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        print(f"Error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        raise typer.Exit(1)
    
    # Validate environment
    if config.environment != "prod" and not force and not config.system.dry_run:
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
    
    if not force and not typer.confirm("\nDo you want to proceed?"):
        raise typer.Abort()
    
    logger.warning("Live trading started - REAL CAPITAL AT RISK")

    # Optional: minimal health server for App Platform worker (readiness on :8080)
    if with_health:
        import threading
        import uvicorn
        import time
        from src.health import worker_health_app
        from src.utils.secret_manager import is_cloud_platform
        port = int(os.environ.get("PORT", "8080"))
        health_host = os.environ.get("WORKER_HEALTH_HOST") or os.environ.get("HEALTH_HOST")
        if not health_host:
            # Default safe behavior:
            # - In App Platform / managed environments, bind publicly for readiness.
            # - On a droplet/VM, bind localhost to avoid exposing debug/metrics endpoints to the internet.
            try:
                health_host = "0.0.0.0" if is_cloud_platform() else "127.0.0.1"
            except Exception:
                health_host = "127.0.0.1"

        # Reduce noisy warnings like "Invalid HTTP request received." (common from port scans).
        # Health server is auxiliary; keep errors, drop warnings by default.
        health_log_level = os.environ.get("WORKER_HEALTH_LOG_LEVEL") or os.environ.get("HEALTH_LOG_LEVEL") or "error"
        
        def _run_health():
            try:
                uvicorn.run(
                    worker_health_app,
                    host=health_host,
                    port=port,
                    log_level=health_log_level,
                    access_log=False,
                )
            except Exception as e:
                logger.error("Health server error: %s", e, exc_info=True)
        
        t = threading.Thread(target=_run_health, daemon=False)  # Non-daemon so it keeps running
        t.start()
        
        # Give the health server a moment to start before proceeding
        time.sleep(1)
        logger.info("Worker health server started", host=health_host, port=port, log_level=health_log_level)

    # Initialize live trading engine
    import asyncio
    import traceback
    from src.live.live_trading import LiveTrading

    async def run_live():
        try:
            logger.info("Initializing LiveTrading engine...")
            engine = LiveTrading(config)
            logger.info("LiveTrading engine initialized successfully")
            logger.info("Starting main trading loop...")
            await engine.run()
        except Exception as e:
            logger.critical(
                "Live trading engine failed",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True
            )
            # Print full traceback for debugging
            logger.critical("Full traceback:\n%s", traceback.format_exc())
            raise

    try:
        asyncio.run(run_live())
    except KeyboardInterrupt:
        logger.info("Live trading stopped by user")
    except Exception as e:
        logger.critical(
            "Live trading failed with unhandled error",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True
        )
        # Print full traceback to stderr for visibility in logs
        import sys
        print("=" * 80, file=sys.stderr)
        print("CRITICAL ERROR - Live Trading Failed", file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        print("=" * 80, file=sys.stderr)
        raise typer.Exit(1)


@app.command()
def test(
    config_path: Path = typer.Option("src/config/config.yaml", "--config", help="Path to config file"),
):
    """
    Run system tests to verify API connection, data acquisition, and signal processing.
    
    Example:
        python run.py test
    """
    import asyncio
    from src.test_system import run_all_tests
    
    # Load config for logging setup
    config = load_config(str(config_path))
    setup_logging(config.monitoring.log_level, config.monitoring.log_format)
    
    success = asyncio.run(run_all_tests())
    if not success:
        raise typer.Exit(1)


@app.command(name="kill-switch")
def kill_switch_cmd(
    action: str = typer.Argument("status", help="Action: activate, deactivate, or status"),
    reason: str = typer.Option("Manual activation", help="Reason for activation")
):
    """
    Emergency kill switch control.
    
    Actions:
    - activate: Stop all trading and close positions
    - deactivate: Resume normal trading
    - status: Check kill switch state
    
    Examples:
        python src/cli.py kill-switch activate --reason "Market volatility"
        python src/cli.py kill-switch deactivate
        python src/cli.py kill-switch status
    """
    from rich.console import Console
    from src.utils.kill_switch import get_kill_switch, KillSwitchReason

    console = Console()
    ks = get_kill_switch()

    if action == "activate":
        ks.activate_sync(reason=KillSwitchReason.MANUAL)
        console.print("[bold red]🚨 KILL SWITCH ACTIVATED[/bold red]")
        console.print(f"Reason: {reason}")
        console.print("\nAll trading halted. Orders will be cancelled and positions closed on next tick.")
        console.print("Use 'acknowledge' to allow restart after resolving the issue.")

    elif action == "acknowledge":
        if ks.acknowledge():
            console.print("[bold green]✅ KILL SWITCH ACKNOWLEDGED[/bold green]")
            console.print("Trading can resume.")
        else:
            console.print("[yellow]Kill switch is not latched - nothing to acknowledge[/yellow]")

    elif action == "deactivate":
        console.print("[yellow]Note: Use 'acknowledge' instead of 'deactivate' for latched kill switch[/yellow]")
        if ks.acknowledge():
            console.print("[bold green]✅ KILL SWITCH DEACTIVATED[/bold green]")
            console.print("Trading can resume.")
        else:
            console.print("[yellow]Kill switch is not active[/yellow]")
        
    elif action == "status":
        status = ks.get_status()
        if status["active"]:
            console.print("[bold red]🚨 KILL SWITCH: ACTIVE[/bold red]")
            console.print(f"Latched: {status['latched']}")
            console.print(f"Activated at: {status['activated_at']}")
            console.print(f"Reason: {status['reason']}")
            console.print(f"Duration: {status['duration_seconds']:.0f}s")
            if status['latched']:
                console.print("\n[yellow]Run 'kill-switch acknowledge' to allow restart[/yellow]")
        else:
            console.print("[bold green]✅ KILL SWITCH: INACTIVE[/bold green]")
            console.print("Trading is operational.")
    else:
        console.print(f"[bold red]Unknown action: {action}[/bold red]")
        console.print("Valid actions: activate, deactivate, status")
        raise typer.Exit(1)


@app.command()
def dashboard(
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind to"),
    port: int = typer.Option(8000, "--port", help="Port to bind to"),
):
    """
    Launch the Web Dashboard.
    
    Example:
        python src/cli.py dashboard
    """
    import subprocess
    import sys
    import webbrowser
    
    app_path = Path("src/dashboard/streamlit_app.py").resolve()
    
    url = f"http://{host}:{port}"
    typer.secho(f"🚀 Dashboard running at: {url}", fg=typer.colors.GREEN, bold=True)
    
    # Auto-open browser
    webbrowser.open(url)
    
    # Run Streamlit
    subprocess.run([
        sys.executable, "-m", "streamlit", "run", str(app_path),
        "--server.port", str(port),
        "--server.address", host,
        "--theme.base", "dark"
    ])


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
    
    # 1. Active Position
    from src.storage.repository import get_active_position, get_all_trades
    from src.domain.models import Side
    
    pos = get_active_position()
    if pos:
        pnl_color = typer.colors.GREEN if pos.unrealized_pnl >= 0 else typer.colors.RED
        typer.secho(f"\n🟢 Active Position: {pos.symbol} ({pos.side.value.upper()})", bold=True)
        typer.echo(f"  Entry:      ${pos.entry_price:,.2f}")
        typer.echo(f"  Current:    ${pos.current_mark_price:,.2f}")
        typer.echo(f"  Size:       ${pos.size_notional:,.2f} ({pos.leverage}x)")
        typer.echo(f"  Liq Price:  ${pos.liquidation_price:,.2f}")
        typer.secho(f"  Unrealized: ${pos.unrealized_pnl:,.2f}", fg=pnl_color)
    else:
        typer.echo("\n⚪️ No Active Position (Scanning...)")
        
    # 2. Recent Trades
    trades = get_all_trades()
    if trades:
        typer.echo(f"\nRecent Trades ({len(trades)} total)")
        typer.echo("-" * 50)
        for t in trades[:5]:
            pnl_color = typer.colors.GREEN if t.net_pnl >= 0 else typer.colors.RED
            icon = "WIN" if t.net_pnl > 0 else "LOSS"
            typer.secho(f"  {t.exited_at.strftime('%Y-%m-%d %H:%M')} | {t.side.value.upper()} | ${t.net_pnl:,.2f} ({icon})", fg=pnl_color)
    else:
        typer.echo("\nNo trades recorded yet.")
        
    typer.echo("\n" + "=" * 50)


@app.command()
def report(
    hours: int = typer.Option(24, "--hours", help="Hours to look back"),
    format: str = typer.Option("text", "--format", help="Output format (text/table)"),
):
    """
    Generate activity report (coins scanned, signals, regimes).
    
    Example:
        python src/cli.py report --hours 24
    """
    from src.reporting.activity import generate_activity_report
    generate_activity_report(hours=hours, format_type=format)



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
