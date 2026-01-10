"""
Streamlit-based Multi-Asset Trading Dashboard.

Professional operational dashboard for monitoring and managing
the multi-asset SMC trading system.
"""
import streamlit as st
from datetime import datetime, timezone

# Page config
st.set_page_config(
    page_title="SMC Trading Dashboard",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for dark mode and better styling
st.markdown("""
<style>
    .stMetric {
        background-color: #1E1E1E;
        padding: 10px;
        border-radius: 5px;
    }
    .stAlert {
        padding: 10px;
    }
    div[data-testid="stMetricValue"] {
        font-size: 24px;
    }
</style>
""", unsafe_allow_html=True)

# Sidebar navigation
st.sidebar.title("📊 SMC Trading Dashboard")
st.sidebar.markdown("---")

page = st.sidebar.radio(
    "Navigation",
    ["Portfolio Overview", "Coin Matrix", "Coin Detail", "Execution Monitor", "Performance"]
)

st.sidebar.markdown("---")
st.sidebar.caption(f"Last Updated: {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")

# Main content
if page == "Portfolio Overview":
    st.title("Portfolio Overview")
    st.info("📌 System State, Positions, and Recent Events")
    
    # Placeholder for now
    st.warning("🚧 Under Construction - Portfolio overview coming soon")
    
    # Basic metrics placeholder
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Mode", "PAPER", delta="Safe")
    with col2:
        st.metric("Equity", "$10,000", delta="+$150")
    with col3:
        st.metric("Positions", "2 / 3", delta="Available: 1")
    with col4:
        st.metric("Daily PnL", "+$150", delta="+1.5%")

elif page == "Coin Matrix":
    st.title("🎯 Coin Matrix - Multi-Asset Opinion Board")
    st.info("📌 Real-time view of system's opinion on every tracked coin")
    
    # Placeholder for now
    st.warning("🚧 Under Construction - Coin Matrix coming soon")
    
    # Sample filters
    col1, col2, col3, col4 = st.columns(4)
    with col1:
        show_filter = st.selectbox("Show", ["All", "ENTER Candidates", "BLOCKED", "In Position"])
    with col2:
        min_quality = st.slider("Min Quality", 0, 100, 0)
    with col3:
        max_basis = st.slider("Max Basis %", 0.0, 2.0, 2.0, 0.1)
    
    st.info("Sample data will appear here once MultiAssetOrchestrator is emitting snapshots")

elif page == "Coin Detail":
    st.title("Coin Detail - Deep Dive")
    st.info("📌 Full reasoning transparency for a single coin")
    
    symbol = st.text_input("Enter Symbol", "BTC/USD")
    st.warning(f"🚧 Under Construction - {symbol} detail view coming soon")

elif page == "Execution Monitor":
    st.title("Execution Monitor")
    st.info("📌 Real-time order flow and system operations")
    
    st.warning("🚧 Under Construction - Execution monitor coming soon")

elif page == "Performance":
    st.title("Performance & Attribution")
    st.info("📌 Portfolio and per-coin performance metrics")
    
    st.warning("🚧 Under Construction - Performance dashboard coming soon")

# Footer
st.sidebar.markdown("---")
st.sidebar.markdown("**System Status**")
st.sidebar.success("✅ Spot Feed: Healthy")
st.sidebar.success("✅ Futures Feed: Healthy")
st.sidebar.success("✅ Database: Connected")
st.sidebar.info("🔵 Kill Switch: ARMED")
