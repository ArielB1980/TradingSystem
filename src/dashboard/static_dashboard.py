#!/usr/bin/env python3
"""
Static Dashboard - Lightweight HTML generator.

Generates a static HTML dashboard from database decision traces.
Much lighter on CPU than the Streamlit log parser.

Run: python -m src.dashboard.static_dashboard
"""
import os
import sys
import json
import time
import signal
import threading
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from decimal import Decimal
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn

# Ensure project root is in path
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Load env vars
from dotenv import load_dotenv
load_dotenv(project_root / ".env")
load_dotenv(project_root / ".env.local")

# CET timezone
CET = timezone(timedelta(hours=1))

# Report paths
DATA_DIR = project_root / "data"
REPORT_FILE = DATA_DIR / "dashboard.html"
JSON_FILE = DATA_DIR / "dashboard.json"


def get_db_connection():
    """Get database connection."""
    import psycopg2
    from psycopg2.extras import RealDictCursor
    
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL not set")
    
    return psycopg2.connect(database_url, cursor_factory=RealDictCursor)


def parse_log_file(log_path: Path, max_lines: int = 2000) -> List[Dict]:
    """Parse the log file to extract signal and auction information."""
    entries = []
    
    if not log_path.exists():
        return entries
    
    try:
        with open(log_path, 'r', errors='ignore') as f:
            # Read last N lines
            lines = f.readlines()[-max_lines:]
        
        for line in lines:
            line = line.strip()
            if not line:
                continue
            
            try:
                entry = {"raw": line}
                
                # Extract timestamp
                if line.startswith("20"):
                    parts = line.split(" ", 2)
                    if len(parts) >= 2:
                        entry["timestamp"] = parts[0]
                
                # Extract log level
                if "[info" in line.lower():
                    entry["level"] = "info"
                elif "[warning" in line.lower():
                    entry["level"] = "warning"
                elif "[error" in line.lower():
                    entry["level"] = "error"
                elif "[critical" in line.lower():
                    entry["level"] = "critical"
                
                # Detect entry type by content
                if "Signal generated" in line or "Signal rejected" in line:
                    entry["type"] = "signal"
                elif "AUCTION" in line or "Auction" in line:
                    entry["type"] = "auction"
                elif "SMC Analysis" in line:
                    entry["type"] = "analysis"
                elif "position" in line.lower():
                    entry["type"] = "position"
                
                # Extract symbol
                import re
                symbol_match = re.search(r'symbol=([A-Z/]+)', line)
                if symbol_match:
                    entry["symbol"] = symbol_match.group(1)
                
                # Extract score
                score_match = re.search(r'score[=:]?\s*(\d+\.?\d*)', line, re.IGNORECASE)
                if score_match:
                    entry["score"] = float(score_match.group(1))
                
                # Extract signal type
                if "signal_type=short" in line.lower() or "type=short" in line.lower():
                    entry["signal_type"] = "short"
                elif "signal_type=long" in line.lower() or "type=long" in line.lower():
                    entry["signal_type"] = "long"
                
                entries.append(entry)
                
            except Exception:
                continue
        
    except Exception as e:
        print(f"Failed to parse log: {e}")
    
    return entries


def fetch_positions() -> List[Dict]:
    """Fetch current positions from database."""
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        
        cur.execute("""
            SELECT symbol, side, state, initial_size, initial_entry_price,
                   current_stop_price, stop_order_id, created_at
            FROM positions
            WHERE state NOT IN ('CLOSED', 'CANCELLED', 'REJECTED')
            ORDER BY created_at DESC
        """)
        
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        return [dict(row) for row in rows]
    except Exception as e:
        print(f"Failed to fetch positions: {e}")
        return []


