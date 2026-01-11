"""
Custom exception hierarchy for the trading system.

Provides clear, specific exceptions for different error scenarios
to improve error handling and debugging.
"""


class TradingSystemError(Exception):
    """Base exception for all trading system errors."""
    pass


class APIError(TradingSystemError):
    """Base exception for API-related errors."""
    pass


class AuthenticationError(APIError):
    """Raised when API authentication fails."""
    pass


class RateLimitError(APIError):
    """Raised when API rate limit is exceeded."""
    pass


class PositionLimitError(TradingSystemError):
    """Raised when position limits would be exceeded."""
    pass


class DataAcquisitionError(TradingSystemError):
    """Raised when data acquisition fails."""
    pass


class OrderExecutionError(TradingSystemError):
    """Raised when order execution fails."""
    pass


class ValidationError(TradingSystemError):
    """Raised when validation checks fail."""
    pass
