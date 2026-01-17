"""
Trading System Dashboard - Rebuilt from Scratch
Modern, clean, and reliable real-time trading monitor.
"""
import streamlit as st
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from decimal import Decimal
import json

# Page config - MUST be first Streamlit command
st.set_page_config(
    page_title="Trading Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded"
)

# =============================================================================
# CUSTOM CSS - Dark theme with green accents (trading aesthetic)
# =============================================================================
st.markdown("""
<style>
    /* Import distinctive font */
    @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Space+Grotesk:wght@400;500;600;700&display=swap');
    
    /* Root variables */
    :root {
        --bg-primary: #0a0a0f;
        --bg-secondary: #12121a;
        --bg-card: #1a1a24;
        --bg-card-hover: #22222e;
        --text-primary: #e8e8e8;
        --text-secondary: #888;
        --text-muted: #555;
        --accent-green: #00d26a;
        --accent-green-dim: #00d26a33;
        --accent-red: #ff4757;
        --accent-red-dim: #ff475733;
        --accent-yellow: #ffa502;
        --accent-blue: #3742fa;
        --border-color: #2a2a3a;
    }
    
    /* Main container */
    .main .block-container {
        padding: 1rem 2rem;
        max-width: 100%;
    }
    
    /* Hide Streamlit branding */
    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {visibility: hidden;}
    
    /* Custom header */
    .dashboard-header {
        font-family: 'Space Grotesk', sans-serif;
        font-size: 2rem;
        font-weight: 700;
        color: var(--text-primary);
        margin-bottom: 0.5rem;
        display: flex;
        align-items: center;
        gap: 0.75rem;
    }
    
    .dashboard-header .live-dot {
        width: 10px;
        height: 10px;
        background: var(--accent-green);
        border-radius: 50%;
        animation: pulse 2s infinite;
        box-shadow: 0 0 10px var(--accent-green);
    }
    
    @keyframes pulse {
        0%, 100% { opacity: 1; }
        50% { opacity: 0.5; }
    }
    
    /* Status bar */
    .status-bar {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.75rem;
        color: var(--text-secondary);
        padding: 0.5rem 0;
        border-bottom: 1px solid var(--border-color);
        margin-bottom: 1rem;
    }
    
    /* Metric cards */
    .metric-card {
        background: var(--bg-card);
        border: 1px solid var(--border-color);
        border-radius: 8px;
        padding: 1rem;
        transition: all 0.2s ease;
    }
    
    .metric-card:hover {
        background: var(--bg-card-hover);
        border-color: var(--accent-green);
    }
    
    .metric-label {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.7rem;
        color: var(--text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-bottom: 0.25rem;
    }
    
    .metric-value {
        font-family: 'Space Grotesk', sans-serif;
        font-size: 1.5rem;
        font-weight: 600;
        color: var(--text-primary);
    }
    
    .metric-value.positive {
        color: var(--accent-green);
    }
    
    .metric-value.negative {
        color: var(--accent-red);
    }
    
    /* Position cards */
    .position-card {
        background: var(--bg-card);
        border: 1px solid var(--border-color);
        border-radius: 8px;
        padding: 1rem;
        margin-bottom: 0.75rem;
        border-left: 3px solid var(--accent-green);
    }
    
    .position-card.short {
        border-left-color: var(--accent-red);
    }
    
    .position-symbol {
        font-family: 'Space Grotesk', sans-serif;
        font-size: 1.1rem;
        font-weight: 600;
        color: var(--text-primary);
    }
    
    .position-side {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.7rem;
        font-weight: 600;
        padding: 0.15rem 0.5rem;
        border-radius: 4px;
        display: inline-block;
    }
    
    .position-side.long {
        background: var(--accent-green-dim);
        color: var(--accent-green);
    }
    
    .position-side.short {
        background: var(--accent-red-dim);
        color: var(--accent-red);
    }
    
    /* Signal badges */
    .signal-badge {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.7rem;
        font-weight: 600;
        padding: 0.2rem 0.5rem;
        border-radius: 4px;
        display: inline-block;
    }
    
    .signal-badge.long {
        background: var(--accent-green-dim);
        color: var(--accent-green);
    }
    
    .signal-badge.short {
        background: var(--accent-red-dim);
        color: var(--accent-red);
    }
    
    .signal-badge.none {
        background: #333;
        color: var(--text-secondary);
    }
    
    /* Table styling */
    .dataframe {
        font-family: 'JetBrains Mono', monospace !important;
        font-size: 0.75rem !important;
    }
    
    /* Section headers */
    .section-header {
        font-family: 'Space Grotesk', sans-serif;
        font-size: 1rem;
        font-weight: 600;
        color: var(--text-primary);
        margin: 1.5rem 0 0.75rem 0;
        padding-bottom: 0.5rem;
        border-bottom: 1px solid var(--border-color);
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    
    /* Kill switch warning */
    .kill-switch-active {
        background: var(--accent-red-dim);
        border: 1px solid var(--accent-red);
        border-radius: 8px;
        padding: 1rem;
        margin-bottom: 1rem;
        font-family: 'JetBrains Mono', monospace;
    }
    
    .kill-switch-active .title {
        color: var(--accent-red);
        font-weight: 600;
        font-size: 0.9rem;
    }
    
    /* Stremlit overrides */
    .stMetric {
        background: var(--bg-card);
        border: 1px solid var(--border-color);
        border-radius: 8px;
        padding: 0.75rem;
    }
    
    div[data-testid="stMetricValue"] {
        font-family: 'Space Grotesk', sans-serif;
    }
    
    div[data-testid="stMetricLabel"] {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.7rem;
    }
    
    /* Sidebar styling */
    section[data-testid="stSidebar"] {
        background: var(--bg-secondary);
    }
    
    section[data-testid="stSidebar"] .block-container {
        padding-top: 2rem;
    }
    
    /* Refresh info */
    .refresh-info {
        font-family: 'JetBrains Mono', monospace;
        font-size: 0.65rem;
        color: var(--text-muted);
        text-align: center;
        padding: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)


# =============================================================================
# DATA LOADING FUNCTIONS (with error handling)
# =============================================================================

def safe_db_connect():
    """Safely check database connection."""
    try:
        from src.storage.db import get_db
        db = get_db()
        return True
    except Exception as e:
        return False


@st.cache_data(ttl=15)
def load_account_state() -> Optional[Dict]:
    """Load latest account state from database."""
    try:
        from src.storage.repository import get_latest_account_state
        state = get_latest_account_state()
        if state:
            return {
                'equity': float(state.get('equity', 0)),
                'balance': float(state.get('balance', 0)),
                'margin_used': float(state.get('margin_used', 0)),
                'available_margin': float(state.get('available_margin', 0)),
                'unrealized_pnl': float(state.get('unrealized_pnl', 0)),
                'timestamp': state.get('timestamp')
            }
        return None
    except Exception as e:
        st.error(f"Failed to load account state: {e}")
        return None


@st.cache_data(ttl=15)
def load_positions() -> List[Dict]:
    """Load active positions from database."""
    try:
        from src.storage.db import get_db
        from src.storage.repository import PositionModel
        
        db = get_db()
        with db.get_session() as session:
            positions = session.query(PositionModel).all()
            
            result = []
            now = datetime.now(timezone.utc)
            
            for p in positions:
                entry = float(p.entry_price)
                current = float(p.current_mark_price)
                side = p.side.upper()
                
                # Calculate PnL %
                if side == 'LONG':
                    pnl_pct = ((current - entry) / entry) * 100 if entry > 0 else 0
                else:
                    pnl_pct = ((entry - current) / entry) * 100 if entry > 0 else 0
                
                # Holding time
                opened_at = p.opened_at.replace(tzinfo=timezone.utc) if p.opened_at else now
                holding_hours = (now - opened_at).total_seconds() / 3600
                
                result.append({
                    'symbol': p.symbol.replace(":USD", "/USD").replace("PF_", ""),
                    'side': side,
                    'entry_price': entry,
                    'current_price': current,
                    'pnl_pct': pnl_pct,
                    'unrealized_pnl': float(p.unrealized_pnl),
                    'size_notional': float(p.size_notional),
                    'leverage': float(p.leverage),
                    'holding_hours': holding_hours,
                    'margin_used': float(p.margin_used) if p.margin_used else 0,
                    'liquidation_price': float(p.liquidation_price) if p.liquidation_price else None,
                    'stop_loss': float(p.initial_stop_price) if p.initial_stop_price else None,
                    'tp1': float(p.tp1_price) if p.tp1_price else None,
                    'tp2': float(p.tp2_price) if p.tp2_price else None,
                })
            
            return sorted(result, key=lambda x: x['size_notional'], reverse=True)
    except Exception as e:
        st.error(f"Failed to load positions: {e}")
        return []


@st.cache_data(ttl=15)
def load_performance_metrics(days: int = 30) -> Dict:
    """Load performance metrics."""
    try:
        from src.storage.repository import get_trades_since
        from datetime import timedelta
        
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        trades = get_trades_since(cutoff)
        
        if not trades:
            return {
                'total_trades': 0,
                'win_rate': 0.0,
                'total_pnl': 0.0,
                'avg_win': 0.0,
                'avg_loss': 0.0,
                'profit_factor': 0.0,
                'best_trade': 0.0,
                'worst_trade': 0.0,
            }
        
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl < 0]
        
        total_pnl = sum(float(t.net_pnl) for t in trades)
        win_rate = (len(wins) / len(trades) * 100) if trades else 0
        
        avg_win = sum(float(t.net_pnl) for t in wins) / len(wins) if wins else 0
        avg_loss = sum(float(t.net_pnl) for t in losses) / len(losses) if losses else 0
        
        total_wins = sum(float(t.net_pnl) for t in wins)
        total_losses = abs(sum(float(t.net_pnl) for t in losses))
        profit_factor = total_wins / total_losses if total_losses > 0 else 0
        
        all_pnl = [float(t.net_pnl) for t in trades]
        
        return {
            'total_trades': len(trades),
            'win_rate': win_rate,
            'total_pnl': total_pnl,
            'avg_win': avg_win,
            'avg_loss': avg_loss,
            'profit_factor': profit_factor,
            'best_trade': max(all_pnl) if all_pnl else 0,
            'worst_trade': min(all_pnl) if all_pnl else 0,
        }
    except Exception as e:
        return {
            'total_trades': 0,
            'win_rate': 0.0,
            'total_pnl': 0.0,
            'avg_win': 0.0,
            'avg_loss': 0.0,
            'profit_factor': 0.0,
            'best_trade': 0.0,
            'worst_trade': 0.0,
        }


@st.cache_data(ttl=15)
def load_recent_trades(limit: int = 20) -> List[Dict]:
    """Load recent completed trades."""
    try:
        from src.storage.repository import get_all_trades
        trades = get_all_trades()
        
        if not trades:
            return []
        
        # Sort by exit time and take most recent
        sorted_trades = sorted(trades, key=lambda t: t.exited_at, reverse=True)[:limit]
        
        return [{
            'symbol': t.symbol.replace(":USD", "/USD").replace("PF_", ""),
            'side': t.side.upper(),
            'entry_price': float(t.entry_price),
            'exit_price': float(t.exit_price),
            'net_pnl': float(t.net_pnl),
            'exit_reason': t.exit_reason,
            'holding_hours': float(t.holding_period_hours),
            'exited_at': t.exited_at.replace(tzinfo=timezone.utc) if t.exited_at else None,
        } for t in sorted_trades]
    except Exception as e:
        return []


@st.cache_data(ttl=15)
def load_coins_analysis() -> List[Dict]:
    """Load latest analysis for all tracked coins."""
    try:
        from src.storage.repository import get_latest_traces
        
        traces = get_latest_traces(limit=500)
        now = datetime.now(timezone.utc)
        
        coins = []
        for trace in traces:
            details = trace.get('details', {})
            timestamp = trace.get('timestamp')
            
            # Calculate freshness
            if timestamp:
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                age_minutes = (now - timestamp).total_seconds() / 60
            else:
                age_minutes = 9999
            
            # Status based on age
            if age_minutes < 60:
                status = 'active'
            elif age_minutes < 360:
                status = 'stale'
            else:
                status = 'dead'
            
            signal = details.get('signal', 'NO_SIGNAL')
            if signal and signal.lower() in ['long', 'short']:
                signal = signal.upper()
            else:
                signal = 'NONE'
            
            coins.append({
                'symbol': trace.get('symbol', 'UNKNOWN'),
                'price': float(details.get('spot_price', 0) or 0),
                'signal': signal,
                'regime': details.get('regime', 'unknown'),
                'bias': details.get('bias', 'neutral'),
                'quality': float(details.get('setup_quality', 0) or 0),
                'adx': float(details.get('adx', 0) or 0),
                'status': status,
                'age_minutes': age_minutes,
            })
        
        return sorted(coins, key=lambda x: x['symbol'])
    except Exception as e:
        return []


@st.cache_data(ttl=15)
def load_recent_signals(limit: int = 30) -> List[Dict]:
    """Load recent trading signals."""
    try:
        from src.storage.repository import get_recent_events
        
        events = get_recent_events(limit=limit * 3, event_type="DECISION_TRACE")
        now = datetime.now(timezone.utc)
        
        signals = []
        for e in events:
            details = e.get('details', {})
            signal = details.get('signal', '')
            
            if signal and signal.lower() in ['long', 'short']:
                timestamp = e.get('timestamp')
                if isinstance(timestamp, str):
                    timestamp = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                
                signals.append({
                    'timestamp': timestamp,
                    'symbol': e.get('symbol', 'UNKNOWN'),
                    'signal': signal.upper(),
                    'quality': float(details.get('setup_quality', 0) or 0),
                    'regime': details.get('regime', 'unknown'),
                })
        
        return sorted(signals, key=lambda x: x['timestamp'], reverse=True)[:limit]
    except Exception as e:
        return []


def get_kill_switch_status() -> Dict:
    """Get kill switch status."""
    try:
        from src.monitoring.kill_switch import get_kill_switch
        ks = get_kill_switch()
        return ks.get_status()
    except:
        return {'active': False, 'reason': None}


# =============================================================================
# RENDER FUNCTIONS
# =============================================================================

def render_header():
    """Render dashboard header."""
    st.markdown("""
        <div class="dashboard-header">
            <div class="live-dot"></div>
            Trading System
        </div>
    """, unsafe_allow_html=True)
    
    now = datetime.now(timezone.utc)
    st.markdown(f"""
        <div class="status-bar">
            Last update: {now.strftime('%Y-%m-%d %H:%M:%S')} UTC
        </div>
    """, unsafe_allow_html=True)


def render_kill_switch_warning(ks_status: Dict):
    """Render kill switch warning if active."""
    if ks_status.get('active'):
        st.markdown(f"""
            <div class="kill-switch-active">
                <div class="title">🚨 KILL SWITCH ACTIVE</div>
                <div>Reason: {ks_status.get('reason', 'Unknown')}</div>
                <div>Trading is suspended.</div>
            </div>
        """, unsafe_allow_html=True)


def render_account_metrics(account: Optional[Dict], positions: List[Dict]):
    """Render account overview metrics."""
    st.markdown('<div class="section-header">💰 Account Overview</div>', unsafe_allow_html=True)
    
    cols = st.columns(5)
    
    if account:
        with cols[0]:
            st.metric("Equity", f"${account['equity']:,.2f}")
        with cols[1]:
            pnl = account['unrealized_pnl']
            st.metric("Unrealized PnL", f"${pnl:,.2f}", delta=f"${pnl:,.2f}")
        with cols[2]:
            st.metric("Margin Used", f"${account['margin_used']:,.2f}")
        with cols[3]:
            st.metric("Available", f"${account['available_margin']:,.2f}")
        with cols[4]:
            st.metric("Open Positions", len(positions))
    else:
        with cols[0]:
            st.metric("Equity", "N/A")
        with cols[1]:
            st.metric("Unrealized PnL", "N/A")
        with cols[2]:
            st.metric("Margin Used", "N/A")
        with cols[3]:
            st.metric("Available", "N/A")
        with cols[4]:
            st.metric("Open Positions", len(positions))


def render_positions(positions: List[Dict]):
    """Render open positions section."""
    st.markdown('<div class="section-header">📊 Open Positions</div>', unsafe_allow_html=True)
    
    if not positions:
        st.info("No open positions")
        return
    
    for pos in positions:
        side_class = 'long' if pos['side'] == 'LONG' else 'short'
        pnl_class = 'positive' if pos['pnl_pct'] >= 0 else 'negative'
        pnl_sign = '+' if pos['pnl_pct'] >= 0 else ''
        
        # Format holding time
        hours = pos['holding_hours']
        if hours < 1:
            time_str = f"{int(hours * 60)}m"
        elif hours < 24:
            time_str = f"{hours:.1f}h"
        else:
            time_str = f"{hours / 24:.1f}d"
        
        col1, col2, col3, col4, col5 = st.columns([2, 1, 2, 2, 1])
        
        with col1:
            st.markdown(f"""
                <div style="display: flex; align-items: center; gap: 0.5rem;">
                    <span class="position-side {side_class}">{pos['side']}</span>
                    <span class="position-symbol">{pos['symbol']}</span>
                </div>
            """, unsafe_allow_html=True)
        
        with col2:
            st.markdown(f"**{pos['leverage']:.0f}x** leverage")
        
        with col3:
            st.markdown(f"Entry: **${pos['entry_price']:.4f}** → ${pos['current_price']:.4f}")
        
        with col4:
            st.markdown(f"""
                <span class="metric-value {pnl_class}">
                    {pnl_sign}{pos['pnl_pct']:.2f}% (${pos['unrealized_pnl']:,.2f})
                </span>
            """, unsafe_allow_html=True)
        
        with col5:
            st.markdown(f"⏱️ {time_str}")
        
        # Show stop loss and take profit info
        details = []
        if pos['stop_loss']:
            details.append(f"SL: ${pos['stop_loss']:.4f}")
        if pos['tp1']:
            details.append(f"TP1: ${pos['tp1']:.4f}")
        if pos['tp2']:
            details.append(f"TP2: ${pos['tp2']:.4f}")
        
        if details:
            st.caption(" | ".join(details))
        
        st.markdown("---")
    
    # Positions table (Local Dev Addition)
    positions_df = pd.DataFrame(positions)
    st.markdown("### Detailed Positions Table")
    st.dataframe(
        positions_df,
        width="stretch",
        hide_index=True,
        height=min(400, 50 + len(positions) * 35),
    )


def render_performance(metrics: Dict):
    """Render performance metrics section."""
    st.markdown('<div class="section-header">📈 Performance (30d)</div>', unsafe_allow_html=True)
    
    cols = st.columns(4)
    
    with cols[0]:
        pnl = metrics['total_pnl']
        pnl_color = "normal" if pnl >= 0 else "inverse"
        st.metric("Total PnL", f"${pnl:,.2f}", delta=f"${pnl:,.2f}", delta_color=pnl_color)
    
    with cols[1]:
        st.metric("Win Rate", f"{metrics['win_rate']:.1f}%")
    
    with cols[2]:
        st.metric("Total Trades", metrics['total_trades'])
    
    with cols[3]:
        st.metric("Profit Factor", f"{metrics['profit_factor']:.2f}")
    
    # Second row
    cols2 = st.columns(4)
    
    with cols2[0]:
        st.metric("Avg Win", f"${metrics['avg_win']:,.2f}")
    
    with cols2[1]:
        st.metric("Avg Loss", f"${metrics['avg_loss']:,.2f}")
    
    with cols2[2]:
        st.metric("Best Trade", f"${metrics['best_trade']:,.2f}")
    
    with cols2[3]:
        st.metric("Worst Trade", f"${metrics['worst_trade']:,.2f}")


def render_recent_trades(trades: List[Dict]):
    """Render recent trades section."""
    st.markdown('<div class="section-header">📜 Recent Trades</div>', unsafe_allow_html=True)
    
    if not trades:
        st.info("No completed trades yet")
        return
    
    data = []
    now = datetime.now(timezone.utc)
    
    for t in trades:
        # Format time ago
        if t['exited_at']:
            delta = now - t['exited_at']
            if delta.total_seconds() < 3600:
                time_ago = f"{int(delta.total_seconds() / 60)}m ago"
            elif delta.total_seconds() < 86400:
                time_ago = f"{int(delta.total_seconds() / 3600)}h ago"
            else:
                time_ago = f"{int(delta.total_seconds() / 86400)}d ago"
        else:
            time_ago = "N/A"
        
        pnl = t['net_pnl']
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        
        data.append({
            'Time': time_ago,
            'Symbol': t['symbol'],
            'Side': t['side'],
            'PnL': pnl_str,
            'Exit': t['exit_reason'],
            'Duration': f"{t['holding_hours']:.1f}h"
        })
    
    df = pd.DataFrame(data)
    st.dataframe(df, use_container_width=True, hide_index=True, height=300)


def render_coin_scanner(coins: List[Dict]):
    """Render coin scanner / analysis table."""
    st.markdown('<div class="section-header">🔍 Coin Scanner</div>', unsafe_allow_html=True)
    
    if not coins:
        st.info("No coin analysis data available")
        return

    df = pd.DataFrame(coins)
    
    # Display table
    st.dataframe(
        df,
        width="stretch",
        height=600,
        hide_index=True,
        column_config={
            "24h %": st.column_config.NumberColumn(
                "24h %",
                format="%.2f%%",
            )
        }
    )

    
    with col1:
        signal_filter = st.selectbox("Signal", ["All", "LONG", "SHORT", "NONE"], key="signal_filter")
    with col2:
        status_filter = st.selectbox("Status", ["All", "active", "stale", "dead"], key="status_filter")
    with col3:
        min_quality = st.slider("Min Quality", 0, 100, 0, key="quality_filter")
    
    # Apply filters
    filtered = coins
    if signal_filter != "All":
        filtered = [c for c in filtered if c['signal'] == signal_filter]
    if status_filter != "All":
        filtered = [c for c in filtered if c['status'] == status_filter]
    if min_quality > 0:
        filtered = [c for c in filtered if c['quality'] >= min_quality]
    
    st.caption(f"Showing {len(filtered)} of {len(coins)} coins")
    
    # Build table data
    data = []
    for c in filtered:
        status_emoji = {'active': '🟢', 'stale': '🟡', 'dead': '🔴'}.get(c['status'], '⚪')
        signal_emoji = {'LONG': '🟢', 'SHORT': '🔴', 'NONE': '⚪'}.get(c['signal'], '⚪')
        
        data.append({
            'Status': status_emoji,
            'Symbol': c['symbol'],
            'Price': f"${c['price']:.4f}" if c['price'] > 0 else "N/A",
            'Signal': f"{signal_emoji} {c['signal']}",
            'Quality': f"{c['quality']:.0f}",
            'Regime': c['regime'],
            'Bias': c['bias'],
            'ADX': f"{c['adx']:.1f}",
            'Age': f"{int(c['age_minutes'])}m" if c['age_minutes'] < 9999 else "N/A"
        })
    
    if data:
        df = pd.DataFrame(data)
        st.dataframe(df, use_container_width=True, hide_index=True, height=400)
    else:
        st.info("No coins match the current filters")


def render_recent_signals(signals: List[Dict]):
    """Render recent signals section."""
    st.markdown('<div class="section-header">⚡ Recent Signals</div>', unsafe_allow_html=True)
    
    if not signals:
        st.info("No recent signals")
        return
    
    data = []
    now = datetime.now(timezone.utc)
    
    for s in signals:
        # Format time ago
        if s['timestamp']:
            delta = now - s['timestamp']
            if delta.total_seconds() < 3600:
                time_ago = f"{int(delta.total_seconds() / 60)}m ago"
            elif delta.total_seconds() < 86400:
                time_ago = f"{int(delta.total_seconds() / 3600)}h ago"
            else:
                time_ago = f"{int(delta.total_seconds() / 86400)}d ago"
        else:
            time_ago = "N/A"
        
        signal_emoji = '🟢' if s['signal'] == 'LONG' else '🔴'
        
        data.append({
            'Time': time_ago,
            'Symbol': s['symbol'],
            'Signal': f"{signal_emoji} {s['signal']}",
            'Quality': f"{s['quality']:.0f}",
            'Regime': s['regime'],
        })
    
    df = pd.DataFrame(data)
    st.dataframe(df, use_container_width=True, hide_index=True, height=300)


def render_sidebar(metrics: Dict, ks_status: Dict):
    """Render sidebar with summary info."""
    with st.sidebar:
        st.markdown("### 📊 Quick Stats")
        
        # Kill switch status
        if ks_status.get('active'):
            st.error("🚨 Kill Switch ACTIVE")
        else:
            st.success("✅ System Running")
        
        st.divider()
        
        # Performance summary
        st.markdown("**30-Day Performance**")
        
        pnl = metrics['total_pnl']
        if pnl >= 0:
            st.markdown(f"💰 PnL: **:green[+${pnl:,.2f}]**")
        else:
            st.markdown(f"💰 PnL: **:red[-${abs(pnl):,.2f}]**")
        
        st.markdown(f"📈 Win Rate: **{metrics['win_rate']:.1f}%**")
        st.markdown(f"🎯 Trades: **{metrics['total_trades']}**")
        st.markdown(f"⚖️ Profit Factor: **{metrics['profit_factor']:.2f}**")
        
        st.divider()
        
        # Manual refresh button
        st.markdown("**Settings**")
        
        if st.button("🔄 Refresh Now", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
        
        st.markdown("""
            <div class="refresh-info">
                Data refreshes every 15 seconds
            </div>
        """, unsafe_allow_html=True)


# =============================================================================
# MAIN APP
# =============================================================================

def main():
    """Main dashboard entry point."""
    # Check DB connection
    if not safe_db_connect():
        st.error("⚠️ Cannot connect to database. Please check your configuration.")
        st.stop()
    
    # Load all data
    account = load_account_state()
    positions = load_positions()
    metrics = load_performance_metrics()
    recent_trades = load_recent_trades()
    coins = load_coins_analysis()
    signals = load_recent_signals()
    ks_status = get_kill_switch_status()
    
    # Render sidebar
    render_sidebar(metrics, ks_status)
    
    # Main content
    render_header()
    render_kill_switch_warning(ks_status)
    
    # Account overview
    render_account_metrics(account, positions)
    
    # Two-column layout for positions and performance
    col_left, col_right = st.columns([1, 1])
    
    with col_left:
        render_positions(positions)
    
    with col_right:
        render_performance(metrics)
        render_recent_trades(recent_trades)
    
    # Full-width sections
    st.markdown("---")
    
    # Tabs for scanner and signals
    tab1, tab2 = st.tabs(["🔍 Coin Scanner", "⚡ Recent Signals"])
    
    with tab1:
        render_coin_scanner(coins)
    
    with tab2:
        render_recent_signals(signals)
    
    # Footer
    st.markdown("""
        <div class="refresh-info">
            Dashboard auto-refreshes every 15 seconds • Data from PostgreSQL
        </div>
    """, unsafe_allow_html=True)


if __name__ == "__main__":
    main()
else:
    # When run via streamlit run
    main()
