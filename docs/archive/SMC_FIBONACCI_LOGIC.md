# SMC and Fibonacci Calculation Logic

## Overview

The system uses **SMC (Smart Money Concepts)** structures and **Fibonacci levels** to score trading signals. These scores help determine signal quality and prioritize trades.

---

## SMC (Smart Money Concepts) Scoring

**Purpose:** Identify institutional trading patterns (order blocks, fair value gaps, break of structure)

**Score Range:** 0-25 points

### SMC Structures Detected

#### 1. Order Block (OB) - 10 points
**What it is:** The last candle of the opposite direction before an impulsive price move.

**Detection Logic:**
- **Bullish OB:** Last DOWN candle before a strong UP move (displacement)
- **Bearish OB:** Last UP candle before a strong DOWN move (displacement)
- Looks back through candle history (configurable lookback)
- Requires a "displacement" (strong move) after the block to confirm
- Displacement threshold is volatility-adjusted (1.5x typical range)

**Example:**
- Price is moving down
- Last green (up) candle appears
- Price then makes a strong downward move
- That green candle becomes a bearish order block

#### 2. Fair Value Gap (FVG) - 8 points
**What it is:** A price gap where one candle's wick doesn't overlap with the previous candle's body.

**Detection Logic:**
- Looks for gaps between candle bodies
- Bullish FVG: Gap upward (current candle's low > previous candle's high)
- Bearish FVG: Gap downward (current candle's high < previous candle's low)
- Must align with current bias (bullish/bearish)

**Example:**
- Candle 1: High = $100
- Candle 2: Low = $102 (gap of $2)
- This creates a bullish FVG between $100-$102

#### 3. Break of Structure (BOS) - 7 points
**What it is:** Confirmation that price has broken through a previous structure (swing high/low).

**Detection Logic:**
- Compares current price to recent swing points
- Bullish BOS: Price breaks above a recent swing high
- Bearish BOS: Price breaks below a recent swing low
- Uses swing point detection to find significant highs/lows
- Can be required or optional (configurable)

**Example:**
- Recent swing high = $105
- Current price breaks above $105
- This confirms a bullish break of structure

### SMC Scoring Summary

```
Score = 0
If Order Block found:     Score += 10
If FVG found:             Score += 8
If BOS confirmed:         Score += 7
Maximum Score:            25 points
```

**Note:** All structures are detected on the 1-hour timeframe for execution signals.

---

## Fibonacci Scoring

**Purpose:** Measure confluence (how close entry price is to key Fibonacci levels)

**Score Range:** 0-20 points

### Fibonacci Level Calculation

#### Step 1: Find Swing Points
- Looks back through last 100 candles (configurable)
- Finds the highest high and lowest low in the lookback window
- These become the swing high and swing low

#### Step 2: Calculate Range
```
Range = Swing High - Swing Low
```

#### Step 3: Calculate Fibonacci Levels

**Retracement Levels (from swing low upward):**
- 23.6% level: `Swing Low + Range × 0.236`
- 38.2% level: `Swing Low + Range × 0.382`
- 50.0% level: `Swing Low + Range × 0.500`
- 61.8% level: `Swing Low + Range × 0.618` (Golden Ratio)
- 78.6% level: `Swing Low + Range × 0.786`

**OTE Zone (Optimal Trade Entry):**
- OTE Low: `Swing Low + Range × 0.705`
- OTE High: `Swing Low + Range × 0.790`
- This is a zone (not a single level) considered optimal for entries

**Extension Levels:**
- 127.2% extension: `Swing Low + Range × 1.272`
- 161.8% extension: `Swing Low + Range × 1.618` (Golden Ratio extension)

### Fibonacci Scoring Logic

The system scores based on how close the **entry price** is to Fibonacci levels:

#### Tier 1: OTE Zone (15 points) - Highest Value
```
If Entry Price is between OTE Low and OTE High:
    Score = 15 points
```

**Example:**
- Swing Low = $100
- Swing High = $120
- Range = $20
- OTE Zone = $114.10 to $115.80
- If entry price = $115.00 → **Score = 15 points**

#### Tier 2: Key Retracement Levels (10 points)
**Levels checked:**
- 38.2% (fib_0_382)
- 61.8% (fib_0_618) - Golden Ratio
- 50.0% (fib_0_500)
- 78.6% (fib_0_786)

**Tolerance:** Entry price must be within 0.2% of the level

