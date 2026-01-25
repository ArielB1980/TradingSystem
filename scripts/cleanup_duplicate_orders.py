#!/usr/bin/env python3
"""
Safe duplicate order cleanup script.

Identifies and removes:
1. Orphan reduce-only orders (SL/TP for closed positions)
2. Exact duplicates (same symbol+side+type+price within tolerance)
3. Multiple SL orders per position (keep most protective)
4. Multiple TP orders per position (remove exact price duplicates)

Dry-run by default. Use --execute to actually cancel orders.
"""
import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from decimal import Decimal
from typing import Dict, List, Optional, Set, Tuple

# Add project root to path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.config.config import load_config
from src.data.kraken_client import KrakenClient
from src.storage.db import get_db
from src.storage.repository import PositionModel


# ============ SYMBOL NORMALIZATION ============

def normalize_symbol_for_comparison(symbol: str) -> str:
    """
    Normalize symbol for comparison (PF_XBTUSD <-> BTC/USD:USD).
    
    Handles both directions:
    - PF_XBTUSD -> BTC/USD:USD
    - BTC/USD:USD -> PF_XBTUSD
    - Special case: XBT <-> BTC
    """
    if not symbol:
        return symbol
    
    # If already in PF_ format, convert to unified
    if symbol.startswith("PF_"):
        base = symbol[3:-3]  # Remove "PF_" and "USD"
        if base == "XBT":
            base = "BTC"
        return f"{base}/USD:USD"
    
    # If in unified format, convert to PF_
    if "/" in symbol and ":" in symbol:
        base = symbol.split("/")[0]
        if base == "BTC":
            base = "XBT"
        return f"PF_{base}USD"
    
    # Return as-is if format not recognized
    return symbol


def get_all_symbol_variants(symbol: str) -> Set[str]:
    """Get all possible symbol variants for comparison."""
    variants = {symbol}
    normalized = normalize_symbol_for_comparison(symbol)
    if normalized != symbol:
        variants.add(normalized)
    # Also add reverse normalization
    reverse = normalize_symbol_for_comparison(normalized)
    if reverse != normalized:
        variants.add(reverse)
    return variants


# ============ ORDER CLASSIFICATION ============

def classify_order(order: dict) -> str:
    """
    Classify order as ENTRY, SL, TP, or OTHER.
    
    ENTRY: reduceOnly=False (never cancel in this script)
    SL: reduceOnly=True AND (stopPrice exists OR type contains "stop")
    TP: reduceOnly=True AND NOT SL AND (type contains "take" OR explicit TP)
    OTHER: everything else
    """
    reduce_only = order.get("reduceOnly", False)
    
    if not reduce_only:
        return "ENTRY"
    
    # Check for stop price or stop type
    has_stop_price = order.get("stopPrice") is not None
    order_type = str(order.get("type", "")).lower()
    has_stop_type = "stop" in order_type
    
    if has_stop_price or has_stop_type:
        return "SL"
    
    # Check for take-profit
    has_take_type = "take" in order_type or "take-profit" in order_type or "take_profit" in order_type
    
    if has_take_type:
        return "TP"
    
    # For reduce-only limit orders, we need to be careful
    # Only classify as TP if explicitly identified (e.g., in DB tp_order_ids)
    # Otherwise, leave as OTHER
    return "OTHER"


# ============ PRICE COMPARISON ============

def prices_match(p1: Decimal, p2: Decimal, tolerance_pct: Decimal = Decimal("0.001")) -> bool:
    """
    Check if two prices are within tolerance using relative difference.
    
    Uses: abs(p1 - p2) / p2 <= tolerance_pct
    """
    if p1 == p2:
        return True
    if p2 == 0:
        return False
    relative_diff = abs(p1 - p2) / p2
    return relative_diff <= tolerance_pct


def get_effective_price(order: dict, order_class: str) -> Optional[Decimal]:
    """Get effective price for an order based on its class."""
    if order_class == "SL":
        price = order.get("stopPrice") or order.get("price")
    elif order_class == "TP":
        price = order.get("price") or order.get("triggerPrice") or order.get("stopPrice")
    else:
        price = order.get("price")
    
    if price is None:
        return None
    
    try:
        return Decimal(str(price))
    except (ValueError, TypeError):
        return None


def cluster_prices_by_tolerance(
    orders_with_prices: List[Tuple[dict, Decimal]],
    tolerance_pct: Decimal
) -> List[List[dict]]:
    """
    Cluster orders by price using relative difference.
    
    Args:
        orders_with_prices: List of (order, price) tuples, sorted by price
        tolerance_pct: Relative tolerance for clustering
    
    Returns:
        List of clusters, each cluster is a list of orders
    """
    if not orders_with_prices:
        return []
    
    clusters = []
    current_cluster = [orders_with_prices[0][0]]
    current_price = orders_with_prices[0][1]
    
    for order, price in orders_with_prices[1:]:
        if prices_match(price, current_price, tolerance_pct):
            # Same cluster
            current_cluster.append(order)
        else:
            # New cluster
            clusters.append(current_cluster)
            current_cluster = [order]
            current_price = price
    
    # Add last cluster
    if current_cluster:
        clusters.append(current_cluster)
    
    return clusters


