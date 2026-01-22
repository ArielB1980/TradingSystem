from src.storage.db import get_db, Base
from sqlalchemy import text
import os
from dotenv import load_dotenv

# Load environment variables (try .env.local first, then .env)
load_dotenv(".env.local")
load_dotenv(".env")

def migrate():
    """Run PostgreSQL schema migrations."""
    print("Running schema migration...")
    db_url = os.environ.get('DATABASE_URL', 'NOT SET')
    print(f"Target Database: {db_url[:50]}...")

    db = get_db()
    engine = db.engine

    # Verify PostgreSQL
    dialect = engine.dialect.name
    if dialect != "postgresql":
        raise RuntimeError(f"Expected PostgreSQL, got {dialect}. Update DATABASE_URL.")

    with engine.connect() as conn:
        # 1. Alter candle columns for higher precision
        print("Applying candle column alterations...")
        for col in ['open', 'high', 'low', 'close', 'volume']:
            conn.execute(text(f"ALTER TABLE candles ALTER COLUMN {col} TYPE NUMERIC(30, 10);"))
            print(f"  ✓ candles.{col}")
        conn.commit()

        # 2. Add new columns to positions table if they don't exist
        print("Adding position columns...")
        new_cols = [
            ("trade_type", "VARCHAR"),
            ("partial_close_pct", "NUMERIC(5, 2)"),
            ("original_size", "NUMERIC(20, 8)"),
            ("tp_order_ids", "VARCHAR"),
            ("basis_at_entry", "NUMERIC(20, 8)"),
            ("basis_current", "NUMERIC(20, 8)"),
            ("funding_rate", "NUMERIC(20, 8)"),
            ("cumulative_funding", "NUMERIC(20, 8)"),
        ]
        for col_name, col_type in new_cols:
            try:
                conn.execute(text(f"ALTER TABLE positions ADD COLUMN IF NOT EXISTS {col_name} {col_type};"))
                print(f"  ✓ positions.{col_name}")
            except Exception as e:
                print(f"  ⚠ positions.{col_name}: {e}")
        conn.commit()

    print("Migration complete!")

if __name__ == "__main__":
    migrate()
