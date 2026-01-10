"""
Database engine and session management.

Supports both PostgreSQL and SQLite with SQLAlchemy ORM.
"""
from sqlalchemy import create_engine, event
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
            database_url: Database connection string
        """
        self.database_url = database_url
        self.engine = create_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,  # Verify connections before using
        )
        
        # Enable foreign key constraints for SQLite
        if database_url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def set_sqlite_pragma(dbapi_conn, connection_record):
                cursor = dbapi_conn.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()
        
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
    
    def create_all(self):
        """Create all tables."""
        Base.metadata.create_all(bind=self.engine)
    
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
    """Get or create the global database instance."""
    global _db_instance
    if _db_instance is None:
        database_url = os.getenv("DATABASE_URL", "sqlite:///./trading.db")
        _db_instance = Database(database_url)
        _db_instance.create_all()
    return _db_instance


def init_db(database_url: str) -> Database:
    """
    Initialize database with specific URL.
    
    Args:
        database_url: Database connection string
    
    Returns:
        Database instance
    """
    global _db_instance
    _db_instance = Database(database_url)
    _db_instance.create_all()
    return _db_instance
