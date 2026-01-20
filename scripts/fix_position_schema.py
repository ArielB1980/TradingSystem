#!/usr/bin/env python3
"""
Fix position table schema by adding missing columns.

This script adds the following missing columns to the positions table:
- trade_type
- partial_close_pct
- original_size
- tp_order_ids
- basis_at_entry
- basis_current
- funding_rate
- cumulative_funding
"""
import sqlite3
import os
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from storage.db import get_db
from decimal import Decimal


def fix_position_schema():
    """Add missing columns to positions table."""
    db = get_db()

    # For SQLite, we need to use ALTER TABLE
    if db.database_url.startswith("sqlite"):
        connection = db.engine.raw_connection()
        cursor = connection.cursor()

        # Check which columns exist
        cursor.execute("PRAGMA table_info(positions)")
        existing_columns = {row[1] for row in cursor.fetchall()}

        print(f"Existing columns: {existing_columns}")

        # Add missing columns one by one
        columns_to_add = [
            ("trade_type", "VARCHAR"),
            ("partial_close_pct", "NUMERIC(10, 4) DEFAULT 0.5"),
            ("original_size", "NUMERIC(20, 8)"),
            ("tp_order_ids", "TEXT"),  # Will store JSON array
            ("basis_at_entry", "NUMERIC(10, 4)"),
            ("basis_current", "NUMERIC(10, 4)"),
            ("funding_rate", "NUMERIC(10, 6)"),
            ("cumulative_funding", "NUMERIC(20, 2) DEFAULT 0"),
        ]

        for column_name, column_type in columns_to_add:
            if column_name not in existing_columns:
                try:
                    sql = f"ALTER TABLE positions ADD COLUMN {column_name} {column_type}"
                    print(f"Adding column: {sql}")
                    cursor.execute(sql)
                    connection.commit()
                    print(f"✓ Added column: {column_name}")
                except sqlite3.OperationalError as e:
                    print(f"⚠ Column {column_name} might already exist or error: {e}")

        cursor.close()
        connection.close()
        print("\n✓ Schema migration complete!")

    else:
        print("PostgreSQL migrations not implemented yet. Please use Alembic for production.")
        return False

    return True


if __name__ == "__main__":
    print("="*60)
    print("Position Table Schema Fix")
    print("="*60)

    success = fix_position_schema()

    if success:
        print("\n✓ Database schema updated successfully!")
        sys.exit(0)
    else:
        print("\n✗ Schema update failed!")
        sys.exit(1)
