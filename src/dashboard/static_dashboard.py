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
from html import escape
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
CET = timezone(timedelta(hours=1), name="CET")

# Report paths
DATA_DIR = project_root / "data"
REPORT_FILE = DATA_DIR / "dashboard.html"
JSON_FILE = DATA_DIR / "dashboard.json"
DISCOVERY_GAP_FILE = DATA_DIR / "discovery_gap_report.json"
DISCOVERY_REPORT_FILE = DATA_DIR / "market-discovery.html"
DISCOVERY_META_FILE = DATA_DIR / "market-discovery-meta.json"
DISCOVERY_PAGE_REFRESH_SECONDS = 24 * 3600


def _parse_iso_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Parse ISO timestamp into timezone-aware datetime."""
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _format_timestamp(dt: Optional[datetime], tz: timezone = CET) -> str:
    """Format timestamp for dashboard display."""
    if not dt:
        return "N/A"
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_age(seconds: Optional[float]) -> str:
    """Human-friendly age string."""
    if seconds is None or seconds < 0:
        return "unknown"
    total = int(seconds)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _load_json_file(path: Path) -> Dict[str, Any]:
    """Load JSON file safely; return empty dict on failure."""
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


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
            SELECT symbol, side, size, entry_price, initial_stop_price,
                   stop_loss_order_id, is_protected, unrealized_pnl, opened_at
            FROM positions
            WHERE size != 0 AND size IS NOT NULL
            ORDER BY opened_at DESC
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
    signals_seen = {}  # For deduplication: symbol -> signal data
    auction_results = {
        "signals_collected": 0,
        "winners": [],
        "opens_executed": 0,
        "opens_failed": 0,
        "closes": [],
        "rejections": {},
    }
    errors = []
    current_smc_symbol = None  # Track current symbol for multi-line SMC analysis
    coins_processed_recently: Optional[int] = None
    total_coins: Optional[int] = None
    
    import re
    
    for entry in entries:
        raw = entry.get("raw", "")
        symbol = entry.get("symbol", "")
        
        # Also extract symbol from raw line if not in entry
        if not symbol:
            symbol_match = re.search(r'symbol=([A-Z0-9/]+)', raw)
            if symbol_match:
                symbol = symbol_match.group(1)
        
        # Track coins from SMC Analysis lines (first line of multi-line block)
        if "SMC Analysis" in raw:
            # Extract symbol from "SMC Analysis SYMBOL:"
            smc_match = re.search(r'SMC Analysis ([A-Z0-9/]+):', raw)
            if smc_match:
                symbol = smc_match.group(1)
                current_smc_symbol = symbol  # Track for subsequent lines
            
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
            
            if symbol and symbol in coins_reviewed:
                # Parse analysis details from this line
                if "Bias Bullish" in raw:
                    coins_reviewed[symbol]["bias"] = "bullish"
                elif "Bias Bearish" in raw:
                    coins_reviewed[symbol]["bias"] = "bearish"
                
                # Some indicators may be on same line
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
        
        # Handle continuation lines (SMC indicators on separate lines)
        elif current_smc_symbol and current_smc_symbol in coins_reviewed:
            # Check if this is a continuation line (starts with checkmark or has indicator)
            if raw.startswith("✓") or raw.startswith("✅") or raw.startswith("❌"):
                if "Order block" in raw:
                    coins_reviewed[current_smc_symbol]["has_ob"] = True
                if "Fair value gap" in raw:
                    coins_reviewed[current_smc_symbol]["has_fvg"] = True
                if "Break of structure" in raw:
                    coins_reviewed[current_smc_symbol]["has_bos"] = True
                if "4H Decision Structure Found" in raw or "4H" in raw:
                    coins_reviewed[current_smc_symbol]["has_4h"] = True
                if "❌ Rejected" in raw:
                    rejection_match = re.search(r'❌ Rejected[:\s]*(.+?)(?:\[|$)', raw)
                    if rejection_match:
                        coins_reviewed[current_smc_symbol]["rejection"] = rejection_match.group(1).strip()
            else:
                # Not a continuation line, reset current symbol
                current_smc_symbol = None
        
        # Track signals - look for "Signal generated with 4H decision authority"
        if "Signal generated" in raw and symbol:
            signal_type = ""
            
            # Extract signal type
            if "signal_type=short" in raw or "type=short" in raw:
                signal_type = "short"
            elif "signal_type=long" in raw or "type=long" in raw:
                signal_type = "long"
            
            # Extract entry/stop from the log line
            entry_match = re.search(r'entry[=:]?\s*(\d+\.?\d*)', raw, re.IGNORECASE)
            stop_match = re.search(r'stop[=:]?\s*(\d+\.?\d*)', raw, re.IGNORECASE)
            
            if signal_type:
                # Deduplicate - only keep latest signal per symbol
                # Score will be updated from Auction candidate lines
                if symbol not in signals_seen:
                    signals_seen[symbol] = {
                        "symbol": symbol,
                        "type": signal_type,
                        "score": None,
                        "entry": entry_match.group(1) if entry_match else None,
                        "stop": stop_match.group(1) if stop_match else None,
                        "tp": None,
                        "regime": coins_reviewed.get(symbol, {}).get("regime", ""),
                    }
                else:
                    # Update existing with latest entry/stop
                    signals_seen[symbol]["entry"] = entry_match.group(1) if entry_match else signals_seen[symbol]["entry"]
                    signals_seen[symbol]["stop"] = stop_match.group(1) if stop_match else signals_seen[symbol]["stop"]
                
                # Update coins_reviewed
                if symbol in coins_reviewed:
                    coins_reviewed[symbol]["signal_type"] = signal_type
        
        # Extract score from Auction candidate lines
        if "Auction candidate created" in raw and symbol:
            score_match = re.search(r'score=(\d+\.?\d*)', raw)
            if score_match:
                try:
                    score = float(score_match.group(1))
                    if symbol in signals_seen:
                        signals_seen[symbol]["score"] = score
                    if symbol in coins_reviewed:
                        coins_reviewed[symbol]["score"] = score
                except:
                    pass
        
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
        
        # Track cycle-level reviewed coin count.
        # This is a robust fallback when detailed per-coin SMC lines are missing
        # from the current log tail window.
        if "Coin processing status summary" in raw:
            recent_match = re.search(r'coins_processed_recently[=:]?\s*(\d+)', raw)
            total_match = re.search(r'total_coins[=:]?\s*(\d+)', raw)
            if recent_match:
                coins_processed_recently = int(recent_match.group(1))
            if total_match:
                total_coins = int(total_match.group(1))
        
        # Track errors (only recent, critical ones)
        if entry.get("level") == "error" and "QTY_MISMATCH" not in raw:
            # Skip common non-critical errors
            if not any(skip in raw for skip in ["SPEC_SANITY", "Failed to cancel"]):
                errors.append(raw[:200])
    
    coins_reviewed_count = len(coins_reviewed)
    if coins_processed_recently is not None or total_coins is not None:
        coins_reviewed_count = max(
            coins_reviewed_count,
            coins_processed_recently or 0,
            total_coins or 0,
        )
    
    return {
        "coins": list(coins_reviewed.values()),
        "signals": list(signals_seen.values()),  # Deduplicated signals
        "auction": auction_results,
        "errors": errors[-5:],  # Keep only last 5 errors
        "coins_reviewed_count": coins_reviewed_count,
        "coins_processed_recently": coins_processed_recently,
        "total_coins": total_coins,
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
    coins_reviewed_count = int(data.get("coins_reviewed_count", len(coins)) or 0)
    
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
                    <td><span class="badge badge-score {score_class}">{f'{score:.1f}' if score else '-'}</span></td>
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
            is_protected = bool(p.get("is_protected") or p.get("stop_loss_order_id"))
            protected_html = '<span class="badge badge-protected">Protected</span>' if is_protected else '<span style="color: #d29922;">Unprotected</span>'
            symbol = str(p.get("symbol", "")).replace("PF_", "").replace("USD", "")
            pnl = p.get("unrealized_pnl", 0) or 0
            pnl_color = "#3fb950" if float(pnl) >= 0 else "#f85149"
            
            positions_rows += f"""
                <tr>
                    <td><strong>{symbol}</strong></td>
                    <td><span class="badge {side_badge}">{side.upper()}</span></td>
                    <td>{p.get('size', '-')}</td>
                    <td>{p.get('entry_price', '-')}</td>
                    <td>{p.get('initial_stop_price', '-')}</td>
                    <td style="color: {pnl_color};">{pnl}</td>
                    <td>{protected_html}</td>
                </tr>
            """
        positions_html = f"""
        <div class="section">
            <div class="section-header">💼 Open Positions <span>{len(positions)} positions</span></div>
            <div class="section-content">
                <table>
                    <thead><tr><th>Symbol</th><th>Side</th><th>Size</th><th>Entry</th><th>Stop</th><th>PnL</th><th>Status</th></tr></thead>
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
    if not coins_rows and coins_reviewed_count > 0:
        coins_rows = f"""
            <tr>
                <td colspan="9" style="text-align: center; color: #8b949e;">
                    Reviewed {coins_reviewed_count} coins in recent cycle; detailed per-coin logs are outside the current log window.
                </td>
            </tr>
        """
    
    coins_html = f"""
    <div class="section">
        <div class="section-header">📋 Coins Reviewed <span>Top 50 of {coins_reviewed_count}</span></div>
        <div class="section-content">
            <table>
                <thead><tr><th>Symbol</th><th>Bias</th><th>OB</th><th>FVG</th><th>BOS</th><th>4H</th><th>Signal</th><th>Score</th><th>Rejection</th></tr></thead>
                <tbody>{coins_rows}</tbody>
            </table>
        </div>
    </div>
    """
    
    # Errors section (shows last 5 errors with details)
    if errors:
        errors_rows = ""
        for err in errors:
            # Truncate long error messages
            err_display = err[:300] + "..." if len(err) > 300 else err
            # Escape HTML
            err_display = err_display.replace("<", "&lt;").replace(">", "&gt;")
            errors_rows += f'<li>{err_display}</li>'
        errors_html = f"""
        <div class="section">
            <div class="section-header" style="background: #2d1a1a; border-bottom-color: #f85149;">❌ Recent Errors <span style="color: #f85149;">{len(errors)} errors</span></div>
            <div class="section-content">
                <ul class="error-list">{errors_rows}</ul>
            </div>
        </div>
        """
    else:
        errors_html = ""
    
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
        .nav {{ display: flex; gap: 10px; margin-bottom: 20px; }}
        .nav-link {{ text-decoration: none; font-size: 13px; font-weight: 600; padding: 8px 12px; border-radius: 8px; color: #c9d1d9; border: 1px solid #30363d; background: #161b22; }}
        .nav-link:hover {{ border-color: #58a6ff; }}
        .nav-link.active {{ background: #58a6ff; color: #0d1117; border-color: #58a6ff; }}
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
        .error-list {{ list-style: none; }}
        .error-list li {{ padding: 10px 12px; background: #2d1a1a; margin: 5px 0; border-radius: 6px; font-size: 12px; font-family: 'SF Mono', 'Monaco', monospace; word-break: break-all; border-left: 3px solid #f85149; color: #f0a0a0; }}
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
        <div class="nav">
            <a class="nav-link active" href="/dashboard">Live Dashboard</a>
            <a class="nav-link" href="/market-discovery">Market Discovery</a>
        </div>
        
        {status_html}
        
        {errors_html}
        
        <div class="stats-grid">
            <div class="stat-card"><div class="stat-value">{coins_reviewed_count}</div><div class="stat-label">Coins Reviewed</div></div>
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


def _render_market_discovery_table(rows: List[Dict[str, Any]], show_metrics: bool = False) -> str:
    """Render common table rows for market discovery entries."""
    if not rows:
        colspan = "11" if show_metrics else "8"
        return f"""
            <tr>
                <td colspan="{colspan}" class="empty">No rows to display.</td>
            </tr>
        """

    status_classes = {
        "eligible": "status-eligible",
        "rejected_by_filters": "status-rejected",
        "unmapped_no_spot": "status-unmapped",
        "excluded_disallowed_base": "status-excluded",
    }
    tier_classes = {
        "A": ("status-eligible", "Tier A"),
        "B": ("status-pill", "Tier B"),
        "C": ("status-unmapped", "Tier C"),
    }
    rendered = []
    for entry in rows:
        status = str(entry.get("status") or "unknown")
        badge_class = status_classes.get(status, "status-unmapped")
        reason = escape(str(entry.get("reason") or "-"))
        candidate_source = escape(str(entry.get("candidate_source") or "-"))
        
        # Tier display
        tier = entry.get("liquidity_tier")
        if tier:
            tier_class, tier_label = tier_classes.get(tier, ("status-unmapped", f"Tier {tier}"))
            tier_html = f'<span class="status-pill {tier_class}">{tier_label}</span>'
        else:
            tier_html = '<span style="color: #484f58;">-</span>'
        
        # Metrics for eligible pairs
        if show_metrics:
            vol = entry.get("futures_volume_24h")
            spread = entry.get("futures_spread_pct")
            oi = entry.get("futures_open_interest")
            
            try:
                vol_fmt = f"${float(vol):,.0f}" if vol else "-"
            except:
                vol_fmt = "-"
            try:
                spread_fmt = f"{float(spread) * 100:.3f}%" if spread else "-"
            except:
                spread_fmt = "-"
            try:
                oi_fmt = f"${float(oi):,.0f}" if oi else "-"
            except:
                oi_fmt = "-"
            
            metrics_cols = f"""
                <td>{tier_html}</td>
                <td>{vol_fmt}</td>
                <td>{spread_fmt}</td>
                <td style="color: #8b949e;">{oi_fmt}</td>
            """
        else:
            metrics_cols = ""
        
        rendered.append(
            f"""
            <tr>
                <td><strong>{escape(str(entry.get("spot_symbol") or "-"))}</strong></td>
                <td><code>{escape(str(entry.get("futures_symbol") or "-"))}</code></td>
                <td><span class="status-pill {badge_class}">{escape(status)}</span></td>
                {metrics_cols}
                <td>{'Yes' if entry.get('is_new') else 'No'}</td>
                <td>{'Yes' if entry.get('spot_market_available') else 'No'}</td>
                <td>{candidate_source}</td>
                <td class="reason">{reason}</td>
            </tr>
            """
        )
    return "".join(rendered)


def generate_market_discovery_html(
    report: Optional[Dict[str, Any]],
    now: Optional[datetime] = None,
) -> str:
    """Generate market discovery diagnostics page."""
    now_utc = now or datetime.now(timezone.utc)
    page_timestamp = _format_timestamp(now_utc, CET)
    report = report or {}

    generated_at = _parse_iso_timestamp(report.get("generated_at"))
    generated_at_cet = _format_timestamp(generated_at, CET)
    generated_at_utc = _format_timestamp(generated_at, timezone.utc)
    report_age_seconds = (
        (now_utc - generated_at).total_seconds() if generated_at else None
    )
    report_age_text = _format_age(report_age_seconds)
    stale_threshold_seconds = DISCOVERY_PAGE_REFRESH_SECONDS + 3600
    is_stale = report_age_seconds is None or report_age_seconds > stale_threshold_seconds

    totals = report.get("totals", {}) if isinstance(report.get("totals"), dict) else {}
    status_counts = (
        report.get("status_counts", {})
        if isinstance(report.get("status_counts"), dict)
        else {}
    )
    tier_distribution = (
        report.get("tier_distribution", {})
        if isinstance(report.get("tier_distribution"), dict)
        else {}
    )
    config = report.get("config", {}) if isinstance(report.get("config"), dict) else {}
    new_summary = (
        report.get("new_futures_summary", {})
        if isinstance(report.get("new_futures_summary"), dict)
        else {}
    )
    top_rejection_reasons = (
        report.get("top_rejection_reasons", [])
        if isinstance(report.get("top_rejection_reasons"), list)
        else []
    )
    entries = report.get("entries", []) if isinstance(report.get("entries"), list) else []
    new_futures_gaps = (
        report.get("new_futures_gaps", [])
        if isinstance(report.get("new_futures_gaps"), list)
        else []
    )

    eligible_entries = [entry for entry in entries if entry.get("status") == "eligible"]
    rejected_entries = [
        entry for entry in entries if entry.get("status") == "rejected_by_filters"
    ]
    unmapped_entries = [
        entry for entry in entries if entry.get("status") == "unmapped_no_spot"
    ]
    excluded_entries = [
        entry for entry in entries if entry.get("status") == "excluded_disallowed_base"
    ]

    if entries:
        status_html = """
            <div class="status-banner status-ok">
                Discovery report loaded. Market discovery is configured for once-daily refresh (24h cadence).
            </div>
        """
    else:
        status_html = """
            <div class="status-banner status-warn">
                No discovery report found yet. The page will populate after the next market discovery run.
            </div>
        """

    if is_stale:
        status_html += f"""
            <div class="status-banner status-warn">
                Discovery data may be stale. Last discovery update: {escape(generated_at_utc)} ({escape(report_age_text)} ago).
            </div>
        """

    rejection_rows = ""
    if top_rejection_reasons:
        for item in top_rejection_reasons:
            rejection_rows += f"""
                <tr>
                    <td>{escape(str(item.get('reason') or '-'))}</td>
                    <td>{int(item.get('count', 0) or 0)}</td>
                </tr>
            """
    else:
        rejection_rows = """
            <tr>
                <td colspan="2" class="empty">No rejection reasons available.</td>
            </tr>
        """

    all_rows_html = _render_market_discovery_table(entries)
    new_gap_rows_html = _render_market_discovery_table(new_futures_gaps)
    unmapped_rows_html = _render_market_discovery_table(unmapped_entries)
    rejected_rows_html = _render_market_discovery_table(rejected_entries)
    eligible_rows_html = _render_market_discovery_table(eligible_entries, show_metrics=True)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="refresh" content="3600">
    <title>Market Discovery Report</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; line-height: 1.5; }}
        .container {{ max-width: 1500px; margin: 0 auto; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; padding: 20px; background: #161b22; border-radius: 12px; margin-bottom: 20px; border: 1px solid #30363d; }}
        .header h1 {{ font-size: 24px; color: #58a6ff; }}
        .header .meta {{ text-align: right; color: #8b949e; font-size: 14px; }}
        .header .time {{ font-size: 18px; color: #c9d1d9; }}
        .nav {{ display: flex; gap: 10px; margin-bottom: 20px; }}
        .nav-link {{ text-decoration: none; font-size: 13px; font-weight: 600; padding: 8px 12px; border-radius: 8px; color: #c9d1d9; border: 1px solid #30363d; background: #161b22; }}
        .nav-link:hover {{ border-color: #58a6ff; }}
        .nav-link.active {{ background: #58a6ff; color: #0d1117; border-color: #58a6ff; }}
        .status-banner {{ padding: 15px 20px; border-radius: 8px; margin-bottom: 12px; font-weight: 600; }}
        .status-ok {{ background: #238636; color: #fff; }}
        .status-warn {{ background: #9e6a03; color: #fff; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin: 20px 0; }}
        .stat-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 10px; padding: 16px; text-align: center; }}
        .stat-value {{ font-size: 30px; font-weight: 700; color: #58a6ff; }}
        .stat-label {{ font-size: 12px; color: #8b949e; margin-top: 4px; text-transform: uppercase; }}
        .section {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; margin-bottom: 16px; overflow: hidden; }}
        .section-header {{ padding: 14px 18px; background: #21262d; border-bottom: 1px solid #30363d; font-weight: 600; font-size: 15px; display: flex; justify-content: space-between; align-items: center; }}
        .section-content {{ padding: 14px 18px; overflow-x: auto; }}
        .meta-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 12px; }}
        .meta-card {{ background: #0f1620; border: 1px solid #30363d; border-radius: 8px; padding: 12px; }}
        .meta-row {{ display: flex; justify-content: space-between; padding: 4px 0; gap: 16px; }}
        .meta-row .label {{ color: #8b949e; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ padding: 9px 10px; text-align: left; border-bottom: 1px solid #21262d; vertical-align: top; }}
        th {{ background: #21262d; font-weight: 600; font-size: 11px; text-transform: uppercase; color: #8b949e; position: sticky; top: 0; }}
        tr:hover {{ background: #1c2128; }}
        .status-pill {{ display: inline-block; padding: 3px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
        .status-eligible {{ background: rgba(35, 134, 54, 0.28); color: #3fb950; }}
        .status-rejected {{ background: rgba(248, 81, 73, 0.20); color: #f85149; }}
        .status-unmapped {{ background: rgba(210, 153, 34, 0.25); color: #d29922; }}
        .status-excluded {{ background: rgba(139, 148, 158, 0.28); color: #8b949e; }}
        .reason {{ max-width: 520px; color: #a5b4c4; font-size: 12px; }}
        .empty {{ text-align: center; color: #8b949e; padding: 20px; }}
        details {{ margin: 16px 0; }}
        details summary {{ cursor: pointer; color: #58a6ff; font-weight: 600; margin-bottom: 10px; }}
        .footer {{ text-align: center; padding: 16px; color: #6e7681; font-size: 12px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>🧭 Market Discovery Report</h1>
            <div class="meta">
                <div class="time">{page_timestamp}</div>
                <div>Page auto-refresh: hourly</div>
            </div>
        </div>
        <div class="nav">
            <a class="nav-link" href="/dashboard">Live Dashboard</a>
            <a class="nav-link active" href="/market-discovery">Market Discovery</a>
        </div>

        {status_html}

        <div class="stats-grid">
            <div class="stat-card"><div class="stat-value">{int(totals.get('futures_markets', 0) or 0)}</div><div class="stat-label">Futures Markets</div></div>
            <div class="stat-card"><div class="stat-value">{int(totals.get('candidate_pairs', 0) or 0)}</div><div class="stat-label">Candidate Pairs</div></div>
            <div class="stat-card"><div class="stat-value">{int(totals.get('eligible_pairs', 0) or 0)}</div><div class="stat-label">Eligible Pairs</div></div>
            <div class="stat-card"><div class="stat-value">{int(totals.get('gap_count', 0) or 0)}</div><div class="stat-label">Coverage Gaps</div></div>
            <div class="stat-card"><div class="stat-value">{int(status_counts.get('unmapped_no_spot', 0) or 0)}</div><div class="stat-label">Unmapped (No Spot)</div></div>
            <div class="stat-card"><div class="stat-value">{int(status_counts.get('rejected_by_filters', 0) or 0)}</div><div class="stat-label">Rejected by Filters</div></div>
            <div class="stat-card" style="background: rgba(35, 134, 54, 0.1); border-color: #238636;"><div class="stat-value" style="color: #3fb950;">{int(tier_distribution.get('A', 0) or 0)}</div><div class="stat-label">Tier A (Majors)</div></div>
            <div class="stat-card" style="background: rgba(88, 166, 255, 0.1); border-color: #58a6ff;"><div class="stat-value" style="color: #58a6ff;">{int(tier_distribution.get('B', 0) or 0)}</div><div class="stat-label">Tier B (Workhorse)</div></div>
            <div class="stat-card" style="background: rgba(210, 153, 34, 0.1); border-color: #d29922;"><div class="stat-value" style="color: #d29922;">{int(tier_distribution.get('C', 0) or 0)}</div><div class="stat-label">Tier C (Opportunistic)</div></div>
        </div>
        
        <div class="section" style="border-color: #58a6ff;">
            <div class="section-header" style="background: rgba(88, 166, 255, 0.1); border-bottom-color: #58a6ff;">
                🔧 V2 Filter Philosophy <span style="color: #58a6ff;">OI & Funding Removed as Gates</span>
            </div>
            <div class="section-content">
                <div class="meta-grid">
                    <div class="meta-card">
                        <div style="font-weight: 600; margin-bottom: 8px; color: #3fb950;">✓ PRIMARY Gates (Reliable)</div>
                        <ul style="list-style: none; color: #a5b4c4; font-size: 13px;">
                            <li>• <strong>Futures Volume 24h</strong> - Tier A: bypass, Tier B: ≥$500k, Tier C: ≥$250k</li>
                            <li>• <strong>Futures Spread</strong> - Tier A: bypass, Tier B: ≤0.25%, Tier C: ≤0.50%</li>
                            <li>• <strong>Price Sanity</strong> - ≥$0.01 minimum</li>
                        </ul>
                    </div>
                    <div class="meta-card">
                        <div style="font-weight: 600; margin-bottom: 8px; color: #d29922;">⚠ OBSERVABILITY Only (Unreliable)</div>
                        <ul style="list-style: none; color: #a5b4c4; font-size: 13px;">
                            <li>• <strong>Open Interest</strong> - Kraken reports $0 for BTC, ETH, DOGE, etc.</li>
                            <li>• <strong>Funding Rate</strong> - Kraken reports -58% for BTC, -19% for YFI, etc.</li>
                            <li style="color: #8b949e; margin-top: 4px;">These are logged for monitoring but NOT used as eligibility gates.</li>
                        </ul>
                    </div>
                    <div class="meta-card">
                        <div style="font-weight: 600; margin-bottom: 8px; color: #58a6ff;">🛡 Entry-Time Safety (Execution Layer)</div>
                        <ul style="list-style: none; color: #a5b4c4; font-size: 13px;">
                            <li>• Real-time spread check before order (max 0.5%)</li>
                            <li>• Depth ratio check (order book ≥ 2x order size)</li>
                            <li style="color: #8b949e; margin-top: 4px;">Replaces OI as liquidity guard - more reliable than Kraken's data.</li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>

        <div class="section">
            <div class="section-header">Discovery Metadata <span>Cadence: once per day</span></div>
            <div class="section-content">
                <div class="meta-grid">
                    <div class="meta-card">
                        <div class="meta-row"><span class="label">Last discovery update (UTC)</span><span>{escape(generated_at_utc)}</span></div>
                        <div class="meta-row"><span class="label">Last discovery update (CET)</span><span>{escape(generated_at_cet)}</span></div>
                        <div class="meta-row"><span class="label">Discovery data age</span><span>{escape(report_age_text)}</span></div>
                        <div class="meta-row"><span class="label">Page generated</span><span>{escape(page_timestamp)}</span></div>
                    </div>
                    <div class="meta-card">
                        <div class="meta-row"><span class="label">allow_futures_only_pairs</span><span>{'true' if config.get('allow_futures_only_pairs') else 'false'}</span></div>
                        <div class="meta-row"><span class="label">allow_futures_only_universe</span><span>{'true' if config.get('allow_futures_only_universe') else 'false'}</span></div>
                        <div class="meta-row"><span class="label">Status: eligible</span><span>{int(status_counts.get('eligible', 0) or 0)}</span></div>
                        <div class="meta-row"><span class="label">Status: excluded disallowed base</span><span>{int(status_counts.get('excluded_disallowed_base', 0) or 0)}</span></div>
                    </div>
                </div>
            </div>
        </div>

        <div class="section">
            <div class="section-header">Top Rejection Reasons <span>{len(top_rejection_reasons)} reasons</span></div>
            <div class="section-content">
                <table>
                    <thead><tr><th>Reason</th><th>Count</th></tr></thead>
                    <tbody>{rejection_rows}</tbody>
                </table>
            </div>
        </div>

        <div class="section">
            <div class="section-header">New Futures with Gaps <span>{len(new_futures_gaps)} rows</span></div>
            <div class="section-content">
                <table>
                    <thead><tr><th>Spot Symbol</th><th>Futures Symbol</th><th>Status</th><th>New</th><th>Spot Available</th><th>Candidate</th><th>Source</th><th>Reason</th></tr></thead>
                    <tbody>{new_gap_rows_html}</tbody>
                </table>
            </div>
        </div>

        <div class="section">
            <div class="section-header">Unmapped Futures (No Spot) <span>{len(unmapped_entries)} rows</span></div>
            <div class="section-content">
                <table>
                    <thead><tr><th>Spot Symbol</th><th>Futures Symbol</th><th>Status</th><th>New</th><th>Spot Available</th><th>Candidate</th><th>Source</th><th>Reason</th></tr></thead>
                    <tbody>{unmapped_rows_html}</tbody>
                </table>
            </div>
        </div>

        <div class="section">
            <div class="section-header">Rejected by Filters <span>{len(rejected_entries)} rows</span></div>
            <div class="section-content">
                <table>
                    <thead><tr><th>Spot Symbol</th><th>Futures Symbol</th><th>Status</th><th>New</th><th>Spot Available</th><th>Candidate</th><th>Source</th><th>Reason</th></tr></thead>
                    <tbody>{rejected_rows_html}</tbody>
                </table>
            </div>
        </div>

        <details open>
            <summary>Eligible Pairs ({len(eligible_entries)})</summary>
            <div class="section">
                <div class="section-content">
                    <table>
                        <thead><tr><th>Spot Symbol</th><th>Futures Symbol</th><th>Status</th><th>Tier</th><th>Volume 24h</th><th>Spread</th><th>OI (logged)</th><th>New</th><th>Spot Available</th><th>Source</th><th>Reason</th></tr></thead>
                        <tbody>{eligible_rows_html}</tbody>
                    </table>
                </div>
            </div>
        </details>

        <details>
            <summary>Full Discovery Catalog ({len(entries)})</summary>
            <div class="section">
                <div class="section-content">
                    <table>
                        <thead><tr><th>Spot Symbol</th><th>Futures Symbol</th><th>Status</th><th>New</th><th>Spot Available</th><th>Candidate</th><th>Source</th><th>Reason</th></tr></thead>
                        <tbody>{all_rows_html}</tbody>
                    </table>
                </div>
            </div>
        </details>

        <details>
            <summary>Excluded by Disallowed Base ({len(excluded_entries)})</summary>
            <div class="section">
                <div class="section-content">
                    <table>
                        <thead><tr><th>Spot Symbol</th><th>Futures Symbol</th><th>Status</th><th>New</th><th>Spot Available</th><th>Candidate</th><th>Source</th><th>Reason</th></tr></thead>
                        <tbody>{_render_market_discovery_table(excluded_entries)}</tbody>
                    </table>
                </div>
            </div>
        </details>

        <div class="footer">
            Last discovery update: {escape(generated_at_utc)} • Discovery refresh target: once per day • Page built: {escape(page_timestamp)}
        </div>
    </div>
</body>
</html>"""


def update_market_discovery_dashboard(force: bool = False) -> bool:
    """
    Update market discovery report page.

    Returns True when page was written, False when skipped.
    """
    now_utc = datetime.now(timezone.utc)
    report = _load_json_file(DISCOVERY_GAP_FILE)
    meta = _load_json_file(DISCOVERY_META_FILE)

    report_generated_at = _parse_iso_timestamp(
        report.get("generated_at") if isinstance(report, dict) else None
    )
    report_generated_at_iso = report_generated_at.isoformat() if report_generated_at else None
    last_page_generated_at = _parse_iso_timestamp(
        meta.get("page_generated_at") if isinstance(meta, dict) else None
    )
    last_report_generated_at = (
        meta.get("report_generated_at") if isinstance(meta, dict) else None
    )

    should_update = force or not DISCOVERY_REPORT_FILE.exists()
    if not should_update and report_generated_at_iso and report_generated_at_iso != last_report_generated_at:
        should_update = True
    if not should_update and last_page_generated_at is None:
        should_update = True
    if not should_update and last_page_generated_at:
        age_seconds = (now_utc - last_page_generated_at).total_seconds()
        if age_seconds >= DISCOVERY_PAGE_REFRESH_SECONDS:
            should_update = True

    if not should_update:
        return False

    html = generate_market_discovery_html(report, now=now_utc)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DISCOVERY_REPORT_FILE, "w") as f:
        f.write(html)
    with open(DISCOVERY_META_FILE, "w") as f:
        json.dump(
            {
                "page_generated_at": now_utc.isoformat(),
                "report_generated_at": report_generated_at_iso,
            },
            f,
            indent=2,
        )
    return True


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
        coins_count = int(data.get("coins_reviewed_count", len(data.get("coins", []))) or 0)
        with open(JSON_FILE, "w") as f:
            json.dump({
                "timestamp": datetime.now(CET).isoformat(),
                "coins_count": coins_count,
                "signals_count": len(data.get("signals", [])),
                "positions_count": len(positions),
                "signals": data.get("signals", []),
                "auction": data.get("auction", {}),
            }, f, indent=2, default=str)

        print(f"[{datetime.now().strftime('%H:%M:%S')}] Dashboard updated: {coins_count} coins, {len(data.get('signals', []))} signals, {len(positions)} positions")
        
    except Exception as e:
        import traceback
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Error updating dashboard: {e}")
        traceback.print_exc()
    finally:
        try:
            discovery_updated = update_market_discovery_dashboard(force=False)
            if discovery_updated:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] "
                    "Market discovery page refreshed"
                )
        except Exception as discovery_error:
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"Warning: failed to refresh discovery page: {discovery_error}"
            )


class QuietHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves from data directory."""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DATA_DIR), **kwargs)
    
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/dashboard":
            self.path = "/dashboard.html"
        elif path in ("/market-discovery", "/market-discovery/", "/discovery", "/discovery/"):
            self.path = "/market-discovery.html"
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