# ============ ORDER PRIORITIZATION ============

def get_order_timestamp(order: dict) -> Optional[float]:
    """Extract timestamp from order (createdAt, timestamp, or serverTime)."""
    # Try various timestamp fields
    for field in ["createdAt", "timestamp", "serverTime", "time"]:
        value = order.get(field)
        if value:
            try:
                # Handle both Unix timestamp and ISO string
                if isinstance(value, (int, float)):
                    return float(value)
                # Could add ISO string parsing if needed
            except (ValueError, TypeError):
                continue
    return None


def get_order_qty(order: dict) -> Optional[Decimal]:
    """Get remaining quantity/amount from order."""
    for field in ["remaining", "amount", "filled", "qty", "quantity"]:
        value = order.get(field)
        if value is not None:
            try:
                return Decimal(str(value))
            except (ValueError, TypeError):
                continue
    return None


def select_keep_order(
    orders: List[dict],
    db_sl_id: Optional[str],
    db_tp_ids: List[str],
    prefer_oldest: bool = False
) -> dict:
    """
    Select which order to keep using priority:
    1. Matches DB metadata (stop_loss_order_id or in tp_order_ids)
    2. Has createdAt/timestamp (newest by default, or oldest if prefer_oldest)
    3. Largest qty
    4. First in stable sort
    """
    if not orders:
        raise ValueError("Cannot select from empty list")
    
    if len(orders) == 1:
        return orders[0]
    
    # Priority 1: DB metadata match
    db_matched = []
    for order in orders:
        order_id = order.get("id")
        if order_id == db_sl_id or order_id in db_tp_ids:
            db_matched.append(order)
    
    if db_matched:
        # If multiple DB matches, continue with priority 2
        candidates = db_matched
    else:
        candidates = orders
    
    # Priority 2: Timestamp
    orders_with_timestamp = [(o, get_order_timestamp(o)) for o in candidates]
    orders_with_ts = [(o, ts) for o, ts in orders_with_timestamp if ts is not None]
    orders_without_ts = [o for o, ts in orders_with_timestamp if ts is None]
    
    if orders_with_ts:
        # Sort by timestamp
        orders_with_ts.sort(key=lambda x: x[1], reverse=not prefer_oldest)
        candidates = [o for o, _ in orders_with_ts]
    elif orders_without_ts:
        # No timestamps, use orders without timestamps
        candidates = orders_without_ts
    else:
        # Fallback to all candidates
        candidates = orders
    
    # Priority 3: Largest qty
    orders_with_qty = [(o, get_order_qty(o)) for o in candidates]
    orders_with_qty = [(o, qty) for o, qty in orders_with_qty if qty is not None]
    
    if orders_with_qty:
        orders_with_qty.sort(key=lambda x: x[1], reverse=True)  # Largest first
        return orders_with_qty[0][0]
    
    # Priority 4: First in stable sort (by order ID for stability)
    candidates.sort(key=lambda o: o.get("id", ""))
    return candidates[0]


# ============ DUPLICATE DETECTION ============

def find_orphan_reduce_only_orders(
    orders: List[dict],
    positions: List[dict]
) -> List[dict]:
    """
    Find reduce-only orders for symbols without open positions.
    
    Returns list of orders to cancel (orphans).
    """
    # Build set of symbols with open positions (all variants)
    open_symbols = set()
    for pos in positions:
        pos_sym = pos.get("symbol")
        if pos_sym and float(pos.get("size", 0)) != 0:
            variants = get_all_symbol_variants(pos_sym)
            open_symbols.update(variants)
    
    orphans = []
    for order in orders:
        if not order.get("reduceOnly", False):
            continue
        
        order_sym = order.get("symbol")
        if not order_sym:
            continue
        
        # Check if symbol (or any variant) has an open position
        order_variants = get_all_symbol_variants(order_sym)
        if not any(variant in open_symbols for variant in order_variants):
            orphans.append(order)
    
    return orphans


