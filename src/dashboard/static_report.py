"""
Static HTML Dashboard Generator.

Generates a lightweight HTML report once per trading cycle.
Much lighter on CPU than continuously parsing logs.
"""
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict

# CET timezone
CET = timezone(timedelta(hours=1))

# Report output path
REPORT_DIR = Path(__file__).parent.parent.parent / "data"
REPORT_FILE = REPORT_DIR / "dashboard.html"


@dataclass
class CoinReview:
    """Summary of a coin's review in a cycle."""
    symbol: str
    bias: str = ""  # "bullish", "bearish", "neutral"
    has_order_block: bool = False
    has_fvg: bool = False
    has_bos: bool = False
    has_4h_structure: bool = False
    regime: str = ""
    signal_type: str = ""  # "long", "short", ""
    score: Optional[float] = None
    rejection_reason: str = ""
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    tp_price: Optional[float] = None


@dataclass
class AuctionResult:
    """Results of the auction allocation."""
    signals_collected: int = 0
    winners: List[str] = field(default_factory=list)
    opens_executed: int = 0
    opens_failed: int = 0
    closes: List[str] = field(default_factory=list)
    rejection_counts: Dict[str, int] = field(default_factory=dict)


@dataclass
class PositionSummary:
    """Current position summary."""
    symbol: str
    side: str
    size: float
    entry_price: float
    current_price: float = 0.0
    pnl_pct: float = 0.0
    is_protected: bool = False
    stop_price: Optional[float] = None


@dataclass
class CycleReport:
    """Complete cycle report."""
    cycle_id: str = ""
    timestamp: str = ""
    duration_seconds: float = 0.0
    coins_processed: int = 0
    coins: List[CoinReview] = field(default_factory=list)
    signals_found: List[CoinReview] = field(default_factory=list)
    auction: AuctionResult = field(default_factory=AuctionResult)
    positions: List[PositionSummary] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    kill_switch_active: bool = False


# Global report state (accumulated during cycle)
_current_report: Optional[CycleReport] = None


def start_cycle(cycle_id: str) -> None:
    """Start a new cycle report."""
    global _current_report
    _current_report = CycleReport(
        cycle_id=cycle_id,
        timestamp=datetime.now(CET).strftime("%Y-%m-%d %H:%M:%S CET"),
    )


def record_coin_review(review: CoinReview) -> None:
    """Record a coin review."""
    global _current_report
    if _current_report:
        _current_report.coins.append(review)
        if review.signal_type:
            _current_report.signals_found.append(review)


def record_auction_result(result: AuctionResult) -> None:
    """Record auction results."""
    global _current_report
    if _current_report:
        _current_report.auction = result


def record_positions(positions: List[PositionSummary]) -> None:
    """Record current positions."""
    global _current_report
    if _current_report:
        _current_report.positions = positions


def record_error(error: str) -> None:
    """Record an error."""
    global _current_report
    if _current_report:
        _current_report.errors.append(error)


def set_kill_switch_status(active: bool) -> None:
    """Set kill switch status."""
    global _current_report
    if _current_report:
        _current_report.kill_switch_active = active


def end_cycle(duration_seconds: float, coins_processed: int) -> None:
    """End the cycle and generate HTML report."""
    global _current_report
    if _current_report:
        _current_report.duration_seconds = duration_seconds
        _current_report.coins_processed = coins_processed
        _generate_html(_current_report)
        # Also save JSON for API access
        _save_json(_current_report)


def _save_json(report: CycleReport) -> None:
    """Save report as JSON."""
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        json_file = REPORT_DIR / "dashboard.json"
        with open(json_file, "w") as f:
            json.dump(asdict(report), f, indent=2, default=str)
    except Exception as e:
        print(f"Failed to save JSON report: {e}")


