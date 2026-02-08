# Architecture Review: Efficiency & Stability Improvements

**Date**: 2026-01-12  
**Reviewer**: AI Assistant  
**System**: Live Trading System (250 coins)

## Executive Summary

The system is functionally working but has several opportunities to improve efficiency and stability. Key issues identified:

1. **Critical Stability Issue**: Candles are stored in memory only, not persisted to database
2. **Efficiency Issue**: Candle fetching is inefficient (10 at a time, requires 50 before processing)
3. **Database Operations**: Mixed blocking/async patterns, no batching
4. **Resource Usage**: Processing all 250 coins synchronously in each tick

---

## 1. Critical Stability Issues

### 1.1 Candle Data Not Persisted to Database ⚠️ **CRITICAL**

**Location**: `src/live/live_trading.py:746-791` (`_update_candles()`)

**Issue**:
- Candles are fetched from API and stored in memory buffers only
- No `save_candle()` calls in `_update_candles()`
- All candle data is lost on system restart
- System requires 50 candles before processing, must rebuild from scratch on restart

**Impact**:
- System takes 5-10 minutes to become operational after restart
- No historical data persistence
- Dashboard shows empty data

**Recommendation**:
```python
# In _update_candles(), after fetching candles:
for candle in candles:
    # Save to database (use async/batched)
    await asyncio.to_thread(save_candle, candle)
```

**Priority**: 🔴 **HIGH** - Stability critical

---

### 1.2 Candle Fetch Limit Too Low

**Location**: `src/live/live_trading.py:759`

**Issue**:
- Fetches only 10 candles per timeframe per symbol
- Requires 50 candles before processing
- Takes 5+ iterations (5+ minutes) to accumulate enough candles

**Impact**:
- Slow startup/warmup time
- Wastes API calls (multiple requests instead of one)

**Recommendation**:
```python
# Change from:
candles = await self.client.get_spot_ohlcv(symbol, tf, limit=10)

# To:
candles = await self.client.get_spot_ohlcv(symbol, tf, limit=100)  # Get more at once
```

**Priority**: 🟡 **MEDIUM** - Efficiency improvement

---

## 2. Efficiency Improvements

### 2.1 Database Write Batching

**Location**: Multiple locations in `src/storage/repository.py`

**Issue**:
- Individual `save_candle()` calls for each candle (blocking)
- `save_candles_bulk()` exists but not used in live trading
- Many individual database transactions

**Impact**:
- Slow database operations
- High transaction overhead
- Potential database lock contention

**Recommendation**:
- Batch candle saves (collect for 1-2 seconds, then bulk insert)
- Use `save_candles_bulk()` in live trading path
- Implement async batcher pattern

**Priority**: 🟡 **MEDIUM** - Performance improvement

---

### 2.2 Candle Fetching Parallelization

**Location**: `src/live/live_trading.py:367` (`_update_candles()` called per coin)

**Issue**:
- Each coin fetches candles individually
- 250 coins × 4 timeframes = 1000+ API calls per tick (throttled)
- Could batch fetches more efficiently

**Impact**:
- High API call volume
- Slower tick execution
- Rate limit risks

**Recommendation**:
- Batch candle fetches by timeframe (fetch all 15m candles, then all 1h, etc.)
- Or use bulk fetch APIs if available
- Reduce redundant fetches (share candles across symbols if same timeframe)

**Priority**: 🟢 **LOW** - Optimization (current approach works but could be better)

---

### 2.3 Database Connection Pooling

**Location**: `src/storage/db.py`

**Issue**:
- SQLite connection (not PostgreSQL)
- No explicit connection pooling configuration
- Each database operation creates new session

**Impact**:
- Connection overhead
- Limited scalability
- SQLite not ideal for high-throughput scenarios

**Recommendation**:
- Consider PostgreSQL for production
- Implement connection pooling explicitly
- Use connection pooling for SQLite (via SQLAlchemy)

**Priority**: 🟡 **MEDIUM** - Scalability improvement

---

### 2.4 Unnecessary Position Sync

**Location**: `src/live/live_trading.py:308-310`

**Issue**:
- `get_all_futures_positions()` called
- Then `_sync_positions()` called (which calls `get_all_futures_positions()` again)
- Duplicate API calls

**Impact**:
- Wasted API calls
- Slower tick execution

**Recommendation**:
- Remove duplicate call
- Pass positions from first call to `_sync_positions()`

**Priority**: 🟢 **LOW** - Minor optimization

---

## 3. Architecture Patterns

### 3.1 Mixed Async/Sync Database Operations

**Location**: `src/storage/repository.py`

**Issue**:
- Some functions are sync (`save_candle`, `save_position`)
- Some are async (`async_record_event`)
- Inconsistent patterns make code harder to maintain

**Impact**:
- Developer confusion
- Inconsistent error handling
- Potential blocking in async contexts

**Recommendation**:
- Standardize on async patterns with `asyncio.to_thread()` wrapper
- Create consistent async repository interface
- Document patterns clearly

**Priority**: 🟡 **MEDIUM** - Code quality improvement

---

### 3.2 Error Handling Isolation

**Location**: `src/live/live_trading.py:334-439` (`process_coin()`)

