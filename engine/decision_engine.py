"""
Decision Engine - Scores parsed signals, determines position sizing,
and decides whether to execute, wait, or skip.
"""

import logging
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, date
from pathlib import Path
import json

from parser.signal_parser import ParsedSignal, SignalType, Urgency
from utils.market_time import trading_date, trading_days_ago, normalize_expiry

logger = logging.getLogger(__name__)


@dataclass
class TradeDecision:
    """The engine's output: what to do with a parsed signal."""
    action: str  # "execute", "queue", "skip", "manage", "notify_only"
    ticker: str
    direction: str  # "call" or "put"
    strike: float
    expiry: str
    contracts: int
    max_cost: float  # Maximum $ to spend on this trade
    stop_loss_pct: float
    entry_price_limit: Optional[float]  # Limit price, or None for market
    conviction_score: float  # 0-100
    sizing_tier: str
    is_0dte: bool
    reason: str  # Human-readable explanation of the decision
    management_rules: dict  # How to manage the trade post-entry
    management_style: str = ""  # "challenge", "managed", or "fire_and_forget"
    source_signal: ParsedSignal = None
    # Timestamp (time.time()) when the originating message first hit _on_signal_received.
    # Set by main.py after evaluate(); used to measure end-to-end pipeline latency.
    received_at: float = 0.0


