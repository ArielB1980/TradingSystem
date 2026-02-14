#!/usr/bin/env python3
"""Quick test to see if database permissions work after production upgrade."""
import sys
import os
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# Set DATABASE_URL if provided
if len(sys.argv) > 1:
    os.environ['DATABASE_URL'] = sys.argv[1]

from src.storage.db import get_db, Base
from src.storage.repository import (
    CandleModel, TradeModel, PositionModel, SystemEventModel, AccountStateModel
)
from sqlalchemy import inspect, text

print("=" * 60)
print("Quick Database Permissions Test")
print("=" * 60)

try:
    # Import models
    import src.storage.repository
    
    print("\n1. Connecting...")
    db = get_db()
    print("   ✅ Connected")
    
    print("\n2. Testing CREATE TABLE permission...")
    try:
        with db.get_session() as session:
            # Try to create a test table
            session.execute(text("""
                CREATE TABLE IF NOT EXISTS _test_permission (
                    id SERIAL PRIMARY KEY,
                    test VARCHAR(10)
                );
            """))
            session.commit()
            print("   ✅ CREATE TABLE works!")
            
            # Clean up
            session.execute(text("DROP TABLE IF EXISTS _test_permission;"))
            session.commit()
            print("   ✅ Test table cleaned up")
            
            # Now try creating actual tables
            print("\n3. Creating application tables...")
            db.create_all()
            print("   ✅ create_all() completed")
            
            # Verify tables
            inspector = inspect(db.engine)
            tables = inspector.get_table_names()
            required = ["candles", "trades", "positions", "system_events", "account_state"]
            found = [t for t in required if t in tables]
            
            print(f"\n4. Verification:")
            print(f"   Required tables: {required}")
            print(f"   Found tables: {found}")
            
            if len(found) == len(required):
                print("\n" + "=" * 60)
                print("✅ SUCCESS! All tables created!")
                print("=" * 60)
                print("\nYour database is ready. Restart your app.")
            else:
                missing = [t for t in required if t not in found]
                print(f"\n⚠️  Missing tables: {missing}")
                print("   Some tables weren't created. Check logs above.")
                
    except Exception as e:
        error_str = str(e).lower()
        if "permission denied" in error_str or "insufficientprivilege" in error_str:
            print(f"   ❌ Permission denied: {e}")
            print("\n" + "=" * 60)
            print("❌ PERMISSIONS STILL NEEDED")
            print("=" * 60)
            print("\nYou still need admin (doadmin) credentials to grant permissions.")
            print("\nNext steps:")
            print("1. Go to DigitalOcean → Databases → Your Database")
            print("2. Look for 'Users' or 'Users & Databases' tab")
            print("3. Find 'doadmin' user and reset its password")
            print("4. Use doadmin credentials to grant permissions")
        else:
            print(f"   ❌ Error: {e}")
            raise
            
except Exception as e:
    print(f"\n❌ Failed: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
