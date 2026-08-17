# ARCHITECTURE.md — System Design & Feature Reference

This document covers every feature of the Signal Trader Agent in detail, including
the reasoning behind design decisions, data flows, edge cases, and configuration.
It's written to be the single document needed to understand the entire system.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Signal Ingestion](#2-signal-ingestion)
3. [AI Signal Parsing](#3-ai-signal-parsing)
4. [Decision Engine](#4-decision-engine)
5. [Position Sizing](#5-position-sizing)
6. [PDT Protection](#6-pdt-protection)
7. [Trade Execution](#7-trade-execution)
8. [Trade Management](#8-trade-management)
9. [Proportional Trimming](#9-proportional-trimming)
10. [Runner Policy](#10-runner-policy)
11. [Breakdown Channels](#11-breakdown-channels)
12. [Context Stores](#12-context-stores)
13. [Startup Sequence](#13-startup-sequence)
14. [Logging System](#14-logging-system)
15. [Configuration Reference](#15-configuration-reference)
16. [Data Flow Examples](#16-data-flow-examples)

---

## 1. System Overview

### Purpose
Follow trade-alert channels ("callers") and technical-analysis sources with a
small live options account. The reference deployment mirrored a caller's
challenge account of the same size, so the contract multiplier is 1.0 — a
direct mirror. The system automates what a human signal-follower would do:
read the signal, decide sizing, place the order, manage the position, and exit
when the caller does.

### Core Principle
**Capital efficiency over over-protection.** The reference configuration is a
deliberately aggressive small-account pilot: the account floor and per-trade
caps are switched off in config, and the hard stops are the daily circuit
breaker (40% loss halts all trading), the expensive-contract guard, and the
PDT gate. Every one of these is a config value — a conservative posture is a
one-line change, and the gate order below is unchanged either way.

### Pipeline
```
Signal Source → Pre-filter → Parser → Decision Engine → Execution → Trade Manager → Exit
                    │            │             │                          │
               Obvious noise  Breakdown/TA  PDT gate               PDT protection
               dropped free   stores        Account floor (off)    Proportional trims
                                            Circuit breaker        Runner policy
                                                                   Profit tiers
                                                                   Stop losses
```

---

## 2. Signal Ingestion

### Message Source (`ingest/jsonl_source.py`)

The public tree ships a generic message-source contract: any ingestion adapter
yields message dicts (content, author, channel, attached images, metadata) and
the rest of the pipeline is source-agnostic. `jsonl_source.py` implements the
contract over a JSONL file for replay, backtesting, and evals; a real-time
adapter implements the same iterator contract.

The original private deployment ingested from live chat and social TA sources.
Those adapters are excluded from the public tree (platform-ToS and third-party
content constraints — see PORTING_NOTES.md); everything downstream of the
contract is unchanged.

**Channel types** (from config, attached as `channel_meta`):

| Type | Config key | Behavior |
|------|-----------|----------|
| `caller` | `type: "caller"` | Alert channels. Generates trades. Has `management_style`. |
| `breakdown` | `type: "breakdown"` | Thesis/analysis. Stores context, never trades. Has `linked_caller`. |

**Management styles** (caller channels only):

| Style | Meaning | Agent behavior |
|-------|---------|---------------|
| `managed` | Caller posts exits, trims, stop updates | Follow caller exits/trims |
| `fire_and_forget` | Caller posts entries only, never manages | Agent manages independently with stops/tiers |

**Message processing** (adapter responsibilities):
1. Author filter check — non-allowed authors are dropped before further processing
2. Image attachments resolved to bytes with media type (PNG, JPG, JPEG, GIF, WEBP)
3. Urgency tagging (e.g. broadcast mentions)
4. `channel_meta` dict built with type, management_style, linked_caller, author, IDs
5. Message dispatched to the parser pipeline

---

## 3. AI Signal Parsing

### Parser (`parser/signal_parser.py`)

The parser uses Claude (Sonnet 4.5) to analyze every message and extract structured
trading data. It handles both text-only and text+image signals.

### ParsedSignal Dataclass

```python
@dataclass
class ParsedSignal:
    signal_type: SignalType     # entry, exit, trim, stop_update, management, technical_analysis, noise
    ticker: str                 # "SPY", "TSLA", etc.
    direction: Direction        # call, put, or None
    strike: float               # Strike price
    expiry: str                 # ISO date string
    entry_price: float          # Entry price per contract
    stop_loss: float            # Stop loss % or price
    target_price: float         # Profit target
    sizing_hint: str            # "starter", "light", "standard", "heavy"
    urgency: Urgency            # immediate, normal, low
    is_0dte: bool               # Expiring today
    key_levels: list            # Support/resistance levels from TA
    notes: str                  # Free-form context
    source: str                 # Where it came from
    raw_message: str            # Original text
    caller_contracts: int       # How many contracts the caller bought/has
    trim_contracts: int         # How many the caller is trimming
```

### Signal Types

| Type | Meaning | Route |
|------|---------|-------|
| `entry` | New trade signal | → Decision Engine → Execution |
| `exit` | Close entire position | → Trade Manager (follow caller) |
| `trim` | Partial position reduction | → Trade Manager (proportional trim) |
| `stop_update` | New stop loss level | → Trade Manager (update SL) |
| `management` | Status update, runner signal | → Trade Manager (runner policy) |
| `technical_analysis` | Chart/TA analysis | → Context Store + Trade Constructor |
| `noise` | Non-trading content | → Discarded |

### Parser Prompt Highlights

The parser prompt includes extensive guidance for:

**Brokerage screenshot format** (Robinhood-style contract symbols):
```
Contract symbol: -SPY260320P650
- Ticker: SPY
- Date: YYMMDD (260320 = 2026-03-20)
- Type: C=Call, P=Put
- Strike: 650
- Prefix "-" = short/sold position
```
Screenshot data always overrides conflicting text data.

**Sizing hint extraction**:
- "ONE starter" / "small" → starter
- "Light size" → light
- "Load the boat" / "Large ports only" → heavy
- No hint → standard

**Expiry parsing**:
- "0DTE" / "0d" → today's date
- "2/14" → nearest matching date (month-first format)
- "Friday" / "next week" → calculated date
- "weeklies" → this Friday

**Runner detection**:
- "X runners with Y% profit SL" → management signal with runner data
- "Letting the rest ride" → runner signal
- "Runners at X%" → status update

**Noise vs context distinction**:
- "Still sitting on it" → management (holding), NOT noise
- "Thesis is solid 205 floor" → technical analysis, NOT noise
- "Scared money don't make it" → noise IF standalone, parse actionable part if paired with entry/SL

### Multi-Image Parsing

`parse_image_signal` accepts a list of images. All images are sent to Claude in a
single request. The prompt instructs: "If there's a brokerage screenshot AND text,
prefer the screenshot data for exact values."

### ta_source Chart Reading

The prompt contains detailed instructions for heatmap_tool/ta_source heatmaps:

**Nodes & Colors**:
- Yellow/bright = positive gamma → price dampening, magnetic, pinning
- Purple/dark = negative gamma → explosive moves
- Green/light blue = neutral → choppy, avoid

**King Node** (★):
- Most influential node, gravitational magnet pulling price
- **Price ABOVE king → pulled DOWN → PUTS** (this is correct, do not invert)
- **Price BELOW king → pulled UP → CALLS**

**Gatekeeper Nodes**:
- Defensive levels that can block price from reaching the king
- If gatekeeper is between price and king, it may reject before reaching king

---

## 4. Decision Engine

### Engine (`engine/decision_engine.py`)

The decision engine receives a `ParsedSignal` and returns a `TradeDecision`. It runs
through gate checks, scores conviction, determines sizing, and sets management rules.

### TradeDecision Dataclass

```python
@dataclass
class TradeDecision:
    action: str              # "execute", "queue", "skip", "manage", "notify_only"
    ticker: str
    direction: str           # "call" or "put"
    strike: float
    expiry: str
    contracts: int
    max_cost: float          # Maximum $ to spend
    stop_loss_pct: float
    entry_price_limit: float # Limit price or None for market
    conviction_score: float  # 0-100
    sizing_tier: str
    is_0dte: bool
    reason: str              # Human-readable explanation
    management_rules: dict
    source_signal: ParsedSignal
```

### Gate Checks (in order)

1. **Account floor**: Disabled (floor = 0). Previously $150 — removed for pilot mode.
2. **Daily circuit breaker**: Daily P&L < -40% of account → skip
3. **PDT gate**: See [PDT Protection](#6-pdt-protection) section
4. **Blocked-ticker filter**: SPY/QQQ and other US ETFs → skip (UK/EU PRIIPs restriction — see `blocked_tickers` in config)
5. **Duplicate check**: Already in position for this ticker → notify only
6. **Scale-in detection**: Entry for ticker we hold → update caller count, notify only

### Conviction Scoring

```
Score = source_weight + urgency_bonus + data_quality_bonus + alignment_bonuses

Source weights:
  paid_caller:           40
  technical_analysis:    35
  unpaid_setup:          15

Urgency:
  @here/@everyone:       +10

Data quality:
  Has entry price:       +5
  Has stop loss:         +5

Alignment:
  TA context matches:    +10   (same ticker, same direction, recent)
  Breakdown backing:     +10   (caller posted thesis < 4 hours ago)
```

**Thresholds**: Low=30, Medium=50, High=70, Extreme=85

**What conviction controls**:
- Stop loss width (high conviction = wider stops)
- Management style (high conviction = trailing only, no tier trimming)
- Runner eligibility (85+ = allowed to leave runners)

**What conviction does NOT control**:
- Position sizing (that's based on sizing hints and account %)
- Entry/skip decision (that's gate checks)

---

## 5. Position Sizing

### Cost-Aware Sizing (`_determine_sizing`)

The key insight: "light size" means different things for a $0.50 contract vs a $4.00
contract. The system determines a budget (% of account), then divides by actual
contract cost to get a contract count.

**Process**:
1. Determine tier from caller hint or conviction default
2. Calculate max spend: `account_balance × tier_pct / 100`
3. Calculate contracts: `max_spend / (entry_price × 100)`
4. Apply expensive contract guard (if 1 contract > 60% of account → force starter)
5. Return contracts, max_cost, tier, notes

**Note**: The `absolute_max_pct` cap and the while-loops that trimmed contracts down
after sizing are disabled in pilot mode (`absolute_max_pct = 100`). The tier percentages
themselves are the effective ceiling.

**Budget tiers**:

| Tier | % of account | When used |
|------|-------------|-----------|
| Starter | 10% | "ONE starter", cautious, or expensive contract forced |
| Light | 15% | "Light size" |
| Standard | 25% | No hint given |
| Heavy | 40% | "Load the boat", high conviction |

**Expensive contract protection**:
If 1 contract costs > 60% of account → forced to starter tier.
Example on a $1k account: TSLA $420P at $4.20 = $420/contract = 42%. Under
threshold, allowed. SPY $650P at $6.50 = $650/contract = 65%. Over threshold,
forced starter.

**Contract multiplier**: Set to **1.0** in the reference config — the mirrored
challenge account and the trading account are the same size. For %-based
sizing channels, the multiplier is not relevant.

**Minimum contracts**: Always 1 if affordable.

### Order Fill Timeout

Configurable via `management.order_fill_timeout_seconds` (default: 90).
The bot places a limit order (GFD), polls every second up to this timeout.
On timeout, the order is **cancelled** immediately — never left resting at the broker.
90 seconds gives the price time to come back without letting a stale order float.

---

## 6. PDT Protection

### The Rule
Under $25k, brokers flag margin accounts with 4+ "day trades" in a rolling 5 **business day**
window. A day trade = opening and closing the same security on the same trading day.

*(Note: the shipped `config.example.yaml` neutralises these layers with
`max_day_trades_per_5_days: 999`, reflecting the June 2026 FINRA move from
the PDT rule to intraday margin standards. The mechanism is kept intact and
documented here — set the value to 3 to restore the original behaviour.)*

### Our Policy
**Never trigger the flag.** Losing instant settlement (switching to cash account with
T+1 settlement) is worse than missing trades or holding a losing position overnight.

### Implementation Layers

**Layer 1 — Entry Gate** (`decision_engine.py: evaluate`):

The system budgets day trade slots with an emergency reserve:
- `max_day_trades_per_5_days`: 3 (Robinhood's limit)
- `pdt_emergency_reserve`: 1 (slots reserved for accidental swing stop-outs)
- `spendable`: max(0, remaining - reserve) = intentional 0DTE slots

| State | 0DTE | Swing | Logic |
|-------|------|-------|-------|
| 0/3 used | ✅ | ✅ | 2 spendable, 1 reserve |
| 1/3 used | ✅ | ✅ | 1 spendable, 1 reserve |
| 2/3 used (spendable=0) | ❌ | ✅ (PDT_CAUTION) | 0 spendable, but swings don't guarantee a slot burn |
| 3/3 used | ❌ | ❌ | Hard lockout on all entries |

**PDT_CAUTION mode** (2/3 used, swing entry allowed):
- Stops are widened by 50% to reduce same-day stop-out probability
- Caller's explicit SL is also widened
- Log: "PDT CAUTION: Widening stop from 30% to 45%"

**Layer 2 — Exit Protection** (`trade_manager.py: _execute_full_exit`):

When a stop loss or exit triggers on a same-day position:
1. Check if it's 0DTE (already counted at entry → no new day trade)
2. Check if `day_trade_recorded` flag is set (already sold this position today → dedup)
3. Check `get_day_trades_remaining()`
4. If remaining > 0: proceed, record the day trade, set `day_trade_recorded`
5. If remaining = 0: **BLOCK THE EXIT**
   - Mark position: `pdt_held = True`, `pdt_sell_next_open = True`
   - Disable stop loss (set to 999%)
   - Disable trailing stop
   - Send push notification
   - Sell at next market open (detected in `check_all_positions`)

**Layer 3 — Trim Protection** (`trade_manager.py: _execute_trim`):

Same-day trims also count as day trades. If at the PDT limit and a caller trims,
the system blocks the trim and notifies: "Caller trimmed but we can't follow — PDT."

**Round-trip deduplication**:
Multiple sells of the same position in one day = 1 round trip, not multiple day trades.
The `day_trade_recorded` flag on Position prevents double-counting.

**Persistence** (`logs/pdt_tracker.json`):
Day trade dates are saved to disk. Survives restarts. Auto-prunes entries > 10 business days old.

---

## 7. Trade Execution

### Executors (`execution/`)

All executors implement the `BaseExecutor` contract (`execution/base.py`):
place/cancel orders, fetch quotes and positions, report fills.

- **`paper.py`** (default) — full contract with simulated fills; drives the
  test suite and backtesting.
- **`mcp_executor.py`** — skeleton adapter for the official Robinhood Agentic
  Trading API (equities in beta; options pending on Robinhood's side).
- The original private brokerage executor is excluded from the public tree
  (unofficial-API constraints — see PORTING_NOTES.md).

**Order placement**: Limit orders at the signal's entry price (or current ask if no price).
Polls order status every second for up to `order_fill_timeout_seconds` (default 90s).
On timeout: order is cancelled, logged as `NO_FILL_CANCELLED`.

### Position Dataclass

```python
@dataclass
class Position:
    ticker: str
    direction: str              # call / put
    strike: float
    expiry: str
    contracts: int              # Original count
    entry_price: float
    current_price: float
    high_water_mark: float
    pnl_pct: float
    stop_loss_pct: float
    trailing_stop_active: bool
    trailing_stop_price: float
    management_rules: dict
    order_id: str
    opened_at: str              # ISO datetime
    source: str
    contracts_remaining: int    # After trims

    # Origin tracking (Session 5)
    bot_managed: bool           # True = bot opened, full stop/trail active
                                # False = manually opened by user, track only

    # Caller tracking
    caller_contracts: int       # Caller's original count
    caller_contracts_remaining: int

    # Conviction (for runner decisions)
    conviction_score: float

    # PDT protection
    pdt_held: bool              # Held overnight to avoid PDT
    pdt_held_reason: str
    pdt_sell_next_open: bool
    day_trade_recorded: bool    # Dedup flag for same-day sells
```

---

## 8. Trade Management

### Trade Manager (`management/trade_manager.py`)

The manager runs a 5-second polling loop (`check_all_positions`) that:
1. Gets current price for each position
2. Updates P&L and high water mark
3. **Skips all management for `bot_managed=False` positions** (manual trades)
4. Checks for PDT-held positions needing next-day sell
5. Skips management on PDT-held positions (committed to holding)
6. Checks hard stop loss
7. Checks trailing stop
8. Checks profit tier trimming

### Manual vs Bot Positions

Positions with `bot_managed=False` are tracked for price/P&L visibility but will
**never** have a stop, trailing stop, or trim automatically applied. This protects
positions the user opened manually on Robinhood from being auto-sold by the bot.
These positions are identified at restart via the cross-reference logic (see
[Startup Sequence](#13-startup-sequence)).

### Stop Loss

Standard: 30%. High conviction (≥70): 50%. Caller-specified: always overrides.

PDT CAUTION: widens by 50% (30% → 45%) to avoid same-day stops.

### Trailing Stop

Activates when position hits +50%. Trails by 25% from high water mark.
The stop price only moves up, never down.

### Profit Tiers (Standard Conviction)

Applied to ORIGINAL contract count, not remaining:

| Gain | Trim | Example (4 contracts) |
|------|------|----------------------|
| +50% | 25% of original | Sell 1 |
| +100% | 25% of original | Sell 1 |
| +200% | 25% of original | Sell 1 |
| Remainder | Trailing stop | 1 rides |

### High Conviction Override (≥70)

Skips profit tiers entirely. Only uses trailing stop.

### Single-Contract Management

If the position ends up with only 1 contract, profit tiers are impossible.
Forces trailing-only management regardless of conviction score.

---

## 9. Proportional Trimming

### Problem
Caller bought 15, trims 5 (33%). We have 3. Old system: flat 25% trim → 0.75 → wrong.

### Solution
Match the caller's **ratio**: `caller_trim / caller_total = our_trim / our_total`.

### Priority Cascade (`_calculate_proportional_trim`)

1. **Exact proportion**: Both `trim_contracts` and `caller_contracts` known → exact ratio
2. **Explicit percentage**: "trimming half" → 50%
3. **Estimated proportion**: Use remaining + trimmed as denominator
4. **Notes hints**: "half" → 50%, "third" → 33%
5. **Fallback**: Trim 1 contract

### Rules
- Always trim at least 1 if trimming at all
- Never trim entire position through caller trims (leave 1 runner... unless runner policy says exit)
- If 1 contract remaining and caller trims → full exit

---

## 10. Runner Policy

### Problem
Caller: "4 runners with 50% profit SL." They have 10+ contracts, can afford 4 runners.
We have 1-2 contracts on a small account. Locking capital in a runner means missing the
next high-conviction play.

### Solution
When the parser detects a "runner" management signal:

| Conviction | Action | Rationale |
|-----------|--------|-----------|
| < 85 | **Full exit** | Free capital for next play |
| ≥ 85 | **Keep with profit-lock SL** | Trade is well-backed, worth riding |

### Profit-Lock Stop (85+ conviction)
- `runner_profit_lock_pct`: 30% (protects 30% of current gain)
- Example: up 62%, profit-lock protects 62% × 0.30 = 18.6% gain minimum
- Uses whichever is tighter between profit-lock and caller's stated SL

---

## 11. Breakdown Channels

### Purpose
Callers have separate channels where they post analysis/thesis before alerting.
This context is valuable for conviction scoring but should never generate trades.

### Config
```yaml
- id: "CHANNEL_ID"
  name: "caller_a-breakdown"
  type: "breakdown"
  linked_caller: "caller_a"
```

### Data Flow
1. Message arrives from breakdown channel
2. Parsed normally (can be TA, management, entry preview, or noise)
3. If actionable: stored via `engine.store_breakdown()` and/or `engine.store_ta_context()`
4. **Never routed to execution** — the orchestrator returns after storing
5. When an alert later fires for a matching ticker:
   - `_check_breakdown_backing()` finds the breakdown
   - Checks: within 4 hours? Directional alignment? Same ticker?
   - If match: +10 conviction bonus

---

## 12. Context Stores

### TA Context Store

Stores recent technical analysis signals for cross-referencing with caller entries.
Checked in `_check_ta_alignment()`: same ticker, same direction, recent → +10 conviction.

### Breakdown Context Store

Stores recent caller thesis/analysis. Keeps last 5 breakdowns per ticker. Auto-expires
after 4 hours for conviction matching purposes.

Both stores are in-memory only (not persisted). They reset on restart but rebuild
quickly as new signals come in.

---

## 13. Startup Sequence

*(This section documents the private deployment's orchestrator, which is
excluded from the public tree — see PORTING_NOTES.md. It is kept here because
the restore/cross-reference logic it describes shapes the Position fields and
trade-log format that ship in this repo.)*

On every start, the orchestrator runs these steps in order:

1. **Brokerage login** — exits hard if credentials fail
2. **Account balance** — logged at startup
3. **Anthropic credit check** (`_check_anthropic_credits`):
   - Sends a 1-token ping to Claude Haiku (cheapest/fastest)
   - If credits exhausted: logs hard error + sends notification, bot continues but signals
     will fail to parse. Previously this was silent for 30+ minutes.
4. **Position restore**:
   - Fetches all open option positions from the brokerage
   - Loads `logs/trades.json` and builds a set of `OPEN` entries with no matching `CLOSE`
   - For each brokerage position:
     - **In trade log** → `bot_managed=True` — stop/trail logic resumes immediately
     - **Not in trade log** → `bot_managed=False` — tracked for price/P&L, no auto-exits
   - Logged as `[RESTORED BOT]` or `[RESTORED MANUAL]`
5. **Social-source login**
6. **Notification** — "Agent started. Balance: $X"
7. **Position monitor thread** starts
8. **Social-source polling thread** starts
9. **Chat listener** starts (blocking)

---

## 14. Logging System

### 8-File Structure (`utils/logging_config.py`)

| File | What's logged | Use case |
|------|--------------|----------|
| `agent.log` | Everything (main application log) | General debugging |
| `discord.log` | Every raw source message: channel, author, content, attachment count | Parser tuning, signal replay |
| `social.log` | Every social TA post checked: author, content, image count | Same |
| `parser.log` | Parser input (raw message) → output (ParsedSignal JSON) | Verifying parser accuracy |
| `decisions.log` | Decision engine evaluations: conviction, sizing, action, reason | Understanding why trades were taken/skipped |
| `trades.log` | Order placements, fills, exits, prices, order IDs | Trade audit trail |
| `positions.log` | Position state: price, P&L, HWM, trailing stop, contracts | Position monitoring |
| `errors.log` | All errors across all components | Triage |
| `trades.json` | Structured JSON: every OPEN, CLOSE, TRIM with P&L | Performance analysis + restart cross-reference |
| `pdt_tracker.json` | Day trade dates array + last updated | PDT compliance |

---

## 15. Configuration Reference

### Key Config Sections (`config.yaml`)

**Risk**:
```yaml
risk:
  account_balance_floor: 0            # Disabled (pilot mode)
  max_single_trade_pct: 100           # Disabled (pilot mode)
  max_day_trades_per_5_days: 3        # PDT limit (shipped example uses 999 — see §6 note)
  pdt_emergency_reserve: 1            # Slots reserved for swing stop-outs
  default_stop_loss_pct: 30           # Standard SL
  high_conviction_stop_loss_pct: 50   # Wide SL for high conviction
  circuit_breaker_daily_loss_pct: 40  # Daily loss halt
```

**Sizing**:
```yaml
sizing:
  contract_multiplier: 1.0            # Mirrored account same size — direct mirror
  starter_max_pct: 10
  light_max_pct: 15
  standard_max_pct: 25
  heavy_max_pct: 40
  absolute_max_pct: 100               # Disabled (pilot mode)
  min_contracts: 1
  expensive_contract_threshold_pct: 60  # Force starter if 1 contract > this %
```

**Management**:
```yaml
management:
  order_fill_timeout_seconds: 90      # Cancel unfilled orders after this many seconds
  trailing_stop_activation_pct: 50
  trailing_stop_distance_pct: 25
  profit_tiers: [{gain_pct: 50, trim_pct: 25}, ...]
  follow_caller_exits: true
  high_conviction_trailing_only: true
  runner_policy:
    runner_conviction_threshold: 85
    runner_profit_lock_pct: 30
```

---

## 16. Data Flow Examples

### Example 1: Caller Challenge Entry (Post-Session 5)

```
#caller_a-challenge: "GLW $190 Call 4/17 • 1 Buy 2.75 @everyone"

Pre-filter: passes (has ticker, price, direction)
1. Parser → entry, GLW, call, 190, 2026-04-17, caller_contracts=1, entry_price=2.75
2. Decision Engine:
   - Gate checks: all pass (account floor disabled)
   - Conviction: paid_caller(40) + @everyone(10) = 50
   - Sizing: challenge mode, 1 contract × 1.0 multiplier = 1 contract
   - Cost: $275. 27.5% of account.
3. Execution: limit order 1x GLW $190C 4/17 @ $2.75, polls 90s
4. Trade Manager: opens position, bot_managed=True, 30% stop, trailing stop
```

### Example 2: Restart with Mixed Positions

```
trades.json has: OPEN GLW_190.0_2026-04-17_call (no matching CLOSE)
The brokerage reports: GLW call + SATL call (user added manually)

Restore:
  [RESTORED BOT]    GLW $190.0 call — bot_managed=True, stop/trail ACTIVE
  [RESTORED MANUAL] SATL $X.0  call — bot_managed=False, tracked only, no auto-exits
```

### Example 3: PDT Hold Overnight

```
State: 3/3 day trades used.
10:00 AM: OKLO weekly call entered (swing, not 0DTE).
2:00 PM: Stop loss triggers.

Trade Manager:
1. Detects same-day close
2. Detects non-0DTE (expiry is Friday)
3. Checks PDT: 0 remaining
4. BLOCKS exit: pdt_held=True, pdt_sell_next_open=True
5. Disables stop loss (999%)
6. Sends notification: "⚠️ PDT HOLD: OKLO stop hit but closing would trigger PDT"

Next day 9:31 AM: checks opened_at vs today → different day → sells at market
```

### Example 4: Breakdown → Alert Conviction Boost

```
14:59 #caller_a-breakdown: "QQQ head and shoulders. Nodes weakening." + heatmap chart
→ Breakdown stored: QQQ, direction=put

15:30 #caller_b-alerts: "QQQ $530P 2/14 @here"
→ paid_caller(40) + @here(10) + breakdown_backing(10) = 60
   Without breakdown: 50 → standard sizing
   With breakdown: 60 → still standard but with wider stop (approaching high conviction)
```

---

## Development Notes

### Status at Extraction
- The public tree runs end-to-end on the paper executor: `run_paper.py`
  replays the bundled sample signals through parser → engine → execution
- Pilot-mode reference config: per-trade caps off, circuit breaker +
  expensive-contract guard as hard stops
- Position persistence working: bot vs manual positions correctly
  distinguished on restart via the trades.json cross-reference
- Pre-filter saving API calls on obvious noise
- 277 test functions (362 with parametrisation), all passing in CI

### Known Outstanding Issues
- No T+1 settlement tracking for cash account mode
- No 0DTE forced exit at 3:45 PM ET (could expire worthless)
- Brokerage session expiry (~24h) had no auto-refresh in the private deployment
- Race conditions on positions dict possible under concurrent signal storm (no thread lock)

### Future Improvements
- 0DTE time-based exit (3:45 PM ET hard close)
- Brokerage session health check + auto-relogin
- Web dashboard for monitoring
- Performance analytics (win rate, avg P&L by caller, by conviction tier)
- Earnings calendar integration (warn before holding through earnings)
