#!/usr/bin/env python3
"""
Fix position table schema for PostgreSQL by adding missing columns.

This script adds missing columns to the positions table using raw SQL.
"""
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from storage.db import get_db
import structlog

logger = structlog.get_logger()


def fix_position_schema_postgres():
    """Add missing columns to positions table (PostgreSQL)."""
    db = get_db()

    # Get a raw connection
    connection = db.engine.raw_connection()
    cursor = connection.cursor()

    try:
        # Check which columns exist
        cursor.execute("""
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = 'positions'
        """)
        existing_columns = {row[0] for row in cursor.fetchall()}

        print(f"Existing columns: {existing_columns}")

        # Define columns to add
        columns_to_add = [
            ("trade_type", "VARCHAR"),
            ("partial_close_pct", "NUMERIC(10, 4) DEFAULT 0.5"),
            ("original_size", "NUMERIC(20, 8)"),
            ("tp_order_ids", "TEXT"),  # JSON array
            ("basis_at_entry", "NUMERIC(10, 4)"),
            ("basis_current", "NUMERIC(10, 4)"),
            ("funding_rate", "NUMERIC(10, 6)"),
            ("cumulative_funding", "NUMERIC(20, 2) DEFAULT 0"),
        ]

        # Add missing columns
        for column_name, column_type in columns_to_add:
            if column_name not in existing_columns:
                try:
                    sql = f"ALTER TABLE positions ADD COLUMN {column_name} {column_type}"
                    print(f"Adding column: {sql}")
                    cursor.execute(sql)
                    connection.commit()
                    print(f"✓ Added column: {column_name}")
                except Exception as e:
                    print(f"⚠ Error adding {column_name}: {e}")
                    connection.rollback()
            else:
                print(f"✓ Column {column_name} already exists")

        cursor.close()
        connection.close()
        print("\n✓ Schema migration complete!")
        return True

    except Exception as e:
        print(f"\n✗ Migration failed: {e}")
        import traceback
        traceback.print_exc()
        cursor.close()
        connection.close()
        return False


if __name__ == "__main__":
    print("="*60)
    print("Position Table Schema Fix (PostgreSQL)")
    print("="*60)

    success = fix_position_schema_postgres()

    if success:
        print("\n✓ Database schema updated successfully!")
        sys.exit(0)
    else:
        print("\n✗ Schema update failed!")
        sys.exit(1)
