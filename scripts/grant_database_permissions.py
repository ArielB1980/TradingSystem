#!/usr/bin/env python3
"""
Grant database permissions to fix 'permission denied for schema public' error.

This script connects to the database and grants CREATE privileges on the public schema.

Run this from your app console or locally if you have database access.
"""
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from sqlalchemy import create_engine, text
from urllib.parse import urlparse

def extract_username(database_url: str) -> str:
    """Extract username from DATABASE_URL."""
    parsed = urlparse(database_url)
    return parsed.username

def grant_permissions(database_url: str):
    """Grant CREATE privileges on public schema."""
    print("=" * 60)
    print("Database Permissions Grant Script")
    print("=" * 60)
    
    # Extract username
    username = extract_username(database_url)
    print(f"\nDatabase URL: {database_url.split('@')[0]}@***")
    print(f"Username: {username}")
    
    # Create engine
    print("\nConnecting to database...")
    engine = create_engine(database_url)
    
    try:
        with engine.connect() as conn:
            # Start transaction
            trans = conn.begin()
            
            try:
                print(f"\nGranting CREATE privilege on schema 'public' to '{username}'...")
                conn.execute(text(f"GRANT CREATE ON SCHEMA public TO {username};"))
                print("✅ GRANT CREATE completed")
                
                print(f"\nGranting default privileges on tables to '{username}'...")
                conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {username};"))
                print("✅ ALTER DEFAULT PRIVILEGES (tables) completed")
                
                print(f"\nGranting default privileges on sequences to '{username}'...")
                conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {username};"))
                print("✅ ALTER DEFAULT PRIVILEGES (sequences) completed")
                
                # Commit transaction
                trans.commit()
                
                print("\n" + "=" * 60)
                print("✅ ALL PERMISSIONS GRANTED SUCCESSFULLY")
                print("=" * 60)
                print("\nYou can now restart your app. Tables should be created automatically.")
                
            except Exception as e:
                trans.rollback()
                error_str = str(e).lower()
                
                if "permission denied" in error_str or "insufficientprivilege" in error_str:
                    print("\n" + "=" * 60)
                    print("❌ PERMISSION ERROR")
                    print("=" * 60)
                    print(f"\nYou don't have permission to grant privileges.")
                    print(f"This script needs to be run by a database superuser (like 'doadmin' or 'postgres').")
                    print(f"\nCurrent user: {username}")
                    print(f"\nSOLUTION:")
                    print(f"1. Connect to your database as a superuser (usually 'doadmin')")
                    print(f"2. Run these SQL commands manually:")
                    print(f"\n   GRANT CREATE ON SCHEMA public TO {username};")
                    print(f"   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {username};")
                    print(f"   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {username};")
                    print(f"\nOr contact DigitalOcean support to grant these permissions.")
                else:
                    print(f"\n❌ Error: {e}")
                    raise
                    
    except Exception as e:
        print(f"\n❌ Connection failed: {e}")
        print("\nMake sure:")
        print("1. DATABASE_URL is set correctly")
        print("2. Database is accessible from this location")
        print("3. Network/firewall allows connections")
        raise

def main():
    """Main entry point."""
    # Get DATABASE_URL from environment or command line
    database_url = os.environ.get('DATABASE_URL')
    
    if not database_url:
        if len(sys.argv) > 1:
            database_url = sys.argv[1]
        else:
            print("❌ DATABASE_URL not found")
            print("\nUsage:")
            print("  python scripts/grant_database_permissions.py")
            print("  (requires DATABASE_URL environment variable)")
            print("\nOr:")
            print("  python scripts/grant_database_permissions.py 'postgresql://user:pass@host/db'")
            sys.exit(1)
    
    try:
        grant_permissions(database_url)
    except Exception as e:
        print(f"\n❌ Failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