def find_exact_duplicates(
    orders: List[dict],
    db_metadata: Dict[str, Dict],
    tolerance_pct: Decimal,
    prefer_oldest: bool = False
) -> List[Dict]:
    """
    Find exact duplicates within SL/TP groups using price clustering.
    
    Returns list of cancellation candidates with reason='EXACT_DUPLICATE'.
    """
    cancellation_candidates = []
    
    # Group orders by (symbol_norm, side, class)
    grouped = defaultdict(lambda: defaultdict(list))
    
    for order in orders:
        order_class = classify_order(order)
        if order_class not in ("SL", "TP"):
            continue
        
        symbol = order.get("symbol")
        if not symbol:
            continue
        
        symbol_norm = normalize_symbol_for_comparison(symbol)
        side = order.get("side", "").lower()
        
        grouped[(symbol_norm, side, order_class)][symbol].append(order)
    
    # Process each group
    for (symbol_norm, side, order_class), symbol_orders_map in grouped.items():
        # Flatten all orders for this group
        all_orders = []
        for orders_list in symbol_orders_map.values():
            all_orders.extend(orders_list)
        
        # Get effective prices and filter out orders without prices
        orders_with_prices = []
        for order in all_orders:
            price = get_effective_price(order, order_class)
            if price is not None:
                orders_with_prices.append((order, price))
        
        if len(orders_with_prices) < 2:
            continue
        
        # Sort by price
        orders_with_prices.sort(key=lambda x: x[1])
        
        # Cluster by price tolerance
        clusters = cluster_prices_by_tolerance(orders_with_prices, tolerance_pct)
        
        # For each cluster with >1 order, keep one, mark others
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            
            # Get DB metadata for this symbol
            db_info = db_metadata.get(symbol_norm, {})
            db_sl_id = db_info.get("stop_loss_order_id")
            db_tp_ids = db_info.get("tp_order_ids", [])
            
            # Select order to keep
            keep_order = select_keep_order(cluster, db_sl_id, db_tp_ids, prefer_oldest)
            
            # Mark others for cancellation
            for order in cluster:
                if order.get("id") != keep_order.get("id"):
                    cancellation_candidates.append({
                        "order": order,
                        "reason": "EXACT_DUPLICATE",
                        "kept_order_id": keep_order.get("id"),
                        "symbol_norm": symbol_norm,
                        "raw_symbol": order.get("symbol"),
                        "class": order_class,
                        "side": order.get("side", "").lower()
                    })
    
    return cancellation_candidates


def find_redundant_sl_orders(
    orders: List[dict],
    positions: List[dict],
    db_metadata: Dict[str, Dict],
    tolerance_pct: Decimal,
    prefer_oldest: bool = False,
    exclude_order_ids: Optional[Set[str]] = None
) -> List[Dict]:
    """
    Find redundant SL orders (keep most protective per position).
    
    Args:
        exclude_order_ids: Set of order IDs to exclude (already marked for cancellation)
    
    Returns list of cancellation candidates with reason='REDUNDANT_SL'.
    """
    cancellation_candidates = []
    exclude_order_ids = exclude_order_ids or set()
    
    # Build position lookup by symbol (all variants)
    pos_by_symbol = {}
    for pos in positions:
        pos_sym = pos.get("symbol")
        if not pos_sym or float(pos.get("size", 0)) == 0:
            continue
        
        variants = get_all_symbol_variants(pos_sym)
        for variant in variants:
            pos_by_symbol[variant] = pos
    
    # Group SL orders by normalized symbol (exclude already-cancelled orders)
    sl_orders_by_symbol = defaultdict(list)
    for order in orders:
        if classify_order(order) != "SL":
            continue
        
        order_id = order.get("id")
        if order_id in exclude_order_ids:
            continue  # Already marked for cancellation
        
        symbol = order.get("symbol")
        if not symbol:
            continue
        
        symbol_norm = normalize_symbol_for_comparison(symbol)
        sl_orders_by_symbol[symbol_norm].append(order)
    
    # Process each symbol with position
    for symbol_norm, sl_orders in sl_orders_by_symbol.items():
        if symbol_norm not in pos_by_symbol:
            continue  # No position, skip (orphans handled separately)
        
        if len(sl_orders) <= 1:
            continue  # Only one SL, keep it
        
        position = pos_by_symbol[symbol_norm]
        position_size = float(position.get("size", 0))
        is_long = position_size > 0
        
        # Get SL orders with valid prices
        sl_with_prices = []
        for order in sl_orders:
            price = get_effective_price(order, "SL")
            if price is not None:
                sl_with_prices.append((order, price))
        
        if not sl_with_prices:
            continue  # No valid prices, skip
        
        # Determine most protective
        if is_long:
            # LONG: highest stop price is most protective
            sl_with_prices.sort(key=lambda x: x[1], reverse=True)
        else:
            # SHORT: lowest stop price is most protective
            sl_with_prices.sort(key=lambda x: x[1])
        
        most_protective_price = sl_with_prices[0][1]
        
        # Get DB metadata
        db_info = db_metadata.get(symbol_norm, {})
        db_sl_id = db_info.get("stop_loss_order_id")
        db_tp_ids = db_info.get("tp_order_ids", [])
        
        # Check if DB-referenced order is within tolerance of most protective
        # But only if it's not already excluded
        keep_order = None
        if db_sl_id and db_sl_id not in exclude_order_ids:
            for order, price in sl_with_prices:
                if order.get("id") == db_sl_id:
                    # Check if within tolerance
                    if prices_match(price, most_protective_price, tolerance_pct):
                        keep_order = order
                        break
        
        # If no DB match or DB order not within tolerance, use most protective
        if keep_order is None:
            # Ensure most protective is not excluded
            for order, price in sl_with_prices:
                if order.get("id") not in exclude_order_ids:
                    keep_order = order
                    break
            
            # If all are excluded, skip this position
            if keep_order is None:
                continue
        
        # Mark others for cancellation
        for order, price in sl_with_prices:
            if order.get("id") != keep_order.get("id"):
                cancellation_candidates.append({
                    "order": order,
                    "reason": "REDUNDANT_SL",
                    "kept_order_id": keep_order.get("id"),
                    "symbol_norm": symbol_norm,
                    "raw_symbol": order.get("symbol"),
                    "class": "SL",
                    "side": order.get("side", "").lower()
                })
    
    return cancellation_candidates


