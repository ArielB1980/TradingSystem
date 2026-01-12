
import asyncio
import functools
import logging
import random
from typing import Type, Tuple, Optional, Callable, Any

from src.monitoring.logger import get_logger

logger = get_logger(__name__)

def retry_on_transient_errors(
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_backoff: float = 10.0,
    transient_errors: Optional[Tuple[Type[Exception], ...]] = None
):
    """
    Decorator to retry async functions on transient errors.
    
    Implements exponential backoff with jitter.
    
    Args:
        max_retries: Maximum number of retry attempts
        base_delay: Initial wait time in seconds (replaces initial_backoff)
        max_backoff: Maximum wait time in seconds
        transient_errors: Tuple of exception types to retry on.
                          Defaults to generic Exception if None (use properly in prod).
    """
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            retry_count = 0
            backoff = base_delay
            
            while True:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    # Determine if we should retry
                    # For now, we assume most exceptions in API calls are transient 
                    # unless it's a clear logic error (ValueError, TypeError)
                    # In a refined version, checking for ccxt.NetworkError is better.
                    
                    is_transient = True
                    if isinstance(e, (ValueError, TypeError, SyntaxError)):
                        is_transient = False
                    
                    if transient_errors and not isinstance(e, transient_errors):
                        is_transient = False
                        
                    if not is_transient:
                        raise
                    
                    if retry_count >= max_retries:
                        logger.warning(
                            f"Max retries ({max_retries}) exhausted for {func.__name__}",
                            error=str(e)
                        )
                        raise
                    
                    # Log retry
                    logger.warning(
                        f"Transient error in {func.__name__}, retrying ({retry_count + 1}/{max_retries})",
                        error=str(e),
                        wait=f"{backoff:.2f}s"
                    )
                    
                    # Wait with backoff
                    await asyncio.sleep(backoff)
                    
                    # Exponential backoff with jitter
                    retry_count += 1
                    backoff = min(backoff * 2, max_backoff)
                    backoff += random.uniform(0, 0.5)  # Jitter
                    
        return wrapper
    return decorator
