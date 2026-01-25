from src.storage.db import get_db, Base
from sqlalchemy import text
import os
from dotenv import load_dotenv

# Load environment variables from files only if they exist (for local dev)
# In production (DigitalOcean), env vars are already set in the runtime environment
if os.path.exists(".env.local"):
    load_dotenv(".env.local")
elif os.path.exists(".env"):
    load_dotenv(".env")

def migrate():
    """Run PostgreSQL schema migrations."""
    print("Running schema migration...")
    db_url = os.environ.get('DATABASE_URL')
    
    if not db_url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set. "
            "In production, this should be set by DigitalOcean App Platform. "
            "For local development, ensure .env.local or .env contains DATABASE_URL."
        )
    
    # Mask sensitive parts of the URL for logging
    if '@' in db_url:
        masked = db_url.split('@')[0].split('://')[0] + '://***@' + '@'.join(db_url.split('@')[1:])
        print(f"Target Database: {masked[:80]}...")
    else:
        print(f"Target Database: {db_url[:80]}...")

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
    try:
        migrate()
    except RuntimeError as e:
        print(f"❌ Migration failed: {e}")
        # Print environment diagnostics
        print("\nEnvironment diagnostics:")
        print(f"  DATABASE_URL present: {bool(os.environ.get('DATABASE_URL'))}")
        print(f"  ENVIRONMENT: {os.environ.get('ENVIRONMENT', 'NOT SET')}")
        print(f"  Available env vars starting with 'DATABASE': {[k for k in os.environ.keys() if 'DATABASE' in k]}")
        raise
    except Exception as e:
        print(f"❌ Unexpected error during migration: {e}")
        import traceback
        traceback.print_exc()
        raise