def find_redundant_tp_orders(
    orders: List[dict],
    positions: List[dict],
    db_metadata: Dict[str, Dict],
    tolerance_pct: Decimal,
    prefer_oldest: bool = False
) -> List[Dict]:
    """
    Find redundant TP orders (exact price duplicates within same level).
    
    Returns list of cancellation candidates with reason='DUPLICATE_TP_LEVEL'.
    """
    cancellation_candidates = []
    
    # Build position lookup by symbol (all variants)
    pos_by_symbol = {}
    for pos in positions:
        pos_sym = pos.get("symbol")
        if not pos_sym or float(pos.get("size", 0)) == 0:
            continue
        
        variants = get_all_symbol_variants(pos_sym)
        for variant in variants:
            pos_by_symbol[variant] = pos
    
    # Group TP orders by normalized symbol
    tp_orders_by_symbol = defaultdict(list)
    for order in orders:
        if classify_order(order) != "TP":
            continue
        
        symbol = order.get("symbol")
        if not symbol:
            continue
        
        symbol_norm = normalize_symbol_for_comparison(symbol)
        tp_orders_by_symbol[symbol_norm].append(order)
    
    # Process each symbol with position
    for symbol_norm, tp_orders in tp_orders_by_symbol.items():
        if symbol_norm not in pos_by_symbol:
            continue  # No position, skip (orphans handled separately)
        
        if len(tp_orders) <= 1:
            continue  # Only one TP, keep it
        
        # Get TP orders with valid prices
        tp_with_prices = []
        for order in tp_orders:
            price = get_effective_price(order, "TP")
            if price is not None:
                tp_with_prices.append((order, price))
        
        if not tp_with_prices:
            continue
        
        # Sort by price
        tp_with_prices.sort(key=lambda x: x[1])
        
        # Cluster by price tolerance
        clusters = cluster_prices_by_tolerance(tp_with_prices, tolerance_pct)
        
        # Get DB metadata
        db_info = db_metadata.get(symbol_norm, {})
        db_sl_id = db_info.get("stop_loss_order_id")
        db_tp_ids = db_info.get("tp_order_ids", [])
        
        # For each cluster, keep one, mark others
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            
            # Select order to keep
            keep_order = select_keep_order(cluster, db_sl_id, db_tp_ids, prefer_oldest)
            
            # Mark others for cancellation
            for order in cluster:
                if order.get("id") != keep_order.get("id"):
                    cancellation_candidates.append({
                        "order": order,
                        "reason": "DUPLICATE_TP_LEVEL",
                        "kept_order_id": keep_order.get("id"),
                        "symbol_norm": symbol_norm,
                        "raw_symbol": order.get("symbol"),
                        "class": "TP",
                        "side": order.get("side", "").lower()
                    })
    
    return cancellation_candidates


# ============ SAFETY CHECKS ============

