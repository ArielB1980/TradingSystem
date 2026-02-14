#!/usr/bin/env python3
"""
Test database connection and attempt to grant permissions.

This will test if the provided credentials have admin privileges.
"""
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text

def test_and_grant(host: str, port: int, username: str, password: str, database: str):
    """Test connection and attempt to grant permissions."""
    database_url = f"postgresql://{username}:{password}@{host}:{port}/{database}?sslmode=require"
    
    print("=" * 60)
    print("Testing Database Connection and Permissions")
    print("=" * 60)
    print(f"\nHost: {host}")
    print(f"Port: {port}")
    print(f"Username: {username}")
    print(f"Database: {database}")
    
    engine = create_engine(database_url)
    
    try:
        with engine.connect() as conn:
            # Check current user
            result = conn.execute(text("SELECT current_user, session_user;"))
            current_user, session_user = result.fetchone()
            print(f"\n✅ Connected successfully!")
            print(f"   Current User: {current_user}")
            print(f"   Session User: {session_user}")
            
            # Check if user is superuser
            result = conn.execute(text("SELECT usesuper FROM pg_user WHERE usename = current_user;"))
            is_superuser = result.fetchone()[0]
            
            if is_superuser:
                print(f"\n✅ User '{current_user}' IS a superuser - can grant permissions!")
            else:
                print(f"\n⚠️  User '{current_user}' is NOT a superuser")
                print(f"   This user may not be able to grant privileges to others")
            
            # Try to grant permissions
            target_user = "dbtradingbot"
            print(f"\n" + "=" * 60)
            print(f"Attempting to grant permissions to '{target_user}'...")
            print("=" * 60)
            
            trans = None
            try:
                # Start transaction
                trans = conn.begin()
                
                print(f"\n1. Granting CREATE on schema 'public'...")
                conn.execute(text(f"GRANT CREATE ON SCHEMA public TO {target_user};"))
                print("   ✅ GRANT CREATE completed")
                
                print(f"\n2. Granting default privileges on tables...")
                conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {target_user};"))
                print("   ✅ ALTER DEFAULT PRIVILEGES (tables) completed")
                
                print(f"\n3. Granting default privileges on sequences...")
                conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {target_user};"))
                print("   ✅ ALTER DEFAULT PRIVILEGES (sequences) completed")
                
                # Commit
                trans.commit()
                
                print("\n" + "=" * 60)
                print("✅ ALL PERMISSIONS GRANTED SUCCESSFULLY!")
                print("=" * 60)
                print("\nYou can now restart your app. Tables will be created automatically.")
                
                return True
                
            except Exception as e:
                if trans:
                    trans.rollback()
                error_str = str(e).lower()
                
                if "permission denied" in error_str or "insufficientprivilege" in error_str:
                    print(f"\n❌ PERMISSION DENIED")
                    print(f"   Error: {e}")
                    print(f"\n   The user '{current_user}' cannot grant privileges.")
                    print(f"   You need to connect as 'doadmin' (superuser) instead.")
                    print(f"\n   SOLUTION:")
                    print(f"   1. Go to DigitalOcean → Databases → Your Database")
                    print(f"   2. Click 'Users & Databases' tab")
                    print(f"   3. Find 'doadmin' user and reset its password")
                    print(f"   4. Use the doadmin credentials to grant permissions")
                else:
                    print(f"\n❌ Error: {e}")
                    raise
                    
    except Exception as e:
        print(f"\n❌ Connection failed: {e}")
        print("\nCheck:")
        print("1. Host, port, username, password are correct")
        print("2. Database is accessible from this location")
        print("3. SSL mode is correct (sslmode=require)")
        return False

def main():
    # Get credentials from command line or environment
    if len(sys.argv) >= 6:
        host = sys.argv[1]
        port = int(sys.argv[2])
        username = sys.argv[3]
        password = sys.argv[4]
        database = sys.argv[5]
    else:
        # Use provided credentials
        host = "app-65e2763f-0c06-4d87-a349-ddc49db0abf3-do-user-31978256-0.l.db.ondigitalocean.com"
        port = 25060
        username = "dbtradingbot"  # Change to "doadmin" if these are admin credentials
        password = "AVNS_3ZbhLloQP64uLYyhxoe"
        database = "dbtradingbot"
        
        print("Using provided credentials...")
        print("If these are admin (doadmin) credentials, update username in script.")
    
    test_and_grant(host, port, username, password, database)

if __name__ == "__main__":
    main()