def parse_log_entries(entries: List[Dict]) -> Dict:
    """Parse log entries into structured data."""
    coins_reviewed = {}
    signals_found = []
    auction_results = {
        "signals_collected": 0,
        "winners": [],
        "opens_executed": 0,
        "opens_failed": 0,
        "closes": [],
        "rejections": {},
    }
    errors = []
    
    import re
    
    for entry in entries:
        raw = entry.get("raw", "")
        symbol = entry.get("symbol", "")
        
        # Track coins from SMC Analysis lines
        if "SMC Analysis" in raw and symbol:
            if symbol not in coins_reviewed:
                coins_reviewed[symbol] = {
                    "symbol": symbol,
                    "bias": "",
                    "has_ob": False,
                    "has_fvg": False,
                    "has_bos": False,
                    "has_4h": False,
                    "signal_type": "",
                    "score": None,
                    "regime": "",
                    "rejection": "",
                }
            
            # Parse analysis details
            if "Bias Bullish" in raw:
                coins_reviewed[symbol]["bias"] = "bullish"
            elif "Bias Bearish" in raw:
                coins_reviewed[symbol]["bias"] = "bearish"
            
            if "Order block detected" in raw or "✓ Order block" in raw:
                coins_reviewed[symbol]["has_ob"] = True
            if "Fair value gap" in raw or "✓ Fair value gap" in raw:
                coins_reviewed[symbol]["has_fvg"] = True
            if "Break of structure" in raw or "✓ Break of structure" in raw:
                coins_reviewed[symbol]["has_bos"] = True
            if "4H Decision Structure Found" in raw or "✅ 4H" in raw:
                coins_reviewed[symbol]["has_4h"] = True
            
            # Extract regime
            regime_match = re.search(r'Market Regime:\s*(\w+)', raw)
            if regime_match:
                coins_reviewed[symbol]["regime"] = regime_match.group(1)
            
            # Check for rejection reason
            if "❌ Rejected" in raw:
                rejection_match = re.search(r'❌ Rejected[:\s]*(.+?)(?:\[|$)', raw)
                if rejection_match:
                    coins_reviewed[symbol]["rejection"] = rejection_match.group(1).strip()
        
        # Track signals
        if entry.get("type") == "signal" or "Signal generated" in raw:
            signal_type = entry.get("signal_type", "")
            score = entry.get("score")
            
            if symbol and signal_type:
                if symbol in coins_reviewed:
                    coins_reviewed[symbol]["signal_type"] = signal_type
                    coins_reviewed[symbol]["score"] = score
                
                # Extract entry/stop/tp from the log line
                entry_match = re.search(r'entry[=:]?\s*(\d+\.?\d*)', raw, re.IGNORECASE)
                stop_match = re.search(r'stop[=:]?\s*(\d+\.?\d*)', raw, re.IGNORECASE)
                tp_match = re.search(r'tp\d?[=:]?\s*(\d+\.?\d*)', raw, re.IGNORECASE)
                
                signals_found.append({
                    "symbol": symbol,
                    "type": signal_type,
                    "score": score,
                    "entry": entry_match.group(1) if entry_match else None,
                    "stop": stop_match.group(1) if stop_match else None,
                    "tp": tp_match.group(1) if tp_match else None,
                    "regime": coins_reviewed.get(symbol, {}).get("regime", ""),
                })
        
        # Track auction results
        if "Auction allocation executed" in raw or "Auction plan generated" in raw:
            opens_match = re.search(r'opens_executed[=:]?\s*(\d+)', raw)
            fails_match = re.search(r'opens_failed[=:]?\s*(\d+)', raw)
            signals_match = re.search(r'signals_collected[=:]?\s*(\d+)', raw)
            
            if opens_match:
                auction_results["opens_executed"] = int(opens_match.group(1))
            if fails_match:
                auction_results["opens_failed"] = int(fails_match.group(1))
            if signals_match:
                auction_results["signals_collected"] = int(signals_match.group(1))
            
            # Extract winners
            winners_match = re.search(r'opens_symbols=\[([^\]]*)\]', raw)
            if winners_match:
                winners_str = winners_match.group(1)
                winners = [w.strip().strip("'\"") for w in winners_str.split(",") if w.strip()]
                auction_results["winners"] = winners
        
        # Track errors
        if entry.get("level") == "error":
            errors.append(raw[:200])
    
    return {
        "coins": list(coins_reviewed.values()),
        "signals": signals_found,
        "auction": auction_results,
        "errors": errors[-10:],  # Keep only last 10 errors
    }