**Issue**:
- Good: Errors in individual coins don't crash system
- Good: Uses `return_exceptions=True` in `asyncio.gather()`
- Issue: Errors logged but no retry logic
- Issue: No circuit breaker pattern for repeatedly failing coins

**Impact**:
- System continues but some coins never process if they keep failing
- No automatic recovery from transient errors

**Recommendation**:
- Implement retry logic with exponential backoff
- Add circuit breaker pattern for coins that repeatedly fail
- Track coin health metrics

**Priority**: 🟡 **MEDIUM** - Resilience improvement

---

### 3.3 Memory Management

**Location**: `src/live/live_trading.py:778-779`

**Issue**:
- Candle buffers limited to 500 candles (good)
- But only in memory (lost on restart)
- No memory pressure monitoring

**Impact**:
- Potential memory leaks if not managed
- No visibility into memory usage

**Recommendation**:
- Monitor memory usage
- Implement memory pressure detection
- Consider LRU cache for old candles

**Priority**: 🟢 **LOW** - Monitoring improvement

---

## 4. Performance Optimizations

### 4.1 Semaphore Concurrency Limit

**Location**: `src/live/live_trading.py:332`

**Issue**:
- Semaphore(20) limits concurrent processing
- Processing 250 coins, so batches of 20
- Could be tuned based on API rate limits and system resources

**Impact**:
- Current limit may be conservative
- Could process more coins in parallel if rate limits allow

**Recommendation**:
- Measure actual API rate limit usage
- Tune semaphore based on:
  - API rate limits (15 req/s for Kraken)
  - System CPU/memory
  - Network latency
- Consider dynamic adjustment based on load

**Priority**: 🟢 **LOW** - Fine-tuning

---

### 4.2 Tick Loop Timing

**Location**: `src/live/live_trading.py:176-179`

**Issue**:
- Dynamic sleep to align with 60-second intervals
- Good: Adapts to tick duration
- Issue: Minimum 5 seconds sleep (could be adjusted)

**Impact**:
- Ensures ~1 minute tick intervals
- Could be tighter if needed

**Recommendation**:
- Current approach is reasonable
- Consider making minimum sleep configurable
- Add metrics for tick duration

**Priority**: 🟢 **LOW** - Already well-designed

---

## 5. Data Flow Issues

### 5.1 Candle Buffer Initialization

**Location**: `src/live/live_trading.py:118-135`

**Issue**:
- Historical data hydration is disabled (commented out)
- Starts with empty candle buffers
- Must accumulate candles from scratch on each restart

**Impact**:
- Slow startup time (5-10 minutes to accumulate 50 candles)
- No historical context

**Recommendation**:
- Re-enable background hydration (but fix GIL issues)
- Or load last 50 candles from database on startup
- Hybrid: Load from DB, then fetch new ones

**Priority**: 🟡 **MEDIUM** - Startup time improvement

---

### 5.2 Data Acquisition Service

**Location**: `src/data/data_acquisition.py` vs `src/live/live_trading.py`

**Issue**:
- `DataAcquisition` service exists but may not be fully utilized
- Live trading uses direct API calls in `_update_candles()`
- Potential duplication of data fetching logic

**Impact**:
- Unclear data flow
- Potential inconsistency

**Recommendation**:
- Clarify role of DataAcquisition service
- Either use it consistently or remove duplication
- Document data flow clearly

**Priority**: 🟡 **MEDIUM** - Architecture clarity

---

## 6. Recommended Action Plan

### Phase 1: Critical Stability (Immediate)
1. ✅ **Fix candle persistence** - Save candles to database in `_update_candles()`
2. ✅ **Increase candle fetch limit** - Change from 10 to 100 candles
3. ✅ **Load candles from DB on startup** - Restore last 50 candles from database

### Phase 2: Efficiency Improvements (Short-term)
1. ✅ **Implement batched database writes** - Use `save_candles_bulk()` 
2. ✅ **Remove duplicate position sync** - Pass positions to `_sync_positions()`
3. ✅ **Standardize async patterns** - Consistent async database operations

### Phase 3: Optimization (Medium-term)
1. ✅ **Implement retry logic** - Exponential backoff for failed operations
2. ✅ **Add circuit breakers** - For repeatedly failing coins
3. ✅ **Optimize candle fetching** - Batch by timeframe
4. ✅ **Tune concurrency limits** - Based on rate limits and system resources

### Phase 4: Monitoring & Observability (Long-term)
1. ✅ **Add metrics** - Memory usage, API call rates, tick duration
2. ✅ **Add health checks** - Per-coin health, system health
3. ✅ **Performance profiling** - Identify bottlenecks

---

## 7. Metrics to Track

To measure improvements:

1. **Startup Time**: Time from start to first signal generation
2. **Tick Duration**: Average time per tick execution
3. **API Call Rate**: Calls per second/minute
4. **Database Write Rate**: Writes per second
5. **Memory Usage**: Peak memory usage
6. **Coin Processing Rate**: Coins processed per tick
7. **Error Rate**: Errors per tick/hour
8. **Candle Cache Hit Rate**: Percentage of candles from cache vs API

---

## Summary

**Critical Issues**: 1 (candle persistence)  
**High Priority**: 0  
**Medium Priority**: 7  
**Low Priority**: 5  

The system is stable and functional but has opportunities for significant efficiency improvements, especially around data persistence and database operations.