from src.storage.db import get_db, Base
from sqlalchemy import text
import os
from dotenv import load_dotenv

# Load environment variables (try .env.local first, then .env)
load_dotenv(".env.local")
load_dotenv(".env")

def migrate():
    print("Running schema migration...")
    print(f"Target Database: {os.environ.get('DATABASE_URL', 'default (sqlite)')}")
    db = get_db()
    engine = db.engine
    
    # We need to alter the columns. SQL alchemy doesn't do this automatically with create_all
    # Direct SQL is safest for cross-db support, but syntax differs. 
    # Since we are on Postgres (Production) and SQLite (Local), we check first.
    
    with engine.connect() as conn:
        # Check dialect
        dialect = engine.dialect.name
        print(f"Detected database dialect: {dialect}")
        
        if dialect == "postgresql":
            print("Applying PostgreSQL column alterations...")
            conn.execute(text("ALTER TABLE candles ALTER COLUMN open TYPE NUMERIC(30, 10);"))
            conn.execute(text("ALTER TABLE candles ALTER COLUMN high TYPE NUMERIC(30, 10);"))
            conn.execute(text("ALTER TABLE candles ALTER COLUMN low TYPE NUMERIC(30, 10);"))
            conn.execute(text("ALTER TABLE candles ALTER COLUMN close TYPE NUMERIC(30, 10);"))
            conn.execute(text("ALTER TABLE candles ALTER COLUMN volume TYPE NUMERIC(30, 10);"))
            conn.commit()
            print("Migration complete!")
        else:
            print("SQLite does not support direct ALTER COLUMN. Skipping (local dev uses different file mostly).")
            # For SQLite, it's dynamic typing anyway, so it usually just works, 
            # or requires a full table rebuild which is overkill for local dev right now.

    # 2. Add new columns to positions table if they don't exist
    # This block handles adding new columns (safe to run if they exist with safeguards, or just let it fail/catch)
    with engine.connect() as conn:
        dialect = engine.dialect.name

        # List of new columns: name, type
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

        if dialect == "postgresql":
            print("Checking/Adding new columns to positions table (PostgreSQL)...")
            for col_name, col_type in new_cols:
                try:
                    conn.execute(text(f"ALTER TABLE positions ADD COLUMN IF NOT EXISTS {col_name} {col_type};"))
                    print(f"  ✓ {col_name}")
                except Exception as e:
                    print(f"  ⚠ {col_name}: {e}")
            conn.commit()

        elif dialect == "sqlite":
            print("Checking/Adding new columns to positions table (SQLite)...")
            # Get existing columns
            result = conn.execute(text("PRAGMA table_info(positions)"))
            existing_cols = {row[1] for row in result.fetchall()}
            print(f"  Existing columns: {len(existing_cols)}")

            for col_name, col_type in new_cols:
                if col_name not in existing_cols:
                    try:
                        conn.execute(text(f"ALTER TABLE positions ADD COLUMN {col_name} {col_type}"))
                        conn.commit()
                        print(f"  ✓ Added {col_name}")
                    except Exception as e:
                        print(f"  ⚠ {col_name}: {e}")
                else:
                    print(f"  - {col_name} exists")

        print("Column addition complete!")

if __name__ == "__main__":
    migrate()