def _generate_html(report: CycleReport) -> None:
    """Generate static HTML report."""
    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        
        # Count signals by type
        long_signals = [s for s in report.signals_found if s.signal_type == "long"]
        short_signals = [s for s in report.signals_found if s.signal_type == "short"]
        
        # Build HTML
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="60">
    <title>Trading Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            padding: 20px;
            line-height: 1.5;
        }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        
        /* Header */
        .header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px;
            background: #161b22;
            border-radius: 12px;
            margin-bottom: 20px;
            border: 1px solid #30363d;
        }}
        .header h1 {{ font-size: 24px; color: #58a6ff; }}
        .header .meta {{ text-align: right; color: #8b949e; font-size: 14px; }}
        .header .time {{ font-size: 18px; color: #c9d1d9; }}
        
        /* Status banner */
        .status-banner {{
            padding: 15px 20px;
            border-radius: 8px;
            margin-bottom: 20px;
            font-weight: 600;
        }}
        .status-ok {{ background: #238636; color: white; }}
        .status-warn {{ background: #9e6a03; color: white; }}
        .status-error {{ background: #da3633; color: white; }}
        
        /* Stats grid */
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 25px;
        }}
        .stat-card {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 10px;
            padding: 20px;
            text-align: center;
        }}
        .stat-value {{ font-size: 32px; font-weight: 700; color: #58a6ff; }}
        .stat-label {{ font-size: 13px; color: #8b949e; margin-top: 5px; text-transform: uppercase; }}
        .stat-value.green {{ color: #3fb950; }}
        .stat-value.red {{ color: #f85149; }}
        .stat-value.yellow {{ color: #d29922; }}
        
        /* Section */
        .section {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            margin-bottom: 20px;
            overflow: hidden;
        }}
        .section-header {{
            padding: 15px 20px;
            background: #21262d;
            border-bottom: 1px solid #30363d;
            font-weight: 600;
            font-size: 16px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .section-content {{ padding: 15px 20px; }}
        
        /* Tables */
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            padding: 12px 15px;
            text-align: left;
            border-bottom: 1px solid #21262d;
        }}
        th {{
            background: #21262d;
            font-weight: 600;
            font-size: 12px;
            text-transform: uppercase;
            color: #8b949e;
        }}
        tr:hover {{ background: #1c2128; }}
        
        /* Badges */
        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
        }}
        .badge-long {{ background: #238636; color: white; }}
        .badge-short {{ background: #da3633; color: white; }}
        .badge-bullish {{ background: rgba(35, 134, 54, 0.2); color: #3fb950; }}
        .badge-bearish {{ background: rgba(218, 54, 51, 0.2); color: #f85149; }}
        .badge-neutral {{ background: rgba(139, 148, 158, 0.2); color: #8b949e; }}
        .badge-protected {{ background: rgba(88, 166, 255, 0.2); color: #58a6ff; }}
        .badge-score {{
            background: #30363d;
            color: #c9d1d9;
            min-width: 50px;
            text-align: center;
        }}
        .badge-score.high {{ background: rgba(35, 134, 54, 0.3); color: #3fb950; }}
        .badge-score.medium {{ background: rgba(210, 153, 34, 0.3); color: #d29922; }}
        .badge-score.low {{ background: rgba(218, 54, 51, 0.3); color: #f85149; }}
        
        /* Checkmarks */
        .check {{ color: #3fb950; }}
        .cross {{ color: #484f58; }}
        
        /* Empty state */
        .empty {{ text-align: center; padding: 40px; color: #8b949e; }}
        
        /* Auction results */
        .auction-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
        }}
        .auction-list {{ list-style: none; }}
        .auction-list li {{
            padding: 8px 12px;
            background: #21262d;
            margin: 5px 0;
            border-radius: 6px;
            display: flex;
            justify-content: space-between;
        }}
        
        /* Footer */
        .footer {{
            text-align: center;
            padding: 20px;
            color: #484f58;
            font-size: 12px;
        }}
        
        /* Responsive */
        @media (max-width: 768px) {{
            .auction-grid {{ grid-template-columns: 1fr; }}
            .stats-grid {{ grid-template-columns: repeat(2, 1fr); }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <!-- Header -->
        <div class="header">
            <h1>📊 Trading Dashboard</h1>
            <div class="meta">
                <div class="time">{report.timestamp}</div>
                <div>Cycle: {report.cycle_id[:20]}... • {report.duration_seconds:.1f}s</div>
            </div>
        </div>
        
        <!-- Status Banner -->
        {_render_status_banner(report)}
        
        <!-- Stats Grid -->
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-value">{report.coins_processed}</div>
                <div class="stat-label">Coins Reviewed</div>
            </div>
            <div class="stat-card">
                <div class="stat-value {'green' if len(report.signals_found) > 0 else ''}">{len(report.signals_found)}</div>
                <div class="stat-label">Signals Found</div>
            </div>
            <div class="stat-card">
                <div class="stat-value green">{len(long_signals)}</div>
                <div class="stat-label">Long Signals</div>
            </div>
            <div class="stat-card">
                <div class="stat-value red">{len(short_signals)}</div>
                <div class="stat-label">Short Signals</div>
            </div>
            <div class="stat-card">
                <div class="stat-value">{len(report.positions)}</div>
                <div class="stat-label">Open Positions</div>
            </div>
            <div class="stat-card">
                <div class="stat-value {'green' if report.auction.opens_executed > 0 else ''}">{report.auction.opens_executed}</div>
                <div class="stat-label">Auction Wins</div>
            </div>
        </div>
        
        <!-- Signals Section -->
        {_render_signals_section(report)}
        
        <!-- Auction Section -->
        {_render_auction_section(report)}
        
        <!-- Positions Section -->
        {_render_positions_section(report)}
        
        <!-- All Coins Section -->
        {_render_coins_section(report)}
        
        <!-- Footer -->
        <div class="footer">
            Auto-refreshes every 60 seconds • Last update: {report.timestamp}
        </div>
    </div>
</body>
</html>"""
        
        with open(REPORT_FILE, "w") as f:
            f.write(html)
            
    except Exception as e:
        print(f"Failed to generate HTML report: {e}")


def _render_status_banner(report: CycleReport) -> str:
    """Render status banner."""
    if report.kill_switch_active:
        return '<div class="status-banner status-error">⚠️ KILL SWITCH ACTIVE - Trading halted</div>'
    elif report.errors:
        return f'<div class="status-banner status-warn">⚠️ {len(report.errors)} errors in last cycle</div>'
    else:
        return '<div class="status-banner status-ok">✓ System operating normally</div>'


def _render_signals_section(report: CycleReport) -> str:
    """Render signals section."""
    if not report.signals_found:
        return """
        <div class="section">
            <div class="section-header">🎯 Signals Found</div>
            <div class="section-content">
                <div class="empty">No signals generated this cycle</div>
            </div>
        </div>
        """
    
    rows = ""
    for s in sorted(report.signals_found, key=lambda x: x.score or 0, reverse=True):
        badge_class = "badge-long" if s.signal_type == "long" else "badge-short"
        score_class = "high" if (s.score or 0) >= 75 else "medium" if (s.score or 0) >= 60 else "low"
        rows += f"""
            <tr>
                <td><strong>{s.symbol.replace('/', '')}</strong></td>
                <td><span class="badge {badge_class}">{s.signal_type.upper()}</span></td>
                <td><span class="badge badge-score {score_class}">{s.score:.1f if s.score else '-'}</span></td>
                <td>{s.regime or '-'}</td>
                <td>${s.entry_price:.4f if s.entry_price else '-'}</td>
                <td>${s.stop_price:.4f if s.stop_price else '-'}</td>
                <td>${s.tp_price:.4f if s.tp_price else '-'}</td>
            </tr>
        """
    
    return f"""
    <div class="section">
        <div class="section-header">
            🎯 Signals Found
            <span>{len(report.signals_found)} signals</span>
        </div>
        <div class="section-content">
            <table>
                <thead>
                    <tr>
                        <th>Symbol</th>
                        <th>Direction</th>
                        <th>Score</th>
                        <th>Regime</th>
                        <th>Entry</th>
                        <th>Stop</th>
                        <th>Target</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>
    </div>
    """


def _render_auction_section(report: CycleReport) -> str:
    """Render auction results section."""
    auction = report.auction
    
    winners_html = ""
    if auction.winners:
        for w in auction.winners:
            winners_html += f'<li>{w} <span class="badge badge-long">OPENED</span></li>'
    else:
        winners_html = '<li style="color: #8b949e;">No positions opened</li>'
    
    closes_html = ""
    if auction.closes:
        for c in auction.closes:
            closes_html += f'<li>{c} <span class="badge badge-short">CLOSED</span></li>'
    else:
        closes_html = '<li style="color: #8b949e;">No positions closed</li>'
    
    rejections = ""
    if auction.rejection_counts:
        for reason, count in auction.rejection_counts.items():
            rejections += f'<li>{reason}: {count}</li>'
    
    return f"""
    <div class="section">
        <div class="section-header">
            🏆 Auction Results
            <span>{auction.signals_collected} candidates → {auction.opens_executed} executed</span>
        </div>
        <div class="section-content">
            <div class="auction-grid">
                <div>
                    <h4 style="margin-bottom: 10px; color: #3fb950;">Winners (Opened)</h4>
                    <ul class="auction-list">{winners_html}</ul>
                </div>
                <div>
                    <h4 style="margin-bottom: 10px; color: #f85149;">Closed Positions</h4>
                    <ul class="auction-list">{closes_html}</ul>
                </div>
            </div>
            {f'<div style="margin-top: 15px; padding-top: 15px; border-top: 1px solid #30363d;"><h4 style="margin-bottom: 10px; color: #8b949e;">Rejections</h4><ul class="auction-list">{rejections}</ul></div>' if rejections else ''}
        </div>
    </div>
    """


def _render_positions_section(report: CycleReport) -> str:
    """Render positions section."""
    if not report.positions:
        return """
        <div class="section">
            <div class="section-header">💼 Open Positions</div>
            <div class="section-content">
                <div class="empty">No open positions</div>
            </div>
        </div>
        """
    
    rows = ""
    for p in report.positions:
        side_badge = "badge-long" if p.side.lower() == "long" else "badge-short"
        pnl_color = "green" if p.pnl_pct >= 0 else "red"
        protected = '<span class="badge badge-protected">Protected</span>' if p.is_protected else '<span style="color: #d29922;">Unprotected</span>'
        
        rows += f"""
            <tr>
                <td><strong>{p.symbol.replace('PF_', '').replace('USD', '')}</strong></td>
                <td><span class="badge {side_badge}">{p.side.upper()}</span></td>
                <td>{p.size:.4f}</td>
                <td>${p.entry_price:.4f}</td>
                <td>${p.current_price:.4f}</td>
                <td style="color: {'#3fb950' if p.pnl_pct >= 0 else '#f85149'};">{p.pnl_pct:+.2f}%</td>
                <td>{protected}</td>
                <td>${p.stop_price:.4f if p.stop_price else '-'}</td>
            </tr>
        """
    
    return f"""
    <div class="section">
        <div class="section-header">
            💼 Open Positions
            <span>{len(report.positions)} positions</span>
        </div>
        <div class="section-content">
            <table>
                <thead>
                    <tr>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Size</th>
                        <th>Entry</th>
                        <th>Current</th>
                        <th>PnL</th>
                        <th>Status</th>
                        <th>Stop</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>
    </div>
    """


def _render_coins_section(report: CycleReport) -> str:
    """Render all coins reviewed section."""
    if not report.coins:
        return ""
    
    # Sort: signals first, then by score
    sorted_coins = sorted(
        report.coins,
        key=lambda x: (x.signal_type != "", x.score or 0),
        reverse=True
    )
    
    rows = ""
    for c in sorted_coins[:50]:  # Limit to top 50
        bias_badge = f'badge-{c.bias}' if c.bias else 'badge-neutral'
        ob = '<span class="check">✓</span>' if c.has_order_block else '<span class="cross">-</span>'
        fvg = '<span class="check">✓</span>' if c.has_fvg else '<span class="cross">-</span>'
        bos = '<span class="check">✓</span>' if c.has_bos else '<span class="cross">-</span>'
        h4 = '<span class="check">✓</span>' if c.has_4h_structure else '<span class="cross">-</span>'
        
        if c.signal_type:
            signal_badge = f'<span class="badge badge-{c.signal_type}">{c.signal_type.upper()}</span>'
        else:
            signal_badge = '<span style="color: #484f58;">-</span>'
        
        score_html = ""
        if c.score:
            score_class = "high" if c.score >= 75 else "medium" if c.score >= 60 else "low"
            score_html = f'<span class="badge badge-score {score_class}">{c.score:.0f}</span>'
        else:
            score_html = '<span style="color: #484f58;">-</span>'
        
        rejection = f'<span style="color: #8b949e; font-size: 11px;">{c.rejection_reason[:30]}...</span>' if c.rejection_reason and len(c.rejection_reason) > 30 else (c.rejection_reason or '-')
        
        rows += f"""
            <tr>
                <td><strong>{c.symbol.replace('/', '')}</strong></td>
                <td><span class="badge {bias_badge}">{c.bias or 'N/A'}</span></td>
                <td>{ob}</td>
                <td>{fvg}</td>
                <td>{bos}</td>
                <td>{h4}</td>
                <td>{signal_badge}</td>
                <td>{score_html}</td>
                <td>{rejection}</td>
            </tr>
        """
    
    return f"""
    <div class="section">
        <div class="section-header">
            📋 Coins Reviewed
            <span>Top 50 of {len(report.coins)}</span>
        </div>
        <div class="section-content">
            <table>
                <thead>
                    <tr>
                        <th>Symbol</th>
                        <th>Bias</th>
                        <th>OB</th>
                        <th>FVG</th>
                        <th>BOS</th>
                        <th>4H</th>
                        <th>Signal</th>
                        <th>Score</th>
                        <th>Rejection Reason</th>
                    </tr>
                </thead>
                <tbody>
                    {rows}
                </tbody>
            </table>
        </div>
    </div>
    """


# Simple HTTP server for serving the dashboard
def serve_dashboard(host: str = "0.0.0.0", port: int = 8080) -> None:
    """Serve the static dashboard via simple HTTP server."""
    import http.server
    import socketserver
    import os
    
    os.chdir(REPORT_DIR)
    
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/" or self.path == "/dashboard":
                self.path = "/dashboard.html"
            return super().do_GET()
        
        def log_message(self, format, *args):
            pass  # Suppress logging
    
    with socketserver.TCPServer((host, port), Handler) as httpd:
        print(f"Dashboard serving at http://{host}:{port}")
        httpd.serve_forever()


if __name__ == "__main__":
    # Test: generate sample report
    start_cycle("test-cycle-123")
    record_coin_review(CoinReview(
        symbol="BTC/USD",
        bias="bullish",
        has_order_block=True,
        has_fvg=True,
        has_4h_structure=True,
        signal_type="long",
        score=85.0,
        regime="trending",
        entry_price=45000.0,
        stop_price=44000.0,
        tp_price=48000.0,
    ))
    record_coin_review(CoinReview(
        symbol="ETH/USD",
        bias="bearish",
        has_order_block=True,
        rejection_reason="No 4H structure",
    ))
    record_auction_result(AuctionResult(
        signals_collected=5,
        winners=["BTC/USD"],
        opens_executed=1,
    ))
    end_cycle(45.3, 20)
    print(f"Test report generated at {REPORT_FILE}")
