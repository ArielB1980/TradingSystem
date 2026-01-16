from src.storage.db import get_db, Base
from sqlalchemy import text

def migrate():
    print("Running schema migration...")
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

if __name__ == "__main__":
    migrate()
