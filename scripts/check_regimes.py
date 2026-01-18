#!/usr/bin/env python3
"""
Check and optionally clear decision traces to force re-analysis.

This script helps verify the current regime distribution in the database
and optionally clears old traces to force fresh analysis with the new
regime classification logic.
"""
import os
import sys
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.storage.repository import get_db_connection
from src.monitoring.logger import get_logger

logger = get_logger(__name__)


def check_regime_distribution():
    """Check current regime distribution in decision traces."""
    print("\n📊 Current Regime Distribution in Database:\n")
    print("=" * 60)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Query regime distribution
    query = """
        SELECT 
            details->>'regime' as regime,
            COUNT(*) as count,
            MAX(timestamp) as latest_update
        FROM decision_traces
        WHERE details->>'regime' IS NOT NULL
        GROUP BY details->>'regime'
        ORDER BY count DESC;
    """
    
    cursor.execute(query)
    results = cursor.fetchall()
    
    if not results:
        print("❌ No decision traces found in database")
        cursor.close()
        conn.close()
        return
    
    total = sum(r[1] for r in results)
    
    for regime, count, latest in results:
        percentage = (count / total) * 100
        print(f"  {regime:20s}: {count:5d} ({percentage:5.1f}%)  Latest: {latest}")
    
    print("=" * 60)
    print(f"  {'TOTAL':20s}: {total:5d} (100.0%)\n")
    
    # Check age of traces
    cursor.execute("""
        SELECT 
            MIN(timestamp) as oldest,
            MAX(timestamp) as newest,
            COUNT(*) as total
        FROM decision_traces;
    """)
    
    oldest, newest, total = cursor.fetchone()
    print(f"📅 Trace Age:")
    print(f"  Oldest: {oldest}")
    print(f"  Newest: {newest}")
    print(f"  Total:  {total} traces\n")
    
    cursor.close()
    conn.close()


def clear_old_traces(hours=1, dry_run=True):
    """
    Clear decision traces older than specified hours.
    
    Args:
        hours: Clear traces older than this many hours
        dry_run: If True, only show what would be deleted
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    
    # Count what would be deleted
    cursor.execute("""
        SELECT COUNT(*) 
        FROM decision_traces 
        WHERE timestamp < %s;
    """, (cutoff,))
    
    count = cursor.fetchone()[0]
    
    if dry_run:
        print(f"\n🔍 DRY RUN: Would delete {count} traces older than {hours}h")
        print(f"   Cutoff time: {cutoff}")
        print("\n   Run with --execute to actually delete\n")
    else:
        print(f"\n🗑️  Deleting {count} traces older than {hours}h...")
        cursor.execute("""
            DELETE FROM decision_traces 
            WHERE timestamp < %s;
        """, (cutoff,))
        conn.commit()
        print(f"   ✅ Deleted {count} traces\n")
    
    cursor.close()
    conn.close()


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Check and clear decision traces')
    parser.add_argument('--clear', type=int, metavar='HOURS',
                        help='Clear traces older than HOURS (default: 1)')
    parser.add_argument('--execute', action='store_true',
                        help='Actually execute the clear (default is dry-run)')
    
    args = parser.parse_args()
    
    # Always show current distribution
    check_regime_distribution()
    
    # Optionally clear old traces
    if args.clear is not None:
        hours = args.clear if args.clear > 0 else 1
        clear_old_traces(hours=hours, dry_run=not args.execute)
        
        if args.execute:
            print("✅ Traces cleared. New analysis will generate fresh regime classifications.\n")
            print("📊 Updated distribution:\n")
            check_regime_distribution()


if __name__ == '__main__':
    main()
