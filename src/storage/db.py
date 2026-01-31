"""
Database engine and session management.

PostgreSQL only - SQLite is no longer supported.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from contextlib import contextmanager
from typing import Generator
import os

# Base class for ORM models
Base = declarative_base()


class Database:
    """Database engine and session manager."""

    def __init__(self, database_url: str):
        """
        Initialize database connection.

        Args:
            database_url: PostgreSQL connection string
        """
        if not database_url.startswith("postgresql"):
            raise ValueError(
                f"Only PostgreSQL is supported. Got: {database_url[:30]}... "
                "Set DATABASE_URL to a postgresql:// connection string."
            )

        self.database_url = database_url

        # Optimized connection pooling for PostgreSQL
        self.engine = create_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,  # Verify connections before using
            pool_size=10,  # Base connections
            max_overflow=20,  # Additional connections under load
            pool_recycle=3600,  # Recycle connections after 1 hour
            pool_timeout=30,  # Wait up to 30s for connection
        )

        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)

    def create_all(self):
        """Create all tables."""
        try:
            Base.metadata.create_all(bind=self.engine)
        except Exception as e:
            error_str = str(e).lower()
            # Log but don't fail for permission errors - these need manual fix
            import logging
            if "permission denied" in error_str or "insufficientprivilege" in error_str:
                logging.warning(
                    f"Table creation failed due to insufficient privileges: {e}\n"
                    "Grant CREATE privilege on schema 'public' to your database user.\n"
                    "The app will continue but database operations will fail until fixed."
                )
                # Don't re-raise - app can start but DB ops will fail
                return
            elif "already exists" in error_str or "duplicate" in error_str:
                # Tables already exist - this is fine
                logging.debug(f"Tables already exist (expected): {e}")
                return
            else:
                # Other errors - log and re-raise
                logging.warning(f"Table creation warning (may be expected): {e}")
                raise

    def drop_all(self):
        """Drop all tables (use with caution!)."""
        Base.metadata.drop_all(bind=self.engine)

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """
        Context manager for database sessions.

        Yields:
            SQLAlchemy Session

        Example:
            with db.get_session() as session:
                session.add(obj)
                session.commit()
        """
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# Global database instance (initialized on first use)
_db_instance: Database | None = None


def get_db() -> Database:
    """
    Get or create the global database instance.
    
    Uses lazy validation with retry logic for cloud platform secret injection.
    """
    global _db_instance
    if _db_instance is None:
        # CRITICAL: Import all ORM models BEFORE creating database instance
        # This ensures Base.metadata contains all table definitions
        # Import is idempotent (safe to import multiple times)
        import src.storage.repository  # This imports all models (CandleModel, TradeModel, etc.)
        
        # Use lazy validation with retry logic for cloud platforms
        from src.utils.secret_manager import get_database_url
        from src.monitoring.logger import get_logger
        from urllib.parse import urlparse
        import os
        
        logger = get_logger(__name__)
        database_url = get_database_url()
        
        # CRITICAL: Log database connection details (without password) for debugging
        try:
            parsed = urlparse(database_url)
            db_host = parsed.hostname or "unknown"
            db_port = parsed.port or 5432
            db_name = parsed.path.lstrip('/') or "unknown"
            db_user = parsed.username or "unknown"
            
            # Informational: connection target (no password) for debugging deployments.
            logger.info(
                "DATABASE_CONNECTION_INIT",
                host=db_host,
                port=db_port,
                database=db_name,
                user=db_user,
                has_password=bool(parsed.password)
            )
            
            # CRITICAL: Fail fast if test_db is detected in production
            if "test_db" in db_name.lower():
                env = os.getenv("ENVIRONMENT", "unknown")
                if env == "prod" or not os.getenv("ENVIRONMENT"):
                    logger.critical(
                        "CRITICAL: test_db detected in production!",
                        database=db_name,
                        environment=env,
                        database_url_preview=database_url[:50] + "..." if len(database_url) > 50 else database_url
                    )
                    raise ValueError(
                        f"CRITICAL: Database 'test_db' detected in production environment. "
                        f"Check DATABASE_URL environment variable. "
                        f"Current database: {db_name}, Environment: {env}"
                    )
        except Exception as e:
            logger.warning("Failed to parse DATABASE_URL for logging", error=str(e))
        
        _db_instance = Database(database_url)
        _db_instance.create_all()  # Create all tables on first connection
    return _db_instance


def init_db(database_url: str) -> Database:
    """
    Initialize database with specific URL.

    Args:
        database_url: PostgreSQL connection string

    Returns:
        Database instance
    """
    global _db_instance
    _db_instance = Database(database_url)
    _db_instance.create_all()
    return _db_instance