def parse_traces(traces: List[Dict]) -> Dict:
    """Parse traces into structured data."""
    coins_reviewed = {}
    signals_found = []
    auction_results = {
        "signals_collected": 0,
        "winners": [],
        "opens_executed": 0,
        "opens_failed": 0,
        "closes": [],
        "rejections": {},
    }
    errors = []
    
    for trace in traces:
        symbol = trace.get("symbol", "")
        decision_type = trace.get("decision_type", "")
        payload = trace.get("payload", {})
        
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except:
                payload = {}
        
        # Track coin reviews
        if symbol and symbol not in coins_reviewed:
            coins_reviewed[symbol] = {
                "symbol": symbol,
                "bias": "",
                "has_ob": False,
                "has_fvg": False,
                "has_bos": False,
                "has_4h": False,
                "signal_type": "",
                "score": None,
                "regime": "",
                "rejection": "",
            }
        
        # Parse decision types
        if decision_type == "SIGNAL_GENERATED":
            signal_type = payload.get("type", payload.get("signal_type", ""))
            score = payload.get("score", payload.get("signal_score"))
            if symbol in coins_reviewed:
                coins_reviewed[symbol]["signal_type"] = signal_type
                coins_reviewed[symbol]["score"] = score
                signals_found.append({
                    "symbol": symbol,
                    "type": signal_type,
                    "score": score,
                    "entry": payload.get("entry"),
                    "stop": payload.get("stop"),
                    "tp": payload.get("tp1"),
                    "regime": payload.get("regime"),
                })
        
        elif decision_type == "SIGNAL_REJECTED":
            reason = payload.get("reason", "")
            if symbol in coins_reviewed:
                coins_reviewed[symbol]["rejection"] = reason
        
        elif decision_type == "BIAS_CALCULATED":
            bias = payload.get("bias", "")
            if symbol in coins_reviewed:
                coins_reviewed[symbol]["bias"] = bias
        
        elif decision_type == "STRUCTURE_DETECTED":
            if symbol in coins_reviewed:
                coins_reviewed[symbol]["has_4h"] = payload.get("has_4h_structure", False)
                coins_reviewed[symbol]["has_ob"] = payload.get("order_block", False)
                coins_reviewed[symbol]["has_fvg"] = payload.get("fvg", False)
                coins_reviewed[symbol]["has_bos"] = payload.get("bos", False)
        
        elif decision_type == "AUCTION_RESULT":
            auction_results["opens_executed"] = payload.get("opens_executed", 0)
            auction_results["opens_failed"] = payload.get("opens_failed", 0)
            auction_results["signals_collected"] = payload.get("signals_collected", 0)
            auction_results["winners"] = payload.get("winners", [])
            auction_results["closes"] = payload.get("closes", [])
        
        elif decision_type == "ERROR":
            errors.append(payload.get("message", str(payload)))
    
    return {
        "coins": list(coins_reviewed.values()),
        "signals": signals_found,
        "auction": auction_results,
        "errors": errors,
    }


