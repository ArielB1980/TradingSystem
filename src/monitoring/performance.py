"""
Performance metrics calculator for trading system.

Calculates key performance indicators:
- Win rate
- Sharpe ratio
- Max drawdown
- Trade statistics
"""
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Dict, List
from typing import Dict, List
from src.monitoring.logger import get_logger

logger = get_logger(__name__)

logger = get_logger(__name__)


def calculate_performance_metrics(days: int = 30) -> Dict:
    """
    Calculate comprehensive performance metrics.
    
    Args:
        days: Number of days to analyze
        
    Returns:
        Dict with performance metrics
    """
    from src.storage.repository import get_trades_since
    
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    trades = get_trades_since(cutoff)
    
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "profit_factor": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown": 0.0,
            "total_pnl": 0.0,
        }
    
    # Basic stats
    total_trades = len(trades)
    winning_trades = [t for t in trades if t.net_pnl > 0]
    losing_trades = [t for t in trades if t.net_pnl < 0]
    
    win_count = len(winning_trades)
    loss_count = len(losing_trades)
    
    win_rate = (win_count / total_trades * 100) if total_trades > 0 else 0.0
    
    # Average win/loss
    avg_win = (sum(t.net_pnl for t in winning_trades) / win_count) if win_count > 0 else Decimal("0")
    avg_loss = (sum(t.net_pnl for t in losing_trades) / loss_count) if loss_count > 0 else Decimal("0")
    
    # Profit factor
    total_wins = sum(t.net_pnl for t in winning_trades)
    total_losses = abs(sum(t.net_pnl for t in losing_trades))
    profit_factor = float(total_wins / total_losses) if total_losses > 0 else 0.0
    
    # Total PnL
    total_pnl = sum(t.net_pnl for t in trades)
    
    # Sharpe ratio (simplified)
    returns = [float(t.net_pnl) for t in trades]
    sharpe_ratio = calculate_sharpe_ratio(returns)
    
    # Max drawdown
    max_drawdown = calculate_max_drawdown(trades)
    
    return {
        "total_trades": total_trades,
        "win_rate": win_rate,
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "profit_factor": profit_factor,
        "sharpe_ratio": sharpe_ratio,
        "max_drawdown": max_drawdown,
        "total_pnl": float(total_pnl),
        "winning_trades": win_count,
        "losing_trades": loss_count,
    }


def calculate_sharpe_ratio(returns: List[float], risk_free_rate: float = 0.0) -> float:
    """
    Calculate Sharpe ratio.
    
    Args:
        returns: List of trade returns
        risk_free_rate: Annual risk-free rate (default 0)
        
    Returns:
        Sharpe ratio
    """
    if not returns or len(returns) < 2:
        return 0.0
    
    import statistics
    
    mean_return = statistics.mean(returns)
    std_return = statistics.stdev(returns)
    
    if std_return == 0:
        return 0.0
    
    # Annualized Sharpe (assuming daily returns)
    sharpe = (mean_return - risk_free_rate) / std_return
    return sharpe * (252 ** 0.5)  # Annualize


def calculate_max_drawdown(trades: List) -> float:
    """
    Calculate maximum drawdown from trade history.
    
    Args:
        trades: List of Trade objects
        
    Returns:
        Max drawdown as percentage
    """
    if not trades:
        return 0.0
    
    # Sort by exit time
    sorted_trades = sorted(trades, key=lambda t: t.exited_at)
    
    # Calculate cumulative PnL
    cumulative_pnl = []
    running_total = Decimal("0")
    
    for trade in sorted_trades:
        running_total += trade.net_pnl
        cumulative_pnl.append(float(running_total))
    
    # Find max drawdown
    peak = cumulative_pnl[0]
    max_dd = 0.0
    
    for value in cumulative_pnl:
        if value > peak:
            peak = value
        
        drawdown = (peak - value) / abs(peak) if peak != 0 else 0.0
        max_dd = max(max_dd, drawdown)
    
    return max_dd * 100  # Return as percentage


def get_trade_statistics() -> Dict:
    """
    Get comprehensive trade statistics.
    
    Returns:
        Dict with trade stats
    """
    from src.storage.repository import get_all_trades
    
    all_trades = get_all_trades()
    
    if not all_trades:
        return {
            "total_trades": 0,
            "avg_holding_hours": 0.0,
            "longest_trade_hours": 0.0,
            "shortest_trade_hours": 0.0,
        }
    
    holding_periods = [float(t.holding_period_hours) for t in all_trades]
    
    return {
        "total_trades": len(all_trades),
        "avg_holding_hours": sum(holding_periods) / len(holding_periods),
        "longest_trade_hours": max(holding_periods),
        "shortest_trade_hours": min(holding_periods),
    }
