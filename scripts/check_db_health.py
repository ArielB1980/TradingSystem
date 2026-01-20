#!/usr/bin/env python3
"""
Database Health Check Script

Checks the health and performance of the PostgreSQL database
used by the trading system.

Usage:
    python scripts/check_db_health.py
"""

import os
import sys
import time
from datetime import datetime
from typing import Dict, Any

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.pool import NullPool
except ImportError:
    print("Error: SQLAlchemy not installed. Run: pip install sqlalchemy psycopg2-binary")
    sys.exit(1)

# Color codes
class Colors:
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    END = '\033[0m'


def print_header(text: str):
    """Print a formatted header"""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'=' * 80}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{text.center(80)}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'=' * 80}{Colors.END}\n")


def print_status(label: str, status: str, color: str = Colors.GREEN):
    """Print a status line"""
    print(f"{Colors.BOLD}{label:.<50}{Colors.END} {color}{status}{Colors.END}")


def get_database_url() -> str:
    """Get database URL from environment"""
    db_url = os.environ.get('DATABASE_URL')
    
    if not db_url:
        print(f"{Colors.RED}Error: DATABASE_URL environment variable not set{Colors.END}")
        print(f"\nSet it with:")
        print(f"  export DATABASE_URL='postgresql://user:pass@host:port/dbname'")
        sys.exit(1)
    
    return db_url