```
For each level:
    If |Entry Price - Fib Level| / Fib Level ≤ 0.002 (0.2%):
        Score = 10 points
        Stop checking (highest match wins)
```

**Example:**
- Fib 61.8% level = $112.36
- Entry price = $112.50
- Distance = $0.14 (0.12% of level)
- Since 0.12% < 0.2% → **Score = 10 points**

#### Tier 3: Extension Levels (5 points)
**Levels checked:**
- 127.2% extension (fib_1_272)
- 161.8% extension (fib_1_618)

**Tolerance:** Entry price must be within 0.2% of the extension level

```
If no retracement match and entry near extension:
    Score = 5 points
```

**Example:**
- Fib 161.8% extension = $132.36
- Entry price = $132.50
- Distance = $0.14 (0.11% of extension)
- Since 0.11% < 0.2% → **Score = 5 points**

### Fibonacci Scoring Summary

```
Score = 0
If Entry in OTE Zone (0.705-0.79):          Score = 15 points
Else If Entry near key retracement (0.2%):  Score = 10 points
Else If Entry near extension (0.2%):        Score = 5 points
Maximum Score:                               20 points
```

**Important Notes:**
- Scoring is **mutually exclusive** - only the highest match counts
- OTE zone takes priority over retracements
- Retracements take priority over extensions
- If no match within tolerance → Score = 0

---

## How They Work Together

### In Signal Generation

1. **SMC structures are detected first** (Order Blocks, FVG, BOS)
   - These create the trading signal
   - Required for signal generation

2. **Fibonacci levels are calculated** from recent price swings
   - Used for confluence scoring only
   - Does NOT generate signals (used to validate/scoring)

3. **Both are scored separately:**
   - SMC Score: 0-25 points (structure quality)
   - Fib Score: 0-20 points (entry price confluence)

4. **Total Signal Quality** includes:
   - SMC Score (0-25)
   - Fib Score (0-20)
   - HTF Alignment (0-20)
   - ADX Strength (0-15)
   - Cost Efficiency (0-20)
   - **Total Maximum: 100 points**

### Example Scenario

**Signal: LONG on BTC/USD**

**SMC Detection:**
- ✅ Order Block found: +10 points
- ✅ FVG found: +8 points
- ✅ BOS confirmed: +7 points
- **SMC Score: 25/25**

**Fibonacci Calculation:**
- Swing Low: $60,000
- Swing High: $65,000
- Range: $5,000
- Entry Price: $63,525
- OTE Zone: $63,525 to $63,950
- Entry is in OTE zone → **Fib Score: 15/20**

**Total Quality Score:**
- SMC: 25
- Fib: 15
- HTF: 18 (example)
- ADX: 12 (example)
- Cost: 16 (example)
- **Total: 86/100** (High quality signal)

---

## Key Design Principles

### Deterministic
- Same input candles → Same SMC/Fib calculations
- No randomness or external factors
- Reproducible results

### Confluence-Based
- Fibonacci is used for **confluence** (validation), not signal generation
- Higher confluence (better Fib alignment) = higher quality signal
- But Fib alone doesn't create trades

### Timeframe Hierarchy
- **SMC structures:** Detected on 1-hour candles
- **Fibonacci levels:** Calculated from 1-hour candles
- **Bias determination:** Uses 4-hour and daily candles

### Caching
- Fibonacci levels are cached to avoid recalculation
- Cache key: (symbol, last_candle_timestamp)
- Cache cleanup prevents memory leaks

---

## When Scores Are Zero

**SMC Score = 0:**
- No order blocks found
- No FVG detected
- No BOS confirmed
- Usually means: No valid trading structure detected

**Fib Score = 0:**
- No Fibonacci levels calculated (insufficient swing data)
- Entry price is not near any Fib level (outside tolerance)
- Usually means: Entry price has no Fibonacci confluence

**Both Zero:**
- This is normal for `NO_SIGNAL` scenarios
- System only calculates scores when a valid signal exists
- Most coins will show zero scores most of the time (no signal)

---

## Configuration

Both SMC and Fibonacci calculations are configurable:

- **Order Block Lookback:** How far back to search for order blocks
- **Displacement Threshold:** How strong a move must be to confirm OB
- **Fibonacci Lookback:** How many candles to analyze (default: 100)
- **Tolerance Levels:** How close price must be to Fib levels (default: 0.2%)

All parameters can be adjusted in the strategy configuration.
