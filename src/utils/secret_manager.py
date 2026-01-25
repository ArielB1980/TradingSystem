"""
Secret management utilities with lazy validation and retry logic.

Handles cloud platform secret injection timing issues by:
- Validating secrets when actually needed (lazy validation)
- Retrying with exponential backoff for secret availability
- Providing clear error messages distinguishing timing vs configuration issues
"""
import os
import time
from typing import Optional, Callable, Any
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def is_cloud_platform() -> bool:
    """
    Detect if running in a cloud platform environment.
    
    Returns:
        True if running in DigitalOcean App Platform or similar cloud environment
    """
    # DigitalOcean App Platform indicators
    if os.getenv("DIGITALOCEAN_APP_ID"):
        return True
    if os.path.exists("/workspace"):
        return True
    
    # AWS Lambda
    if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
        return True
    
    # Google Cloud Run
    if os.getenv("K_SERVICE"):
        return True
    
    # Heroku
    if os.getenv("DYNO"):
        return True
    
    return False


def get_secret_with_retry(
    secret_name: str,
    max_retries: int = 5,
    initial_delay: float = 1.0,
    max_delay: float = 5.0,
    backoff_factor: float = 1.5,
    validator: Optional[Callable[[str], bool]] = None
) -> str:
    """
    Get a secret from environment with retry logic for cloud platforms.
    
    In cloud platforms, secrets may be injected asynchronously after container
    startup. This function waits for secrets to become available with exponential
    backoff.
    
    Args:
        secret_name: Name of the environment variable
        max_retries: Maximum number of retry attempts
        initial_delay: Initial delay between retries in seconds
        max_delay: Maximum delay between retries in seconds
        backoff_factor: Multiplier for exponential backoff
        validator: Optional function to validate the secret value (returns True if valid)
    
    Returns:
        The secret value as a string
    
    Raises:
        RuntimeError: If secret is not available after all retries
        ValueError: If secret value fails validation
    """
    is_cloud = is_cloud_platform()
    delay = initial_delay
    
    for attempt in range(max_retries):
        value = os.getenv(secret_name)
        
        # Check if value exists and is not empty
        if value and value.strip():
            # Run validator if provided
            if validator:
                if not validator(value):
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Secret {secret_name} failed validation, retrying...",
                            attempt=attempt + 1,
                            max_retries=max_retries
                        )
                        time.sleep(delay)
                        delay = min(delay * backoff_factor, max_delay)
                        continue
                    else:
                        raise ValueError(
                            f"Secret {secret_name} failed validation after {max_retries} attempts. "
                            f"Please check the value in your environment configuration."
                        )
            
            # Success - log if we had to retry
            if attempt > 0:
                logger.info(
                    f"Secret {secret_name} became available after {attempt} retry attempts",
                    total_attempts=attempt + 1
                )
            return value
        
        # Secret not available yet
        if attempt < max_retries - 1:
            if is_cloud:
                logger.debug(
                    f"Secret {secret_name} not yet available (cloud platform secret injection in progress)",
                    attempt=attempt + 1,
                    max_retries=max_retries,
                    next_retry_seconds=delay
                )
            else:
                # In local dev, fail faster
                if attempt == 0:
                    logger.warning(
                        f"Secret {secret_name} not available. Retrying once for local development...",
                        attempt=attempt + 1
                    )
                else:
                    # Second attempt in local - fail
                    break
            
            time.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)
        else:
            # Last attempt failed
            break
    
    # All retries exhausted
    if is_cloud:
        error_msg = (
            f"Secret {secret_name} is not available after {max_retries} retry attempts. "
            f"This may indicate:\n"
            f"  1. Secret injection timing issue (secrets may be injected later)\n"
            f"  2. Secret not configured in app spec (check .do/app.yaml)\n"
            f"  3. Secret scope mismatch (ensure scope is RUN_TIME for runtime secrets)\n\n"
            f"Please verify the secret is configured correctly in your deployment platform."
        )
    else:
        error_msg = (
            f"Secret {secret_name} is required but not set. "
            f"Set it in your environment or .env.local file.\n"
            f"Example: export {secret_name}=your_value"
        )
    
    raise RuntimeError(error_msg)


def get_database_url() -> str:
    """
    Get DATABASE_URL with lazy validation and retry logic.
    
    Returns:
        PostgreSQL connection string
    
    Raises:
        RuntimeError: If DATABASE_URL is not available after retries
        ValueError: If DATABASE_URL is not a PostgreSQL connection string
    """
    def validate_db_url(url: str) -> bool:
        """Validate that URL is a PostgreSQL connection string."""
        if not url.startswith("postgresql://") and not url.startswith("postgresql+psycopg2://"):
            return False
        return True
    
    url = get_secret_with_retry(
        "DATABASE_URL",
        max_retries=5,
        initial_delay=1.0,
        validator=validate_db_url
    )
    
    return url


def get_kraken_api_key() -> str:
    """
    Get KRAKEN_FUTURES_API_KEY with lazy validation and retry logic.
    
    Returns:
        Kraken Futures API key
    
    Raises:
        RuntimeError: If API key is not available after retries
    """
    return get_secret_with_retry(
        "KRAKEN_FUTURES_API_KEY",
        max_retries=5,
        initial_delay=1.0
    )


def get_kraken_api_secret() -> str:
    """
    Get KRAKEN_FUTURES_API_SECRET with lazy validation and retry logic.
    
    Returns:
        Kraken Futures API secret
    
    Raises:
        RuntimeError: If API secret is not available after retries
    """
    return get_secret_with_retry(
        "KRAKEN_FUTURES_API_SECRET",
        max_retries=5,
        initial_delay=1.0
    )


def check_secret_availability(secret_name: str) -> tuple[bool, Optional[str]]:
    """
    Check if a secret is available without raising exceptions.
    
    Args:
        secret_name: Name of the environment variable
    
    Returns:
        Tuple of (is_available, error_message)
        - is_available: True if secret is available and non-empty
        - error_message: None if available, error description if not
    """
    value = os.getenv(secret_name)
    
    if not value or not value.strip():
        is_cloud = is_cloud_platform()
        if is_cloud:
            return (False, f"{secret_name} not yet available (cloud platform secret injection may be in progress)")
        else:
            return (False, f"{secret_name} not set (required for local development)")
    
    return (True, None)


def get_environment() -> str:
    """
    Get environment name, standardizing on ENVIRONMENT variable.
    
    Returns:
        Environment name (default: "prod")
    """
    # Standardize on ENVIRONMENT (not ENV)
    return os.getenv("ENVIRONMENT", "prod")
