from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from collections import defaultdict
import pandas as pd
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from src.storage.repository import get_decision_traces_since

def generate_activity_report(hours: int = 24, format_type: str = "text") -> None:
    """
    Generate and print an activity report for the last N hours.
    
    Args:
        hours: Number of hours to look back
        format_type: Output format ('text' or 'table')
    """
    console = Console()
    
    # 1. Fetch Data
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    console.print(f"[bold cyan]Generating activity report for the last {hours} hours (since {cutoff.strftime('%Y-%m-%d %H:%M')})...[/bold cyan]")
    
    events = get_decision_traces_since(cutoff)
    
    # 2. Process Data
    stats = {
        "total_reviews": 0,
        "distinct_coins": set(),
        "coins_data": defaultdict(lambda: {
            "review_count": 0,
            "latest_regime": "unknown",
            "latest_bias": "neutral",
            "latest_quality": 0,
            "signals": []
        }),
        "signals_generated": []
    }
    
    for e in events:
        stats["total_reviews"] += 1
        symbol = e["symbol"]
        stats["distinct_coins"].add(symbol)
        
        # Update coin stats
        c_stat = stats["coins_data"][symbol]
        c_stat["review_count"] += 1
        
        details = e["details"]
        if not details:
            continue
            
        # Update latest state (events are strictly ordered by time asc, so last one is latest)
        c_stat["latest_regime"] = details.get("regime", "unknown")
        c_stat["latest_bias"] = details.get("bias", "neutral")
        c_stat["latest_quality"] = details.get("setup_quality", 0)
        
        # Check for signal
        signal = details.get("signal")
        if signal and signal.lower() not in ("none", "no_signal"):
            sig_entry = {
                "timestamp": e["timestamp"],
                "symbol": symbol,
                "type": signal,
                "quality": details.get("setup_quality", 0),
                "regime": details.get("regime", "unknown")
            }
            c_stat["signals"].append(sig_entry)
            stats["signals_generated"].append(sig_entry)

    # 3. Output Report
    
    if stats["total_reviews"] == 0:
        console.print("[yellow]No activity recorded in this period.[/yellow]")
        return
    
    # Header
    console.print("\n[bold]📈 System Activity Summary[/bold]")
    console.print(f"Total Reviews: [green]{stats['total_reviews']}[/green]")
    console.print(f"Active Coins Scanned: [green]{len(stats['distinct_coins'])}[/green]")
    console.print(f"Signals Generated: [magenta]{len(stats['signals_generated'])}[/magenta]\n")
    
    if format_type == "table":
        # Coin Summary Table
        table = Table(title=f"Coin Activity ({hours}h)")
        table.add_column("Symbol", style="cyan")
        table.add_column("Reviews", style="magenta")
        table.add_column("Latest Regime", style="green")
        table.add_column("Latest Bias", style="yellow")
        table.add_column("Qual", style="blue")
        table.add_column("Signals", style="red")
        
        # Sort by review count desc
        sorted_coins = sorted(
            stats["coins_data"].items(), 
            key=lambda x: x[1]["review_count"], 
            reverse=True
        )
        
        for symbol, data in sorted_coins:
            sig_count = len(data["signals"])
            sig_str = f"{sig_count}" if sig_count > 0 else ""
            
            table.add_row(
                symbol,
                str(data["review_count"]),
                data["latest_regime"],
                data["latest_bias"],
                f"{data['latest_quality']:.1f}",
                sig_str
            )
            
        console.print(table)
        
    else:
        # Text Summary (Grouped by Bias/Regime)
        by_regime = defaultdict(list)
        for symbol, data in stats["coins_data"].items():
            by_regime[data["latest_regime"]].append(symbol)
            
        console.print("[bold]Market Regimes Overview:[/bold]")
        for regime, coins in by_regime.items():
            console.print(f"  • [cyan]{regime}[/cyan]: {len(coins)} coins")
            
    # Signals Detail
    if stats["signals_generated"]:
        console.print("\n[bold]🚨 Signals Identified:[/bold]")
        
        # Sort signals by time desc
        sorted_signals = sorted(
            stats["signals_generated"], 
            key=lambda x: x["timestamp"], 
            reverse=True
        )
        
        for s in sorted_signals:
            ts_str = s["timestamp"].strftime("%H:%M")
            qual_color = "green" if s["quality"] >= 70 else "yellow"
            console.print(
                f"  • {ts_str} [bold]{s['symbol']}[/bold] "
                f"[{qual_color}]{s['type'].upper()} ({s['quality']:.0f})[/{qual_color}] "
                f"in {s['regime']}"
            )
            
    console.print("")
