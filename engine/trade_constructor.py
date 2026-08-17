"""
Trade Constructor - Converts ta_source TA signals into actionable option trades.

When a signal is purely technical analysis (chart + commentary, no explicit trade),
this module constructs the optimal option contract to express that thesis.
"""

import logging
import re
from dataclasses import dataclass
from typing import Optional
from datetime import date, timedelta


from utils import market_time

logger = logging.getLogger(__name__)


@dataclass
class ConstructedTrade:
    """An actionable trade constructed from a TA signal."""
    ticker: str
    direction: str  # "call" or "put"
    strike: float
    expiry: str  # ISO date
    estimated_premium: Optional[float]
    target_price: float  # King node / target level
    current_price: float
    stop_invalidation: float  # Price level where thesis is invalid
    expected_move_pct: float  # Expected % move in underlying
    timeframe_days: int  # Expected days to target
    confidence: str  # "low", "medium", "high", "extreme"
    reasoning: str  # Human-readable explanation


class TradeConstructor:
    """
    Takes parsed ta_source TA signals and constructs optimal option trades.

    Logic:
    1. Determine direction from current price vs King Node
    2. Pick strike (slightly OTM toward target for better R/R)
    3. Pick expiry (match the heatmap column timeframe + buffer)
    4. Identify invalidation level (nearest gatekeeper in opposite direction)
    5. Estimate the expected move and assess confidence
    """

    def __init__(self, config: dict):
        self.config = config
        # How far OTM to go as % of the move to target
        self.strike_otm_pct = 0.3  # 30% of the way to target
        # Extra days to add to expiry beyond the heatmap timeframe
        self.expiry_buffer_days = 3
        # Minimum days to expiry (never buy weeklies expiring tomorrow)
        self.min_dte = 2

    def construct_from_ta(
        self, parsed_signal, available_strikes: Optional[list] = None
    ) -> Optional[ConstructedTrade]:
        """
        Construct an actionable trade from a parsed TA signal.
        Handles both ta_source (king nodes) and traditional TA (patterns, Fibs).

        parsed_signal: A ParsedSignal with signal_type == TECHNICAL_ANALYSIS
        available_strikes: optional list of tradable strikes for this
            ticker/expiry (from executor.get_tradable_strikes, passed by
            main.py). Session 9 (D9): when provided, the computed strike is
            snapped to the nearest real strike instead of an increment guess.
        """
        ticker = parsed_signal.ticker
        if not ticker:
            logger.warning("TA signal has no ticker, cannot construct trade")
            return None

        # Extract key info
        current_price = parsed_signal.current_price or self._get_current_price(ticker)
        if not current_price:
            logger.error(f"Cannot get current price for {ticker}")
            return None

        target_price = parsed_signal.target_price
        key_levels = parsed_signal.key_levels or []

        # Find the best target from key_levels (priority order)
        king_node = None
        pattern_target = None
        for level in key_levels:
            if level.get("type") == "king_node" and not king_node:
                king_node = level
            elif level.get("type") == "pattern_target" and not pattern_target:
                pattern_target = level

        # Use king node if available, then pattern target, then parsed target_price
        if king_node:
            target_price = king_node["price"]
        elif pattern_target:
            target_price = pattern_target["price"]

        if not target_price:
            # If we still have no target but have direction and key levels,
            # use the nearest significant level in the trade direction
            if parsed_signal.direction and key_levels:
                target_price = self._infer_target_from_levels(
                    current_price, parsed_signal.direction.value, key_levels
                )

        if not target_price:
            logger.warning(f"No target price found for {ticker}")
            return None

        # --- Step 1: Determine direction ---
        if current_price > target_price:
            geometry_direction = "put"
            expected_move_pct = (current_price - target_price) / current_price * 100
        else:
            geometry_direction = "call"
            expected_move_pct = (target_price - current_price) / current_price * 100

        # Session 9 (D25): if the parser's direction CONFLICTS with the
        # price-vs-target geometry, skip construction entirely — the strike
        # and expected-move math below would describe the opposite thesis.
        direction = geometry_direction
        if parsed_signal.direction and parsed_signal.direction.value != geometry_direction:
            logger.warning(
                f"DIRECTION CONFLICT for {ticker}: parser says "
                f"{parsed_signal.direction.value} but price ${current_price:.2f} "
                f"vs target ${target_price:.2f} implies {geometry_direction} — "
                f"skipping trade construction"
            )
            return None

        # --- Step 2: Pick strike ---
        strike = self._select_strike(
            current_price, target_price, direction, available_strikes
        )

        # --- Step 3: Pick expiry ---
        expiry, timeframe_days = self._select_expiry(parsed_signal, key_levels)

        # --- Step 4: Find invalidation level ---
        stop_invalidation = self._find_invalidation(
            current_price, target_price, direction, key_levels
        )

        # --- Step 5: Assess confidence ---
        confidence = self._assess_confidence(
            key_levels, expected_move_pct, parsed_signal
        )

        # --- Step 6: Try to get option premium estimate ---
        estimated_premium = self._estimate_premium(
            ticker, strike, expiry, direction
        )

        reasoning = self._build_reasoning(
            ticker, current_price, target_price, direction, strike,
            expiry, stop_invalidation, king_node, key_levels
        )

        trade = ConstructedTrade(
            ticker=ticker,
            direction=direction,
            strike=strike,
            expiry=expiry,
            estimated_premium=estimated_premium,
            target_price=target_price,
            current_price=current_price,
            stop_invalidation=stop_invalidation,
            expected_move_pct=round(expected_move_pct, 1),
            timeframe_days=timeframe_days,
            confidence=confidence,
            reasoning=reasoning,
        )

        logger.info(
            f"Constructed trade: {ticker} ${strike} {direction} "
            f"exp {expiry} | Target: ${target_price} | "
            f"Confidence: {confidence}"
        )

        return trade

    def _select_strike(
        self,
        current_price: float,
        target_price: float,
        direction: str,
        available_strikes: Optional[list] = None,
    ) -> float:
        """
        Select a strike price that balances cost and probability.

        Strategy: Go slightly OTM toward the target for better R/R,
        but not so far OTM that theta kills us if the move is slow.
        We aim for ~30% of the way from current price to target.
        """
        move = abs(target_price - current_price)
        otm_distance = move * self.strike_otm_pct

        if direction == "put":
            # For puts, strike below current price (toward target)
            raw_strike = current_price - otm_distance
        else:
            # For calls, strike above current price (toward target)
            raw_strike = current_price + otm_distance

        # Session 9 (D9): snap to the real option chain when main.py provides
        # it; the increment guess below remains as fallback only.
        if available_strikes:
            return float(min(available_strikes, key=lambda s: abs(s - raw_strike)))

        # Round to nearest standard strike increment
        strike = self._round_to_strike(raw_strike, current_price)

        return strike

    def _round_to_strike(self, raw_strike: float, current_price: float) -> float:
        """Round to the nearest available strike increment."""
        # Standard strike increments vary by price:
        # < $25: $0.50 or $1 increments
        # $25-$200: $1 or $2.50 increments
        # > $200: $5 increments
        if current_price < 25:
            increment = 0.5
        elif current_price < 100:
            increment = 1.0
        elif current_price < 200:
            increment = 2.5
        else:
            increment = 5.0

        return round(raw_strike / increment) * increment

    def _select_expiry(
        self, parsed_signal, key_levels: list
    ) -> tuple[str, int]:
        """
        Select an expiry date based on the signal context.

        Priority:
        1. Explicit expiry from the signal ("March puts" → March monthly opex)
        2. Heatmap column timeframe
        3. Default: 2 weeks out for swing trades

        Session 9 (D8/D26): all date math goes through utils.market_time (ET
        calendar, weekend- AND holiday-aware).  "0DTE"/"1DTE" are handled
        explicitly, past candidates roll forward, and recognizable relative
        terms can no longer silently fall through to the 2-week default.
        """
        today = market_time.trading_date()

        # If the parser extracted an expiry from the signal, use it
        if parsed_signal.expiry:
            raw = str(parsed_signal.expiry).strip().upper()

            if raw in ("0DTE", "0D", "TODAY"):
                d = today if market_time.is_trading_day(today) else market_time.next_trading_day(today)
                return d.isoformat(), (d - today).days

            # Session 9 (D8): "1DTE" was unhandled and fell through to the
            # 2-week default; it means the next TRADING day.
            if raw in ("1DTE", "1D", "TOMORROW"):
                d = market_time.next_trading_day(today)
                return d.isoformat(), (d - today).days

            # normalize_expiry resolves ISO dates, M/D forms, "WEEKLY", etc.
            # against the market calendar; past dates come back as None.
            iso = market_time.normalize_expiry(parsed_signal.expiry, today)
            if iso:
                exp_date = date.fromisoformat(iso)
                days = (exp_date - today).days

                if days > 5:
                    # For longer-dated, don't add buffer — respect the expiry,
                    # snapped to a valid (non-holiday) Friday
                    friday = market_time.this_or_next_friday(exp_date)
                    return friday.isoformat(), (friday - today).days
                buffered = exp_date + timedelta(days=self.expiry_buffer_days)
                friday = market_time.this_or_next_friday(buffered)
                return friday.isoformat(), (friday - today).days

            logger.warning(
                f"Unresolvable/past expiry {parsed_signal.expiry!r} — "
                f"falling back to month/default expiry selection"
            )

        # Check signal notes for month references ("March puts", "April calls")
        notes = (parsed_signal.notes or "") + " " + (parsed_signal.raw_message or "")
        month_expiry = self._parse_month_expiry(notes, today)
        if month_expiry:
            return month_expiry.isoformat(), (month_expiry - today).days

        # Default: 2 weeks out, snapped to a valid Friday (Session 9, D26)
        friday = market_time.this_or_next_friday(today + timedelta(days=14))
        days = (friday - today).days

        if days < self.min_dte:
            friday = market_time.this_or_next_friday(
                today + timedelta(days=self.min_dte + 1)
            )
            days = (friday - today).days

        return friday.isoformat(), days

    # Month tokens → month number.  Matched as whole word tokens (Session 9,
    # H12) so "mar" no longer matches inside "market", "jan" inside
    # "January-effect" style words, etc.
    _MONTHS = {
        "january": 1, "jan": 1, "february": 2, "feb": 2,
        "march": 3, "mar": 3, "april": 4, "apr": 4, "may": 5,
        "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9,
        "october": 10, "oct": 10, "november": 11, "nov": 11,
        "december": 12, "dec": 12,
    }
    _MONTH_CONTEXT = {"calls", "puts", "options", "expiry", "opex", "monthlies"}

    def _parse_month_expiry(self, text: str, today: date) -> Optional[date]:
        """
        Parse month references like 'March puts' into monthly opex dates.

        Session 9 (H12): month names match as whole word tokens AND must sit
        within 3 words of a trading-context word (calls/puts/options/expiry/
        opex/monthlies).  Plain "may" as a modal verb ("market may bounce")
        no longer produces a May expiry.
        Session 9 (D7): a third Friday already in the past rolls forward to
        the next month (with year roll as needed).
        """
        words = re.findall(r"[a-z]+", text.lower())

        month_num = None
        for i, w in enumerate(words):
            if w not in self._MONTHS:
                continue
            window = words[max(0, i - 3):i] + words[i + 1:i + 4]
            if any(cw in self._MONTH_CONTEXT for cw in window):
                month_num = self._MONTHS[w]
                break

        if month_num is None:
            return None

        year = today.year
        if month_num < today.month:
            year += 1  # Next year if month has passed

        third_friday = self._third_friday(year, month_num)
        # Session 9 (D7): "July puts" said after July opex → roll to the next
        # month's opex (year-rolls across December)
        while third_friday < today:
            month_num += 1
            if month_num > 12:
                month_num = 1
                year += 1
            third_friday = self._third_friday(year, month_num)

        return third_friday

    @staticmethod
    def _third_friday(year: int, month: int) -> date:
        """
        Monthly opex: the third Friday of the month, moved to the prior
        trading day if that Friday is a market holiday (Session 9, D26 —
        Good-Friday-type holidays otherwise produce nonexistent expiries).
        """
        first_day = date(year, month, 1)
        # Day of week: 0=Monday, 4=Friday
        first_friday = first_day + timedelta(days=(4 - first_day.weekday()) % 7)
        third = first_friday + timedelta(weeks=2)
        if not market_time.is_trading_day(third):
            third = market_time.previous_trading_day(third)
        return third

    def _find_invalidation(
        self,
        current_price: float,
        target_price: float,
        direction: str,
        key_levels: list,
    ) -> float:
        """
        Find the price level where the thesis is invalidated.

        For puts: invalidation is a significant level ABOVE current price
        For calls: invalidation is a significant level BELOW current price
        Also considers scale-in levels (if poster says "scale in at X", price
        reaching X doesn't invalidate — going PAST it does).
        """
        invalidation_candidates = []
        invalidation_types = (
            "gatekeeper", "positive_gamma", "king_node",
            "resistance", "support", "fib_level",
            "head_shoulders_neckline",
        )

        for level in key_levels:
            price = level.get("price", 0)
            level_type = level.get("type", "")
            strength = abs(level.get("strength", 0))

            # Skip scale-in levels — those aren't invalidation points
            if level_type == "scale_in_level":
                continue

            if direction == "put":
                if price > current_price and level_type in invalidation_types:
                    invalidation_candidates.append((price, strength))
            else:
                if price < current_price and level_type in invalidation_types:
                    invalidation_candidates.append((price, strength))

        if invalidation_candidates:
            if direction == "put":
                invalidation_candidates.sort(key=lambda x: x[0])
            else:
                invalidation_candidates.sort(key=lambda x: -x[0])
            return invalidation_candidates[0][0]

        # Fallback: 5% beyond current price in the wrong direction
        if direction == "put":
            return round(current_price * 1.05, 2)
        else:
            return round(current_price * 0.95, 2)

    def _infer_target_from_levels(
        self, current_price: float, direction: str, key_levels: list
    ) -> Optional[float]:
        """
        When no explicit target exists, infer one from key levels.
        Pick the most significant level in the trade direction.
        """
        candidates = []
        target_types = (
            "king_node", "pattern_target", "support", "resistance",
            "fib_level", "positive_gamma", "negative_gamma",
        )

        for level in key_levels:
            price = level.get("price", 0)
            level_type = level.get("type", "")
            strength = abs(level.get("strength", 0))

            if level_type not in target_types:
                continue

            if direction == "put" and price < current_price:
                candidates.append((price, strength))
            elif direction == "call" and price > current_price:
                candidates.append((price, strength))

        if not candidates:
            return None

        # Pick the strongest level, or if similar strength, the nearest one
        candidates.sort(key=lambda x: (-x[1], abs(x[0] - current_price)))
        return candidates[0][0]

    def _assess_confidence(
        self, key_levels: list, expected_move_pct: float, parsed_signal
    ) -> str:
        """
        Assess trade confidence based on:
        - King node strength (ta_source) or pattern quality (traditional TA)
        - Distance to target
        - Gatekeepers / resistance in the way
        - Source priority
        - Whether the poster revealed their own position (high conviction indicator)
        """
        score = 50  # Start neutral

        # --- ta_source-specific scoring ---
        king_strength = 0
        max_other_strength = 0
        gatekeepers_in_way = 0

        for level in key_levels:
            strength = abs(level.get("strength", 0))
            level_type = level.get("type", "")

            if level_type == "king_node":
                king_strength = strength
            elif level_type == "gatekeeper":
                gatekeepers_in_way += 1
            elif strength > 0:
                max_other_strength = max(max_other_strength, strength)

        if king_strength > 0 and max_other_strength > 0:
            dominance = king_strength / max_other_strength
            if dominance > 3:
                score += 25
            elif dominance > 1.5:
                score += 15
            elif dominance > 1:
                score += 5

        # --- Traditional TA scoring ---
        pattern_types_found = set()
        fib_levels_found = 0

        for level in key_levels:
            level_type = level.get("type", "")
            if level_type == "pattern_target":
                pattern_types_found.add("pattern")
            elif level_type == "fib_level":
                fib_levels_found += 1
            elif level_type in ("support", "resistance"):
                pattern_types_found.add("sr_level")
            elif level_type == "head_shoulders_neckline":
                pattern_types_found.add("h_and_s")
            elif level_type == "scale_in_level":
                # Poster has a scale-in plan = they're committed = higher conviction
                score += 10

        # Multiple confirming levels = higher confidence
        if len(pattern_types_found) >= 2:
            score += 15  # Multiple pattern types agree
        elif len(pattern_types_found) >= 1:
            score += 8

        if fib_levels_found >= 3:
            score += 10  # Multiple Fib levels = well-analyzed

        # --- Position reveal scoring ---
        notes = (parsed_signal.notes or "").lower()
        raw = (parsed_signal.raw_message or "").lower()
        combined = notes + " " + raw

        # If the poster reveals they're already in the trade = strong conviction
        position_phrases = [
            "already in", "chilling on my", "holding", "in some puts",
            "in some calls", "loaded", "positioned",
        ]
        if any(phrase in combined for phrase in position_phrases):
            score += 15

        # --- Universal scoring ---

        # Expected move size
        if expected_move_pct < 3:
            score += 10
        elif expected_move_pct < 5:
            score += 5
        elif expected_move_pct < 10:
            score -= 5
        else:
            score -= 15

        # Gatekeepers / resistance in the way
        score -= gatekeepers_in_way * 10

        # Source priority
        if parsed_signal.source_priority == "high":
            score += 10
        elif parsed_signal.source_priority == "low":
            score -= 10

        # Map to confidence label
        if score >= 75:
            return "extreme"
        elif score >= 60:
            return "high"
        elif score >= 40:
            return "medium"
        else:
            return "low"

    def _estimate_premium(
        self, ticker: str, strike: float, expiry: str, direction: str
    ) -> Optional[float]:
        """Try to get a real-time premium estimate from Robinhood."""
        try:
            data = self.executor._get_option_market_data(ticker, expiry, strike, direction) if getattr(self, "executor", None) else None
            data = [ [data] ] if data else None,
                optionType=direction,
            )
            if data and len(data) > 0:
                entry = data[0][0] if isinstance(data[0], list) else data[0]
                bid = float(entry.get("bid_price", 0))
                ask = float(entry.get("ask_price", 0))
                return round((bid + ask) / 2, 2)
        except Exception as e:
            logger.debug(f"Could not get premium estimate: {e}")
        return None

    def _get_current_price(self, ticker: str) -> Optional[float]:
        """Get current stock price from Robinhood."""
        try:
            quote = None  # underlying quote comes from the executor backend when available
            if quote and quote[0]:
                return float(quote[0])
        except Exception as e:
            logger.debug(f"Could not get price for {ticker}: {e}")
        return None

    def _build_reasoning(
        self,
        ticker: str,
        current_price: float,
        target_price: float,
        direction: str,
        strike: float,
        expiry: str,
        stop_invalidation: float,
        king_node: Optional[dict],
        key_levels: list,
    ) -> str:
        """Build a human-readable explanation of the trade construction."""
        king_strength = ""
        if king_node:
            king_strength = f" (strength: ${king_node.get('strength', '?')})"

        gatekeeper_warnings = []
        for level in key_levels:
            if level.get("type") == "gatekeeper":
                gatekeeper_warnings.append(
                    f"  Gatekeeper at ${level['price']}: {level.get('description', '')}"
                )

        lines = [
            f"ta_source TA TRADE: {ticker}",
            f"Current: ${current_price:.2f} → King Node target: ${target_price:.2f}{king_strength}",
            f"Direction: {direction.upper()}S (price {'above' if direction == 'put' else 'below'} king node → pulled {'down' if direction == 'put' else 'up'})",
            f"Strike: ${strike} {direction} (exp {expiry})",
            f"Invalidation: ${stop_invalidation:.2f} (thesis breaks if price moves here)",
            f"Expected move: {abs(current_price - target_price):.2f} ({abs(current_price - target_price) / current_price * 100:.1f}%)",
        ]

        if gatekeeper_warnings:
            lines.append("Gatekeeper warnings (may block or slow the move):")
            lines.extend(gatekeeper_warnings)

        return "\n".join(lines)