class DecisionEngine:
    def __init__(self, config: dict):
        self._config = config  # Full config — needed for top-level fields like blocked_tickers
        self.risk_config = config["risk"]
        self.sizing_config = config["sizing"]
        self.scoring_config = config["scoring"]
        self.mgmt_config = config["management"]
        # Session 9: engine-level tunables (e.g. ta_context_max_age_hours)
        self.engine_config = config.get("engine", {}) or {}

        # Account type: "cash" or "margin"
        self.account_type = config.get("account", {}).get("type", "margin")

        # Track state
        self.active_trades: list[dict] = []
        self.daily_pnl: float = 0.0
        # Session 9: ET trading date, not host-local (UK host rolls at 7-8 PM ET)
        self._last_pnl_reset_date: str = trading_date().isoformat()

        # Session 9: market-regime sizing hook. main.py calls set_regime() after
        # RegimeDetector.refresh(); scales percentage-mode tier sizing only.
        self._regime_multiplier: float = 1.0
        self._regime_label: str = "normal"

        # PDT day trade tracker: rolling 5 business day window
        # Stores dates of each day trade (round-trip same day)
        # A "day trade" = opening AND closing the same position in the same trading day
        # 0DTE counts automatically since it must close same day (expiry)
        # 2026-08-04: resolved at construction — see the ledger-clobber fix
        # in TradeManager.__init__ (same relative-path/cwd-flip hazard).
        self._pdt_log_path = Path("./logs/pdt_tracker.json").resolve()
        self._pdt_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._day_trade_dates: list[str] = self._load_pdt_log()
        self._max_day_trades = self.risk_config.get("max_day_trades_per_5_days", 3)

        # TA context store: recent TA signals for cross-referencing
        # {ticker: {"direction": str, "target": float, "key_levels": list, "timestamp": str, "source": str}}
        self.ta_context: dict[str, dict] = {}

        # Breakdown context store: caller thesis/analysis from breakdown channels
        # Stores recent breakdowns by ticker AND by caller for matching
        # {ticker: [{"caller": str, "direction": str, "thesis": str, "tickers_mentioned": list, 
        #            "timestamp": str, "key_levels": list}]}
        self.breakdown_context: dict[str, list[dict]] = {}

    def _reset_daily_pnl_if_new_day(self):
        """Reset the daily P&L accumulator at the start of a new ET trading day."""
        today_str = trading_date().isoformat()
        if today_str != self._last_pnl_reset_date:
            logger.info(
                f"New trading day: resetting daily P&L from ${self.daily_pnl:+.2f} to $0.00"
            )
            self.daily_pnl = 0.0
            self._last_pnl_reset_date = today_str

    def record_realized_pnl(self, amount: float):
        """
        Session 9 (C1): bridge for realized P&L booked by TradeManager exits and
        trims.  The daily-loss circuit breaker in evaluate() gates on
        self.daily_pnl, but nothing ever wrote to it — the headline "40% daily
        loss halts trading" safety net could never fire.  TradeManager calls
        this after each realized P&L event, making the breaker live.
        """
        self._reset_daily_pnl_if_new_day()
        self.daily_pnl += amount
        logger.info(
            f"Realized P&L recorded: ${amount:+.2f} "
            f"(day total: ${self.daily_pnl:+.2f})"
        )

    def set_regime(self, multiplier: float, label: str):
        """
        Session 9: store the current market-regime sizing multiplier (from
        engine/regime.py via main.py). Percentage-mode tier sizing is scaled by
        this; challenge mirror mode is NOT (we mirror the caller's count).
        """
        try:
            self._regime_multiplier = float(multiplier) if multiplier is not None else 1.0
        except (TypeError, ValueError):
            self._regime_multiplier = 1.0
        self._regime_label = label or "normal"
        logger.info(
            f"Regime set: {self._regime_label} "
            f"(sizing multiplier x{self._regime_multiplier:g})"
        )

    def _prune_ta_context(self):
        """
        Session 9 (D21): drop stale TA context entries.  Called at the start of
        evaluate() so the scoring path (_check_ta_alignment) never mutates the
        store while reading it.  Entries with unparseable timestamps are also
        dropped — freshness can't be verified, so they must not boost conviction.
        """
        max_age_hours = self.engine_config.get("ta_context_max_age_hours", 24)
        stale = []
        for ticker, ta in self.ta_context.items():
            try:
                ta_time = datetime.fromisoformat(ta.get("timestamp", ""))
                age_hours = (datetime.utcnow() - ta_time).total_seconds() / 3600
                if age_hours > max_age_hours:
                    stale.append(ticker)
            except (ValueError, TypeError):
                stale.append(ticker)
        for ticker in stale:
            del self.ta_context[ticker]
            logger.debug(f"Pruned stale TA context for {ticker}")

    def store_breakdown(self, parsed_signal: ParsedSignal, caller: str = ""):
        """
        Store a breakdown analysis for cross-referencing with future alerts.
        Breakdowns are the caller's thesis — when they explain WHY they're looking
        at a ticker, it means the subsequent alert is more researched.
        
        Also stores by mentioned tickers so if caller_a posts breakdown about QQQ
        and later an alert fires for QQQ puts, we can link them.
        """
        tickers = []
        if parsed_signal.ticker:
            tickers.append(parsed_signal.ticker.upper())

        # Extract any additional tickers mentioned in notes
        if parsed_signal.notes:
            import re
            mentioned = re.findall(r'\$([A-Z]{1,5})', parsed_signal.notes)
            tickers.extend([t.upper() for t in mentioned])
            # Also check for untagged ticker mentions
            for common in ["SPY", "QQQ", "SPX", "AAPL", "TSLA", "AMZN", "NVDA", "META", "GOOGL", "MSFT"]:
                if common in parsed_signal.notes.upper() and common not in tickers:
                    tickers.append(common)

        breakdown_entry = {
            "caller": caller,
            "direction": parsed_signal.direction.value if parsed_signal.direction else None,
            "thesis": parsed_signal.notes or parsed_signal.raw_message[:200],
            "tickers_mentioned": tickers,
            "key_levels": parsed_signal.key_levels,
            "timestamp": datetime.utcnow().isoformat(),
        }

        for ticker in tickers:
            if ticker not in self.breakdown_context:
                self.breakdown_context[ticker] = []
            self.breakdown_context[ticker].append(breakdown_entry)
            # Keep only last 5 breakdowns per ticker
            self.breakdown_context[ticker] = self.breakdown_context[ticker][-5:]

        logger.info(
            f"Breakdown stored: caller={caller}, tickers={tickers}, "
            f"direction={breakdown_entry['direction']}, "
            f"thesis={breakdown_entry['thesis'][:80]}..."
        )

    def _check_breakdown_backing(self, signal: ParsedSignal) -> bool:
        """
        Check if there's a recent breakdown backing this signal.
        E.g., caller posted thesis about QQQ weakness in breakdown channel,
        then alert fires for QQQ puts → this is a backed/researched trade.
        
        Returns True if a matching breakdown was found within the last 4 hours.
        """
        if not signal.ticker:
            return False

        ticker = signal.ticker.upper()
        breakdowns = self.breakdown_context.get(ticker, [])

        if not breakdowns:
            return False

        # Check freshness (within 4 hours)
        for bd in reversed(breakdowns):  # Check most recent first
            try:
                bd_time = datetime.fromisoformat(bd["timestamp"])
                hours_old = (datetime.utcnow() - bd_time).total_seconds() / 3600
            except (ValueError, TypeError):
                continue

            if hours_old > 4:
                continue

            # Check directional alignment if both have direction
            bd_dir = bd.get("direction")
            sig_dir = signal.direction.value if signal.direction else None

            if bd_dir and sig_dir and bd_dir != sig_dir:
                # Session 9 (D20): the MOST RECENT fresh breakdown is the
                # caller's current thesis.  On conflict, stop — do NOT keep
                # scanning older breakdowns for one that happens to align.
                logger.info(
                    f"Breakdown CONFLICT: {ticker} most recent breakdown says "
                    f"{bd_dir}, alert says {sig_dir} — no backing bonus"
                )
                return False

            logger.info(
                f"BREAKDOWN BACKING: {ticker} alert backed by breakdown "
                f"({hours_old:.1f}h ago) | Thesis: {bd['thesis'][:60]}..."
            )
            return True

        return False

    def store_ta_context(self, parsed_signal):
        """Store a TA signal for cross-referencing with future caller entries."""
        if not parsed_signal.ticker:
            return

        ticker = parsed_signal.ticker.upper()
        self.ta_context[ticker] = {
            "direction": parsed_signal.direction.value if parsed_signal.direction else None,
            "target": parsed_signal.target_price,
            "key_levels": parsed_signal.key_levels,
            "timestamp": datetime.utcnow().isoformat(),
            "source": parsed_signal.source,
        }
        logger.info(
            f"TA context stored: {ticker} | "
            f"Direction: {self.ta_context[ticker]['direction']} | "
            f"Target: {self.ta_context[ticker]['target']}"
        )

    def evaluate(
        self,
        signal: ParsedSignal,
        account_balance: float,
        existing_positions: list[dict],
        sizing_mode: str = "percentage",
        management_style: str = "managed",
    ) -> TradeDecision:
        """
        Evaluate a parsed signal and return a trade decision.
        
        sizing_mode: "challenge" (direct multiplier on caller contracts) or
                     "percentage" (conviction-based % of account)
                     Set by channel config, not by the parser.
        """
        # --- Daily P&L reset at start of new trading day ---
        self._reset_daily_pnl_if_new_day()

        # Session 9 (D21): prune stale TA context outside the scoring path
        self._prune_ta_context()

        # Session 9 (D13): route exit/trim/management signals BEFORE the gate
        # checks.  Gates (circuit breaker, PDT, balance floor) exist to block
        # new ENTRIES — they must never suppress an exit, which matters most
        # during exactly the sessions when gates are firing.
        if signal.signal_type in (
            SignalType.EXIT,
            SignalType.TRIM,
            SignalType.STOP_UPDATE,
            SignalType.MANAGEMENT,
        ):
            return self._management_decision(signal, existing_positions)

        # Noise - skip (needs no balance or gates)
        if signal.signal_type == SignalType.NOISE:
            return self._skip_decision(signal, "Noise / not actionable")

        # TA signal - store for context but don't trade directly
        if signal.signal_type == SignalType.TECHNICAL_ANALYSIS:
            return self._ta_decision(signal)

        # --- Entry signal evaluation from here on ---

        # Session 9 (D14): guard unknown/zero balance before ANY balance math
        # (executor returns None on fetch failure; 0 would ZeroDivision in sizing)
        if account_balance is None or account_balance <= 0:
            return self._skip_decision(signal, "account balance unavailable")

        # Session 9 (H8): normalize expiry ONCE at decision time.  "1DTE",
        # "WEEKLY", M/D forms etc. become ISO here (ET-calendar, holiday-aware);
        # an unresolvable expiry is skipped rather than sent downstream.
        # is_0dte is DERIVED from the normalized date, overriding the parser's
        # flag (fixes constructed-TA trades hardcoding is_0dte=False).
        normalized_expiry = normalize_expiry(signal.expiry)
        if normalized_expiry is None:
            return self._skip_decision(
                signal, f"unresolvable expiry: {signal.expiry!r}"
            )
        derived_0dte = normalized_expiry == trading_date().isoformat()
        if derived_0dte != signal.is_0dte:
            logger.info(
                f"Session 9 (H8): overriding parser is_0dte={signal.is_0dte} → "
                f"{derived_0dte} for {signal.ticker} expiry {normalized_expiry}"
            )
            signal.is_0dte = derived_0dte  # keep gates/stops/reason consistent

        # --- Gate checks (hard stops — entries only) ---

        # Circuit breaker: account too low (floor=0 disables this check)
        if self.risk_config["account_balance_floor"] > 0 and account_balance < self.risk_config["account_balance_floor"]:
            return self._skip_decision(signal, "Account below safety floor")

        # Circuit breaker: daily loss limit
        if self.daily_pnl < -(account_balance * self.risk_config["circuit_breaker_daily_loss_pct"] / 100):
            return self._skip_decision(signal, "Daily loss circuit breaker triggered")

        # ================================================================
        # PDT PROTECTION (MARGIN ACCOUNTS ONLY)
        # Cash accounts have no PDT restrictions — skip entirely.
        # ================================================================
        pdt_caution = False

        if self.account_type == "margin":
            day_trades_in_window = self.get_day_trades_in_window()
            remaining = self._max_day_trades - day_trades_in_window
            emergency_reserve = self.risk_config.get("pdt_emergency_reserve", 1)
            spendable = max(0, remaining - emergency_reserve)

            if remaining <= 0:
                return self._skip_decision(
                    signal,
                    f"PDT LOCKOUT: {day_trades_in_window}/{self._max_day_trades} day trades "
                    f"in 5-day window. ALL entries blocked until a trade falls out. "
                    f"Next slot opens when oldest day trade is >5 business days ago.",
                )

            if signal.is_0dte and spendable <= 0:
                return self._skip_decision(
                    signal,
                    f"PDT BUDGET: {day_trades_in_window}/{self._max_day_trades} day trades used. "
                    f"0DTE blocked — remaining {remaining} slot(s) reserved as emergency buffer "
                    f"(pdt_emergency_reserve: {emergency_reserve}). "
                    f"Swing entries still allowed.",
                )

            pdt_caution = (spendable <= 0) and not signal.is_0dte
            if pdt_caution:
                logger.warning(
                    f"PDT CAUTION: {day_trades_in_window}/{self._max_day_trades} day trades used. "
                    f"Allowing swing entry for {signal.ticker} but widening stops to "
                    f"minimize chance of same-day stop-out."
                )

        # "Large ports only" = skip for small accounts
        if signal.sizing_hint == "skip":
            return self._skip_decision(
                signal,
                f"LARGE_PORTS_ONLY: Caller flagged this as large accounts only. "
                f"Skipping for our small account.",
            )

        # Blocked tickers — configured in config.yaml under blocked_tickers.
        # Any signal for a blocked ticker is skipped entirely regardless of DTE.
        # UK Robinhood does not support SPY/QQQ options; SPX is used instead.
        blocked = [t.upper() for t in self._config.get("blocked_tickers", [])]
        if signal.ticker and signal.ticker.upper() in blocked:
            logger.info(f"Skipping {signal.ticker} — ticker is on the blocked list")
            return self._skip_decision(
                signal,
                f"{signal.ticker} is blocked (not available / not tradeable on this account)",
            )

        # Score the signal
        conviction_score = self._calculate_conviction(signal)

        # Determine sizing (cost-aware)
        sizing_result = self._determine_sizing(signal, conviction_score, account_balance, sizing_mode)
        contracts = sizing_result["contracts"]
        max_cost = sizing_result["max_cost"]
        sizing_tier = sizing_result["tier"]
        sizing_notes = sizing_result["notes"]

        if contracts == 0:
            return self._skip_decision(
                signal,
                f"Cannot afford even 1 contract. {sizing_notes}",
            )

        # Determine stop loss
        # If PDT caution (2/3 day trades used), widen stops to reduce
        # chance of same-day stop-out which would trigger PDT flag
        stop_loss_pct = self._determine_stop_loss(signal, conviction_score, pdt_caution, management_style)

        # Determine management rules
        management_rules = self._determine_management(signal, conviction_score, management_style)

        # Determine action
        action = "execute"
        if signal.urgency == Urgency.LOW:
            action = "queue"  # Watchlist, don't execute immediately

        # Check for duplicate / already in position
        # Session 9 (D22): match ticker AND direction — holding OXY calls must
        # not block an OXY puts entry (different trade, opposite thesis).
        is_scale_in = "scale_in" in (signal.notes or "").lower()
        sig_direction = signal.direction.value if signal.direction else "call"
        for pos in existing_positions:
            if (
                pos.get("ticker") == signal.ticker
                and pos.get("direction") == sig_direction
            ):
                if is_scale_in:
                    # Caller is adding to their position — we already updated
                    # their contract count in main.py. For now, just notify.
                    # On a $1k account, doubling down is risky.
                    action = "notify_only"
                    sizing_notes += " | SCALE-IN: caller adding, we're holding current size"
                else:
                    action = "notify_only"
                    sizing_notes += " | Already in position for this ticker"
                break

        return TradeDecision(
            action=action,
            ticker=signal.ticker,
            direction=sig_direction,
            strike=signal.strike or 0,
            expiry=normalized_expiry,  # Session 9 (H8): always ISO YYYY-MM-DD
            contracts=contracts,
            max_cost=max_cost,
            stop_loss_pct=stop_loss_pct,
            entry_price_limit=signal.entry_price,
            conviction_score=conviction_score,
            sizing_tier=sizing_tier,
            is_0dte=signal.is_0dte,
            reason=self._build_reason(signal, conviction_score, sizing_tier, sizing_notes),
            management_rules=management_rules,
            management_style=management_style,
            source_signal=signal,
        )

    def _calculate_conviction(self, signal: ParsedSignal) -> float:
        """Score a signal from 0-100 based on source, urgency, and context."""
        score = 0.0
        weights = self.scoring_config["source_weights"]

        # Source weight — uses source_priority field set from channel config,
        # NOT string matching on channel names (which would miss channels like
        # "caller_a-alerts" or "caller_b-alerts" that don't contain "caller" or "paid")
        if signal.source_priority == "high":
            score += weights["paid_caller"]
        elif signal.source_priority == "medium":
            score += weights["unpaid_setup"]
        else:
            score += 5  # Low priority source

        # Urgency bonus
        # Session 9 (D24): +10, matching the documented scoring model (+15 in
        # code sat on a sizing-tier boundary and inflated every @everyone alert)
        if signal.urgency == Urgency.IMMEDIATE:
            score += 10
        elif signal.urgency == Urgency.STANDARD:
            score += 5

        # Sizing hint from caller (they're telling you to size up = higher conviction)
        if signal.sizing_hint == "heavy":
            score += 15
        elif signal.sizing_hint == "full":
            score += 10
        elif signal.sizing_hint == "light":
            score += 0  # Neutral
        elif signal.sizing_hint == "starter":
            score -= 5  # They're being cautious

        # Has specific entry price (more precise = more thought out)
        if signal.entry_price:
            score += 5

        # Has stop loss defined
        if signal.stop_loss:
            score += 5

        # Check if we have TA alignment (stored from previous TA signals)
        ta_alignment = self._check_ta_alignment(signal)
        if ta_alignment:
            score += weights["multi_source_alignment"]

        # Check if caller posted a breakdown/thesis backing this trade
        breakdown_backing = self._check_breakdown_backing(signal)
        if breakdown_backing:
            score += weights.get("breakdown_backing", 10)

        return min(100, max(0, score))

    def _determine_sizing(
        self, signal: ParsedSignal, conviction: float, account_balance: float,
        sizing_mode: str = "percentage",
    ) -> dict:
        """
        Cost-aware position sizing with dual-mode support.

        MODE 1: CHALLENGE (caller_a $500 challenge plays)
        - Channel config sets sizing_mode: "challenge"
        - Apply contract_multiplier to caller's count (2x for $1k vs $500 account)
        - Then cap against account safety limits
        
        MODE 2: PERCENTAGE (caller_b, or caller_a main account plays)
        - Channel config sets sizing_mode: "percentage"
        - Use conviction-based % tiers to determine budget
        - Calculate how many contracts fit in that budget
        
        Both modes apply the same safety caps (expensive contract, absolute max).
        """
        sizing = self.sizing_config
        thresholds = self.scoring_config["thresholds"]
        notes_parts = []

        # Session 9 (D14): balance unknown/zero → cannot size anything
        if not account_balance or account_balance <= 0:
            return {
                "contracts": 0,
                "max_cost": 0,
                "tier": "none",
                "target_pct": 0,
                "notes": "account balance unavailable",
            }

        # Challenge mode: channel config says this is a $500 challenge channel
        is_challenge = sizing_mode == "challenge"
        has_caller_contracts = signal.caller_contracts and signal.caller_contracts > 0

        # --- MODE 1: Challenge direct-multiplier sizing ---
        if is_challenge and has_caller_contracts:
            multiplier = sizing.get("contract_multiplier", 1.0)
            raw_contracts = max(1, round(signal.caller_contracts * multiplier))
            tier = "challenge"
            notes_parts.append(
                f"CHALLENGE MODE: caller {signal.caller_contracts} × {multiplier}x = "
                f"{raw_contracts} contracts"
            )

            # Still need to check if we can afford it
            contract_cost = None
            if signal.entry_price and signal.entry_price > 0:
                contract_cost = signal.entry_price * 100

            if contract_cost and contract_cost > 0:
                abs_max_spend = account_balance * (sizing["absolute_max_pct"] / 100)

                # Check expensive contract protection
                cost_as_pct = (contract_cost / account_balance) * 100
                if cost_as_pct >= sizing["expensive_contract_threshold_pct"]:
                    raw_contracts = 1
                    notes_parts.append(
                        f"EXPENSIVE CONTRACT: 1 contract = ${contract_cost:.0f} "
                        f"({cost_as_pct:.0f}% of account) → capped to 1"
                    )

                # Session 9 (M1): enforce sizing.absolute_max_pct — previously
                # computed and never compared.  No-op at the pilot value (100).
                total_cost = raw_contracts * contract_cost
                if total_cost > abs_max_spend:
                    fit = int(abs_max_spend / contract_cost)
                    logger.warning(
                        f"ABSOLUTE MAX CAP: {raw_contracts} contracts = "
                        f"${total_cost:.0f} > {sizing['absolute_max_pct']}% cap "
                        f"${abs_max_spend:.0f} → trimmed to {fit}"
                    )
                    notes_parts.append(
                        f"ABSOLUTE_MAX_PCT: trimmed {raw_contracts} → {fit} "
                        f"contracts to fit ${abs_max_spend:.0f} cap"
                    )
                    raw_contracts = fit

                # Session 9 (M1): enforce risk.max_single_trade_pct (both modes)
                raw_contracts = self._cap_to_single_trade_pct(
                    raw_contracts, contract_cost, account_balance, notes_parts
                )

                if raw_contracts < 1:
                    notes_parts.append(
                        f"Cannot fit 1 contract (${contract_cost:.0f}) under caps"
                    )
                    return {
                        "contracts": 0,
                        "max_cost": 0,
                        "tier": tier,
                        "target_pct": 0,
                        "notes": " | ".join(notes_parts),
                    }

                actual_cost = raw_contracts * contract_cost
                notes_parts.append(
                    f"Contract cost: ${contract_cost:.0f} × {raw_contracts} = "
                    f"${actual_cost:.0f} ({actual_cost/account_balance*100:.0f}% of account)"
                )
            else:
                actual_cost = 0
                notes_parts.append("No entry price — using multiplied count, will cap at execution")

            return {
                "contracts": raw_contracts,
                "max_cost": actual_cost if actual_cost else account_balance * (sizing["standard_max_pct"] / 100),
                "tier": tier,
                "target_pct": 0,  # N/A for challenge mode
                "notes": " | ".join(notes_parts),
            }

        # --- MODE 2: Percentage-based sizing ---
        # Determine tier from sizing_hint or conviction
        if signal.sizing_hint == "starter":
            tier = "starter"
            target_pct = sizing["starter_max_pct"]
        elif signal.sizing_hint == "light":
            tier = "light"
            target_pct = sizing["light_max_pct"]
        elif signal.sizing_hint == "heavy":
            tier = "heavy"
            target_pct = sizing["heavy_max_pct"]
        elif signal.sizing_hint == "full":
            tier = "standard"
            target_pct = sizing["standard_max_pct"]
        else:
            # No hint from caller — size by conviction
            if conviction >= thresholds["extreme"]:
                tier = "heavy"
                target_pct = sizing["heavy_max_pct"]
            elif conviction >= thresholds["high"]:
                tier = "standard"
                target_pct = sizing["standard_max_pct"]
            elif conviction >= thresholds["medium"]:
                tier = "light"
                target_pct = sizing["light_max_pct"]
            else:
                tier = "starter"
                target_pct = sizing["starter_max_pct"]

        # For percentage-mode plays from caller_a-alerts (main brokerage account), note it
        if sizing_mode == "percentage" and "caller_a" in (signal.source or "").lower():
            notes_parts.append("MAIN_ACCOUNT MODE: using %-based sizing (caller on larger account)")

        # Session 9 (regime): scale tier percentage by market-regime multiplier.
        # Challenge mirror mode is NOT scaled — we mirror the caller's count.
        if self._regime_multiplier != 1.0:
            target_pct = target_pct * self._regime_multiplier
            notes_parts.append(
                f"regime={self._regime_label} (sizing x{self._regime_multiplier:g})"
            )

        # Never exceed absolute max
        target_pct = min(target_pct, sizing["absolute_max_pct"])
        max_spend = account_balance * (target_pct / 100)

        notes_parts.append(f"Tier: {tier} ({target_pct}% = ${max_spend:.0f} max)")

        # Calculate contracts from budget
        contract_cost = None
        if signal.entry_price and signal.entry_price > 0:
            contract_cost = signal.entry_price * 100

        if contract_cost and contract_cost > 0:
            # Check expensive contract protection FIRST
            cost_as_pct = (contract_cost / account_balance) * 100
            if cost_as_pct >= sizing["expensive_contract_threshold_pct"]:
                notes_parts.append(
                    f"EXPENSIVE CONTRACT: 1 contract = ${contract_cost:.0f} "
                    f"({cost_as_pct:.0f}% of account) → forced starter"
                )
                tier = "starter"
                target_pct = sizing["starter_max_pct"] * self._regime_multiplier
                max_spend = account_balance * (target_pct / 100)

            raw_contracts = int(max_spend / contract_cost)

            # Session 9 (H1): when the budget doesn't cover a single contract,
            # return 0 so evaluate()'s skip branch fires.  The old
            # max(min_contracts, 0) floor forced a $900 contract onto a $100
            # budget, making every budget guard above it meaningless.
            if raw_contracts < 1:
                notes_parts.append(
                    f"UNAFFORDABLE: 1 contract = ${contract_cost:.0f} > "
                    f"budget ${max_spend:.0f} — 0 contracts"
                )
                contracts = 0
                actual_cost = 0
            else:
                # min_contracts floor only applies when >= 1 contract fits
                contracts = max(sizing.get("min_contracts", 1), raw_contracts)

                # Session 9 (M1): enforce risk.max_single_trade_pct (both modes)
                contracts = self._cap_to_single_trade_pct(
                    contracts, contract_cost, account_balance, notes_parts
                )
                if contracts < 1:
                    notes_parts.append(
                        f"Cannot fit 1 contract (${contract_cost:.0f}) under "
                        f"max_single_trade_pct"
                    )
                    contracts = 0
                    actual_cost = 0
                else:
                    actual_cost = contracts * contract_cost
                    notes_parts.append(
                        f"Contract cost: ${contract_cost:.0f} × {contracts} = "
                        f"${actual_cost:.0f} ({actual_cost/account_balance*100:.0f}% of account)"
                    )
        else:
            contracts = sizing.get("min_contracts", 1)
            actual_cost = max_spend
            notes_parts.append("No entry price available — defaulting to 1 contract")

        return {
            "contracts": contracts,
            "max_cost": actual_cost,
            "tier": tier,
            "target_pct": target_pct,
            "notes": " | ".join(notes_parts),
        }

    def _cap_to_single_trade_pct(
        self,
        contracts: int,
        contract_cost: float,
        account_balance: float,
        notes_parts: list,
    ) -> int:
        """
        Session 9 (M1): enforce risk.max_single_trade_pct — previously a dead
        knob (read nowhere).  At the pilot value (100) this is a no-op, but the
        plumbing now works when the cap is re-tightened post-pilot.
        """
        max_trade_pct = self.risk_config.get("max_single_trade_pct", 100)
        if max_trade_pct >= 100 or not contract_cost or contracts <= 0:
            return contracts
        cap = account_balance * (max_trade_pct / 100)
        if contracts * contract_cost > cap:
            fit = int(cap / contract_cost)
            logger.warning(
                f"MAX SINGLE TRADE CAP: {contracts} → {fit} contracts "
                f"(max_single_trade_pct={max_trade_pct}% = ${cap:.0f}, "
                f"${contract_cost:.0f}/contract)"
            )
            notes_parts.append(
                f"MAX_SINGLE_TRADE_PCT: trimmed {contracts} → {fit} "
                f"contracts to fit ${cap:.0f} cap"
            )
            return fit
        return contracts

    def _determine_stop_loss(
        self,
        signal: ParsedSignal,
        conviction: float,
        pdt_caution: bool = False,
        management_style: str = "managed",
    ) -> float:
        """
        Determine stop loss percentage.

        management_style="challenge": returns 0 (no hard stop) — caller_a
        always posts explicit exits so we rely on caller signals, not automation.

        When pdt_caution is True (2/3 day trades used), we widen stops on
        non-0DTE trades to reduce the chance of a same-day stop-out, which
        would trigger the PDT flag. Better to give a swing trade more room
        than to accidentally become a day trader.
        """
        # If signal has explicit SL from the caller, honour it (all styles).
        # Session 9 (H9): callers post stops as premium PRICES ("SL .90") as
        # often as percentages — a raw 0.9 stored into stop_loss_pct fires on
        # the first spread tick.  Disambiguate by magnitude:
        #   < 5 with known entry  → premium price level → convert to % vs entry
        #   < 5 without entry     → ambiguous, ignore (default stop applies)
        #   5–90                  → percentage, use as-is
        #   > 90                  → clamp to 90
        if signal.stop_loss is not None and signal.stop_loss > 0:
            raw_sl = float(signal.stop_loss)
            sl = None
            if raw_sl < 5:
                if signal.entry_price and signal.entry_price > 0:
                    pct = (1 - raw_sl / signal.entry_price) * 100
                    sl = min(90.0, max(5.0, pct))
                    logger.info(
                        f"Session 9 (H9): caller SL {raw_sl} interpreted as "
                        f"premium price level vs entry {signal.entry_price} → "
                        f"{sl:.0f}% stop"
                    )
                else:
                    logger.info(
                        f"Session 9 (H9): caller SL {raw_sl} < 5 with no entry "
                        f"price — ambiguous units, ignoring (default stop applies)"
                    )
            elif raw_sl > 90:
                sl = 90.0
                logger.info(
                    f"Session 9 (H9): caller SL {raw_sl}% clamped to 90%"
                )
            else:
                sl = raw_sl

            if sl is not None:
                if pdt_caution and not signal.is_0dte:
                    widened = sl * 1.5
                    logger.info(
                        f"PDT CAUTION: Widening caller SL from {sl}% to {widened:.0f}% "
                        f"to avoid same-day stop-out"
                    )
                    return widened
                return sl

        # Challenge channel: no hard stop — let caller_a's explicit exit
        # signals do the work. Trail at 60%+ is the only automated protection.
        # Return 0 as sentinel meaning "disabled" (_check_stop_loss skips on 0).
        if management_style == "challenge":
            logger.debug("Challenge mode: no hard stop loss (relying on caller exits)")
            return 0

        thresholds = self.scoring_config["thresholds"]

        if conviction >= thresholds["high"]:
            base_sl = self.risk_config["high_conviction_stop_loss_pct"]
        else:
            base_sl = self.risk_config["default_stop_loss_pct"]

        if pdt_caution and not signal.is_0dte:
            widened = base_sl * 1.5
            logger.info(
                f"PDT CAUTION: Widening stop from {base_sl}% to {widened:.0f}% "
                f"to avoid same-day stop-out"
            )
            return widened

        return base_sl

    def _determine_management(
        self, signal: ParsedSignal, conviction: float, management_style: str = "managed"
    ) -> dict:
        """Determine how the trade should be managed post-entry.

        management_style="challenge": caller_a-challenge-challenge fast trades. No profit
        tiers — caller_a exits explicitly. Trailing only activates at 60%+ so
        normal bid/ask noise doesn't fire it. Hard stop is set separately in
        _determine_stop_loss.

        management_style="managed": caller posts explicit exits/trims, lower
        trail threshold (20%) to capture swing gains before they reverse.

        management_style="fire_and_forget": no caller exits; rely on tiers +
        trailing. Activation stays at 50% for more volatile short-dated plays.
        """
        mgmt = self.mgmt_config

        # ── Challenge mode ────────────────────────────────────────────────────
        # caller_a is a fast trader who always posts explicit exits; we follow
        # those and rely on the hard stop for protection. No auto profit tiers.
        # Trail only activates deep in profit (60%+) so noise doesn't trigger it.
        if management_style == "challenge":
            trail_activation = mgmt.get("challenge_trailing_stop_activation_pct", 60)
            trail_distance = mgmt.get("challenge_trailing_stop_distance_pct", 20)
            return {
                "strategy": "trailing_stop_only",   # No tiered trims
                "trailing_activation_pct": trail_activation,
                "trailing_distance_pct": trail_distance,
                "follow_caller_exits": mgmt.get("follow_caller_exits", True),
            }

        thresholds = self.scoring_config["thresholds"]

        # ── Caller-managed vs fire-and-forget trail thresholds ────────────────
        if management_style == "managed":
            trail_activation = mgmt.get(
                "caller_managed_trailing_stop_activation_pct",
                mgmt["trailing_stop_activation_pct"],
            )
            trail_distance = mgmt.get(
                "caller_managed_trailing_stop_distance_pct",
                mgmt["trailing_stop_distance_pct"],
            )
        else:
            trail_activation = mgmt["trailing_stop_activation_pct"]
            trail_distance = mgmt["trailing_stop_distance_pct"]

        if conviction >= thresholds["high"] and mgmt.get("high_conviction_override"):
            # High conviction: just trail, don't auto-trim
            return {
                "strategy": "trailing_stop_only",
                "trailing_activation_pct": trail_activation,
                "trailing_distance_pct": trail_distance,
                "follow_caller_exits": mgmt["follow_caller_exits"],
            }
        else:
            # Standard: use profit tiers + trailing stop
            return {
                "strategy": "tiered_profit_taking",
                "profit_tiers": mgmt["profit_tiers"],
                "trailing_activation_pct": trail_activation,
                "trailing_distance_pct": trail_distance,
                "follow_caller_exits": mgmt["follow_caller_exits"],
            }

    def _check_ta_alignment(self, signal: ParsedSignal) -> bool:
        """
        Check if the signal aligns with any stored TA signals.
        E.g., if TA showed IONQ has a node at $30 and a caller posts IONQ puts,
        that's alignment = higher conviction.
        """
        if not signal.ticker:
            return False

        ticker = signal.ticker.upper()
        ta = self.ta_context.get(ticker)

        if not ta:
            return False

        # Session 9 (D21): staleness window is configurable
        # (engine.ta_context_max_age_hours, default 24h — ta_source maps are
        # daily; the old hardcoded 7 days matched against week-old maps).
        # Pruning/mutation happens in _prune_ta_context() at the start of
        # evaluate() — the scoring path only reads.
        max_age_hours = self.engine_config.get("ta_context_max_age_hours", 24)
        try:
            ta_time = datetime.fromisoformat(ta["timestamp"])
            age_hours = (datetime.utcnow() - ta_time).total_seconds() / 3600
            if age_hours > max_age_hours:
                return False
        except (ValueError, TypeError):
            pass

        # Check directional alignment
        ta_direction = ta.get("direction")
        signal_direction = signal.direction.value if signal.direction else None

        if ta_direction and signal_direction:
            if ta_direction == signal_direction:
                logger.info(
                    f"TA ALIGNMENT: {ticker} - both TA and caller say {ta_direction}. "
                    f"TA target: ${ta.get('target')}"
                )
                return True
            else:
                logger.info(
                    f"TA CONFLICT: {ticker} - TA says {ta_direction}, "
                    f"caller says {signal_direction}"
                )

        return False

    def _management_decision(
        self, signal: ParsedSignal, existing_positions: list[dict]
    ) -> TradeDecision:
        """Handle exit/trim/management signals."""
        return TradeDecision(
            action="manage",
            ticker=signal.ticker or "",
            direction="",
            strike=0,
            expiry="",
            contracts=0,
            max_cost=0,
            stop_loss_pct=0,
            entry_price_limit=None,
            conviction_score=0,
            sizing_tier="",
            is_0dte=False,
            reason=f"Management signal: {signal.signal_type.value} - {signal.notes}",
            management_rules={},
            source_signal=signal,
        )

    def _ta_decision(self, signal: ParsedSignal) -> TradeDecision:
        """Store TA signal for context, don't trade directly."""
        # TODO: Store in TA context cache for alignment checking
        return TradeDecision(
            action="notify_only",
            ticker=signal.ticker or "",
            direction=signal.direction.value if signal.direction else "",
            strike=0,
            expiry="",
            contracts=0,
            max_cost=0,
            stop_loss_pct=0,
            entry_price_limit=None,
            conviction_score=0,
            sizing_tier="",
            is_0dte=False,
            reason=f"TA signal stored for context: {signal.notes}",
            management_rules={},
            source_signal=signal,
        )

    def _skip_decision(self, signal: ParsedSignal, reason: str) -> TradeDecision:
        """Create a skip decision."""
        return TradeDecision(
            action="skip",
            ticker=signal.ticker or "",
            direction="",
            strike=0,
            expiry="",
            contracts=0,
            max_cost=0,
            stop_loss_pct=0,
            entry_price_limit=None,
            conviction_score=0,
            sizing_tier="",
            is_0dte=signal.is_0dte,
            reason=reason,
            management_rules={},
            source_signal=signal,
        )

    def _build_reason(self, signal: ParsedSignal, conviction: float, sizing: str, sizing_notes: str = "") -> str:
        """Build a human-readable explanation."""
        parts = [
            f"Conviction: {conviction:.0f}/100",
            f"Sizing: {sizing}",
            f"Source: {signal.source} ({signal.source_priority})",
        ]
        if sizing_notes:
            parts.append(f"Sizing detail: {sizing_notes}")
        # Always show PDT budget status
        day_trades = self.get_day_trades_in_window()
        emergency_reserve = self.risk_config.get("pdt_emergency_reserve", 1)
        spendable = max(0, self._max_day_trades - day_trades - emergency_reserve)
        if signal.is_0dte:
            parts.append(
                f"0DTE → day trade #{day_trades + 1}/{self._max_day_trades} "
                f"({spendable - 1} spendable 0DTE slots after)"
            )
        else:
            parts.append(
                f"PDT: {day_trades}/{self._max_day_trades} used, "
                f"{spendable} 0DTE-spendable + {emergency_reserve} emergency reserve"
            )
        if signal.notes:
            parts.append(f"Notes: {signal.notes}")
        return " | ".join(parts)

    # =========================================
    # PDT (Pattern Day Trader) Protection
    # =========================================
    # Robinhood flags accounts with < $25k that make 4+ day trades
    # in a rolling 5 business day window. A "day trade" is opening and
    # closing the same security on the same trading day.
    #
    # 0DTE options are ALWAYS day trades (they expire same day).
    # Swing trades become day trades if the stop loss or exit triggers same day.

    def _load_pdt_log(self) -> list[str]:
        """Load day trade dates from disk (survives restarts)."""
        try:
            if self._pdt_log_path.exists():
                data = json.loads(self._pdt_log_path.read_text())
                return data.get("day_trade_dates", [])
        except Exception as e:
            logger.error(f"Failed to load PDT log: {e}")
        return []

    def _save_pdt_log(self):
        """Persist day trade dates to disk."""
        try:
            self._pdt_log_path.write_text(json.dumps({
                "day_trade_dates": self._day_trade_dates,
                "last_updated": datetime.utcnow().isoformat(),
            }, indent=2))
        except Exception as e:
            logger.error(f"Failed to save PDT log: {e}")

    def get_day_trades_in_window(self) -> int:
        """
        Count day trades in the rolling 5-trading-day window: today plus the
        previous 4 TRADING days — FINRA/Robinhood's definition.

        Session 9 (M3): was today + 5 prior weekdays (a 6-day window) with no
        holiday awareness; now uses market_time.trading_days_ago (ET calendar,
        holiday-aware).
        """
        cutoff = trading_days_ago(4)
        count = 0
        for dt_str in self._day_trade_dates:
            try:
                trade_date = date.fromisoformat(dt_str)
                if trade_date >= cutoff:
                    count += 1
            except ValueError:
                continue
        return count

    def get_day_trades_remaining(self) -> int:
        """How many day trades can we still make without triggering PDT."""
        return max(0, self._max_day_trades - self.get_day_trades_in_window())

    def record_day_trade(self, trade_date: date = None):
        """
        Record a day trade. Called when:
        1. A 0DTE trade is filled (guaranteed day trade)
        2. A non-0DTE position is opened AND closed on the same calendar day
        """
        trade_date = trade_date or trading_date()
        dt_str = trade_date.isoformat()
        self._day_trade_dates.append(dt_str)

        # Prune old entries (older than 10 trading days — keep some buffer).
        # Session 9 (D12): malformed dates are filtered out, not fatal — one
        # bad entry in pdt_tracker.json must not kill PDT persistence.
        cutoff = trading_days_ago(10)
        kept = []
        for d in self._day_trade_dates:
            try:
                if date.fromisoformat(d) >= cutoff:
                    kept.append(d)
            except (ValueError, TypeError):
                logger.warning(f"Dropping malformed PDT log entry: {d!r}")
        self._day_trade_dates = kept

        self._save_pdt_log()

        remaining = self.get_day_trades_remaining()
        logger.warning(
            f"DAY TRADE RECORDED: {dt_str} | "
            f"Day trades in 5-day window: {self.get_day_trades_in_window()}/{self._max_day_trades} | "
            f"Remaining: {remaining}"
        )

        if remaining <= 1:
            logger.critical(
                f"⚠️  PDT WARNING: Only {remaining} day trade(s) remaining! "
                f"Next 0DTE trade will use last allowed slot."
            )

    # Session 9 (D23): check_same_day_close_pdt removed — dead code duplicating
    # the same-day-close PDT logic that lives in management/trade_manager.py.
