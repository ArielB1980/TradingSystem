"""
System-wide constants for the trading system.

Centralizes magic numbers and configuration values used across modules.
"""

# API Configuration
KRAKEN_FUTURES_BASE_URL = "https://futures.kraken.com/derivatives"
KRAKEN_SPOT_BASE_URL = "https://api.kraken.com"

# API Endpoints
FUTURES_OPENPOSITIONS_ENDPOINT = "/api/v3/openpositions"
FUTURES_TICKERS_ENDPOINT = "/api/v3/tickers"
FUTURES_INSTRUMENTS_ENDPOINT = "/api/v3/instruments"

# Rate Limiting
PUBLIC_API_CAPACITY = 20
PUBLIC_API_REFILL_RATE = 1.0  # requests per second
PRIVATE_API_CAPACITY = 20
PRIVATE_API_REFILL_RATE = 0.33  # ~20 per minute

# Timeouts and Retries
DEFAULT_API_TIMEOUT = 30  # seconds
MAX_RETRY_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2

# Position Management
DEFAULT_LEVERAGE = 5.0
MAX_LEVERAGE = 10.0
ACCOUNT_SYNC_INTERVAL = 15  # seconds

# Data Acquisition
WARMUP_LOOKBACK_DAYS = 200
CANDLE_BUFFER_SIZE = 500

# Logging
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