def post_plan_safety_check(
    positions: List[dict],
    orders_to_cancel: List[Dict],
    all_orders: List[dict]
) -> Tuple[bool, List[str]]:
    """
    Check if any position would have 0 SL after cleanup.
    
    Returns (safe, warnings)
    """
    warnings = []
    
    # Build set of order IDs to cancel
    cancel_order_ids = {c["order"].get("id") for c in orders_to_cancel}
    
    # Build position lookup
    pos_by_symbol = {}
    for pos in positions:
        pos_sym = pos.get("symbol")
        if not pos_sym or float(pos.get("size", 0)) == 0:
            continue
        
        variants = get_all_symbol_variants(pos_sym)
        for variant in variants:
            pos_by_symbol[variant] = pos
    
    # Build map of kept order IDs (from cancellation candidates)
    # These are orders that are preserved even though they're in duplicate clusters
    kept_order_ids = set()
    for candidate in orders_to_cancel:
        kept_id = candidate.get("kept_order_id")
        if kept_id:
            kept_order_ids.add(kept_id)
    
    # Count SL orders per position after cancellation
    # An order remains if:
    # 1. It's not in the cancellation list, OR
    # 2. It's a kept order (preserved from duplicate cluster)
    sl_orders_by_symbol = defaultdict(list)
    for order in all_orders:
        if classify_order(order) != "SL":
            continue
        
        order_id = order.get("id")
        if not order_id:
            continue
        
        symbol = order.get("symbol")
        if not symbol:
            continue
        
        symbol_norm = normalize_symbol_for_comparison(symbol)
        
        # Count if not being cancelled, or if it's a kept order
        if order_id not in cancel_order_ids:
            sl_orders_by_symbol[symbol_norm].append(order)
        elif order_id in kept_order_ids:
            # Kept order - count it even though it might be in cancellation list
            sl_orders_by_symbol[symbol_norm].append(order)
    
    # Check each position (use set to avoid duplicate warnings for same position)
    checked_positions = set()
    for symbol_norm, position in pos_by_symbol.items():
        # Use original position symbol to avoid duplicate warnings
        pos_sym = position.get("symbol", symbol_norm)
        if pos_sym in checked_positions:
            continue
        checked_positions.add(pos_sym)
        
        remaining_sl = sl_orders_by_symbol.get(symbol_norm, [])
        if len(remaining_sl) == 0:
            warnings.append(
                f"Position {pos_sym} would have 0 SL orders after cleanup"
            )
    
    safe = len(warnings) == 0
    return safe, warnings


# ============ MAIN CLEANUP FUNCTION ============

