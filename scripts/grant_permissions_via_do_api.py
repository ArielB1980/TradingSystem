#!/usr/bin/env python3
"""
Grant database permissions using DigitalOcean API.

This script uses the DigitalOcean API to grant CREATE privileges on the public schema.

Requirements:
- DigitalOcean API token with write access
- python-digitalocean library: pip install python-digitalocean

Usage:
    export DIGITALOCEAN_TOKEN=your_api_token
    python scripts/grant_permissions_via_do_api.py <database-cluster-id> <username>
"""
import sys
import os
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

def grant_via_api(database_cluster_id: str, username: str = "dbtradingbot"):
    """Grant permissions using DigitalOcean API."""
    try:
        import digitalocean
    except ImportError:
        print("❌ python-digitalocean library not installed")
        print("\nInstall it with:")
        print("  pip install python-digitalocean")
        print("\nOr use the SQL method instead (see GET_ADMIN_CREDENTIALS.md)")
        sys.exit(1)
    
    token = os.environ.get('DIGITALOCEAN_TOKEN')
    if not token:
        print("❌ DIGITALOCEAN_TOKEN environment variable not set")
        print("\nSet it with:")
        print("  export DIGITALOCEAN_TOKEN=your_api_token")
        print("\nGet your token from: https://cloud.digitalocean.com/account/api/tokens")
        sys.exit(1)
    
    print("=" * 60)
    print("Granting Database Permissions via DigitalOcean API")
    print("=" * 60)
    print(f"\nDatabase Cluster ID: {database_cluster_id}")
    print(f"Username: {username}")
    print(f"Token: {token[:10]}...")
    
    try:
        manager = digitalocean.Manager(token=token)
        
        # Get database cluster
        print("\nFetching database cluster...")
        databases = manager.get_all_database_clusters()
        cluster = None
        for db in databases:
            if db.id == database_cluster_id:
                cluster = db
                break
        
        if not cluster:
            print(f"❌ Database cluster '{database_cluster_id}' not found")
            print("\nAvailable clusters:")
            for db in databases:
                print(f"  - {db.id}: {db.name} ({db.engine})")
            sys.exit(1)
        
        print(f"✅ Found database: {cluster.name}")
        
        # Note: DigitalOcean API doesn't directly support granting schema privileges
        # We need to get connection details and run SQL
        print("\n⚠️  DigitalOcean API doesn't support direct privilege grants")
        print("   We need to connect via SQL instead.")
        print("\n   Getting connection details...")
        
        # Get connection pool or connection string
        # The API doesn't expose admin credentials directly for security
        print("\n" + "=" * 60)
        print("LIMITATION: DigitalOcean API doesn't expose admin credentials")
        print("=" * 60)
        print("\nYou have two options:")
        print("\n1. Use DigitalOcean Web Console:")
        print("   - Go to Databases → Your Database → Console tab")
        print("   - Run SQL commands directly")
        print("\n2. Reset admin password and use it:")
        print("   - Go to Databases → Your Database → Users tab")
        print("   - Reset doadmin password")
        print("   - Use new password in connection string")
        print("\nSQL Commands to run:")
        print(f"   GRANT CREATE ON SCHEMA public TO {username};")
        print(f"   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO {username};")
        print(f"   ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO {username};")
        
        return False
        
    except Exception as e:
        print(f"\n❌ API Error: {e}")
        print("\nMake sure:")
        print("1. DIGITALOCEAN_TOKEN is valid and has write access")
        print("2. Database cluster ID is correct")
        print("3. Your account has access to the database")
        return False

def main():
    if len(sys.argv) < 2:
        print("Usage:")
        print("  export DIGITALOCEAN_TOKEN=your_api_token")
        print("  python scripts/grant_permissions_via_do_api.py <database-cluster-id> [username]")
        print("\nExample:")
        print("  python scripts/grant_permissions_via_do_api.py e2db78ca-4d22-4203-822f-2e03ed2f08f7 dbtradingbot")
        print("\nGet your API token from:")
        print("  https://cloud.digitalocean.com/account/api/tokens")
        sys.exit(1)
    
    cluster_id = sys.argv[1]
    username = sys.argv[2] if len(sys.argv) > 2 else "dbtradingbot"
    
    grant_via_api(cluster_id, username)

if __name__ == "__main__":
    main()