def generate_html(data: Dict, positions: List[Dict]) -> str:
    """Generate static HTML dashboard."""
    now = datetime.now(CET)
    timestamp = now.strftime("%Y-%m-%d %H:%M:%S CET")
    
    coins = data.get("coins", [])
    signals = data.get("signals", [])
    auction = data.get("auction", {})
    errors = data.get("errors", [])
    
    # Count signals by type
    long_signals = [s for s in signals if s.get("type") == "long"]
    short_signals = [s for s in signals if s.get("type") == "short"]
    
    # Status banner
    if errors:
        status_html = f'<div class="status-banner status-warn">⚠️ {len(errors)} errors detected</div>'
    else:
        status_html = '<div class="status-banner status-ok">✓ System operating normally</div>'
    
    # Generate signals table
    signals_rows = ""
    if signals:
        for s in sorted(signals, key=lambda x: x.get("score") or 0, reverse=True):
            badge_class = "badge-long" if s.get("type") == "long" else "badge-short"
            score = s.get("score")
            score_class = "high" if (score or 0) >= 75 else "medium" if (score or 0) >= 60 else "low"
            signals_rows += f"""
                <tr>
                    <td><strong>{s.get('symbol', '').replace('/', '')}</strong></td>
                    <td><span class="badge {badge_class}">{str(s.get('type', '')).upper()}</span></td>
                    <td><span class="badge badge-score {score_class}">{score:.1f if score else '-'}</span></td>
                    <td>{s.get('regime', '-')}</td>
                    <td>{s.get('entry', '-')}</td>
                    <td>{s.get('stop', '-')}</td>
                    <td>{s.get('tp', '-')}</td>
                </tr>
            """
        signals_html = f"""
        <div class="section">
            <div class="section-header">🎯 Signals Found <span>{len(signals)} signals</span></div>
            <div class="section-content">
                <table>
                    <thead><tr><th>Symbol</th><th>Direction</th><th>Score</th><th>Regime</th><th>Entry</th><th>Stop</th><th>TP</th></tr></thead>
                    <tbody>{signals_rows}</tbody>
                </table>
            </div>
        </div>
        """
    else:
        signals_html = """
        <div class="section">
            <div class="section-header">🎯 Signals Found</div>
            <div class="section-content"><div class="empty">No signals in the last hour</div></div>
        </div>
        """
    
    # Positions table
    positions_rows = ""
    if positions:
        for p in positions:
            side = str(p.get("side", "")).lower()
            side_badge = "badge-long" if side == "long" else "badge-short"
            is_protected = bool(p.get("stop_order_id"))
            protected_html = '<span class="badge badge-protected">Protected</span>' if is_protected else '<span style="color: #d29922;">Unprotected</span>'
            symbol = str(p.get("symbol", "")).replace("PF_", "").replace("USD", "")
            
            positions_rows += f"""
                <tr>
                    <td><strong>{symbol}</strong></td>
                    <td><span class="badge {side_badge}">{side.upper()}</span></td>
                    <td>{p.get('initial_size', '-')}</td>
                    <td>{p.get('initial_entry_price', '-')}</td>
                    <td>{p.get('current_stop_price', '-')}</td>
                    <td>{protected_html}</td>
                    <td>{p.get('state', '-')}</td>
                </tr>
            """
        positions_html = f"""
        <div class="section">
            <div class="section-header">💼 Open Positions <span>{len(positions)} positions</span></div>
            <div class="section-content">
                <table>
                    <thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Stop</th><th>Status</th><th>State</th></tr></thead>
                    <tbody>{positions_rows}</tbody>
                </table>
            </div>
        </div>
        """
    else:
        positions_html = """
        <div class="section">
            <div class="section-header">💼 Open Positions</div>
            <div class="section-content"><div class="empty">No open positions</div></div>
        </div>
        """
    
    # Auction section
    winners_list = "".join([f'<li>{w} <span class="badge badge-long">OPENED</span></li>' for w in auction.get("winners", [])]) or '<li style="color: #8b949e;">None</li>'
    closes_list = "".join([f'<li>{c} <span class="badge badge-short">CLOSED</span></li>' for c in auction.get("closes", [])]) or '<li style="color: #8b949e;">None</li>'
    
    auction_html = f"""
    <div class="section">
        <div class="section-header">🏆 Last Auction <span>{auction.get('signals_collected', 0)} candidates → {auction.get('opens_executed', 0)} executed</span></div>
        <div class="section-content">
            <div class="auction-grid">
                <div><h4 style="color: #3fb950;">Winners</h4><ul class="auction-list">{winners_list}</ul></div>
                <div><h4 style="color: #f85149;">Closed</h4><ul class="auction-list">{closes_list}</ul></div>
            </div>
        </div>
    </div>
    """
    
    # Coins reviewed table (top 50)
    coins_rows = ""
    sorted_coins = sorted(coins, key=lambda x: (x.get("signal_type", "") != "", x.get("score") or 0), reverse=True)[:50]
    for c in sorted_coins:
        bias = c.get("bias", "")
        bias_badge = f'badge-{bias}' if bias else 'badge-neutral'
        ob = '<span class="check">✓</span>' if c.get("has_ob") else '<span class="cross">-</span>'
        fvg = '<span class="check">✓</span>' if c.get("has_fvg") else '<span class="cross">-</span>'
        bos = '<span class="check">✓</span>' if c.get("has_bos") else '<span class="cross">-</span>'
        h4 = '<span class="check">✓</span>' if c.get("has_4h") else '<span class="cross">-</span>'
        
        signal_type = c.get("signal_type", "")
        if signal_type:
            signal_badge = f'<span class="badge badge-{signal_type}">{signal_type.upper()}</span>'
        else:
            signal_badge = '<span style="color: #484f58;">-</span>'
        
        score = c.get("score")
        if score:
            score_class = "high" if score >= 75 else "medium" if score >= 60 else "low"
            score_html = f'<span class="badge badge-score {score_class}">{score:.0f}</span>'
        else:
            score_html = '<span style="color: #484f58;">-</span>'
        
        rejection = c.get("rejection", "")[:35] + "..." if len(c.get("rejection", "")) > 35 else c.get("rejection", "-")
        
        coins_rows += f"""
            <tr>
                <td><strong>{c.get('symbol', '').replace('/', '')}</strong></td>
                <td><span class="badge {bias_badge}">{bias or 'N/A'}</span></td>
                <td>{ob}</td>
                <td>{fvg}</td>
                <td>{bos}</td>
                <td>{h4}</td>
                <td>{signal_badge}</td>
                <td>{score_html}</td>
                <td style="font-size: 11px; color: #8b949e;">{rejection}</td>
            </tr>
        """
    
    coins_html = f"""
    <div class="section">
        <div class="section-header">📋 Coins Reviewed <span>Top 50 of {len(coins)}</span></div>
        <div class="section-content">
            <table>
                <thead><tr><th>Symbol</th><th>Bias</th><th>OB</th><th>FVG</th><th>BOS</th><th>4H</th><th>Signal</th><th>Score</th><th>Rejection</th></tr></thead>
                <tbody>{coins_rows}</tbody>
            </table>
        </div>
    </div>
    """
    
    # Full HTML
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="60">
    <title>Trading Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; line-height: 1.5; }}
        .container {{ max-width: 1400px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; padding: 20px; background: #161b22; border-radius: 12px; margin-bottom: 20px; border: 1px solid #30363d; }}
        .header h1 {{ font-size: 24px; color: #58a6ff; }}
        .header .meta {{ text-align: right; color: #8b949e; font-size: 14px; }}
        .header .time {{ font-size: 18px; color: #c9d1d9; }}
        .status-banner {{ padding: 15px 20px; border-radius: 8px; margin-bottom: 20px; font-weight: 600; }}
        .status-ok {{ background: #238636; color: white; }}
        .status-warn {{ background: #9e6a03; color: white; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin-bottom: 25px; }}
        .stat-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 20px; text-align: center; }}
        .stat-value {{ font-size: 32px; font-weight: 700; color: #58a6ff; }}
        .stat-label {{ font-size: 13px; color: #8b949e; margin-top: 5px; text-transform: uppercase; }}
        .stat-value.green {{ color: #3fb950; }}
        .stat-value.red {{ color: #f85149; }}
        .section {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; margin-bottom: 20px; overflow: hidden; }}
        .section-header {{ padding: 15px 20px; background: #21262d; border-bottom: 1px solid #30363d; font-weight: 600; font-size: 16px; display: flex; justify-content: space-between; align-items: center; }}
        .section-content {{ padding: 15px 20px; max-height: 500px; overflow-y: auto; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 10px 12px; text-align: left; border-bottom: 1px solid #21262d; }}
        th {{ background: #21262d; font-weight: 600; font-size: 11px; text-transform: uppercase; color: #8b949e; position: sticky; top: 0; }}
        tr:hover {{ background: #1c2128; }}
        .badge {{ display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
        .badge-long {{ background: #238636; color: white; }}
        .badge-short {{ background: #da3633; color: white; }}
        .badge-bullish {{ background: rgba(35, 134, 54, 0.2); color: #3fb950; }}
        .badge-bearish {{ background: rgba(218, 54, 51, 0.2); color: #f85149; }}
        .badge-neutral {{ background: rgba(139, 148, 158, 0.2); color: #8b949e; }}
        .badge-protected {{ background: rgba(88, 166, 255, 0.2); color: #58a6ff; }}
        .badge-score {{ background: #30363d; color: #c9d1d9; min-width: 40px; text-align: center; }}
        .badge-score.high {{ background: rgba(35, 134, 54, 0.3); color: #3fb950; }}
        .badge-score.medium {{ background: rgba(210, 153, 34, 0.3); color: #d29922; }}
        .badge-score.low {{ background: rgba(218, 54, 51, 0.3); color: #f85149; }}
        .check {{ color: #3fb950; }}
        .cross {{ color: #484f58; }}
        .empty {{ text-align: center; padding: 40px; color: #8b949e; }}
        .auction-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
        .auction-list {{ list-style: none; }}
        .auction-list li {{ padding: 8px 12px; background: #21262d; margin: 5px 0; border-radius: 6px; display: flex; justify-content: space-between; }}
        .footer {{ text-align: center; padding: 20px; color: #484f58; font-size: 12px; }}
        @media (max-width: 768px) {{ .auction-grid {{ grid-template-columns: 1fr; }} .stats-grid {{ grid-template-columns: repeat(2, 1fr); }} }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Trading Dashboard</h1>
            <div class="meta">
                <div class="time">{timestamp}</div>
                <div>Auto-refreshes every 60 seconds</div>
            </div>
        </div>
        
        {status_html}
        
        <div class="stats-grid">
            <div class="stat-card"><div class="stat-value">{len(coins)}</div><div class="stat-label">Coins Reviewed</div></div>
            <div class="stat-card"><div class="stat-value {'green' if len(signals) > 0 else ''}">{len(signals)}</div><div class="stat-label">Signals Found</div></div>
            <div class="stat-card"><div class="stat-value green">{len(long_signals)}</div><div class="stat-label">Long Signals</div></div>
            <div class="stat-card"><div class="stat-value red">{len(short_signals)}</div><div class="stat-label">Short Signals</div></div>
            <div class="stat-card"><div class="stat-value">{len(positions)}</div><div class="stat-label">Open Positions</div></div>
            <div class="stat-card"><div class="stat-value {'green' if auction.get('opens_executed', 0) > 0 else ''}">{auction.get('opens_executed', 0)}</div><div class="stat-label">Auction Wins</div></div>
        </div>
        
        {signals_html}
        {auction_html}
        {positions_html}
        {coins_html}
        
        <div class="footer">Last update: {timestamp} • Data from last hour</div>
    </div>
</body>
</html>"""
    
    return html


def update_dashboard():
    """Update the static dashboard."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Updating dashboard...")
    
    try:
        # Find the active log file
        log_dir = project_root / "logs"
        run_log = log_dir / "run.log"
        
        # Parse log file
        log_entries = parse_log_file(run_log, max_lines=3000)
        
        # Fetch positions from database
        positions = fetch_positions()
        
        # Parse log entries into structured data
        data = parse_log_entries(log_entries)
        
        # Generate HTML
        html = generate_html(data, positions)
        
        # Write files
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(REPORT_FILE, "w") as f:
            f.write(html)
        
        # Also save JSON
        with open(JSON_FILE, "w") as f:
            json.dump({
                "timestamp": datetime.now(CET).isoformat(),
                "coins_count": len(data.get("coins", [])),
                "signals_count": len(data.get("signals", [])),
                "positions_count": len(positions),
                "signals": data.get("signals", []),
                "auction": data.get("auction", {}),
            }, f, indent=2, default=str)
        
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Dashboard updated: {len(data.get('coins', []))} coins, {len(data.get('signals', []))} signals, {len(positions)} positions")
        
    except Exception as e:
        import traceback
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error updating dashboard: {e}")
        traceback.print_exc()


class QuietHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves from data directory."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DATA_DIR), **kwargs)
    
    def do_GET(self):
        if self.path == "/" or self.path == "/dashboard":
            self.path = "/dashboard.html"
        return super().do_GET()
    
    def log_message(self, format, *args):
        pass  # Suppress logging


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server."""
    daemon_threads = True


def run_server(port: int = 8080):
    """Run the HTTP server."""
    server = ThreadedHTTPServer(("0.0.0.0", port), QuietHandler)
    print(f"Dashboard server running at http://0.0.0.0:{port}")
    server.serve_forever()


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Static Trading Dashboard")
    parser.add_argument("--port", type=int, default=8080, help="HTTP server port")
    parser.add_argument("--interval", type=int, default=60, help="Update interval in seconds")
    args = parser.parse_args()
    
    print("=" * 50)
    print("Static Trading Dashboard")
    print("=" * 50)
    print(f"Port: {args.port}")
    print(f"Update interval: {args.interval}s")
    print(f"Data directory: {DATA_DIR}")
    print("=" * 50)
    
    # Initial update
    update_dashboard()
    
    # Start server in background thread
    server_thread = threading.Thread(target=run_server, args=(args.port,), daemon=True)
    server_thread.start()
    
    # Update loop
    running = True
    
    def signal_handler(sig, frame):
        nonlocal running
        print("\nShutting down...")
        running = False
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    while running:
        time.sleep(args.interval)
        if running:
            update_dashboard()
    
    print("Dashboard stopped.")


if __name__ == "__main__":
    main()
