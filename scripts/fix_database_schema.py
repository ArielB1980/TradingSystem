#!/usr/bin/env python3
"""
Fix database schema by adding missing columns to positions table.

Adds:
- is_protected (BOOLEAN, default False)
- protection_reason (VARCHAR, nullable)
"""
import sys
import os
from sqlalchemy import text
from pathlib import Path

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load environment variables from .env.local if it exists
env_local = Path(__file__).parent.parent / ".env.local"
if env_local.exists():
    from dotenv import load_dotenv
    load_dotenv(env_local)

from src.storage.db import get_db
from src.monitoring.logger import get_logger, setup_logging

logger = get_logger(__name__)
setup_logging("INFO", "json")


def fix_position_schema():
    """Add missing columns to positions table."""
    db = get_db()
    
    print("=" * 80)
    print("DATABASE SCHEMA FIX")
    print("=" * 80)
    print("\nAdding missing columns to positions table...")
    print()
    
    with db.get_session() as session:
        try:
            # Check if columns already exist
            result = session.execute(text("""
                SELECT column_name 
                FROM information_schema.columns 
                WHERE table_name = 'positions' 
                AND column_name IN ('is_protected', 'protection_reason')
            """))
            existing_columns = {row[0] for row in result}
            
            # Add is_protected if missing
            if 'is_protected' not in existing_columns:
                print("Adding column: is_protected")
                session.execute(text("""
                    ALTER TABLE positions 
                    ADD COLUMN is_protected BOOLEAN NOT NULL DEFAULT FALSE
                """))
                session.commit()
                print("✅ Added is_protected column")
            else:
                print("✅ is_protected column already exists")
            
            # Add protection_reason if missing
            if 'protection_reason' not in existing_columns:
                print("Adding column: protection_reason")
                session.execute(text("""
                    ALTER TABLE positions 
                    ADD COLUMN protection_reason VARCHAR
                """))
                session.commit()
                print("✅ Added protection_reason column")
            else:
                print("✅ protection_reason column already exists")
            
            print()
            print("=" * 80)
            print("✅ DATABASE SCHEMA FIX COMPLETE")
            print("=" * 80)
            print()
            print("The positions table now has all required columns.")
            print("Live trading should be able to sync positions without errors.")
            print()
            
        except Exception as e:
            session.rollback()
            logger.error("Failed to fix database schema", error=str(e))
            print()
            print("=" * 80)
            print("❌ ERROR: Failed to fix database schema")
            print("=" * 80)
            print(f"Error: {e}")
            print()
            import traceback
            traceback.print_exc()
            sys.exit(1)


if __name__ == "__main__":
    try:
        fix_position_schema()
    except Exception as e:
        logger.error("Script failed", error=str(e))
        import traceback
        traceback.print_exc()
        sys.exit(1)