def check_connection(engine) -> Dict[str, Any]:
    """Test database connection"""
    try:
        start_time = time.time()
        with engine.connect() as conn:
            result = conn.execute(text("SELECT 1"))
            result.fetchone()
        
        connection_time = (time.time() - start_time) * 1000
        
        return {
            "success": True,
            "connection_time_ms": round(connection_time, 2)
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def get_database_info(engine) -> Dict[str, Any]:
    """Get database version and basic info"""
    try:
        with engine.connect() as conn:
            # PostgreSQL version
            version_result = conn.execute(text("SELECT version()"))
            version = version_result.fetchone()[0]
            
            # Database size
            size_result = conn.execute(text("""
                SELECT pg_size_pretty(pg_database_size(current_database()))
            """))
            db_size = size_result.fetchone()[0]
            
            # Current database name
            db_result = conn.execute(text("SELECT current_database()"))
            db_name = db_result.fetchone()[0]
            
            return {
                "success": True,
                "version": version.split(',')[0],  # First part of version string
                "database_name": db_name,
                "database_size": db_size
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def get_table_info(engine) -> Dict[str, Any]:
    """Get information about tables"""
    try:
        with engine.connect() as conn:
            # List all tables
            tables_result = conn.execute(text("""
                SELECT 
                    schemaname,
                    tablename,
                    pg_size_pretty(pg_total_relation_size(schemaname||'.'||tablename)) as size
                FROM pg_tables
                WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
                ORDER BY pg_total_relation_size(schemaname||'.'||tablename) DESC
                LIMIT 10
            """))
            
            tables = []
            for row in tables_result:
                tables.append({
                    "schema": row[0],
                    "name": row[1],
                    "size": row[2]
                })
            
            return {
                "success": True,
                "tables": tables,
                "table_count": len(tables)
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def get_connection_info(engine) -> Dict[str, Any]:
    """Get active connection information"""
    try:
        with engine.connect() as conn:
            # Active connections
            conn_result = conn.execute(text("""
                SELECT 
                    count(*) as total_connections,
                    count(*) FILTER (WHERE state = 'active') as active_connections,
                    count(*) FILTER (WHERE state = 'idle') as idle_connections
                FROM pg_stat_activity
                WHERE datname = current_database()
            """))
            
            row = conn_result.fetchone()
            
            return {
                "success": True,
                "total_connections": row[0],
                "active_connections": row[1],
                "idle_connections": row[2]
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def check_trading_tables(engine) -> Dict[str, Any]:
    """Check trading system specific tables"""
    try:
        with engine.connect() as conn:
            # Check for common trading tables
            table_check = conn.execute(text("""
                SELECT table_name 
                FROM information_schema.tables 
                WHERE table_schema = 'public'
                AND table_name IN ('trades', 'positions', 'market_data', 'signals', 'events')
            """))
            
            existing_tables = [row[0] for row in table_check]
            
            # Get row counts for existing tables
            table_counts = {}
            for table in existing_tables:
                count_result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                table_counts[table] = count_result.fetchone()[0]
            
            return {
                "success": True,
                "existing_tables": existing_tables,
                "table_counts": table_counts
            }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def main():
    print_header("Database Health Check")
    print(f"{Colors.BOLD}Timestamp:{Colors.END} {datetime.utcnow().isoformat()}")
    
    # Get database URL
    db_url = get_database_url()
    
    # Mask password in display
    display_url = db_url
    if '@' in db_url:
        parts = db_url.split('@')
        if ':' in parts[0]:
            user_pass = parts[0].split(':')
            display_url = f"{user_pass[0]}:****@{parts[1]}"
    
    print(f"{Colors.BOLD}Database:{Colors.END} {display_url}\n")
    
    # Create engine
    try:
        engine = create_engine(db_url, poolclass=NullPool)
    except Exception as e:
        print(f"{Colors.RED}Failed to create database engine: {e}{Colors.END}")
        sys.exit(1)
    
    # Run checks
    print_header("Connection Test")
    
    conn_check = check_connection(engine)
    if conn_check["success"]:
        print_status("Connection", f"✓ Connected ({conn_check['connection_time_ms']}ms)", Colors.GREEN)
    else:
        print_status("Connection", f"✗ Failed: {conn_check['error']}", Colors.RED)
        sys.exit(1)
    
    # Database info
    print_header("Database Information")
    
    db_info = get_database_info(engine)
    if db_info["success"]:
        print_status("Database Name", db_info["database_name"])
        print_status("Database Size", db_info["database_size"])
        print_status("PostgreSQL Version", db_info["version"])
    else:
        print_status("Database Info", f"✗ Failed: {db_info['error']}", Colors.RED)
    
    # Connection info
    print_header("Connection Pool Status")
    
    conn_info = get_connection_info(engine)
    if conn_info["success"]:
        print_status("Total Connections", str(conn_info["total_connections"]))
        print_status("Active Connections", str(conn_info["active_connections"]))
        print_status("Idle Connections", str(conn_info["idle_connections"]))
    else:
        print_status("Connection Info", f"✗ Failed: {conn_info['error']}", Colors.RED)
    
    # Table info
    print_header("Database Tables")
    
    table_info = get_table_info(engine)
    if table_info["success"]:
        print_status("Total Tables", str(table_info["table_count"]))
        if table_info["tables"]:
            print(f"\n{Colors.BOLD}Top Tables by Size:{Colors.END}")
            for table in table_info["tables"][:5]:
                print(f"  • {table['schema']}.{table['name']}: {table['size']}")
    else:
        print_status("Table Info", f"✗ Failed: {table_info['error']}", Colors.RED)
    
    # Trading tables
    print_header("Trading System Tables")
    
    trading_check = check_trading_tables(engine)
    if trading_check["success"]:
        if trading_check["existing_tables"]:
            print(f"{Colors.BOLD}Found Tables:{Colors.END}")
            for table in trading_check["existing_tables"]:
                count = trading_check["table_counts"].get(table, 0)
                print_status(f"  {table}", f"{count:,} rows")
        else:
            print(f"{Colors.YELLOW}⚠ No trading tables found. Database may need initialization.{Colors.END}")
    else:
        print_status("Trading Tables", f"✗ Failed: {trading_check['error']}", Colors.RED)
    
    # Summary
    print_header("Summary")
    
    all_success = (
        conn_check["success"] and
        db_info["success"] and
        conn_info["success"] and
        table_info["success"]
    )
    
    if all_success:
        print(f"{Colors.BOLD}{Colors.GREEN}✓ DATABASE HEALTHY{Colors.END}\n")
        print(f"{Colors.GREEN}All checks passed successfully.{Colors.END}")
    else:
        print(f"{Colors.BOLD}{Colors.YELLOW}⚠ DATABASE ISSUES DETECTED{Colors.END}\n")
        print(f"{Colors.YELLOW}Some checks failed. Review errors above.{Colors.END}")
    
    print(f"\n{Colors.BOLD}Recommendations:{Colors.END}")
    if conn_info.get("total_connections", 0) > 50:
        print(f"  {Colors.YELLOW}• High connection count. Consider connection pooling.{Colors.END}")
    else:
        print(f"  {Colors.GREEN}• Connection count is healthy.{Colors.END}")
    
    if not trading_check.get("existing_tables"):
        print(f"  {Colors.YELLOW}• Initialize database with: python migrate_schema.py{Colors.END}")
    
    engine.dispose()


if __name__ == "__main__":
    main()
