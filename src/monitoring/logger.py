"""
Structured logging setup for the trading system.

Uses structlog for JSON-formatted context-aware logging with all required fields.
"""
import structlog
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone


def setup_logging(log_level: str = "INFO", log_format: str = "json", log_file: str | None = None) -> None:
    """
    Configure structured logging for the application.
    
    Args:
        log_level: Logging level (DEBUG/INFO/WARNING/ERROR/CRITICAL)
        log_format: Format (json or text)
        log_file: Optional log file path
    """
    # Set log level
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, log_level.upper()),
    )
    
    # Configure structlog processors
    processors = [
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.contextvars.merge_contextvars,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    
    if log_format == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())
    
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
    
    # Add file handler if specified
    if log_file:
        from logging.handlers import RotatingFileHandler
        
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Rotating File Handler (10MB limit, 5 backups)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024, # 10MB
            backupCount=5
        )
        file_handler.setLevel(getattr(logging, log_level.upper()))
        logging.root.addHandler(file_handler)


def get_logger(name: str) -> structlog.BoundLogger:
    """
    Get a structured logger instance.
    
    Args:
        name: Logger name (typically __name__)
    
    Returns:
        Structured logger instance
    """
    return structlog.get_logger(name)