async def cleanup_duplicate_orders(
    dry_run: bool = True,
    max_cancellations: int = 50,
    price_tolerance_pct: Decimal = Decimal("0.001"),
    symbol_filter: Optional[str] = None,
    prefer_oldest: bool = False,
    verbose: bool = False
) -> Dict:
    """
    Main cleanup function - executes steps 0-5 in order.
    
    Returns summary dict with cancellation candidates and statistics.
    """
    # Step 0: Fetch state once
    config = load_config()
    client = KrakenClient(
        api_key=config.exchange.api_key,
        api_secret=config.exchange.api_secret,
        futures_api_key=config.exchange.futures_api_key,
        futures_api_secret=config.exchange.futures_api_secret,
        use_testnet=config.exchange.use_testnet
    )
    
    await client.initialize()
    
    try:
        print("Step 0: Fetching state...")
        positions_raw, orders_raw = await asyncio.gather(
            client.get_all_futures_positions(),
            client.get_futures_open_orders()
        )
        
        # Filter positions (non-zero size)
        positions = [p for p in positions_raw if float(p.get("size", 0)) != 0]
        
        # Apply symbol filter if specified
        if symbol_filter:
            symbol_variants = get_all_symbol_variants(symbol_filter)
            positions = [p for p in positions if any(v in get_all_symbol_variants(p.get("symbol", "")) for v in symbol_variants)]
            orders_raw = [o for o in orders_raw if any(v in get_all_symbol_variants(o.get("symbol", "")) for v in symbol_variants)]
        
        # Load DB metadata
        db = get_db()
        db_metadata = {}
        with db.get_session() as session:
            db_positions = session.query(PositionModel).all()
            for db_pos in db_positions:
                symbol_norm = normalize_symbol_for_comparison(db_pos.symbol)
                tp_ids = json.loads(db_pos.tp_order_ids) if db_pos.tp_order_ids else []
                db_metadata[symbol_norm] = {
                    "stop_loss_order_id": db_pos.stop_loss_order_id,
                    "tp_order_ids": tp_ids
                }
        
        print(f"  Total orders: {len(orders_raw)}")
        print(f"  Total positions: {len(positions)} (non-zero)")
        print(f"  DB positions with SL: {sum(1 for m in db_metadata.values() if m['stop_loss_order_id'])}")
        print(f"  DB positions with TP: {sum(1 for m in db_metadata.values() if m['tp_order_ids'])}")
        
        # Step 1: Find orphan reduce-only orders
        print("\nStep 1: Finding orphan reduce-only orders...")
        orphan_orders = find_orphan_reduce_only_orders(orders_raw, positions)
        orphan_candidates = [
            {
                "order": o,
                "reason": "ORPHAN",
                "kept_order_id": None,
                "symbol_norm": normalize_symbol_for_comparison(o.get("symbol", "")),
                "raw_symbol": o.get("symbol"),
                "class": classify_order(o),
                "side": o.get("side", "").lower()
            }
            for o in orphan_orders
        ]
        print(f"  Found: {len(orphan_candidates)} orphan orders")
        
        # Step 2: Classify orders
        print("\nStep 2: Classifying orders...")
        classification_counts = defaultdict(int)
        for order in orders_raw:
            classification_counts[classify_order(order)] += 1
        for cls, count in sorted(classification_counts.items()):
            print(f"  {cls}: {count}")
        
        # Filter out orphan orders for subsequent steps
        orphan_order_ids = {o.get("id") for o in orphan_orders}
        active_orders = [o for o in orders_raw if o.get("id") not in orphan_order_ids]
        
        # Step 3: Find exact duplicates
        print("\nStep 3: Finding exact duplicates...")
        exact_duplicate_candidates = find_exact_duplicates(
            active_orders,
            db_metadata,
            price_tolerance_pct,
            prefer_oldest
        )
        print(f"  Found: {len(exact_duplicate_candidates)} exact duplicates")
        
        # Track orders marked for cancellation so far
        cancelled_order_ids = orphan_order_ids.copy()
        cancelled_order_ids.update({c["order"].get("id") for c in exact_duplicate_candidates})
        
        # Filter out already-cancelled orders for Step 4
        orders_for_sl_check = [o for o in active_orders if o.get("id") not in cancelled_order_ids]
        
        # Step 4: Find redundant SL orders (only consider orders not already marked for cancellation)
        print("\nStep 4: Finding redundant SL orders...")
        redundant_sl_candidates = find_redundant_sl_orders(
            orders_for_sl_check,
            positions,
            db_metadata,
            price_tolerance_pct,
            prefer_oldest,
            exclude_order_ids=cancelled_order_ids
        )
        print(f"  Found: {len(redundant_sl_candidates)} redundant SL orders")
        
        # Update cancelled order IDs
        cancelled_order_ids.update({c["order"].get("id") for c in redundant_sl_candidates})
        
        # Filter out already-cancelled orders for Step 5
        orders_for_tp_check = [o for o in active_orders if o.get("id") not in cancelled_order_ids]
        
        # Step 5: Find redundant TP orders (only consider orders not already marked for cancellation)
        print("\nStep 5: Finding redundant TP orders...")
        redundant_tp_candidates = find_redundant_tp_orders(
            orders_for_tp_check,
            positions,
            db_metadata,
            price_tolerance_pct,
            prefer_oldest
        )
        print(f"  Found: {len(redundant_tp_candidates)} redundant TP orders")
        
        # Combine all cancellation candidates
        all_candidates = (
            orphan_candidates +
            exact_duplicate_candidates +
            redundant_sl_candidates +
            redundant_tp_candidates
        )
        
        # Remove duplicates (same order ID)
        seen_order_ids = set()
        unique_candidates = []
        for candidate in all_candidates:
            order_id = candidate["order"].get("id")
            if order_id and order_id not in seen_order_ids:
                seen_order_ids.add(order_id)
                unique_candidates.append(candidate)
        
        # Smart selection: ensure we don't cancel all SL orders for any position
        # Build position lookup
        pos_variants_map = {}
        for pos in positions:
            pos_sym = pos.get("symbol")
            if pos_sym and float(pos.get("size", 0)) != 0:
                variants = get_all_symbol_variants(pos_sym)
                for variant in variants:
                    pos_variants_map[variant] = pos_sym
        
        # Count total SL orders per position
        sl_orders_by_pos = defaultdict(set)
        for order in orders_raw:
            if classify_order(order) == "SL":
                symbol = order.get("symbol")
                if symbol:
                    symbol_norm = normalize_symbol_for_comparison(symbol)
                    if symbol_norm in pos_variants_map:
                        sl_orders_by_pos[symbol_norm].add(order.get("id"))
        
        # Build map of kept orders (from exact duplicates) - these are preserved
        # Also build set of all kept order IDs to never cancel them
        kept_sl_by_pos = defaultdict(set)
        all_kept_order_ids = set()
        for candidate in unique_candidates:
            kept_id = candidate.get("kept_order_id")
            if kept_id:
                all_kept_order_ids.add(kept_id)
                if candidate["class"] == "SL":
                    symbol_norm = candidate["symbol_norm"]
                    if symbol_norm in sl_orders_by_pos:
                        kept_sl_by_pos[symbol_norm].add(kept_id)
        
        # Select candidates, ensuring at least one SL remains per position
        candidates_to_process = []
        sl_cancelled_in_selection = defaultdict(set)
        
        for candidate in unique_candidates:
            if len(candidates_to_process) >= max_cancellations:
                break
            
            order = candidate["order"]
            order_id = order.get("id")
            order_class = candidate["class"]
            symbol_norm = candidate["symbol_norm"]
            
            # Never cancel a kept order
            if order_id in all_kept_order_ids:
                continue
            
            # For SL orders, check if cancelling would leave position with 0 SL
            if order_class == "SL" and symbol_norm in sl_orders_by_pos:
                total_sl_ids = sl_orders_by_pos[symbol_norm]
                total_sl = len(total_sl_ids)
                already_cancelled_ids = sl_cancelled_in_selection[symbol_norm]
                kept = kept_sl_by_pos.get(symbol_norm, set())
                
                # Calculate which SL orders would remain after this cancellation
                would_be_cancelled = already_cancelled_ids.copy()
                would_be_cancelled.add(order_id)
                
                # Calculate remaining SL orders after this cancellation
                # Start with all SL orders
                remaining_ids = total_sl_ids.copy()
                
                # Remove all that would be cancelled (including this one)
                remaining_ids -= would_be_cancelled
                
                # Add back kept orders (they're preserved even if in cancellation list)
                # But only if they're actually in the total_sl_ids set
                for kept_id in kept:
                    if kept_id in total_sl_ids:
                        # Kept order will remain, add it back if it was removed
                        remaining_ids.add(kept_id)
                
                # Also check if this candidate has a kept_order_id that would remain
                if candidate.get("kept_order_id"):
                    kept_id = candidate["kept_order_id"]
                    if kept_id in total_sl_ids:
                        # This kept order will remain
                        remaining_ids.add(kept_id)
                
                if len(remaining_ids) <= 0:
                    # Would leave position with 0 SL, skip this candidate
                    # This ensures we never cancel all SL orders for a position
                    continue
                
                sl_cancelled_in_selection[symbol_norm].add(order_id)
            
            # Add candidate (passed all safety checks)
            candidates_to_process.append(candidate)
        
        # Post-plan safety check
        print("\n=== Post-Plan Safety Check ===")
        safe, warnings = post_plan_safety_check(
            positions,
            candidates_to_process,
            orders_raw
        )
        
        # If safety check fails, reduce candidates until it passes
        # Remove candidates that would leave positions without SL
        if not safe:
            print("  ⚠️  Safety check failed - removing unsafe candidates...")
            unsafe_positions = {w.split()[1] for w in warnings}  # Extract position symbols from warnings
            
            # Build map of positions to their SL orders
            pos_sl_orders = defaultdict(set)
            for order in orders_raw:
                if classify_order(order) == "SL":
                    symbol = order.get("symbol")
                    if symbol:
                        symbol_norm = normalize_symbol_for_comparison(symbol)
                        for pos in positions:
                            pos_sym = pos.get("symbol")
                            if pos_sym and float(pos.get("size", 0)) != 0:
                                pos_variants = get_all_symbol_variants(pos_sym)
                                if symbol_norm in pos_variants:
                                    pos_sl_orders[pos_sym].add(order.get("id"))
            
            # Remove candidates that would leave unsafe positions with 0 SL
            safe_candidates = []
            sl_cancelled_by_pos = defaultdict(set)
            kept_by_pos = defaultdict(set)
            
            # Build kept orders map
            for candidate in candidates_to_process:
                if candidate.get("kept_order_id") and candidate["class"] == "SL":
                    symbol_norm = candidate["symbol_norm"]
                    for pos in positions:
                        pos_sym = pos.get("symbol")
                        if pos_sym and float(pos.get("size", 0)) != 0:
                            pos_variants = get_all_symbol_variants(pos_sym)
                            if symbol_norm in pos_variants:
                                kept_by_pos[pos_sym].add(candidate["kept_order_id"])
            
            for candidate in candidates_to_process:
                order = candidate["order"]
                order_id = order.get("id")
                order_class = candidate["class"]
                symbol_norm = candidate["symbol_norm"]
                
                # Check if this candidate would leave any position with 0 SL
                would_be_unsafe = False
                if order_class == "SL":
                    for pos in positions:
                        pos_sym = pos.get("symbol")
                        if pos_sym and float(pos.get("size", 0)) != 0:
                            pos_variants = get_all_symbol_variants(pos_sym)
                            if symbol_norm in pos_variants and pos_sym in unsafe_positions:
                                # This position is already unsafe
                                total_sl = pos_sl_orders.get(pos_sym, set())
                                already_cancelled = sl_cancelled_by_pos[pos_sym]
                                kept = kept_by_pos.get(pos_sym, set())
                                
                                would_cancel = already_cancelled.copy()
                                would_cancel.add(order_id)
                                
                                remaining = total_sl - would_cancel
                                for kept_id in kept:
                                    if kept_id in total_sl:
                                        remaining.add(kept_id)
                                
                                if len(remaining) <= 0:
                                    would_be_unsafe = True
                                    break
                
                if not would_be_unsafe:
                    safe_candidates.append(candidate)
                    if order_class == "SL":
                        for pos in positions:
                            pos_sym = pos.get("symbol")
                            if pos_sym and float(pos.get("size", 0)) != 0:
                                pos_variants = get_all_symbol_variants(pos_sym)
                                if symbol_norm in pos_variants:
                                    sl_cancelled_by_pos[pos_sym].add(order_id)
            
            candidates_to_process = safe_candidates
            print(f"  Reduced to {len(candidates_to_process)} safe candidates")
            
            # Re-run safety check
            safe, warnings = post_plan_safety_check(
                positions,
                candidates_to_process,
                orders_raw
            )
        
        if warnings:
            for warning in warnings:
                print(f"  ⚠️  {warning}")
        
        if safe:
            print("  ✅ Safety check: PASSED")
        else:
            print("  ❌ Safety check: FAILED - Would leave positions without SL")
            if not dry_run:
                print("  ABORTING - Not executing cancellations")
                return {
                    "safe": False,
                    "cancelled": 0,
                    "candidates": candidates_to_process,
                    "warnings": warnings
                }
        
        # Print detailed report
        print("\n=== Cancellation Candidates ===")
        if verbose:
            for candidate in candidates_to_process:
                order = candidate["order"]
                price = get_effective_price(order, candidate["class"])
                stop_price = order.get("stopPrice")
                print(f"\n  Order ID: {order.get('id')[:30]}...")
                print(f"    symbol_norm: {candidate['symbol_norm']}")
                print(f"    raw_symbol: {candidate['raw_symbol']}")
                print(f"    class: {candidate['class']}")
                print(f"    side: {candidate['side']}")
                print(f"    price: {price}")
                if stop_price:
                    print(f"    stopPrice: {stop_price}")
                print(f"    reason: {candidate['reason']}")
                if candidate['kept_order_id']:
                    print(f"    kept_order_id: {candidate['kept_order_id'][:30]}...")
        else:
            print(f"  Total: {len(candidates_to_process)} orders")
            by_reason = defaultdict(int)
            for c in candidates_to_process:
                by_reason[c["reason"]] += 1
            for reason, count in sorted(by_reason.items()):
                print(f"    {reason}: {count}")
        
        # Execute cancellations if not dry-run
        cancelled_count = 0
        errors = []
        
        if not dry_run and safe:
            print(f"\n=== Executing Cancellations ===")
            for candidate in candidates_to_process:
                order = candidate["order"]
                order_id = order.get("id")
                symbol = order.get("symbol")
                
                try:
                    await client.cancel_futures_order(order_id, symbol)
                    cancelled_count += 1
                    if verbose:
                        print(f"  ✅ Cancelled {order_id[:30]}... ({candidate['reason']})")
                except Exception as e:
                    errors.append((order_id, str(e)))
                    if verbose:
                        print(f"  ❌ Failed to cancel {order_id[:30]}...: {e}")
        else:
            print(f"\n[MODE: DRY-RUN - No orders cancelled]")
            print("Run with --execute to cancel orders")
        
        # Summary
        print("\n=== Summary ===")
        print(f"Total orders to cancel: {len(candidates_to_process)}")
        by_reason = defaultdict(int)
        for c in candidates_to_process:
            by_reason[c["reason"]] += 1
        for reason, count in sorted(by_reason.items()):
            print(f"  - {reason}: {count}")
        
        if not dry_run:
            print(f"\nCancelled: {cancelled_count}")
            if errors:
                print(f"Errors: {len(errors)}")
                for order_id, error in errors[:5]:
                    print(f"  {order_id[:30]}...: {error}")
        
        return {
            "safe": safe,
            "cancelled": cancelled_count,
            "candidates": candidates_to_process,
            "warnings": warnings,
            "errors": errors
        }
    
    finally:
        await client.close()


# ============ CLI ============

def main():
    parser = argparse.ArgumentParser(
        description="Clean up duplicate orders safely (dry-run by default)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually cancel orders (default: dry-run)"
    )
    parser.add_argument(
        "--max-cancellations",
        type=int,
        default=50,
        help="Max orders to cancel per run (default: 50)"
    )
    parser.add_argument(
        "--price-tolerance",
        type=float,
        default=0.001,
        help="Price tolerance for duplicate detection as decimal (default: 0.001 = 0.1%%)"
    )
    parser.add_argument(
        "--symbol",
        type=str,
        help="Only process specific symbol (normalized or raw format)"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed output for each order"
    )
    parser.add_argument(
        "--prefer-oldest",
        action="store_true",
        help="When multiple orders match, prefer oldest by timestamp (default: prefer newest)"
    )
    
    args = parser.parse_args()
    
    asyncio.run(cleanup_duplicate_orders(
        dry_run=not args.execute,
        max_cancellations=args.max_cancellations,
        price_tolerance_pct=Decimal(str(args.price_tolerance)),
        symbol_filter=args.symbol,
        prefer_oldest=args.prefer_oldest,
        verbose=args.verbose
    ))


if __name__ == "__main__":
    main()
