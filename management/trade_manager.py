"""
Trade Manager - Monitors open positions and handles:
- Stop losses (hard and trailing)
- Profit tier trimming
- Caller exit signals
- Position tracking and P&L
"""

import logging
import os
import threading
import time
import json
import re
from datetime import datetime, timezone, date, time as dt_time
from typing import Optional, Tuple
from pathlib import Path

from execution.position import Position
from execution.base import BaseExecutor
from engine.decision_engine import TradeDecision
from utils import market_time
from utils.logging_config import log_trade_execution, log_position_check

logger = logging.getLogger(__name__)
# Session 13: the same file log_position_check writes to. A quote rejected as
# implausible never reaches that function — the monitor stops short of it — so
# without this the only record of a bad price series would be a throttled
# sample in the main log. See _quote_is_sane.
_positions_log = logging.getLogger("positions")


# Session 15 (BUG-36): how many monitor passes an unconfirmed new high stays
# usable as corroboration. At a ~6s poll this is about twenty seconds — long
# enough for choppy tape to second a genuine high a dip or two later, short
# enough that a phantom cannot sit in the slot waiting to vouch for a second
# phantom minutes afterwards. Blunt on purpose; not worth a config knob.
_PENDING_HIGH_MAX_AGE_PASSES = 3


# Session 15 (BUG-36): minimum seconds between "automated exit held" notices
# for one position. A price oscillating around the trail trigger vetoes every
# other pass, which is 100 Discord posts in twenty minutes — and notify_status
# is a blocking POST inside the monitor loop, so the spam also stalls every
# other position's stop check. Same lesson as _should_alert_unusable.
_VETO_NOTICE_COOLDOWN_SECONDS = 900


# Session 10f: what "trimmed most" mirrors to when the caller gives no count.
# A blunt failsafe rather than a tuned number — he says it rarely, and being
# roughly right beats the old behaviour of taking a single contract.
_MOST_TRIM_FRACTION = 0.80

# Session 10f: an exit VERB governed by an INTENT construction is a plan, not a
# fill. On 2026-07-27 "So good, we are going to TP DAL today" landed 29 minutes
# before the open and parsed as signal_type="exit". We held no DAL so nothing
# happened, but had we, the bot would have closed 32 minutes before his real
# "1.55 200% all out DAL" at +200%.
#
# The parser prompt already teaches this ("Plans are not fills") and still got
# it wrong, which is exactly why the defence is here as well as there — same
# belt-and-braces shape as the `trimmed N` override below.
#
# Deliberately narrow: the intent word must GOVERN the exit verb ("going to
# TP", "need to cut"), not merely appear somewhere in the message. Validated
# against all 539 scraped caller messages — it flags exactly one, the message
# the prompt itself uses as its canonical intent example ("if TE doesnt reclaim
# 6.20 today, we need to cut it") — and against every real exit phrasing he
# uses, none of which match.
INTENT_RE = re.compile(
    r'\b(going\s+to|gonna|need(?:s)?\s+to|will|about\s+to|plan(?:ning|ing)?\s+to|'
    r'looking\s+to|hoping\s+to|might|may|should|would)\s+'
    r'(?:\w+\s+){0,2}'
    r'(tp|take\s+profit|cut|sell|exit|close|trim|scale\s+out|get\s+out)\b',
    re.IGNORECASE,
)

# Day 4 (the operator, 2026-08-03): take-profit wording that EXECUTES. "Lets TP AAL
# 30%" was parsed as a suggestion and the bot held while the caller took
# profit. TP is the one exit verb exempt from the intent gate — acting on TP
# intent fails flat-with-profit, while acting on ENTRY intent fails
# holding-something-he-never-bought (the SOUN "Will buy" lesson), so the
# asymmetry is deliberate. Matched against the caller's OWN raw text only,
# never the parser's notes (which paraphrase holds with the words "take
# profit").
# Day 4 review round 2 (M5): cover the tenses caller_a actually types — "Taking
# profit on AAL here", "Took profit", "TP'd" — or the original bug recurs in
# a different tense with no notification at all.
_TP_TOKEN = r"(?:tp'?d?|tak(?:e|ing|en)\s+profits?|took\s+profits?)"
TP_EXEC_RE = re.compile(r'\b' + _TP_TOKEN + r'\b', re.IGNORECASE)

# Day 4 review rounds 1+2: the bare token is NOT a directive. Round 1 found
# six hold-shaped messages that all sold on the naive match ("Not going to TP
# AAL yet", "TP hit — leaving 4 runners with a 50% profit SL", "TP at 1.50,
# SL at 0.80"...). Round 2 found the guard itself was one-directional and
# same-line only: "TP not hit yet" (negation AFTER the token), "TP later,
# holding for now", a line-broken "Not going to\nTP yet", and SL-less target
# talk ("TP at 1.50") all still sold. Both directions now match, the window
# spans newlines (sentence-bounded only), and "TP at/target/zone <number>" is
# level talk. A TP executes only when none of these hold markers surround it;
# everything else stays management and is surfaced rather than traded.
_TP_NEGATION_BEFORE_RE = re.compile(
    r"\b(?:not|no|never|won'?t|wont|don'?t|dont|can'?t|cant|until|unless|"
    r"before|wait(?:ing)?|hold(?:ing)?(?:\s+off)?)\b"
    r"[^.!?]{0,60}?\b" + _TP_TOKEN + r"\b",
    re.IGNORECASE,
)
_TP_NEGATION_AFTER_RE = re.compile(
    r"\b" + _TP_TOKEN + r"\b"
    r"[^.!?]{0,60}?"
    r"\b(?:not|no|never|yet|later|soon|until|unless|when|if|"
    r"wait(?:ing)?|hold(?:ing)?)\b",
    re.IGNORECASE,
)
# "TP at 1.50" / "TP target 2.00" / "TP zone $1.20" are levels, not fills.
# A bare percentage ("Lets TP 160% XSP") is caller_a's GAIN vocabulary and stays a
# directive — only preposition/target-word + number is refused.
# Round 3 (F2): the number tail accepts bare-point decimals — "TP at .50" is
# this caller's documented sub-dollar format ("SL .90"), and missing it sold
# a hold. (F3): a PAST-tense token before a price is a FILL REPORT, not a
# target ("Took profit at 1.50") — see _TP_PAST_RE in tp_directive.
# Round 4 (R4-1): up to three intervening words ("TP the rest at .80" is the
# documented shape for a target on a held remainder), but an intervening word
# may not BEGIN a new TP token — otherwise "TP'd half, TP at 1.20" spans one
# match containing the past token and the future target inherits its
# exemption. The past-tense exemption is applied PER MATCH in tp_directive,
# never message-globally.
_TP_LEVEL_TALK_RE = re.compile(
    r"\b" + _TP_TOKEN + r"\b"
    r"(?:\s+(?!" + _TP_TOKEN + r"\b)\S+){0,3}?"
    r"\s+(?:at|target|zone|around|near)\b\s*\$?\.?\d",
    re.IGNORECASE,
)
_TP_PAST_RE = re.compile(r"\b(?:tp'd|took\s+profits?|taken\s+profits?)\b",
                         re.IGNORECASE)
_TP_STOP_TALK_RE = re.compile(r"\bsl\b|\bstop\b", re.IGNORECASE)


def _parse_runner_level(text: str):
    """The caller's stated runner-stop level, as a float percent, or None.

    Round 2 (L2): word-order tolerant — "50% profit SL", "profit SL 50%",
    "50% profit stop loss" and "profit stop at 50%" all state the same
    number. Number-first tried first (caller_b's observed phrasing).
    """
    if not text:
        return None
    # Round 4 (R4-2): the sign guard — "-10% profit SL" must not parse as
    # +10 (a floor ABOVE entry for a stop the caller placed BELOW it). A
    # signed level parses as no level → the safe "nothing armed" hold.
    m = re.search(
        r"(?<![\d.-])(\d+(?:\.\d+)?)\s*%\s*profit\s*(?:sl|stop(?:\s*loss)?)\b",
        text,
    ) or re.search(
        r"profit\s*(?:sl|stop(?:\s*loss)?)\s*(?:at|of|:)?\s*(?<![\d.-])(\d+(?:\.\d+)?)\s*%",
        text,
    )
    return float(m.group(1)) if m else None


def tp_directive(raw: str) -> bool:
    """True only for TP wording that plausibly INSTRUCTS a sale.

    Applied to the caller's OWN raw text (never parser notes). Refuses:
      - runner posts ("TP hit — leaving 4 runners...") — those are HOLD
        structures and belong to the runner branch, which promotion would
        defeat (a hold instruction inverting into an exit, the Session 15
        worst case);
      - stop/target talk ("TP at 1.50, SL at 0.80") — levels, not a fill;
      - negated/deferred TP ("not going to TP yet", "won't TP until 2.00",
        "waiting to TP") — the opposite of an instruction.
    A refusal is a hold with a notification, which is the pre-Day-4
    behaviour: fails safe, and the human decides.
    """
    if not raw or not TP_EXEC_RE.search(raw):
        return False
    if "runner" in raw.lower():
        return False
    if _TP_STOP_TALK_RE.search(raw):
        return False
    for _lvl_m in _TP_LEVEL_TALK_RE.finditer(raw):
        # Level talk refuses only future/neutral tense, judged PER MATCH
        # (round 4 R4-1 — a past-tense fill elsewhere in the message must
        # not exempt a separate future target: "Took profit at .50, TP at
        # .80 for the rest" holds). "Took profit at 1.50" alone states a
        # fill's price, not a target (round 3 F3), and executes. The
        # negation windows below still apply to past tense — "Took some
        # profit but not selling the rest" stays held.
        if not _TP_PAST_RE.search(_lvl_m.group(0)):
            return False
    if _TP_NEGATION_BEFORE_RE.search(raw) or _TP_NEGATION_AFTER_RE.search(raw):
        return False
    return True


def ledger_row_matches_mode(row: dict, current_mode: str) -> bool:
    """Does this trades.json row belong to the mode we are running in?

    Session 12 (GO_LIVE B4/B5). The ledger never recorded which mode wrote a
    row, so paper and live money were indistinguishable — the 40% daily
    circuit breaker seeded itself from simulated P&L on every restart, and a
    stale paper OPEN could classify a hand-managed broker position as
    bot-managed. New rows carry `mode`; this is the single filter every
    reader goes through, so the rule can never drift between them.

    Legacy rows (pre-Session-12) have no `mode` field. Paper CLOSE/TRIM rows
    are identifiable by their `[PAPER]` reason prefix; untagged legacy rows
    cannot be told apart and keep the old (unfiltered) behaviour — the
    go-live procedure archives the ledger precisely so that ambiguity never
    meets real money.
    """
    row_mode = row.get("mode")
    if row_mode:
        return row_mode == current_mode
    if str(row.get("reason") or "").startswith("[PAPER]"):
        return current_mode == "paper"
    return True


class TradeManager:
    # Session 9 (H13): serialize all trades.json read-modify-write cycles.
    # Class-level so every TradeManager instance shares the same file lock.
    _trade_log_lock = threading.Lock()
    # Session 10f: separate lock for the volatile position-state sidecar, so a
    # high-water-mark write never queues behind a trade-log write.
    _state_lock = threading.Lock()
    # Session 13: a position whose price is unusable — rejected as implausible,
    # or never fetched at all — is unmanaged in silence. The outage watchdog
    # does not cover either case: one healthy position keeps last_quote_ok_ts
    # fresh for the whole manager, and a rejected quote is still a quote that
    # arrived. Neither state self-heals, so alert early and then keep saying it;
    # a warning from six hours ago is not a live warning. At a ~6s poll: first
    # at ~20s, then roughly every 10 minutes for as long as it lasts.
    _UNUSABLE_PRICE_ALERT_AT = 3
    _UNUSABLE_PRICE_ALERT_EVERY = 100

    @classmethod
    def _should_alert_unusable(cls, count: int) -> bool:
        return (
            count == cls._UNUSABLE_PRICE_ALERT_AT
            or (count > 0 and count % cls._UNUSABLE_PRICE_ALERT_EVERY == 0)
        )

    def __init__(self, config: dict, executor: BaseExecutor, decision_engine=None, notifier=None):
        self.config = config["management"]
        self.risk_config = config["risk"]
        self.executor = executor
        self.decision_engine = decision_engine  # For PDT checks
        self.notifier = notifier  # For PDT flag alerts
        self.positions: dict[str, Position] = {}  # key -> Position
        # 2026-08-04 LEDGER-CLOBBER FIX: this path was relative and resolved
        # against the CURRENT cwd on every read/write. The test suite chdirs
        # each test into a scratch dir; a straggling exit-worker thread from
        # one test hit the between-tests window where the cwd flips back to
        # the project root — it READ its (empty) world from scratch and then
        # os.replace'd a one-fixture-row file over the REAL ledger on the operator's
        # machine (2026-08-04 00:24 BST, during run_tests.bat). Resolving at
        # construction freezes the directory: whatever cwd a manager is born
        # in owns its ledger forever, and no later chdir can redirect it.
        self.trade_log_path = Path(
            config["operations"].get("trade_log_path", "./logs/trades.json")
        ).resolve()
        self.trade_log_path.parent.mkdir(parents=True, exist_ok=True)
        # Session 10f: volatile per-position runtime state — the high-water mark
        # and trailing-stop arming. Deliberately NOT in trades.json, which is a
        # ledger of events rather than a live snapshot. Without this a restart
        # re-anchored the HWM to the current price: a position that had run to
        # +150% and pulled back to +50% came back with the trail disarmed and,
        # under challenge sizing (stop_loss_pct 0), no stop of any kind.
        self.state_path = self.trade_log_path.with_name("position_state.json")
        # Session 14: what we closed this session, by ticker — when, why, at
        # what P&L. Exists for one consumer: the no-match branch of
        # handle_caller_exit. On 2026-07-29 the bot's trailing stop closed XSP
        # at +74%, caller_a posted a 💯 P&L flex 15 minutes later, and the no-match
        # notification announced "Caller exited XSP ... the entry was missed" —
        # both halves wrong, and alarming enough that the operator read it as the flex
        # having CLOSED the position. "We already closed it 15 minutes ago via
        # trailing stop" was the true answer, and this dict is how the branch
        # can know it. In-memory only: after a restart the book restore covers
        # the same question through trades.json.
        self._recently_closed: dict[str, dict] = {}
        # Session 10f: log the opening-bell settle once per trading day.
        self._settle_logged_for = None
        # Session 15 (BUG-36): monitor pass counter. Breach runs are counted
        # in passes rather than in readings, so a pass where a position could
        # not be evaluated at all breaks the run instead of silently
        # preserving it.
        self._pass_seq = 0
        self.daily_pnl = 0.0
        # Session 9: trading-day math is US/Eastern, never host-local.
        self._last_pnl_reset_date: str = market_time.trading_date().isoformat()
        # Session 9 (IMP-4): guards all positions-dict mutations and snapshot
        # creation. RLock so nested acquisition within one thread is safe.
        # Network I/O and notifications must stay OUTSIDE this lock.
        self._lock = threading.RLock()
        # Paper trade mode: parse signals and track positions in memory but
        # never send real orders to Robinhood — including exits/stops.
        self.paper_trade: bool = config.get("notifications", {}).get("paper_trade", False)
        # Session 12: last COMPLETED position-monitor pass (watchdog heartbeat).
        self.last_position_check_ts = time.time()
        # Session 12: last SUCCESSFUL price fetch — a pass that completes with
        # every quote failing is an outage too (the fast-fail mode).
        self.last_quote_ok_ts = time.time()
        # Session 16: how many 0DTE positions the last sweep LOOKED at, as
        # against how many it dispatched. Zero dispatched can mean either "none
        # held" or "held one and could not act on it", and the old log line
        # printed the reassuring reading of both.
        self.last_0dte_sweep_considered = 0
        # Session 16: set on shutdown so a sell order resting into the close
        # stops waiting and gets cancelled while the process is still alive.
        # Exit workers are daemon threads: without this, a `!stop` at 15:50
        # kills the wait mid-rest and leaves a live good-for-day order at the
        # broker that can still fill, with nothing left running to book it.
        self._shutdown = threading.Event()
        # Day 4 (the operator, 2026-08-03): trails removed. Say so at startup — a
        # silent master switch is how the dead `enable_trailing_stop` key sat
        # unnoticed in config.yaml for eight sessions.
        if not self.config.get("enable_trailing_stop", True):
            logger.warning(
                "Trailing stops DISABLED (enable_trailing_stop: false) — "
                "exits come from caller signals, runner profit floors, the "
                "0DTE sweep and expiry booking only"
            )

    # ── Session 9 helpers ────────────────────────────────────────────────

    def _positions_snapshot(self) -> list:
        """Thread-safe snapshot of (key, Position) pairs."""
        with self._lock:
            return list(self.positions.items())

    # ── Session 10f: high-water-mark / trailing-stop persistence ─────────

    @staticmethod
    def _state_record(position: Position) -> dict:
        """The volatile fields worth surviving a restart, plus an identity stamp.

        `entry_price` and `opened_at` are the guard: position keys are
        ticker_strike_expiry_direction, so a close-then-re-enter of the same
        contract reuses the key, and without the stamp the new position would
        inherit the old one's high-water mark and an armed trail it never
        earned.
        """
        return {
            "high_water_mark": position.high_water_mark,
            "trailing_stop_active": position.trailing_stop_active,
            "trailing_stop_price": position.trailing_stop_price,
            # Day 4 (2026-08-03): the caller's stated runner stop must survive
            # a restart — losing it silently disarms an instruction (the
            # BUG-20 class of failure). The cleared flag rides with it, or a
            # restart would re-run clearance and a pending floor could engage
            # off two stale post-restore readings.
            "profit_floor_price": getattr(position, "profit_floor_price", 0.0),
            "profit_floor_cleared": bool(
                getattr(position, "profit_floor_cleared", False)
            ),
            # Mirror fidelity: _calculate_proportional_trim needs the caller's
            # own count to turn "trimmed 2 of 5" into 40%. At 0 it skips both
            # proportional methods and guesses from keywords instead.
            "caller_contracts": position.caller_contracts,
            "caller_contracts_remaining": position.caller_contracts_remaining,
            "pdt_sell_next_open": position.pdt_sell_next_open,
            "trim_carry": position.trim_carry,
            # 2026-08-04 (D-8): tiers already taken must survive a restart or
            # they re-fire and double-trim (dormant live; tiered styles only).
            "tiers_hit": list(
                (position.management_rules or {}).get("_tiers_hit", []) or []
            ),
            # Session 10f: a Friday-night exit must still fire on Monday, and
            # a restart in between must not lose it.
            # Round 2 (R2-5): the "our own order may have filled" latch must
            # survive a restart in the 🚨→16:05 window, or the reconcile books
            # the -100% the latch exists to refuse.
            "sell_state_unknown": bool(
                getattr(position, "sell_state_unknown", False)
            ),
            # 2026-08-04 (F6): a caller's stop_update mutates only this field
            # — restoring the OPEN row's value (0 for challenge) silently
            # disarmed his instructed stop on every restart (BUG-20 class).
            "stop_loss_pct": float(getattr(position, "stop_loss_pct", 0.0) or 0.0),
            # 2026-08-04 (B6): a caller exit queued behind an in-flight
            # attempt must survive a restart in that window — same class as
            # exit_at_open, narrower window.
            "pending_caller_exit": bool(
                getattr(position, "pending_caller_exit", False)
            ),
            "pending_caller_exit_limit": float(
                getattr(position, "pending_caller_exit_limit", 0.0) or 0.0
            ),
            "exit_at_open": position.exit_at_open,
            "exit_at_open_reason": position.exit_at_open_reason,
            # 2026-08-04 (C1/F1): a deferred trim's quantity must survive a
            # restart with the flag itself, or it degrades to a full exit.
            "exit_at_open_trim_contracts": int(
                getattr(position, "exit_at_open_trim_contracts", 0) or 0
            ),
            "exit_at_open_trim_pct": float(
                getattr(position, "exit_at_open_trim_pct", 0.0) or 0.0
            ),
            "exit_at_open_trim_notes": str(
                getattr(position, "exit_at_open_trim_notes", "") or ""
            ),
            "entry_price": position.entry_price,
            "opened_at": position.opened_at,
        }

    def save_position_state(self):
        """Persist HWM / trail state for every open position. Never raises.

        Called on change (new high, trail arming or ratcheting, open, exit) —
        not on every price poll. New highs are rare relative to the ~6s poll,
        so this is a handful of small atomic writes per position per day.
        """
        try:
            snapshot = {
                k: self._state_record(p) for k, p in self._positions_snapshot()
            }
        except Exception as e:
            logger.debug(f"Could not snapshot position state: {e}")
            return
        with TradeManager._state_lock:
            try:
                tmp_path = self.state_path.with_name(self.state_path.name + ".tmp")
                tmp_path.write_text(json.dumps(snapshot, indent=2))
                os.replace(tmp_path, self.state_path)
            except Exception as e:
                # Losing the sidecar costs a high-water mark, not a position —
                # never let it take down the monitoring loop.
                logger.warning(f"Could not write {self.state_path.name}: {e}")

    def load_position_state(self) -> dict:
        """Read the sidecar; {} on any problem.

        Callers degrade to seeding the high-water mark from entry/current price,
        which is exactly the pre-10f behaviour — a missing or corrupt sidecar
        must never block a restore.
        """
        try:
            if not self.state_path.exists():
                return {}
            data = json.loads(self.state_path.read_text())
            if not isinstance(data, dict):
                logger.warning(
                    f"{self.state_path.name} root is {type(data).__name__}, not "
                    f"a dict — ignoring saved high-water marks"
                )
                return {}
            return data
        except Exception as e:
            logger.warning(
                f"Could not read {self.state_path.name}: {e} — restoring "
                f"without saved high-water marks"
            )
            return {}

    @staticmethod
    def _parse_opened_at(opened_at: str) -> Optional[datetime]:
        """
        Parse a position's opened_at into a tz-aware ET datetime.
        Handles legacy naive timestamps (pre-Session-9 `datetime.utcnow()`)
        by assuming UTC. Returns None on malformed input.
        """
        try:
            dt = datetime.fromisoformat(opened_at)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(market_time.ET)

    def _is_same_trading_day(self, position: Position) -> bool:
        """True if the position was opened on the current US trading date."""
        opened_dt = self._parse_opened_at(position.opened_at)
        if opened_dt is None:
            return False
        return market_time.trading_date(opened_dt) == market_time.trading_date()

    def _is_0dte_today(self, position: Position) -> bool:
        """True if the position's expiry normalizes to today's trading date."""
        return (
            market_time.normalize_expiry(position.expiry)
            == market_time.trading_date().isoformat()
        )

    def _record_day_trade_after_fill(self, position: Position, is_same_day: bool, is_0dte: bool, context: str):
        """
        Session 9 (M2): record a PDT day trade only AFTER a confirmed fill,
        never before the sell attempt (failed sells must not burn a slot).
        """
        if is_same_day and not is_0dte and self.decision_engine:
            if not position.day_trade_recorded:
                self.decision_engine.record_day_trade()
                position.day_trade_recorded = True
                logger.info(
                    f"Same-day {context} of {position.ticker} recorded as day "
                    f"trade (after confirmed fill)"
                )

    def _confirm_sell_fill(self, order_id: str, timeout: Optional[float] = None,
                           resting: bool = False) -> Tuple[str, float, float]:
        """
        Session 9 (C2): poll a sell order until filled or timeout.

        Polls executor.check_order_status until `timeout` (default
        management.sell_fill_timeout_seconds, 45s): every 1s normally, every
        5s when `resting` — a 0DTE order left working into the close waits
        minutes, and 1 Hz would be hundreds of broker calls for one dying
        contract. `resting` also lets a shutdown end the wait early so the
        order can be cancelled rather than stranded.

        Returns (state, filled_qty, fill_price_per_share) where state is
        "filled" | "partial" | "unfilled". Terminal non-filled states
        (cancelled/rejected/failed) end polling early.
        """
        if timeout is None:
            timeout = self.config.get("sell_fill_timeout_seconds", 45)
        deadline = time.monotonic() + float(timeout)
        # Session 16: a 0DTE order resting into the close waits ~11 minutes,
        # and polling that at 1 Hz is ~650 broker calls for one dying contract.
        # A resting order polls every 5s; every ordinary confirmation keeps 1s,
        # because there a second of latency on an urgent stop is worth paying
        # for. Keyed off the CALLER's intent rather than off the timeout value
        # — an earlier version compared `timeout > 90`, which silently
        # degraded every stop-loss confirmation the moment someone tuned
        # sell_fill_timeout_seconds past 90.
        poll_interval = 5 if resting else 1
        filled_qty = 0.0
        fill_pps = 0.0
        # Session 16: a RESTING order is the one sell that can outlive a
        # shutdown request, so it stops waiting when one arrives and the
        # caller cancels it while the process is still alive to do so.
        #
        # Deliberately not applied to ordinary confirmations. Review round two
        # caught the unconditional version: a `!stop` five seconds into a
        # trailing-stop sell returned "unfilled", which cancelled a good-for-
        # day order that was still working and might yet have filled. Losing a
        # protective fill to a shutdown is worse than a 45-second wait.
        stop_waiting = self._shutdown if resting else None
        # Money-path review 2026-08-04 (C4): keep the BEST read seen across
        # polls, not the LAST. A poll that reports "partially_filled, 1 @
        # $0.50" followed by a broker 5xx window (whose error shape reports
        # filled_quantity 0) used to return zero fills at timeout — a real
        # fill's P&L unrecorded and the book overstated. cancel_order got
        # exactly this keep-the-last-good-read fix in BUG-38 round 5; this is
        # its twin. Fill quantities are monotonic for one order, so max() is
        # the honest aggregator.
        best_qty = 0.0
        best_pps = 0.0
        while True:
            try:
                status = self.executor.check_order_status(order_id) or {}
            except Exception as e:
                logger.warning(f"check_order_status({order_id}) failed: {e}")
                status = {}
            state = str(status.get("status", "unknown")).lower()
            try:
                filled_qty = float(status.get("filled_quantity") or 0)
            except (TypeError, ValueError):
                filled_qty = 0.0
            try:
                fill_pps = float(status.get("average_price_per_share") or 0)
            except (TypeError, ValueError):
                fill_pps = 0.0
            if filled_qty > best_qty:
                best_qty = filled_qty
                best_pps = fill_pps
            elif filled_qty < best_qty:
                filled_qty, fill_pps = best_qty, best_pps

            if state == "filled":
                return ("filled", filled_qty, fill_pps)
            if state in ("cancelled", "canceled", "rejected", "failed", "expired"):
                # Session 17 / BUG-38: say WHICH state ended the wait, and how
                # early. On 2026-07-31 a resting 0DTE order died 6 seconds into
                # a 771-second rest and the logs could not say why — the exit
                # simply reported "no fill", which reads like a quiet market
                # rather than an order the broker handed straight back. If the
                # broker is refusing our orders, that has to be legible.
                elapsed = float(timeout) - max(0.0, deadline - time.monotonic())
                # Review round 2: state the STATE, do not editorialise the
                # cause. The bot itself invites the operator to "exit by hand
                # on Robinhood" during exactly this window, so a hand cancel
                # lands here too — an earlier draft asserted "this was NOT our
                # cancel", which would have accused the broker of the
                # operator's own action.
                logger.warning(
                    f"Sell order {order_id} ended in broker state '{state}' "
                    f"after {elapsed:.0f}s of a {float(timeout):.0f}s wait "
                    f"(filled {filled_qty:g}) — it did not survive to the end "
                    f"of the wait. Causes: broker rejection, an exchange "
                    f"cancel, or a manual cancel on Robinhood."
                )
                return (
                    "partial" if filled_qty > 0 else "unfilled",
                    filled_qty,
                    fill_pps,
                )
            if time.monotonic() >= deadline:
                return (
                    "partial" if filled_qty > 0 else "unfilled",
                    filled_qty,
                    fill_pps,
                )
            if stop_waiting is not None and stop_waiting.is_set():
                logger.warning(
                    f"Shutdown requested while waiting on sell order "
                    f"{order_id} — stopping the wait so it can be cancelled "
                    f"rather than left resting at the broker"
                )
                return (
                    "partial" if filled_qty > 0 else "unfilled",
                    filled_qty,
                    fill_pps,
                )
            wait = min(poll_interval, max(0.0, deadline - time.monotonic()))
            if stop_waiting is not None:
                # Wakes immediately on shutdown instead of sleeping out the
                # rest of a 5-second poll interval.
                stop_waiting.wait(wait)
            else:
                time.sleep(wait)

    def _cancel_and_inspect(self, order_id: str) -> Tuple[float, float, str]:
        """
        Session 9 (C2/H4): cancel a sell order and INSPECT the result — the
        cancel race may reveal a fill or partial fill that occurred between
        the last status poll and the cancel request.

        Returns (filled_qty, fill_price_per_share, final_status).
        """
        try:
            result = self.executor.cancel_order(order_id)
        except Exception as e:
            logger.error(f"cancel_order({order_id}) failed: {e}")
            return (0.0, 0.0, "error")
        if isinstance(result, dict):
            try:
                qty = float(result.get("filled_quantity") or 0)
            except (TypeError, ValueError):
                qty = 0.0
            try:
                pps = float(result.get("average_price_per_share") or 0)
            except (TypeError, ValueError):
                pps = 0.0
            final = str(result.get("final_status", "")).lower()
            # Session 17 / BUG-38 review round 3. The executor already decides
            # whether the cancel was CONFIRMED (`cancelled`) and logs "cancel
            # NOT confirmed — final state: …" when it was not. That key was
            # being discarded here, and the caller only escalated on
            # ("unknown", "error"). Robinhood cancels are asynchronous, so
            # "queued" / "pending_cancel" / "confirmed" 0.5s after the request
            # is the NORMAL reading — and every one of those sailed through as
            # if the order were dead. Attempt 2 then submitted a full-quantity
            # close for contracts already committed to a live order, which the
            # broker refuses asynchronously: an order placed at 20:45:10 and
            # gone by 20:45:16, which is the 2026-07-31 signature exactly.
            #
            # Anything that is not a definite end-state is reported as
            # "unknown", which the caller already treats as "do NOT re-price"
            # — the safe direction, because the alternative risks a double
            # sell.
            _TERMINAL = ("cancelled", "canceled", "filled", "rejected",
                         "failed", "expired")
            confirmed = result.get("cancelled")
            if final not in _TERMINAL and (confirmed is False or final):
                logger.warning(
                    f"Cancel of order {order_id} NOT confirmed dead "
                    f"(final_status={final!r}, cancelled={confirmed!r}) — "
                    f"treating as UNKNOWN so nothing re-prices over an order "
                    f"that may still be working at the broker"
                )
                final = "unknown"
            if qty > 0:
                logger.warning(
                    f"Cancel race on order {order_id}: {qty} contract(s) "
                    f"filled @ ${pps:.2f} before/while cancelling "
                    f"(final_status={final}) — honoring the fill"
                )
            return (qty, pps, final)
        # Legacy bool-returning executor — no fill info available
        return (0.0, 0.0, "")

    # ─────────────────────────────────────────────────────────────────────

    def handle_caller_scale_in(self, ticker: str, added_contracts: int, notes: str = "", source_channel: Optional[str] = None):
        """
        Handle when a caller adds to an existing position.
        Updates their tracked contract count. Whether WE add depends on
        the decision engine (it goes through the normal entry pipeline).

        Session 9 (M12b): only updates positions whose source channel matches
        `source_channel` when provided (None = legacy match-all behavior).
        """
        with self._lock:
            matching_positions = [
                (k, p) for k, p in self.positions.items()
                if p.ticker == ticker
                and (source_channel is None or p.source == source_channel)
            ]

        for key, position in matching_positions:
            old_count = position.caller_contracts_remaining
            position.caller_contracts += added_contracts
            position.caller_contracts_remaining += added_contracts
            # 2026-08-04 (F8): stamp what was pre-counted so a subsequent
            # merge of OUR OWN executed add doesn't count it a second time.
            position._scale_in_pre_counted = int(
                getattr(position, "_scale_in_pre_counted", 0) or 0
            ) + added_contracts
            self.save_position_state()  # Session 10f

            logger.info(
                f"Caller scaled in: {ticker} | "
                f"+{added_contracts} contracts → "
                f"caller now has {position.caller_contracts_remaining} "
                f"(was {old_count}) | Notes: {notes}"
            )

    def open_position(self, decision: TradeDecision, order_id: str, fill_price: float, management_style: str = ""):
        """Register a new open position after a fill."""
        key = f"{decision.ticker}_{decision.strike}_{decision.expiry}_{decision.direction}"

        # Extract caller's original contract count from the parsed signal
        caller_contracts = 0
        if decision.source_signal:
            caller_contracts = decision.source_signal.caller_contracts or 0

        position = Position(
            ticker=decision.ticker,
            direction=decision.direction,
            strike=decision.strike,
            expiry=decision.expiry,
            contracts=decision.contracts,
            entry_price=fill_price,
            current_price=fill_price,
            high_water_mark=fill_price,
            stop_loss_pct=decision.stop_loss_pct,
            management_rules=decision.management_rules,
            order_id=order_id,
            opened_at=market_time.now_et().isoformat(),
            source=decision.source_signal.source if decision.source_signal else "",
            contracts_remaining=decision.contracts,
            caller_contracts=caller_contracts,
            caller_contracts_remaining=caller_contracts,
            conviction_score=decision.conviction_score,
            management_style=management_style,
        )

        # Session 12 (review round 3): entering a position re-arms the quote
        # heartbeat. It only advances while positions are being priced, so it
        # goes stale through any flat spell — and the first entry afterwards
        # would race the 30s watchdog tick into a false "NO BROKER QUOTES"
        # alert that also consumed the cooldown a real alert would need.
        self.last_quote_ok_ts = time.time()

        # Session 9 (H5): on key collision, MERGE instead of silently
        # overwriting — an overwrite would leave the first fill's contracts
        # live on Robinhood but invisible to management.
        with self._lock:
            existing = self.positions.get(key)
            if existing is not None:
                old_contracts = existing.contracts_remaining
                old_entry = existing.entry_price
                total_remaining = old_contracts + decision.contracts
                if total_remaining > 0:
                    merged_entry = (
                        old_entry * old_contracts + fill_price * decision.contracts
                    ) / total_remaining
                else:
                    merged_entry = fill_price
                existing.contracts += decision.contracts
                existing.contracts_remaining += decision.contracts
                existing.entry_price = round(merged_entry, 4)
                existing.current_price = fill_price
                existing.high_water_mark = max(existing.high_water_mark, fill_price)
                # Money-path review 2026-08-04 (F8): main.py's scale-in
                # handler (handle_caller_scale_in) already added the caller's
                # new contracts BEFORE the entry executed — adding them again
                # here made "caller 6 + adds 4" read 14, and his next
                # "trimmed 5" (half) computed 5/14 and under-trimmed us. The
                # scale-in handler stamps the count it added; consume it.
                _pre_counted = int(
                    getattr(existing, "_scale_in_pre_counted", 0) or 0
                )
                _to_add = max(0, caller_contracts - _pre_counted)
                # R2-7: consume only what THIS merge accounts for — two
                # concurrent scale-ins stamp 4+4, and A's merge must leave
                # B's 4 for B's merge.
                existing._scale_in_pre_counted = max(
                    0, _pre_counted - caller_contracts
                )
                existing.caller_contracts += _to_add
                existing.caller_contracts_remaining += _to_add
                if existing.entry_price > 0:
                    existing.pnl_pct = (
                        (existing.current_price - existing.entry_price)
                        / existing.entry_price * 100
                    )
                # Keep earliest opened_at, original source, and original
                # stop/management rules — the first entry owns the position.
                log_position = existing
                merged = True
            else:
                self.positions[key] = position
                log_position = position
                merged = False

        # Session 10f: give the new position a sidecar record immediately, so a
        # crash before its first new high still restores a correct HWM.
        self.save_position_state()

        if merged:
            logger.warning(
                f"POSITION MERGE: duplicate open for {key} — merged instead of "
                f"overwriting. {old_contracts}x @ ${old_entry:.2f} + "
                f"{decision.contracts}x @ ${fill_price:.2f} → "
                f"{log_position.contracts_remaining}x @ "
                f"${log_position.entry_price:.2f} weighted avg. "
                f"Kept original opened_at/source/management."
            )
            # Money-path review 2026-08-04 (D-1/A3): the merge row is
            # stamped with the position's KEPT opened_at. The restore reads
            # opened_at from the LATEST OPEN row and rejects the sidecar when
            # it disagrees (_same_open, 5s tolerance) — a now-stamped merge
            # row therefore invalidated the identity stamp on every restart,
            # silently discarding the runner floor, a queued caller exit,
            # caller_contracts and trim_carry (the BUG-20/BUG-34 classes).
            self._log_trade(
                "OPEN", log_position,
                reason=f"position_merge (+{decision.contracts}x @ ${fill_price})",
                timestamp=log_position.opened_at or None,
            )
        else:
            self._log_trade("OPEN", position)
        sl_display = f"{decision.stop_loss_pct}%" if decision.stop_loss_pct else "None (challenge)"
        logger.info(
            f"Opened position: {key} | {decision.contracts}x @ ${fill_price} | "
            f"SL: {sl_display} | "
            f"Caller contracts: {caller_contracts or 'unknown'}"
        )

    def _quote_is_sane(self, position: Position, price: float, key: str) -> bool:
        """False if this quote cannot plausibly belong to this contract.

        Session 13 — the backstop for the 2026-07-28 XSP incident. That quote
        arrived perfectly well-formed: $43.33 on an $0.82 contract, +5,184%,
        trailing stop armed at $34.70 on the FIRST poll, two minutes after the
        position opened. Nothing downstream questioned it.

        _resolve_option_id now stops wrong-contract quotes at the source. This
        guard is deliberately independent of that fix: it is the last thing
        standing between a bad number and an armed trailing stop, and the
        source of the NEXT bad number is by definition not yet known.

        Reference is the last ACCEPTED quote, or the entry price before there is
        one. A quote further than `quote_sanity_max_ratio`x from it in either
        direction is discarded exactly like a failed fetch: no P&L, no
        high-water mark, no stop or trail evaluation.

        There is deliberately NO self-healing escape hatch. The first draft of
        this guard accepted a rejected price once N consecutive quotes agreed
        with each other, on the reasoning that a real gap would be consistent
        while an error would be noise. The XSP incident disproves it: a foreign
        contract is a REAL contract, so its price is perfectly consistent, and
        the hatch re-anchored onto $43.33 after three polls and armed the trail
        anyway — defeating the guard in exactly the case it was written for.
        Consistency does not distinguish a true price from a wrong one.

        So a rejection stands until an in-band quote arrives, and the band is
        set wide (10x) rather than clever. The cost of being wrong in each
        direction is not symmetric:

          - reject a real move  -> the position stops being price-managed, we
                                   say so loudly in Discord, and CALLER EXITS
                                   STILL WORK. On a mirror bot those are the
                                   primary exit anyway; the trail is secondary.
          - accept a false move -> a phantom trail arms and fires, and in live
                                   mode that sells a real position at a price
                                   that never existed.

        The first is a bad afternoon. The second is the bug we are fixing.
        """
        max_ratio = float(self.config.get("quote_sanity_max_ratio", 10) or 0)
        if max_ratio <= 1:
            return True  # disabled

        if price <= 0:
            logger.warning(f"QUOTE REJECTED for {key}: non-positive price {price}")
            return False

        # A position restored mid-life without a live quote has no accepted
        # price yet, so entry is the only honest anchor available.
        ref = position.current_price if position.current_price > 0 else position.entry_price
        if ref <= 0:
            return True  # nothing to compare against — cannot judge, don't block

        low, high = ref / max_ratio, ref * max_ratio
        if low <= price <= high:
            position._quote_reject_total = 0
            return True

        total = getattr(position, "_quote_reject_total", 0) + 1
        position._quote_reject_total = total
        # EVERY rejected quote is recorded in positions.log, unthrottled. This
        # is the forensic trail: the whole series
        #   "XSP $720.0P | Entry: $0.82 -> Now: $43.33 | P&L: +5184.1%"
        # is how the 2026-07-28 incident was reconstructed, and a rejected quote
        # never reaches log_position_check because the monitor stops short of
        # it. Throttling the only record of an unknown future failure would
        # trade spam for the evidence. positions.log rotates and is pruned at 7
        # days, which is what it is for.
        _positions_log.debug(
            f"REJECTED {position.ticker} ${position.strike}"
            f"{position.direction[:1].upper()} | Entry: ${position.entry_price:.2f}"
            f" | Quoted: ${price:.2f} | Ref: ${ref:.2f} | Band: "
            f"${low:.2f}-${high:.2f} | Consecutive: {total}"
        )
        # The operator-facing line is throttled, like the price-failure path
        # next to it: at a ~6s poll an unthrottled warning is ~4,000 lines a
        # session, which is how the 318-error day happened.
        if total <= 1 or total % 10 == 0:
            logger.warning(
                f"QUOTE REJECTED for {key}: ${price:.2f} against reference "
                f"${ref:.2f} (outside {max_ratio:g}x band ${low:.2f}-${high:.2f}) "
                f"— discarding, {total} consecutive."
            )
        # A position rejecting every quote is unmanaged in silence: the outage
        # watchdog stays happy because quotes ARE arriving. Say so — and keep
        # saying it, because there is no automatic recovery and a single alert
        # six hours ago is not a live warning.
        if self._should_alert_unusable(total) and self.notifier:
            try:
                self.notifier.notify_error(
                    f"⚠️ **{position.ticker} ${position.strike} "
                    f"{position.direction}** — {total} price quotes rejected as "
                    f"implausible (latest ${price:.2f} vs reference ${ref:.2f}). "
                    f"This position is NOT being price-managed: no stop, trail "
                    f"or P&L is updating. A caller exit will still be followed. "
                    f"If the move is real, close it by hand or restart to "
                    f"re-anchor."
                )
            except Exception as e:  # a notification must never break the monitor
                logger.debug(f"Could not send quote-reject alert: {e}")
        return False

    # ── Session 15 (BUG-36): a single tick may not close a position ──────────
    #
    # Live, 2026-07-29, first day of real money. XSP 710P, trail armed with a
    # $0.30 trigger:
    #
    #   20:50:03  Now: $0.34   +30.8%   Trail: ACTIVE @ $0.30
    #   20:50:09  Now: $0.40   +53.8%   Trail: ACTIVE @ $0.30
    #   20:50:14  TRAILING STOP HIT | Price: $0.28 | Trail: $0.30
    #   20:50:16  EXIT CONFIRMED | filled @ $0.37          <-- the proof
    #
    # An urgent sell prices off the bid, so a $0.37 fill two seconds later
    # means the real book never went near $0.28. Mid went 0.40 -> 0.28 -> (a
    # book at 0.37+) in seven seconds: a momentary one-sided quote ten minutes
    # before the close. caller_a TP'd the same contract at +160% seven minutes
    # later; we booked +42%.
    #
    # Note what was NOT wrong. The genuine pullback that afternoon bottomed at
    # $0.33 against a $0.30 trigger — the 30% distance the operator set in Session 14
    # was holding, exactly as intended. The parameters were right; the trigger
    # was wrong. ONE reading of a mid price closed a live position.
    #
    # _quote_is_sane cannot catch this and should not try. Its 10x band exists
    # for a quote belonging to a DIFFERENT contract (the 2026-07-28 QQQ
    # incident); $0.28 on a $0.26 contract is perfectly plausible for this
    # one. The defect is not an implausible price, it is an unconfirmed one.
    #
    # Same class as the Session 10f opening-bell spike, which armed AND fired
    # TE's trail on the 09:30:01 print. That was fixed with a time window,
    # which covers the first 60 seconds of the session. This covers the other
    # six and a half hours.
    #
    # The rule: no exit INFERRED FROM PRICE fires on one reading. Instructions
    # are untouched — caller exits, the deferred exit_at_open, the PDT
    # next-day sell and the 15:45 0DTE sweep are decisions already taken and
    # never wait for a second opinion.

    def _confirmation_ticks_required(self) -> int:
        """Consecutive agreeing readings needed before an inferred exit fires.

        <= 1 fires on a single reading (the re-quote check is separate and
        still applies unless it too is disabled).

        A key that is present but BLANK in YAML parses as None, and `or 0`
        would quietly turn the whole guard off. Only an explicit number
        counts; anything else falls back to the default, loudly enough to
        find in the log.
        """
        raw = self.config.get("exit_confirmation_ticks", 2)
        if raw is None or raw == "":
            return 2
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning(
                f"exit_confirmation_ticks={raw!r} is not a number — using 2"
            )
            return 2

    def _guard_flag(self, name: str) -> bool:
        """A boolean guard switch that fails SAFE.

        `bool(self.config.get(name, True))` looks right and is not: a key
        present but blank in YAML parses as None, so `confirm_high_water_mark:`
        with nothing after the colon — an operator mid-edit — silently turned
        the guard off with no log line. Only an explicit false disables one of
        these.
        """
        raw = self.config.get(name, True)
        if raw is None or raw == "":
            return True
        if isinstance(raw, str):
            return raw.strip().lower() not in ("false", "no", "off", "0")
        return bool(raw)

    def _confirm_high_water_mark(self) -> bool:
        """Whether a new high needs corroborating before it ratchets the mark.

        Deliberately its own switch rather than riding on
        exit_confirmation_ticks. They are different mechanisms with different
        evidence behind them: the fire delay costs a tick on a genuine
        collapse, while the high-water guard has no downside in a monotone
        move and prevents a permanent corruption. Turning one down should not
        silently disable the other.
        """
        return self._guard_flag("confirm_high_water_mark")

    @staticmethod
    def _display_key(position: Position) -> str:
        return (
            f"{position.ticker}_{position.strike}_{position.expiry}_"
            f"{position.direction}"
        )

    def _quote_in_band(self, position: Position, price: float) -> bool:
        """The band test from _quote_is_sane, with none of its side effects,
        and measured against ENTRY rather than the last accepted price.

        Two departures, both deliberate.

        Side effects: _quote_is_sane counts consecutive rejections and
        escalates to Discord with "this position is NOT being price-managed"
        — true of a tick quote, false and alarming for a CONFIRMATION quote,
        where the tick itself was fine and the exit fires regardless.

        Reference: _quote_is_sane anchors on `current_price`, which by the
        time a confirmation runs IS the suspect tick — the monitor wrote it in
        before the checks. A descending glitch therefore drags the band down
        with it and the HONEST re-quote gets thrown out as implausible, which
        does not veto, so the exit fires anyway. Concretely: entry $0.50, real
        book $0.55, ticks of $0.08 then $0.02 (each within 10x of the one
        before), re-quote returns the true $0.55, band computed around $0.02
        is $0.002-$0.20, verdict "implausible", sold at $0.02 into a $0.55
        book. Entry is the one anchor a run of bad ticks cannot move.
        """
        try:
            max_ratio = float(self.config.get("quote_sanity_max_ratio", 10) or 0)
        except (TypeError, ValueError):
            return True
        if max_ratio <= 1:
            return True
        if price is None or price <= 0:
            return False
        ref = position.entry_price if position.entry_price > 0 else position.current_price
        if ref <= 0:
            return True
        return ref / max_ratio <= price <= ref * max_ratio

    def _run_is_broken(self, position: Position, kind: str) -> bool:
        """True when the previous agreeing reading was not the previous pass.

        This is what makes "consecutive" honest. Every early `continue` in the
        monitor loop — no quote, an out-of-band quote, the opening settle, the
        entry cooldown, a PDT hold, a manual position — skips the price checks
        without touching the counters. Counting readings alone, a phantom at
        15:50 and another at 16:10 with an eight-minute quote outage between
        them would "confirm" each other and close the position on what is
        really one reading each.

        Comparing monitor passes closes that: a pass this position could not
        be evaluated on simply never records, so the gap voids the run. Direct
        calls outside the loop (tests, and _check_trailing_stop used as a
        predicate) see an unchanged pass index and are treated as consecutive.
        """
        last = position.breach_pass.get(kind) if isinstance(
            getattr(position, "breach_pass", None), dict
        ) else None
        return last is not None and last < self._pass_seq - 1

    def _ratchet_high_water_mark(self, position: Position, price: float) -> None:
        """Raise the high-water mark, but only to a level two readings agree on.

        The high-water mark is the one piece of monitor state a phantom tick
        corrupts PERMANENTLY: it only ever goes up, the sidecar persists it,
        and every future trail trigger is computed from it. S has carried a
        phantom $1.07 since 2026-07-27 — 19% above anything it ever traded.

        So a new high is held as `pending_high` and the mark moves to the
        highest level TWO readings have both reached — in effect the
        second-highest recent reading. The consequence worth stating plainly:
        a phantom's own value can never become the mark, which is the whole
        point, but a phantom CAN act as the second sighting for a genuine
        high that follows it. That is why the evidence is consumed on use
        (`pending_high` drops to the price that just ratcheted) instead of
        sitting at the phantom level for the rest of the session.

        `pending_high` deliberately survives readings BELOW the mark; only a
        gap in the pass sequence clears it. Clearing it on every dip pins the
        mark solid on choppy tape — 0.31, 0.29, 0.33, 0.29, 0.35, 0.29, 0.42
        would never ratchet once, and a position up 60% would carry a trail
        computed from its opening mark. That failure is worse than the one
        being prevented.

        Cost, on a monotone rally: the mark is one reading behind the peak
        (0.41, 0.42, 0.43 marks 0.42), which widens the trigger slightly —
        the harmless direction.
        """
        if not self._confirm_high_water_mark():
            if price > position.high_water_mark:
                position.high_water_mark = price
                self.save_position_state()
            return

        # Evidence expires. `pending_high_pass` is the pass the pending high
        # was RAISED on, not the last pass we looked — so a corroborator that
        # nothing has renewed for a few passes is discarded.
        #
        # Round 3 of review caught this: the first version only cleared on a
        # GAP in the pass sequence, so on a position evaluated every pass a
        # phantom $1.07 sat in pending_high indefinitely, and a second phantom
        # twenty passes later was "corroborated" by it — the mark ratcheted on
        # a single reading and the trail then fired on the genuine book. That
        # is defect 1 of round 2 wearing a different hat: two lone readings,
        # far apart, confirming each other.
        stale_after = _PENDING_HIGH_MAX_AGE_PASSES
        if (
            position.pending_high > 0
            and self._pass_seq - position.pending_high_pass > stale_after
        ):
            position.pending_high = 0.0

        if price <= position.high_water_mark:
            return

        corroborated = (
            min(price, position.pending_high) if position.pending_high > 0 else 0.0
        )
        if corroborated > position.high_water_mark:
            position.high_water_mark = corroborated
            position.pending_high = price   # evidence spent, not banked
            position.pending_high_pass = self._pass_seq
            # Session 10f: a new high is exactly the state a restart used to
            # throw away. Persist it now — the process may not get a shutdown.
            self.save_position_state()
        elif price > position.pending_high:
            position.pending_high = price
            position.pending_high_pass = self._pass_seq

    @staticmethod
    def _reanchor_mark_for_arming(position: Position) -> None:
        """Drop a high-water mark that sits above the price we are arming at.

        Session 15 (BUG-36). A peak ABOVE the arming price is a peak this
        trail never trailed from: either stale (it predates the arming) or
        phantom (S has carried $1.07 since 2026-07-27). It cannot
        retroactively protect anything, and leaving it in place puts the
        trigger above the market — arming straight into a fire. The monitor's
        ratchet recomputes the trigger from the mark on the very next pass, so
        clamping only the trigger does not hold; the mark itself has to come
        down. That also finally clears a poisoned mark off disk.

        Normal arming never sees this: a trail arms as a position makes new
        highs, so the mark IS the current price and nothing changes. Call it
        from EVERY site that sets trailing_stop_active True.
        """
        if position.current_price <= 0:
            return
        if position.high_water_mark > position.current_price:
            logger.warning(
                f"{position.ticker}: high-water mark "
                f"${position.high_water_mark:.2f} is above the arming price "
                f"${position.current_price:.2f} — it predates this trail, so "
                f"anchoring to the current price instead (BUG-36)."
            )
            position.high_water_mark = position.current_price

    def _breach_confirmed(
        self,
        position: Position,
        kind: str,
        still_breached,
        describe: str,
    ) -> bool:
        """True when `kind`'s threshold has been breached by enough readings.

        `still_breached(price) -> bool` must be the SAME test the caller
        branched on, expressed as a function of price. Not a threshold: the
        two are algebraically equal and not equal in IEEE754. A 30% stop on a
        $0.40 entry branches on `pnl_pct <= -30` at exactly $0.28, while
        `0.28 <= 0.40 * 0.7` is False — 0.4*0.7 is 0.27999999999999997. The
        re-quote would have vetoed that stop on every single tick, for ever,
        and logged "did not survive a second look" about a price identical to
        the one that triggered it. Thirteen such (entry, price) pairs exist at
        the default 30% stop alone.

        Two confirmations, in increasing order of cost:

          1. N consecutive monitor readings, ~6s apart — consecutive in
             PASSES, not merely in count (see _run_is_broken). This alone
             would have saved the XSP trade; the phantom was gone by the very
             next tick, and every phantom in the live logs so far has lasted
             exactly one tick.
          2. One fresh quote at the moment of firing, which must agree.

        They are NOT independent, and the second is much the weaker: it is
        taken a fraction of a second after the reading it is checking, so any
        glitch that outlives one poll interval will be seen by both. Layer 1
        is what does the work. Layer 2 catches the narrow case where the book
        heals in between, and — the reason it earns its place — it is what
        makes the price we log and book the freshest real one, instead of the
        "P&L: 7.7%" the 2026-07-29 exit recorded off a quote that never was.

        A re-quote that fails or comes back implausible does NOT veto the
        exit. By then N consecutive readings have agreed, and refusing to act
        because the broker went quiet is how a stop quietly stops protecting
        anything. It is logged either way.
        """
        required = self._confirmation_ticks_required()
        for attr in ("breach_ticks", "breach_pass"):
            if not isinstance(getattr(position, attr, None), dict):
                setattr(position, attr, {})   # restored/legacy objects

        _run_broken = self._run_is_broken(position, kind)
        if _run_broken and int(position.breach_ticks.get(kind) or 0) > 0:
            # Money-path review 2026-08-04 (B2): an intermittent quote (a thin
            # book flickering one-sided every other poll) voids the run each
            # time and the breach can NEVER confirm — while every alarm stays
            # silent, because the failure ladder counts only CONSECUTIVE
            # misses. Count the voids and say so: a protection that cannot
            # fire must not be a secret.
            _vc = getattr(position, "_breach_void_counts", None)
            if not isinstance(_vc, dict):
                _vc = {}
                position._breach_void_counts = _vc
            _voids = int(_vc.get(kind, 0) or 0) + 1
            _vc[kind] = _voids
            # Round 2 (R2-4): notify_status, not notify_error — the retrying
            # error webhook blocks the monitor thread up to ~90s, the exact
            # head-of-line stall the veto notice below was demoted for.
            if _voids in (8, 40, 120) and self.notifier:
                try:
                    self.notifier.notify_status(
                        f"⚠️ **{position.ticker}** — the {kind} exit has "
                        f"breached and been interrupted by quote gaps "
                        f"{_voids} times: it may be UNABLE to confirm on "
                        f"this book. Not being reliably price-managed — "
                        f"watch it manually (caller exits still act)."
                    )
                except Exception as _ve:  # noqa: BLE001
                    logger.debug(f"Void-run alert failed: {_ve}")
        prior = 0 if _run_broken else int(
            position.breach_ticks.get(kind) or 0
        )
        ticks = prior + 1
        position.breach_ticks[kind] = ticks
        position.breach_pass[kind] = self._pass_seq

        if ticks < required:
            logger.info(
                f"{describe} — breach {ticks}/{required}, holding for "
                f"confirmation (BUG-36 guard)"
            )
            return False

        # An exit is already being placed for this position; re-deciding it
        # every 6s only spends broker calls inside the monitor loop, which is
        # the one place a blocking call stalls every OTHER position's checks.
        if position.exit_in_flight:
            logger.debug(f"{describe} — exit already in flight, not re-deciding")
            return False

        if self._guard_flag("exit_confirmation_requote"):
            key = self._display_key(position)
            fresh = None
            try:
                fresh = self.executor.get_option_price(
                    position.ticker,
                    position.expiry,
                    position.strike,
                    position.direction,
                )
            except Exception as e:
                logger.warning(f"Confirmation re-quote failed for {key}: {e}")

            if fresh is None:
                logger.warning(
                    f"{describe} — no confirmation quote available; firing on "
                    f"{ticks} agreeing readings"
                )
            elif not self._quote_in_band(position, fresh):
                logger.warning(
                    f"{describe} — confirmation quote ${fresh:.2f} is "
                    f"implausible; firing on {ticks} agreeing readings"
                )
            elif not still_breached(fresh):
                msg = (
                    f"{describe} — VETOED by re-quote ${fresh:.2f}. The "
                    f"reading that triggered this did not survive a second "
                    f"look; the position stays open (BUG-36 guard)."
                )
                logger.warning(msg)
                # the operator needs to see this: "the trail decided to exit and was
                # overruled" is exactly the event worth a human's attention.
                #
                # THROTTLED, because the first version was not and a price
                # oscillating around the trigger vetoes every other pass — 100
                # Discord posts in twenty minutes, each a blocking POST inside
                # the monitor loop, stalling every other position's stop
                # check, and at info level they are not even retried when
                # Discord starts rate-limiting. The log line above is
                # unthrottled and keeps the full forensic record.
                now = time.time()
                last = getattr(position, "_last_veto_notice_ts", 0.0)
                if self.notifier and now - last >= _VETO_NOTICE_COOLDOWN_SECONDS:
                    position._last_veto_notice_ts = now
                    try:
                        self.notifier.notify_status(
                            f"🛑 **{position.ticker}** — automated "
                            f"{kind} exit held: the trigger price "
                            f"${position.current_price:.2f} did not survive a "
                            f"fresh quote (${fresh:.2f}). Position still open. "
                            f"(Further holds on this position are logged, not "
                            f"posted, for {_VETO_NOTICE_COOLDOWN_SECONDS // 60} min.)"
                        )
                    except Exception as e:
                        logger.debug(f"Could not send veto notice: {e}")
                position.breach_ticks[kind] = 0
                # A real quote is a real quote: keep it, so P&L and the next
                # sanity reference are current rather than the number we just
                # refused to trade on.
                self._apply_price(position, fresh)
                return False
            else:
                # Confirmed. Decide, log and book from the fresher price — the
                # 2026-07-29 exit logged "P&L: 7.7%" off the phantom while the
                # fill came back +42.3%.
                self._apply_price(position, fresh)

        logger.info(f"{describe} — CONFIRMED by {ticks} readings")
        _vc = getattr(position, "_breach_void_counts", None)
        if isinstance(_vc, dict):
            _vc.pop(kind, None)
        return True

    @staticmethod
    def _clear_breach(position: Position, kind: str) -> None:
        """Forget an in-progress breach. Any reading on the right side of the
        threshold breaks the run — confirmation means CONSECUTIVE.

        2026-08-04 (B2): a clear is a GENUINE recovery (a reading on the safe
        side), not a quote gap — reset the void alarm with it."""
        ticks = getattr(position, "breach_ticks", None)
        if isinstance(ticks, dict):
            if ticks.pop(kind, None):
                # R2-4: per-kind — a floor recovery must not silence the
                # stop's tally.
                _vc = getattr(position, "_breach_void_counts", None)
                if isinstance(_vc, dict):
                    _vc.pop(kind, None)

    @staticmethod
    def _apply_price(position: Position, price: float) -> None:
        """Adopt a fresh quote as the position's current price and P&L.

        Deliberately does NOT touch the high-water mark: a re-quote is a
        single unconfirmed reading, and the mark only moves through
        _ratchet_high_water_mark on the regular tick cadence.
        """
        position.current_price = price
        if position.entry_price:
            position.pnl_pct = (
                (price - position.entry_price) / position.entry_price * 100
            )

    def check_all_positions(self):
        """
        Main loop call - check all open positions and manage them.
        This should be called every few seconds during market hours.
        """
        try:
            self._check_all_positions_inner()
        finally:
            # Session 12: heartbeat for the position-monitor watchdog. Set on
            # EVERY return path — the watchdog fires on the timestamp going
            # stale, which happens when a brokerage API call HANGS mid-pass
            # (observed live during the 2026-07-28 Robinhood outage: quotes
            # stopped, no exception, no log line, the monitor thread just
            # stopped ticking — which in live mode is stop-losses silently
            # not being checked).
            self.last_position_check_ts = time.time()

    def _check_all_positions_inner(self):
        # Session 15 (BUG-36): monitor passes are numbered so a breach run can
        # tell "the previous reading" from "a reading before an eight-minute
        # quote outage". Only the monitor thread touches this.
        self._pass_seq += 1

        # Reset daily P&L at start of each new trading day.
        # This runs even with zero open positions (unlike evaluate() which
        # only fires on incoming signals).
        # Session 9: US trading date, not host-local (UK) date.
        today_str = market_time.trading_date().isoformat()
        if today_str != self._last_pnl_reset_date:
            logger.info(
                f"New trading day: resetting TradeManager daily P&L "
                f"from ${self.daily_pnl:+.2f} to $0.00"
            )
            self.daily_pnl = 0.0
            self._last_pnl_reset_date = today_str

        keys_to_remove = []

        # ── Session 10f: opening-bell settle ─────────────────────────────────
        # The first quotes after 9:30 ET are not trustworthy. On 2026-07-27 the
        # tick at 09:30:01 printed TE at $1.02 against a real $0.55 — that
        # cleared the 60% trailing-stop activation, armed the trail at $0.82,
        # and fired six seconds later when the quote reverted, closing the
        # position at -8.3%. S spiked in the same instant and touched $1.00+ on
        # 4 ticks out of 6,171 that day, all 17 seconds of it at the open.
        #
        # So for the first `market_open_settle_seconds` we neither ratchet the
        # high-water mark nor evaluate any AUTOMATED exit. A phantom high is
        # not just a bad trade — it raises the HWM permanently, and the sidecar
        # then persists that corrupted peak (S is still carrying $1.07 from
        # that tick, 19% above anything it actually traded).
        #
        # Caller-driven exits are deliberately NOT gated: they arrive through
        # handle_caller_exit, not this loop, so a "cut it" posted at 9:31 still
        # fires immediately. The settle suppresses OUR inferences about price,
        # never the caller's instructions.
        settle_secs = self.config.get("market_open_settle_seconds", 60)
        _since_open = market_time.seconds_since_open()
        in_open_settle = _since_open is not None and _since_open < settle_secs
        if in_open_settle and not self._settle_logged_for == market_time.trading_date():
            self._settle_logged_for = market_time.trading_date()
            logger.info(
                f"Opening-bell settle: holding automated stop/trail checks and "
                f"high-water-mark updates for the first {settle_secs}s of the "
                f"session. Caller exits are unaffected."
            )

        # Snapshot to avoid "dictionary changed size during iteration" if a
        # position is removed by another thread (e.g. handle_caller_exit) while
        # the position monitor loop is mid-iteration.
        for key, position in self._positions_snapshot():
            try:
                # Get current price
                current_price = self.executor.get_option_price(
                    position.ticker,
                    position.expiry,
                    position.strike,
                    position.direction,
                )

                if current_price is None:
                    # BUG-19: track consecutive failures per position to reduce
                    # log spam (318 errors on 3/13 from expired ONDS contracts).
                    # Only log every 10th failure after the first.
                    fail_count = getattr(position, '_price_fail_count', 0) + 1
                    position._price_fail_count = fail_count
                    if fail_count <= 1 or fail_count % 10 == 0:
                        logger.warning(
                            f"Could not get price for {key} "
                            f"(consecutive failures: {fail_count})"
                        )
                    # Session 13: this used to be log-only, and that is a hole.
                    # A position that cannot be priced has no stop, no trail and
                    # no P&L — and the outage watchdog will not notice, because
                    # last_quote_ok_ts is manager-wide and any OTHER healthy
                    # position keeps it fresh. After the resolver hardening the
                    # most likely cause is a contract this account cannot quote
                    # at all, which is a permanent state, not a blip.
                    if self._should_alert_unusable(fail_count) and self.notifier:
                        try:
                            self.notifier.notify_error(
                                f"⚠️ **{position.ticker} ${position.strike} "
                                f"{position.direction}** — no usable price for "
                                f"{fail_count} consecutive checks. This position "
                                f"is NOT being price-managed: no stop, trail or "
                                f"P&L is updating. A caller exit will still be "
                                f"followed. Check the logs for CONTRACT NOT "
                                f"RESOLVED."
                            )
                        except Exception as e:
                            logger.debug(f"Could not send price-failure alert: {e}")
                    # Session 17 / BUG-38 review round 5. Do NOT `continue`.
                    #
                    # This skip sat ABOVE the PDT next-day sell and the
                    # deferred caller exit, so "no price" silently withheld
                    # two decisions that were already taken and never needed a
                    # price — the exact failure the comment 150 lines below
                    # warns about ("how a caller's 'all out', queued
                    # overnight, gets silently dropped the next morning").
                    # It became reachable far more often once a one-sided book
                    # started reporting no price instead of half an ask, and a
                    # one-sided book minutes after 09:30 is the normal case —
                    # which is exactly when exit_at_open fires.
                    #
                    # Treat it as the price verdict it is: no trustworthy
                    # quote, so no price-INFERRED exit, while everything
                    # instruction-driven runs on as before.
                    quote_ok = False
                else:
                    # Reset failure counter on success
                    position._price_fail_count = 0
                    # Session 12: a real price came back — the fast-fail outage
                    # signal for the watchdog. Distinct from the pass
                    # heartbeat: a pass also "completes" when every quote
                    # fails, which is exactly what a non-hanging outage looks
                    # like. Stays inside the success branch (round 5): a pass
                    # with NO price must not refresh the outage clock, or the
                    # watchdog stops being able to see an outage at all.
                    self.last_quote_ok_ts = time.time()

                    # Session 13: is this price believable for this contract?
                    # Deliberately evaluated AFTER last_quote_ok_ts — the
                    # broker DID answer, so this is not an outage and the
                    # watchdog must not treat it as one. _quote_is_sane raises
                    # its own alarm.
                    #
                    # An unbelievable quote is not a reason to skip the whole
                    # position: the deferred caller exit and the PDT next-day
                    # sell below are decisions already made and do not need a
                    # price to be correct. So we carry the verdict down to just
                    # above the checks that INFER an exit from price, and stop
                    # there.
                    quote_ok = self._quote_is_sane(position, current_price, key)

                if quote_ok:
                    position.current_price = current_price
                    position.pnl_pct = (
                        (current_price - position.entry_price)
                        / position.entry_price * 100
                    )

                    # Update high water mark — but not on an opening-bell print,
                    # which would raise it permanently on a quote that lasted one
                    # tick, and (Session 15 / BUG-36) not on any single tick:
                    # the same phantom can print at 15:50 as at 09:30.
                    if not in_open_settle:
                        self._ratchet_high_water_mark(position, current_price)

                # --- PDT-HELD: sell at next market open ---
                # If this position was blocked from closing yesterday due to PDT,
                # sell it immediately today (it's now a different day = not a day trade)
                # Session 9 (C3): trading-day rollover is computed in US/Eastern
                # (UK midnight is NOT a new trading day), the sell only fires
                # during market hours, and the position is only removed when the
                # exit actually confirms.
                if position.pdt_sell_next_open:
                    # Day 4 review round 1: this retry re-fires every tick on
                    # failure — the exact BUG-40 spam loop, through the PDT
                    # door. It is a retry of a decided instruction, so a
                    # backoff-cadence retry is still faithful to it.
                    _pdt_backoff = float(
                        getattr(position, "exit_backoff_until", 0.0) or 0.0
                    )
                    if _pdt_backoff and time.time() < _pdt_backoff:
                        logger.debug(
                            f"PDT-held {key}: sell-failure backoff active — "
                            f"retrying after it lapses"
                        )
                        continue
                    opened_dt = self._parse_opened_at(position.opened_at)
                    if opened_dt is not None:
                        if not market_time.is_market_hours():
                            logger.debug(
                                f"PDT-held {key}: market closed — waiting for "
                                f"next open to sell"
                            )
                            continue
                        if market_time.trading_date(opened_dt) < market_time.trading_date():
                            # It's a new trading day — safe to close
                            logger.info(
                                f"PDT-HELD POSITION: Selling {key} at open | "
                                f"Original exit reason: {position.pdt_held_reason} | "
                                f"P&L: {position.pnl_pct:.1f}%"
                            )
                            # Re-enable normal exit (bypass PDT check since it's next day)
                            position.pdt_held = False
                            position.pdt_sell_next_open = False
                            # Money-path review 2026-08-04 (B3): this sell ran
                            # INLINE on the monitor thread — placement +
                            # confirm + cancel is 50-95s (minutes with an
                            # inline re-auth) during which no other position
                            # got stop/floor/deferred-exit checks, and the
                            # FROZEN watchdog then blamed a hung broker call.
                            # A worker, with the flags re-latched on failure.
                            def _pdt_relatch(p=position, k=key):
                                p.pdt_held = True
                                p.pdt_sell_next_open = True
                                logger.error(
                                    f"PDT-held next-day sell did not confirm "
                                    f"for {k} — position stays tracked, will "
                                    f"retry"
                                )
                            spawned = self._spawn_exit_worker(
                                key, position,
                                f"pdt_held_next_day_sell ({position.pdt_held_reason})",
                                urgent=True, on_failure=_pdt_relatch,
                            )
                            if not spawned:
                                position.pdt_held = True
                                position.pdt_sell_next_open = True
                            continue
                        else:
                            # Still same trading day — keep holding
                            logger.debug(f"PDT-held {key} still same trading day, continuing hold")
                            continue

                # --- MANUALLY OPENED: track price but never auto-manage ---
                if not position.bot_managed:
                    logger.debug(
                        f"MANUAL {key}: price "
                        f"{'unavailable' if current_price is None else f'${current_price:.2f}'} "
                        f"({position.pnl_pct:+.1f}%) — not bot-managed, skipping auto-exits"
                    )
                    continue

                # --- PDT-HELD: skip all management if holding overnight ---
                if position.pdt_held:
                    logger.debug(
                        f"PDT-HELD {key}: Skipping management | P&L: {position.pnl_pct:.1f}%"
                    )
                    continue

                # --- Check stop loss ---
                # Session 9 verify-pass: exits confirm fills and can block for
                # up to ~2× sell_fill_timeout_seconds. Run them in worker
                # threads so one confirming exit can't stall stop checks for
                # every OTHER position during a broad selloff (head-of-line
                # blocking). exit_in_flight prevents duplicate workers.
                if in_open_settle:
                    # Price is still settling — skip every inference we would
                    # draw from it. The caller-exit path is untouched.
                    logger.debug(
                        f"{key} | opening-bell settle "
                        f"({_since_open:.0f}s < {settle_secs}s) — "
                        f"automated checks held | quote "
                        f"{'unavailable' if current_price is None else f'${current_price:.2f}'}"
                    )
                    continue

                # --- Deferred caller exit (Session 10f) ---
                # Runs after the settle gate above, so a queued exit does not
                # sell into the opening-bell print.
                if position.exit_at_open:
                    # Round 3 (F1): the monitor runs 24/7 and the only gate
                    # above is the opening settle, which is False whenever
                    # seconds_since_open() is None — i.e. ALL after-hours and
                    # pre-market. The Session 10f deferral therefore never
                    # actually waited for the open while the bot stayed up:
                    # it fired into the shut book one pass after deferral,
                    # logging "market is open and settled". One line makes
                    # the deferral mean what it says; flags are retained.
                    if not market_time.is_market_hours():
                        continue
                    reason = position.exit_at_open_reason or "caller_exit"
                    # Money-path review 2026-08-04 (C1/F1): a deferred TRIM is
                    # a partial instruction — routing it through the full-exit
                    # worker liquidated the caller's held remainder. It now
                    # replays through handle_caller_exit's trim path (all the
                    # proportional math and caller-count bookkeeping), on its
                    # own thread so a slow trim cannot stall the monitor.
                    if reason == "caller_trim":
                        _t_contracts = int(getattr(
                            position, "exit_at_open_trim_contracts", 0) or 0)
                        _t_pct = float(getattr(
                            position, "exit_at_open_trim_pct", 0.0) or 0.0)
                        position.exit_at_open = False
                        position.exit_at_open_reason = ""
                        position.exit_at_open_trim_contracts = 0
                        position.exit_at_open_trim_pct = 0.0
                        self.save_position_state()
                        logger.warning(
                            f"Firing deferred caller_trim for {key} — market "
                            f"is open and settled (caller trimmed "
                            f"{_t_contracts or 'an unstated number'})"
                        )
                        _info = {
                            "type": "trim",
                            "trim_contracts": _t_contracts or None,
                            "trim_pct": _t_pct or None,
                            "notes": (
                                getattr(position, "exit_at_open_trim_notes", "")
                                or "deferred after-hours caller trim"
                            ),
                            "raw_message": "",
                            "source_channel": position.source,
                            # Round 2 (R2-2): each flagged position replays
                            # ITSELF only. Without this, two flagged MRVL
                            # positions each replayed a ticker-wide trim —
                            # every position trimmed twice and caller counts
                            # decremented twice.
                            "only_key": key,
                        }
                        threading.Thread(
                            target=self.handle_caller_exit,
                            args=(position.ticker, _info),
                            daemon=True,
                            name=f"deferred-trim-{position.ticker}",
                        ).start()
                        continue
                    # Round 2 (M4): clear the flags only when a worker
                    # actually started. _spawn_exit_worker returns False on
                    # exit_in_flight, and the old clear-first shape consumed
                    # the deferred instruction in exactly the race round 1's
                    # fix closed for live caller exits.
                    spawned = self._spawn_exit_worker(
                        key, position, reason, urgent=True
                    )
                    if spawned:
                        logger.warning(
                            f"Firing deferred {reason} for {key} — market is "
                            f"open and settled"
                        )
                        position.exit_at_open = False
                        position.exit_at_open_reason = ""
                        self.save_position_state()
                    else:
                        logger.info(
                            f"Deferred {reason} for {key} waiting on an "
                            f"in-flight exit — will retry next pass"
                        )
                    continue

                # --- Day 4 review round 1: queued caller exit ---
                # An instruction that arrived while another exit attempt held
                # exit_in_flight. Re-fired here as an instruction — above the
                # quote gate and NOT subject to the sell-failure backoff
                # (instructions are never gated). If the flight is still up,
                # leave the flag for the next pass; if the spawn loses a race
                # to a new flight, put the flag back rather than lose the
                # caller's exit.
                if (getattr(position, "pending_caller_exit", False)
                        and not position.exit_in_flight):
                    # Round 2 (M3): a queue drained after the close must
                    # convert to the deferred-exit path, not sell into a shut
                    # book (fail twice, walk the broker, burn the queue).
                    if not market_time.is_market_hours():
                        position.pending_caller_exit = False
                        position.exit_at_open = True
                        position.exit_at_open_reason = "caller_exit"
                        self.save_position_state()
                        logger.warning(
                            f"Queued caller exit for {key} drained after "
                            f"hours — converted to exit-at-open"
                        )
                        continue
                    position.pending_caller_exit = False
                    _plim = getattr(position, "pending_caller_exit_limit", 0.0)
                    logger.warning(
                        f"Re-firing queued caller exit for {key}"
                    )
                    spawned = self._spawn_exit_worker(
                        key, position, "caller_exit", urgent=True,
                        limit_price=(_plim or None),
                    )
                    if not spawned:
                        position.pending_caller_exit = True
                    continue

                # Session 13: the line between instructions and inferences.
                #
                # Everything ABOVE is instruction- or time-driven — a deferred
                # caller exit and the PDT next-day sell are decisions already
                # taken, and a price we do not believe is no reason to withhold
                # them. Holding them here is how a caller's "all out", queued
                # overnight, gets silently dropped the next morning.
                #
                # Everything BELOW infers an exit FROM the price, so it needs a
                # price worth trusting. Without one it does not run at all: a
                # stop-loss, a trailing stop and a profit trim computed from a
                # foreign contract's quote are the 2026-07-28 incident.
                # --- Money-path review 2026-08-04 (B1): hours gate ---
                # The monitor runs 24/7 and everything below infers exits
                # FROM PRICE. Options do not trade outside the session, and
                # a frozen after-hours snapshot can sit below an engaged
                # profit floor all night — before this gate that meant a
                # fire→fail→walk→🚨 cycle every backoff window until the
                # 09:30 settle (~130 pings overnight). Instructions all sit
                # ABOVE this line and are untouched; floor CLEARANCE also
                # correctly waits for live prices now.
                if not market_time.is_market_hours():
                    continue

                # --- Entry cooldown: suppress PRICE-INFERRED exits after fill ---
                # Prevents two failure modes:
                #   1. Bid/ask spread illusion: fill at ask, first mid-price check
                #      shows an apparent loss that looks like a stop trigger.
                #   2. Fast callers: if the caller exits within the same window as
                #      our fill (common with caller_a), the exit arrives before the
                #      position has had any time to develop.
                # 2026-08-04 (B5): moved BELOW the deferred-exit and queued-
                # caller-exit blocks — it used to gate those instructions too,
                # violating "instructions are never gated" whenever the
                # cooldown is non-zero (live config is 0, so inert, but the
                # ordering must not lie in wait).
                cooldown_secs = self.config.get("entry_stop_cooldown_seconds", 120)
                opened_dt = self._parse_opened_at(position.opened_at)
                if opened_dt is not None:
                    age_seconds = (market_time.now_et() - opened_dt).total_seconds()
                    if age_seconds < cooldown_secs:
                        logger.debug(
                            f"COOLDOWN {key}: {age_seconds:.0f}s old "
                            f"(cooldown={cooldown_secs}s) — skipping stop/exit checks"
                        )
                        continue

                if not quote_ok:
                    continue

                # --- Day 4 (BUG-40): sell-failure backoff ---
                # A sell that failed to PLACE while the broker still (or
                # possibly still) holds the contract pauses price-inferred
                # exits for this position. Without this, a confirmed trail on
                # an unsellable position re-fired every ~11s tick — two
                # placement attempts and a 🚨 webhook per cycle, forever
                # (AAL, 2026-08-03: ~10 pings in 100 seconds until a restart).
                # Instructions are untouched: they sit above the quote_ok
                # gate, and a caller exit that fails runs the same broker
                # verification that set this backoff in the first place.
                _backoff_until = float(
                    getattr(position, "exit_backoff_until", 0.0) or 0.0
                )
                if _backoff_until and time.time() < _backoff_until:
                    logger.debug(
                        f"{key} | sell-failure backoff active for another "
                        f"{_backoff_until - time.time():.0f}s — price-inferred "
                        f"exit checks held"
                    )
                    continue

                if self._check_stop_loss(position):
                    self._spawn_exit_worker(key, position, "stop_loss", urgent=True)
                    continue

                # --- Check trailing stop ---
                if self._check_trailing_stop(position):
                    self._spawn_exit_worker(key, position, "trailing_stop", urgent=True)
                    continue

                # --- Day 4: caller-stated runner profit floor ---
                if self._check_profit_floor(position):
                    self._spawn_exit_worker(key, position, "profit_floor", urgent=True)
                    continue

                # --- Check profit tiers ---
                self._check_profit_tiers(position)

                # Session 15 (BUG-36): report position.current_price, not the
                # local `current_price` from the top of the pass. A
                # confirmation re-quote can supersede the tick, and pairing a
                # stale price with a fresh P&L in positions.log would corrupt
                # the record both 2026-07-28 and 2026-07-29 were reconstructed
                # from.
                _floor_p = float(getattr(position, "profit_floor_price", 0.0) or 0.0)
                logger.debug(
                    f"{key} | Price: ${position.current_price:.2f} | "
                    f"P&L: {position.pnl_pct:.1f}% | "
                    f"HWM: ${position.high_water_mark:.2f} | "
                    f"Contracts left: {position.contracts_remaining}"
                    + (
                        f" | Floor: ${_floor_p:.2f} "
                        f"({'engaged' if getattr(position, 'profit_floor_cleared', False) else 'pending'})"
                        if _floor_p > 0 else ""
                    )
                )

                # === LOG POSITION STATE ===
                log_position_check(
                    ticker=position.ticker,
                    direction=position.direction,
                    strike=position.strike,
                    entry_price=position.entry_price,
                    current_price=position.current_price,
                    pnl_pct=position.pnl_pct,
                    hwm=position.high_water_mark,
                    trailing_active=position.trailing_stop_active,
                    trailing_price=position.trailing_stop_price,
                    contracts_remaining=position.contracts_remaining,
                )

            except Exception as e:
                logger.error(f"Error checking position {key}: {e}")

        # Clean up closed positions
        # Session 9 (MEDIUM-7): pop(key, None) — another thread may already
        # have removed the key between the snapshot and this cleanup.
        with self._lock:
            for key in keys_to_remove:
                self.positions.pop(key, None)
        if keys_to_remove:
            self.save_position_state()  # Session 10f: prune closed keys

    def handle_caller_exit(self, ticker: str, exit_info: dict):
        """
        Handle an exit/trim/management signal from the caller.
        
        PROPORTIONAL TRIMMING:
        If the caller bought 15 contracts and trims 5, that's 33%.
        If we have 3 contracts, we trim 1 (33% rounded, min 1).
        
        RUNNER POLICY (small account):
        When caller leaves runners, we exit fully UNLESS conviction >= threshold.
        On a $1k account we need capital freed up for the next play.
        
        exit_info contains:
          type: "exit" | "trim" | "stop_update" | "management"
          trim_contracts: int (how many the caller trimmed)
          trim_pct: float (percentage trim, if specified)
          stop_level: float (new stop level)
          notes: str
          source_channel: str (channel that sent the exit — only close positions from that channel)
        """
        # Filter by source channel if provided.
        # This prevents a "cut OXY" from caller_a-challenge-challenge from closing an OXY
        # position that was opened from caller_a-alerts (different account, different
        # trade). Without this filter, any exit signal for a ticker would close
        # ALL positions in that ticker regardless of which channel opened them.
        source_channel = exit_info.get("source_channel", "")
        with self._lock:
            # Money-path review 2026-08-04 (F2): bot_managed=False positions
            # are the USER'S — the restore's own docstring promises "no
            # auto-exits, the user is managing it themselves", and the
            # monitor, 0DTE sweep and expiry sweep all honour that. This was
            # the one door that didn't: the channel fallback matched manual
            # positions on ticker alone, so a caller's "Cut OXY" could sell a
            # hand-managed OXY contract of a different strike/expiry.
            manual_held = [
                p for p in self.positions.values()
                if p.ticker == ticker and not p.bot_managed
            ]
            matching_positions = [
                (k, p) for k, p in self.positions.items()
                if p.ticker == ticker
                and p.bot_managed
                and (not source_channel or p.source == source_channel)
            ]
            _only_key = exit_info.get("only_key")
            if _only_key:
                # Round 2 (R2-2): a deferred-trim replay is scoped to the one
                # position that deferred it.
                matching_positions = [
                    (k, p) for k, p in matching_positions if k == _only_key
                ]
            if source_channel and not matching_positions and not _only_key:
                # No match on channel — fall back to all BOT positions for
                # this ticker so exits still work for positions opened before
                # this fix was deployed.
                fallback = [
                    (k, p) for k, p in self.positions.items()
                    if p.ticker == ticker and p.bot_managed
                ]
                if fallback:
                    logger.warning(
                        f"handle_caller_exit: no {ticker} positions from channel "
                        f"'{source_channel}' — falling back to all {len(fallback)} "
                        f"{ticker} position(s) (may include other channels)"
                    )
                    matching_positions = fallback

        if not matching_positions:
            # Session 10f: this used to be a bare return. Four of these fired
            # during the 07-23/24 run and none of them reached Discord — the
            # only record was a log line nobody was reading. A dropped exit
            # means the book has diverged from the caller: either we never took
            # the entry (the backfill discarded it), or we hold it under a key
            # this lookup can't see. Both are worth knowing about the same day,
            # not at the end of a paper week.
            #
            # Session 14: two corrections to what this branch SAYS, both from
            # 2026-07-29. First, it announced "Caller exited" for every signal
            # type — a 💯 P&L flex (correctly parsed as `management`) produced
            # "Caller exited XSP", and the operator reasonably read that as the flex
            # having closed the position. Say what actually arrived. Second,
            # "the entry was missed" was the only explanation offered, and it
            # was the wrong one: WE closed XSP fifteen minutes earlier via
            # trailing stop. When the ticker is in _recently_closed, that is
            # the explanation. (Also the shape the go-live gate produces: our
            # trail beating the caller's exit lands exactly here.)
            sig = exit_info.get("type", "exit")
            held = sorted({p.ticker for _k, p in self._positions_snapshot()})
            recent = self._recently_closed.get(ticker)
            logger.warning(
                f"handle_caller_exit: no open positions found for {ticker} "
                f"(signal_type='{sig}', source_channel='{source_channel}'). "
                f"Signal dropped. Currently holding: {held or 'nothing'}"
                + (
                    f" | we closed {ticker} at "
                    f"{recent['at']:%H:%M ET} ({recent['reason']}, "
                    f"{recent['pnl_pct']:+.1f}%)" if recent else ""
                )
            )
            if self.notifier:
                if recent:
                    why = (
                        f"We already closed ours at {recent['at']:%H:%M ET} "
                        f"— {recent['reason']}, {recent['pnl_pct']:+.1f}%."
                    )
                elif manual_held:
                    why = (
                        f"We hold {ticker} MANUALLY (hand-managed, not opened "
                        f"by the bot) — not touching it; close it yourself if "
                        f"you're following this exit."
                    )
                else:
                    why = f"(If we should have been in {ticker}, the entry was missed.)"
                what = (
                    f"Caller exited **{ticker}**"
                    if sig in ("exit", "full_exit", "trim")
                    else f"Caller {sig} note for **{ticker}**"
                )
                self.notifier.notify_status(
                    f"⚠️ {what} but we hold no matching position — "
                    f"nothing to do. "
                    f"Open: {', '.join(held) if held else 'none'}. {why}"
                )
            return

        keys_to_remove = []

        # Day 4 review round 1: the promotion/demotion/trim-override branches
        # below rebind `exit_info` (a copy) and `signal_type`. Without a reset
        # per iteration, position 2 of a multi-match starts from position 1's
        # REWRITTEN type — evaluated under a type that is not the signal's.
        # Today the branches happen to re-converge; one reorder and it's
        # first-position-held / second-position-sold on a single message.
        _orig_exit_info = exit_info

        for key, position in matching_positions:
            exit_info = _orig_exit_info
            if not position.management_rules.get("follow_caller_exits", True):
                logger.info(f"Ignoring caller exit for {key} (follow_caller_exits=False)")
                continue

            signal_type = exit_info.get("type", "exit")
            notes = exit_info.get("notes", "")
            # Caller's stated exit price (e.g. "Out at 0.50").
            # Used as the sell limit so we don't undershoot by pricing off bid.
            caller_exit_price = exit_info.get("exit_price")
            # 2026-08-04 (QCOM exit follow-up): exit_price may now arrive via
            # the parser's current_price fallback, which is coerce-only (no
            # range check) — so an UNDERLYING price misread as the contract
            # price ("QCOM 172, out") would rest a sell limit 450× above the
            # market for 45s before the urgent attempt rescued it. A real
            # premium exit is bounded by the trade itself: 12× entry admits
            # every documented runner (+160% is 2.6×; a +1000% 0DTE is 11×)
            # while an underlying-vs-premium confusion is hundreds of ×. The
            # $500 cap mirrors the entry_price validation range. A dropped
            # limit is never a dropped EXIT — pricing falls back to the
            # ladder/spread-aware flow.
            try:
                caller_exit_price = (
                    float(caller_exit_price) if caller_exit_price else None
                )
            except (TypeError, ValueError):
                caller_exit_price = None
            if caller_exit_price is not None:
                _entry = float(position.entry_price or 0)
                if caller_exit_price <= 0 or caller_exit_price > 500 or (
                    _entry > 0 and caller_exit_price > _entry * 12
                ):
                    logger.warning(
                        f"Caller exit price ${caller_exit_price:g} for {key} "
                        f"is implausible as a premium (entry "
                        f"${_entry:g}) — ignoring it as a limit; the exit "
                        f"still fires with market-based pricing"
                    )
                    caller_exit_price = None

            # ── Day 4 (the operator, 2026-08-03): TP posts execute ───────────────────
            # "Lets TP AAL 30%" parsed as `management` ("phrased as a
            # suggestion") and the bot held while the caller took profit —
            # the operator sold by hand at ~+60%. In caller_a's vocabulary "TP <ticker>
            # <N>%" is an instruction, and the N% is the GAIN level, not a
            # fraction of the position (cf. his "1.15 30% S all out" = out at
            # 1.15, +30%). A management-typed signal whose text says TP is
            # therefore promoted to a full exit. Scanned against the CALLER'S
            # OWN raw text only — never the parser's notes, which routinely
            # paraphrase with the words "take profit" while describing a hold
            # ("not taking profit yet" must not sell). The "trimmed N" / "N/M"
            # override below still demotes to a trim when the message states a
            # partial on its face.
            if signal_type == "management":
                _tp_raw = str(exit_info.get("raw_message", "") or "")
                if tp_directive(_tp_raw):
                    logger.warning(
                        f"TP POST for {key}: management-typed signal contains "
                        f"take-profit wording — executing as a full exit "
                        f"(policy: the operator 2026-08-03). "
                        f"(message: {' '.join(_tp_raw.split())[:90]!r})"
                    )
                    signal_type = "exit"
                    exit_info = dict(exit_info)
                    exit_info["type"] = "exit"
                elif (TP_EXEC_RE.search(_tp_raw)
                      and "runner" not in _tp_raw.lower()):
                    # Review round 1: TP mentioned but negated / target-talk.
                    # Holding is the safe read, but the operator asked for TP posts to
                    # act — so a refusal must reach him, not just the log, in
                    # case the heuristic read it wrong. (Runner posts are
                    # excluded: the runner branch sends its own 🏃 notice.)
                    logger.info(
                        f"TP WORDING for {key} read as hold-shaped (runner/"
                        f"negation/stop-talk) — not promoted. "
                        f"(message: {' '.join(_tp_raw.split())[:90]!r})"
                    )
                    if self.notifier:
                        self.notifier.notify_status(
                            f"🤔 **{position.ticker}**: TP mentioned but the "
                            f"message reads as hold/target talk — NOT selling. "
                            f"(\"{' '.join(_tp_raw.split())[:70]}\") "
                            f"Close manually if that's wrong."
                        )

            # ── Entry cooldown, exits only ───────────────────────────────────
            # A fast caller (e.g. caller_a) can post "out" within seconds of
            # entry; with auto_trade=true the buy may have JUST filled.
            #
            # BUG-39 (2026-08-03): this check used to sit ABOVE the
            # signal-type read and so applied to EVERY signal routed here. A
            # pure status-update `management` signal (caller_a's brokerage screenshot,
            # 14s after the MRVL entry, parser notes literally "not an exit or
            # trim") was "suppressed" into an armed -15% cooldown trail — a
            # HARSHER outcome than the same signal outside the window, which
            # arms nothing. It sold MRVL at +8.7% while the caller held to
            # +60%. Management signals now dispatch regardless of age; only
            # real exit intents are cooldown-relevant at all.
            #
            # With trails removed (enable_trailing_stop: false, the operator
            # 2026-08-03) the old arm-a-trail fallback would leave the
            # position with no exit path and no instruction — so the exit is
            # honoured instead: if the caller round-tripped in seconds, the
            # mirror round-trips too. The live config also sets
            # entry_stop_cooldown_seconds: 0, which disables this block
            # entirely; the branch below keeps older configs coherent.
            if signal_type in ("exit", "full_exit", "trim"):
                cooldown_secs = self.config.get("entry_stop_cooldown_seconds", 120)
                opened_dt = self._parse_opened_at(position.opened_at)
                if opened_dt is not None:
                    age_seconds = (market_time.now_et() - opened_dt).total_seconds()
                    if age_seconds < cooldown_secs:
                        if not self.config.get("enable_trailing_stop", True):
                            logger.warning(
                                f"COOLDOWN WAIVED for {key}: caller "
                                f"{signal_type} at {age_seconds:.0f}s old and "
                                f"trails are disabled — honouring the exit "
                                f"(mirror the caller)."
                            )
                        else:
                            logger.warning(
                                f"COOLDOWN: Suppressing caller exit for {key} — "
                                f"position is only {age_seconds:.0f}s old "
                                f"(cooldown={cooldown_secs}s). "
                                f"Caller exited very quickly — will apply trailing stop instead."
                            )
                            # Session 9 (M14): ARM the trailing stop directly instead of
                            # setting activation_pct=0 — activation requires pnl >= the
                            # threshold, so a LOSING position would never arm and would
                            # ride unprotected. Arming directly protects both cases.
                            trail_distance = 15  # tight 15% cooldown trail
                            position.management_rules["strategy"] = "trailing_stop_only"
                            position.management_rules["trailing_distance_pct"] = trail_distance
                            position.trailing_stop_active = True
                            position.trailing_stop_price = position.high_water_mark * (
                                1 - trail_distance / 100
                            )
                            logger.info(
                                f"Cooldown trailing stop armed for {key}: "
                                f"trail ${position.trailing_stop_price:.2f} "
                                f"(HWM ${position.high_water_mark:.2f}, -{trail_distance}%)"
                            )
                            continue

            # ── Session 10f: intent is not a fill ────────────────────────────
            # Checked BEFORE the market-hours guard and the trim override, so
            # a plan never queues an exit for the open either.
            #
            # Day 4 (the operator, 2026-08-03): ONE exemption — TP. "going to TP DAL
            # today" used to demote to management; TP posts now execute (see
            # TP_EXEC_RE). The demotion is keyed on the VERB the intent regex
            # matched, so "might sell later" still holds while "will TP" acts.
            if signal_type in ("full_exit", "exit", "trim"):
                _raw_own = str(exit_info.get("raw_message", ""))
                _raw = _raw_own or notes
                _m_intent = INTENT_RE.search(_raw)
                # The TP exemption requires the CALLER'S OWN text. When only
                # notes exist, the match may be the parser paraphrasing
                # ("caller says he is going to TP later") — executing a sell
                # on a paraphrase is the notes-hazard the promotion above
                # already guards against, so the fallback demotes as before.
                # Review round 1: and it must be a DIRECTIVE — "Not going to
                # TP AAL yet" matches INTENT_RE on "going to … TP", and the
                # naive verb-only exemption executed it. tp_directive's
                # negation window sees the leading "not" and demotes.
                if (
                    _m_intent
                    and _raw_own
                    and TP_EXEC_RE.search(_m_intent.group(2) or "")
                    and tp_directive(_raw_own)
                ):
                    logger.warning(
                        f"TP INTENT for {key}: phrased as a plan, but TP posts "
                        f"execute (policy: the operator 2026-08-03) — proceeding with "
                        f"{signal_type}. "
                        f"(message: {' '.join(_raw.split())[:90]!r})"
                    )
                elif _m_intent:
                    logger.warning(
                        f"INTENT, NOT A FILL: {key} received "
                        f"signal_type='{signal_type}' but the message describes "
                        f"a plan, not a completed action — treating as "
                        f"management. (message: {' '.join(_raw.split())[:90]!r})"
                    )
                    if self.notifier:
                        self.notifier.notify_status(
                            f"💭 Caller mentioned exiting **{position.ticker}** "
                            f"as a *plan*, not a fill — holding. "
                            f"(\"{' '.join(_raw.split())[:70]}\")"
                        )
                    signal_type = "management"
                    exit_info = dict(exit_info)
                    exit_info["type"] = "management"

            # Money-path review 2026-08-04 (C1/F1): the trimmed-N override runs
            # BEFORE the market-hours guard, so an after-hours "trimmed 1"
            # message defers with the CORRECTED type — the old order deferred
            # it as caller_exit and sold the whole position at the open.
            # ── DEFENSIVE: catch parser mis-classification of "trimmed N" ────
            # "1.6 | 28% APP trimmed 1 @everyone" should be signal_type="trim"
            # but Haiku/Sonnet can return "exit" when seeing the word "trimmed"
            # in an unfamiliar format.  If the raw message (in notes or exit_info)
            # contains "trimmed <number>", force it back to a trim so we never
            # sell an entire position when the caller only trimmed part of it.
            if signal_type in ("full_exit", "exit"):
                # Session 10f: the 2026-07-26 eval surfaced a second shape this
                # missed. "Sold 4/6 ONDS at -5% / Taking some risks off / Only
                # have 2 ONDS" parses as `exit` (case
                # bt-1480964879597699225) — he sold four of six and says so, but
                # nothing here matched, so the bot would have closed the whole
                # position instead of trimming. `N/M` is unambiguous: it states
                # a partial on its face. Bare "sold N" is deliberately NOT
                # matched — "sold 2" of a 2-lot is a full exit.
                _haystacks = (notes, str(exit_info.get("raw_message", "")))
                trim_override_match = None
                for _pat in (
                    r'\btrimmed\s+(\d+)\b',
                    r'\b(?:sold|trimmed|selling|sell)\s+(\d+)\s*/\s*\d+\b',
                ):
                    for _hay in _haystacks:
                        trim_override_match = re.search(_pat, _hay, re.IGNORECASE)
                        if trim_override_match:
                            break
                    if trim_override_match:
                        break
                if trim_override_match:
                    inferred_count = int(trim_override_match.group(1))
                    logger.warning(
                        f"PARSER CORRECTION: '{key}' received signal_type='exit' "
                        f"but notes contain 'trimmed {inferred_count}' — "
                        f"overriding to trim (not full exit). "
                        f"This prevents selling all contracts when caller only trimmed {inferred_count}."
                    )
                    signal_type = "trim"
                    exit_info = dict(exit_info)  # shallow copy to avoid mutating original
                    exit_info["type"] = "trim"
                    exit_info["trim_contracts"] = inferred_count
            # ─────────────────────────────────────────────────────────────────

            # ── Session 10f: market-hours guard on ACTIONS ───────────────────
            # Entries have had one since Session 9 (main.py, "MARKET CLOSED —
            # skipping trade"). Exits never did. Options do not trade outside
            # 9:30-16:00 ET, so an exit arriving pre-market had two possible
            # outcomes and both were wrong: in paper it booked P&L straight off
            # `current_price`, which is the previous session's stale mark — a
            # fill that could not have happened — and in live it fired a sell
            # into a closed market.
            #
            # Live example, 2026-07-27: "So good, we are going to TP DAL today"
            # landed at 09:01 ET, 29 minutes before the bell, and parsed as an
            # exit. Had we held DAL we would have booked a fictional close on
            # Friday's mark, and his real "1.55 200% all out DAL" at 09:33
            # would have arrived to an empty book.
            #
            # So we defer instead. The monitor fires it once the session is
            # open AND past the opening-bell settle, so the deferred exit does
            # not sell into the same phantom quote the settle exists to ignore.
            # stop_update and management signals are NOT deferred — they only
            # change state, they place no order.
            if (
                signal_type in ("full_exit", "exit", "trim")
                and not market_time.is_market_hours()
            ):
                position.exit_at_open = True
                position.exit_at_open_reason = f"caller_{signal_type}"
                # Money-path review 2026-08-04 (C1/F1): a deferred TRIM must
                # carry its quantity, or the open-time firing degrades to a
                # FULL EXIT — "Trimmed 1/6" after hours used to liquidate the
                # whole position at the bell, silently framed as obedience.
                # Persisted via the sidecar with the flag itself.
                if signal_type == "trim":
                    try:
                        position.exit_at_open_trim_contracts = int(
                            exit_info.get("trim_contracts") or 0
                        )
                    except (TypeError, ValueError):
                        position.exit_at_open_trim_contracts = 0
                    # Round 2 (R2-6): keep the caller's own words — a no-count
                    # trim ("trimmed most here") replays through the keyword
                    # fallback, which needs the words, not our synthetic note.
                    position.exit_at_open_trim_notes = " ".join(
                        f"{exit_info.get('notes', '')} "
                        f"{exit_info.get('raw_message', '')}".split()
                    )[:200]
                    try:
                        position.exit_at_open_trim_pct = float(
                            exit_info.get("trim_pct") or 0.0
                        )
                    except (TypeError, ValueError):
                        position.exit_at_open_trim_pct = 0.0
                self.save_position_state()
                now_et = market_time.now_et()
                logger.warning(
                    f"MARKET CLOSED — deferring caller {signal_type} for {key} "
                    f"to the next open (received {now_et:%H:%M %a} ET)"
                )
                if self.notifier:
                    self.notifier.notify_status(
                        f"⏸️ Caller {signal_type} for **{position.ticker}** "
                        f"arrived at {now_et:%H:%M ET} with the market shut — "
                        f"queued for the next open rather than booked at a "
                        f"stale mark."
                    )
                continue


            # --- Full exit ---
            if signal_type in ("full_exit", "exit"):
                price_note = f" @ ${caller_exit_price:.2f}" if caller_exit_price else ""
                logger.info(f"Caller full exit for {key}{price_note}")
                exited = self._execute_full_exit(position, "caller_exit", limit_price=caller_exit_price)
                if exited:
                    keys_to_remove.append(key)
                # If not exited, position is PDT-held (sells next day)

            # --- Trim ---
            elif signal_type == "trim":
                caller_trim_count = exit_info.get("trim_contracts")
                explicit_trim_pct = exit_info.get("trim_pct")

                # Session 9 (LOW-4): decrement the caller's tracked count
                # BEFORE calculating, regardless of whether our trim fills —
                # it tracks the CALLER's position, not ours. The calculator
                # reconstructs the pre-trim denominator as remaining + trim.
                if caller_trim_count and position.caller_contracts_remaining > 0:
                    position.caller_contracts_remaining = max(
                        0, position.caller_contracts_remaining - caller_trim_count
                    )
                    self.save_position_state()  # Session 10f

                our_trim = self._calculate_proportional_trim(
                    position=position,
                    caller_trim_count=caller_trim_count,
                    explicit_trim_pct=explicit_trim_pct,
                    notes=notes,
                    raw_message=exit_info.get("raw_message", "") or "",
                )
                self.save_position_state()  # Session 10f: persist trim_carry

                if our_trim > 0:
                    trimmed = self._execute_trim(
                        position, our_trim, "caller_proportional_trim"
                    )
                    # A trim against our last contract escalates to a full
                    # exit inside _execute_trim (H4a) — remove if fully closed.
                    if trimmed and position.contracts_remaining <= 0:
                        keys_to_remove.append(key)
                else:
                    # Session 10f: this is now a real outcome rather than an
                    # impossibility (the old max(1, ...) floor could never
                    # return 0), so it must reach the user — a caller trim that
                    # silently does nothing is exactly the failure mode we keep
                    # finding elsewhere.
                    logger.info(
                        f"Proportional trim for {key} rounded to 0 — holding. "
                        f"(Caller trimmed {caller_trim_count}, we have "
                        f"{position.contracts_remaining}, "
                        f"carry {position.trim_carry:.2f})"
                    )
                    if self.notifier:
                        self.notifier.notify_status(
                            f"✂️ Caller trimmed **{position.ticker}** but our "
                            f"share rounds to 0 of {position.contracts_remaining}"
                            f" — holding. Carrying {position.trim_carry:.2f} "
                            f"toward the next trim."
                        )

            # --- Stop loss update ---
            elif signal_type == "stop_update":
                # Session 9 (MEDIUM-6): callers post stops as premium PRICE
                # levels ("stop at .50"). Writing 0.5 into stop_loss_pct made
                # a 0.5% stop that fired on the first spread tick. Interpret
                # small values as price levels and convert vs entry.
                new_stop = exit_info.get("stop_level")
                if new_stop is not None:
                    try:
                        new_stop = float(new_stop)
                    except (TypeError, ValueError):
                        logger.warning(
                            f"stop_update for {key}: unparseable stop_level "
                            f"{new_stop!r} — ignored"
                        )
                        continue
                    if new_stop < 5 and position.entry_price > 0:
                        # Premium price level → convert to a % below entry
                        pct = (1 - new_stop / position.entry_price) * 100
                        if pct <= 0:
                            # Money-path review 2026-08-04 (F4): a stop AT or
                            # ABOVE entry is the caller protecting PROFIT
                            # ("SL to 1.50" on a $1.00 entry = never give back
                            # below +50%). The old fallback armed a tight 5%
                            # LOSS stop — the caller said "floor at +50%" and
                            # the bot answered "sell at -5%", riding the whole
                            # gain down first. Route it through the Day-4
                            # profit-floor machinery instead: static, pending
                            # clearance, BUG-36 confirmed, sidecar-persisted.
                            with self._lock:
                                position.profit_floor_price = float(new_stop)
                                position.profit_floor_cleared = False
                                position._floor_clear_ticks = 0
                            self.save_position_state()
                            logger.info(
                                f"stop_update for {key}: level ${new_stop:.2f} "
                                f"is at/above entry "
                                f"${position.entry_price:.2f} — armed as a "
                                f"profit floor (pending clearance), not a "
                                f"loss stop"
                            )
                            if self.notifier:
                                self.notifier.notify_status(
                                    f"🧱 Caller moved the stop on "
                                    f"**{position.ticker}** to "
                                    f"${new_stop:.2f} (above entry) — armed "
                                    f"as a profit floor; engages once two "
                                    f"readings confirm price above it."
                                )
                            continue
                        pct = max(1.0, min(95.0, pct))
                        position.stop_loss_pct = pct
                        self.save_position_state()
                        logger.info(
                            f"Updated stop loss for {key}: interpreted "
                            f"{new_stop} as PRICE level → {pct:.1f}% stop "
                            f"(entry ${position.entry_price:.2f})"
                        )
                    else:
                        pct = min(95.0, new_stop)
                        position.stop_loss_pct = pct
                        self.save_position_state()
                        logger.info(
                            f"Updated stop loss for {key} to {pct:.1f}% "
                            f"(interpreted as percent"
                            f"{', clamped from ' + str(new_stop) if new_stop > 95 else ''})"
                        )

            # --- Management (includes runner signals) ---
            elif signal_type == "management":
                removed = self._handle_management_signal(key, position, exit_info)
                if removed:
                    keys_to_remove.append(key)

        with self._lock:
            for key in keys_to_remove:
                self.positions.pop(key, None)
        if keys_to_remove:
            self.save_position_state()  # Session 10f: prune closed keys

    def _handle_management_signal(
        self, key: str, position: Position, exit_info: dict
    ) -> bool:
        """
        Handle management signals including runner decisions.
        Returns True if the position was fully closed and should be removed.
        
        RUNNER POLICY (Session 10f — conviction no longer decides this):
        When the caller says "X runners with Y% profit SL", they have trimmed
        most of the position and are letting the rest ride behind a profit stop.

        This used to branch on `conviction_score >= 85`, exiting fully below
        that "to free up capital on a small account". In practice the branch was
        dead in one direction: challenge entries carry conviction 50, so it
        ALWAYS took the full exit — the bot sold out every time the caller said
        they were keeping a runner. That is the largest deviation from pure
        mirroring in the codebase, and it fired silently.

        Conviction is the right lever for TA-context sources, not for
        signal-following (the operator, 2026-07-26), so the gate is gone. Default is
        to do what the caller did: keep the position and mirror his STATED
        stop as a static profit floor (Day 4, 2026-08-03 — the old
        `runner_profit_lock_pct` percentage-of-gain lock is retired; the key
        is read nowhere). No stated level → hold with nothing armed; his exit
        post closes it.

        Set `runner_policy.follow_caller_runners: false` to restore the
        bank-it-and-free-the-capital behaviour. Config, not conviction.

        Example (caller_b AMZN): had 10, trimmed 6 at +62%, "4 runners with a
        50% profit SL" → we keep ours and lock in 30% of the gain.
        """
        notes = exit_info.get("notes", "")
        notes_lower = notes.lower()
        # Day 4 review round 1: scan the caller's OWN text as well as the
        # parser's notes — for detection AND for the stated level. The first
        # draft read only notes_lower, so a parser paraphrase that dropped the
        # number ("leaving runners with a profit stop") lost the caller's
        # stated stop entirely. Reading the level from either source is safe
        # (unlike TP promotion, a paraphrased NUMBER is still the caller's
        # number); missing it from raw is not.
        raw_lower = str(exit_info.get("raw_message", "") or "").lower()
        runner_text = f"{raw_lower}\n{notes_lower}"

        runner_policy = self.config.get("runner_policy", {})
        follow_caller_runners = runner_policy.get("follow_caller_runners", True)
        # Day 4 (2026-08-03): `runner_profit_lock_pct` retired — the floor now
        # mirrors the caller's STATED level instead of locking a percentage of
        # whatever our gain happened to be when his message arrived.

        is_runner_signal = "runner" in runner_text

        if is_runner_signal:
            logger.info(
                f"RUNNER SIGNAL for {key} | Current P&L: {position.pnl_pct:.1f}% | "
                f"policy: {'follow the caller' if follow_caller_runners else 'bank and free capital'}"
            )

            if follow_caller_runners:
                # Day 4 (the operator, 2026-08-03): mirror the caller's stated stop as
                # a STATIC profit floor, not a trail. "4 runners with a 50%
                # profit SL" means HIS stop sits at the +50% profit LEVEL —
                # entry × 1.5 — so that is exactly what we arm: a floor that
                # never ratchets and involves no high-water mark. The previous
                # code armed a trailing stop at N% OF THE CURRENT GAIN, which
                # is a different number and a different mechanism from what
                # the caller's words describe; it also survived the trail
                # removal only by accident of phrasing. Caller-INSTRUCTED
                # stops are exempt from `enable_trailing_stop` — that switch
                # removes price exits the bot invents, not ones the caller
                # states.
                #
                # No stated level → hold with nothing armed, and say so; the
                # caller's eventual exit post is what closes it.
                logger.info(f"RUNNER: keeping {key} (mirror the caller)")

                # Review round 1: level from the caller's OWN text first, the
                # parser's notes as fallback — a paraphrase that drops the
                # number must not lose the caller's stated stop.
                # Round 3 (F5): 0% ("breakeven runner stop") is a real
                # level — `or` would drop it as falsy.
                profit_sl_match = _parse_runner_level(raw_lower)
                if profit_sl_match is None:
                    profit_sl_match = _parse_runner_level(notes_lower)
                floor_price = 0.0
                level_pct = 0.0
                if profit_sl_match is not None and position.entry_price > 0:
                    level_pct = profit_sl_match
                    floor_price = position.entry_price * (1 + level_pct / 100.0)
                    # Session 15's arming rule: a trigger must never be armed
                    # already through the market. Round 1 killed the permanent
                    # REFUSAL (it discarded the caller's stated stop off a
                    # possibly-stale price); round 2 killed the fresh-quote
                    # shortcut that replaced it — ONE arming-time reading
                    # deciding "cleared" is exactly the single-reading trust
                    # BUG-36 exists to forbid (a phantom at arming would
                    # engage a floor above the real market, which then sells
                    # the runner on the next two honest readings). So the
                    # floor ALWAYS arms pending: the monitor engages it after
                    # two consecutive readings above the level — the same
                    # discipline firing uses — costing ~12s when the level is
                    # genuinely below market, and nothing when it is not.
                    _prev_floor = float(
                        getattr(position, "profit_floor_price", 0.0) or 0.0
                    )
                    _prev_cleared = bool(
                        getattr(position, "profit_floor_cleared", False)
                    )
                    if _prev_cleared and abs(_prev_floor - floor_price) < 0.005:
                        # Round 3 (F4): a repost/edit of the same runner note
                        # must not demote an ENGAGED floor to pending — with
                        # price at/below the level it would never re-clear
                        # and the caller's stop would be silently dead.
                        logger.info(
                            f"PROFIT FLOOR re-stated for {key} at the same "
                            f"level ${floor_price:.2f} — already engaged, "
                            f"unchanged"
                        )
                        if self.notifier:
                            self.notifier.notify_status(
                                f"🏃 Caller re-stated the runner stop on "
                                f"**{position.ticker}** (${floor_price:.2f}) "
                                f"— floor already engaged, unchanged."
                            )
                        return False  # Position kept
                    with self._lock:
                        position.profit_floor_price = floor_price
                        position.profit_floor_cleared = False
                        position._floor_clear_ticks = 0
                    self.save_position_state()
                    logger.info(
                        f"PROFIT FLOOR stored (pending clearance) for {key}: "
                        f"${floor_price:.2f} (caller's {level_pct:g}% profit "
                        f"SL, entry ${position.entry_price:.2f}) — engages "
                        f"after two confirmed readings above it"
                    )
                if self.notifier:
                    if floor_price:
                        self.notifier.notify_status(
                            f"🏃 Caller left runners on **{position.ticker}** "
                            f"— holding ours with his stated stop mirrored: "
                            f"profit floor ${floor_price:.2f} "
                            f"(+{level_pct:g}% level). It engages once two "
                            f"readings confirm price above it (guards against "
                            f"arming off a stale/phantom quote); his exit "
                            f"posts act immediately regardless."
                        )
                    else:
                        self.notifier.notify_status(
                            f"🏃 Caller left runners on **{position.ticker}** — "
                            f"holding ours (P&L {position.pnl_pct:+.1f}%). No "
                            f"stop level stated, so nothing is armed; his exit "
                            f"post closes it."
                        )
                return False  # Position kept

            else:
                # follow_caller_runners: false — bank it and free the capital.
                logger.info(
                    f"RUNNER EXIT (capital efficiency): closing {key} | "
                    f"locking in {position.pnl_pct:.1f}%"
                )
                if self.notifier:
                    self.notifier.notify_status(
                        f"🏦 Caller left runners on **{position.ticker}** — "
                        f"closing ours instead to free capital "
                        f"(P&L {position.pnl_pct:+.1f}%). "
                        f"`runner_policy.follow_caller_runners: true` to mirror "
                        f"the caller instead."
                    )
                exited = self._execute_full_exit(position, "runner_exit_capital_efficiency")
                return exited  # True if closed, False if PDT-held
        else:
            logger.info(f"Management signal for {key}: {notes}")
            return False

    def restore_daily_pnl_from_trade_log(self) -> float:
        """Rebuild today's realized P&L from trades.json. Returns the total.

        Session 10f. `daily_pnl` lived only in memory, in TWO places — here and
        on DecisionEngine — so every restart silently reset the 40% daily-loss
        circuit breaker mid-day, and docker-compose runs `restart:
        on-failure:10`. Down 35% on the day, restart, and the breaker is back
        to zero. That defeats C1, whose whole point was making the breaker able
        to fire at all.

        Derived from the ledger rather than snapshotted: trades.json already
        records `pnl_usd` and an ET timestamp on every CLOSE and TRIM, and
        recomputing is idempotent — repeated restarts in one day converge on
        the same number instead of accumulating. Never raises; on any problem
        it leaves the accumulators at zero, which is the old behaviour.
        """
        today = market_time.trading_date()
        if not self.trade_log_path.exists():
            return 0.0
        total = 0.0
        counted = 0
        current_mode = "paper" if self.paper_trade else "live"
        try:
            for e in json.loads(self.trade_log_path.read_text()):
                if e.get("action") not in ("CLOSE", "TRIM"):
                    continue
                # Session 12 (GO_LIVE B4): never seed the circuit breaker with
                # the other mode's P&L. The go-live gate ("wait for a caller
                # exit to land") guarantees a fresh paper CLOSE in the ledger
                # on flip day — a good paper morning would hand the live
                # account 3x its intended rope; a bad one would block live
                # entries for losses that never happened.
                if not ledger_row_matches_mode(e, current_mode):
                    continue
                try:
                    when = datetime.fromisoformat(str(e.get("timestamp") or ""))
                except (TypeError, ValueError):
                    continue
                if when.tzinfo is None:
                    # Legacy pre-Session-9 rows were written host-local. Read
                    # them as ET rather than letting astimezone() guess from
                    # the container clock.
                    when = when.replace(tzinfo=market_time.ET)
                if market_time.trading_date(when) != today:
                    continue
                total += float(e.get("pnl_usd") or 0)
                counted += 1
        except Exception as ex:
            logger.warning(
                f"Could not rebuild daily P&L from the trade log: {ex} — "
                f"circuit breaker starts the session at $0.00"
            )
            return 0.0

        today_str = today.isoformat()
        self.daily_pnl = total
        self._last_pnl_reset_date = today_str
        if self.decision_engine is not None:
            # Set directly rather than via record_realized_pnl(): this is a
            # restore, not a new realization, and the accumulator must land on
            # the ledger total exactly however many times we restart today.
            self.decision_engine.daily_pnl = total
            self.decision_engine._last_pnl_reset_date = today_str

        if counted:
            logger.info(
                f"Restored today's realized P&L from the trade log: "
                f"${total:+.2f} across {counted} exit/trim event(s) — "
                f"circuit breaker resumes from there"
            )
        else:
            logger.info("No realized P&L yet today — daily accumulator at $0.00")
        return total

    def _whole_contracts_with_carry(
        self, position: Position, exact: float, our_remaining: int, why: str
    ) -> int:
        """Turn a fractional trim target into whole contracts, carrying the rest.

        Session 10f. The old `max(1, ...)` floor systematically over-mirrored
        small positions: a caller trimming 1 of 10 (10%) against our 3 rounded
        up to 1 = 33%, every single time, with no memory between trims. Keeping
        the signed remainder on the position means the error cancels across
        trims instead of compounding, so the long-run ratio tracks the caller.
        Mirror fidelity improves as the book grows; the carry is what makes it
        hold together while positions are still small.

        Rounding stays half-up (Session 9's call — under-mirroring is the worse
        direction), so a single 50%-of-5 trim still takes 3, not 2. It just now
        carries −0.5 into the next one.

        The runner cap is applied afterwards and its excess is deliberately NOT
        carried — that cap is a policy floor, not a rounding artefact, so
        carrying it would let the debt grow without bound across repeated
        large trims.
        """
        total = exact + position.trim_carry
        # Round HALF-UP, not floor: Session 9 established that
        # under-mirroring is the worse direction (round(2.5) == 2
        # gives back less than the caller took). The carry is what's
        # new — it can go negative, meaning we already trimmed more
        # than our share and should take less next time, so the bias
        # cancels instead of compounding.
        whole = max(0, int(total + 0.5))
        carry = total - whole
        capped = min(whole, max(0, our_remaining - 1))
        if capped != whole:
            logger.info(
                f"Runner cap: {why} wanted {whole} of {our_remaining} — trimming "
                f"{capped} to keep a runner (excess not carried)"
            )
            carry = 0.0
        position.trim_carry = round(carry, 6)
        logger.info(
            f"{why}: exact {exact:.2f} (+carry) → trim {capped}/{our_remaining}, "
            f"carry now {position.trim_carry:.2f}"
        )
        return capped

    def _calculate_proportional_trim(
        self,
        position: Position,
        caller_trim_count: Optional[int],
        explicit_trim_pct: Optional[float],
        notes: str,
        raw_message: str = "",
    ) -> int:
        """
        Calculate how many of OUR contracts to trim based on the caller's trim.
        
        Priority:
        1. If caller_trim_count AND caller_contracts are known → exact proportion
        2. If explicit_trim_pct given (e.g., "trimming half") → use that %
        3. If caller_trim_count only (no original count known) → estimate ratio
        4. Qualitative wording ("most" → 80%, "half", "third")
        5. Fallback: half (Session 10f — was 1 contract)
        
        Always trims at least 1 if we should trim at all,
        and never trims the entire position through caller trims
        (leave at least 1 runner).
        """
        our_remaining = position.contracts_remaining

        if our_remaining <= 1:
            # Only 1 contract left — caller trim means full exit
            # (they're reducing, we can't reduce further without closing)
            logger.info(
                f"Only 1 contract remaining for {position.ticker} — "
                f"caller trim triggers full exit"
            )
            return our_remaining

        # Method 1: Exact proportional (best case)
        # Session 9 (LOW-4): the caller's tracked remaining count is
        # decremented BEFORE this calculation runs, so the caller's count
        # before THIS trim is (remaining + trim). Prefer that over the
        # original caller_contracts so sequential trims mirror the caller's
        # actual remaining ratio (5 of 15, then 5 of 10 = 50%, not 33%).
        # Round half-up, not banker's round (round(2.5) == 2 under-mirrors).
        if caller_trim_count and position.caller_contracts > 0:
            if position.caller_contracts_remaining > 0:
                caller_total = position.caller_contracts_remaining + caller_trim_count
            else:
                caller_total = position.caller_contracts
            trim_ratio = caller_trim_count / caller_total
            return self._whole_contracts_with_carry(
                position,
                our_remaining * trim_ratio,
                our_remaining,
                f"Proportional trim (caller {caller_trim_count}/{caller_total} "
                f"= {trim_ratio:.0%})",
            )

        # Method 2: Explicit percentage
        if explicit_trim_pct:
            return self._whole_contracts_with_carry(
                position,
                our_remaining * explicit_trim_pct / 100,
                our_remaining,
                f"Percentage trim ({explicit_trim_pct}%)",
            )

        # Method 3: Caller trim count but unknown original
        # Use caller's remaining vs trim to estimate
        if caller_trim_count and position.caller_contracts_remaining > 0:
            # Estimate: caller had (remaining + trimmed) contracts
            estimated_total = position.caller_contracts_remaining + caller_trim_count
            trim_ratio = caller_trim_count / estimated_total
            return self._whole_contracts_with_carry(
                position,
                our_remaining * trim_ratio,
                our_remaining,
                f"Estimated proportional trim (caller {caller_trim_count} of "
                f"est. {estimated_total})",
            )

        # Method 4: Check notes for ratio hints
        # Session 10f: scan the caller's own words too, not just the parser's
        # notes — "trimmed most" is his phrasing and may not survive into notes.
        notes_lower = f"{notes} {raw_message}".lower()

        # "most" is caller_a's own word (twice in 11 historical trims) and there is
        # no count to work from. 80% as a blunt failsafe — deliberately NOT
        # conviction-weighted: for a pure mirror the caller's stated intent is
        # the whole signal. \bmost\b so "almost" doesn't match.
        if re.search(r'\bmost\b', notes_lower):
            our_trim = int(our_remaining * _MOST_TRIM_FRACTION + 0.5)
            our_trim = min(our_trim, our_remaining - 1)  # keep a runner
            our_trim = max(1, our_trim)
            logger.info(
                f"'Most' trim from caller wording → we trim "
                f"{our_trim}/{our_remaining} "
                f"({our_trim / our_remaining:.0%}, target "
                f"{_MOST_TRIM_FRACTION:.0%})"
            )
            return our_trim

        if "half" in notes_lower or "50%" in notes_lower:
            our_trim = max(1, our_remaining // 2)
            our_trim = min(our_trim, our_remaining - 1)
            logger.info(f"Half trim from notes → we trim {our_trim}/{our_remaining}")
            return our_trim
        elif "third" in notes_lower or "33%" in notes_lower:
            our_trim = max(1, our_remaining // 3)
            our_trim = min(our_trim, our_remaining - 1)
            logger.info(f"Third trim from notes → we trim {our_trim}/{our_remaining}")
            return our_trim

        # Fallback: he trimmed, but gave no size.
        # Session 10f: this used to take exactly 1 contract, which only makes
        # sense when you hold 2 — at 5 it mirrors a 20% trim and at 20 a 5% one.
        # caller_a's own median when he DOES state a size is half ("Trimmed 1/2 and
        # let 1 runner", "trimmed 1 & leaving 1 runner" of 2), so half is the
        # closer default. Deliberately the same floor division as the explicit
        # "half" branch above, so a stated half and a bare trim agree. No carry:
        # this is a guess at an unknown size, not a rounded known ratio.
        our_trim = max(1, our_remaining // 2)
        our_trim = min(our_trim, our_remaining - 1)
        logger.info(
            f"Trim with no stated size — defaulting to half: "
            f"{our_trim}/{our_remaining}"
        )
        return our_trim

    def _check_stop_loss(self, position: Position) -> bool:
        """Check if the hard stop loss has been hit.
        stop_loss_pct == 0 means disabled (challenge mode relies on caller exits).

        Session 15 (BUG-36): confirmed over consecutive readings like the
        trailing stop. A phantom tick cuts both ways — the 2026-07-27 opening
        spike closed TE at -8.3% on a print that never traded — and the cost
        of the guard here is ~6 seconds on a stop that is already a blunt
        instrument.
        """
        if not position.stop_loss_pct:
            return False

        def breached(price: float) -> bool:
            """The branch condition below, as a function of price. Recomputed
            the same way rather than inverted into a threshold — see
            _breach_confirmed on why the algebra is not enough."""
            if not position.entry_price:
                return False
            pnl = (price - position.entry_price) / position.entry_price * 100
            return pnl <= -position.stop_loss_pct

        if breached(position.current_price):
            return self._breach_confirmed(
                position,
                "stop",
                breached,
                (
                    f"STOP LOSS: {position.ticker} | "
                    f"Price: ${position.current_price:.2f} | "
                    f"P&L: {position.pnl_pct:.1f}% | SL: -{position.stop_loss_pct}%"
                ),
            )

        self._clear_breach(position, "stop")
        return False

    def _check_profit_floor(self, position: Position) -> bool:
        """Static caller-stated runner stop (Day 4, the operator 2026-08-03).

        Fires when price falls to or through the profit LEVEL the caller said
        his runner stop sits at ("4 runners with a 50% profit SL" →
        entry × 1.5). Static by design: it never ratchets and reads nothing
        from the high-water mark — the caller stated a level, not a trail.

        The trigger is still an inference FROM PRICE at fire time, so the
        BUG-36 confirmation guard applies exactly as it does to stops and
        trails. NOT gated by `enable_trailing_stop`: that switch removes
        price exits the bot invents, and this level is the caller's own
        instruction, mirrored.
        """
        floor = float(getattr(position, "profit_floor_price", 0.0) or 0.0)
        if floor <= 0:
            return False

        # Review round 1: a floor stored at/above the market must not fire on
        # arrival — it ENGAGES only after the market proves itself above the
        # level, with the same two-consecutive-readings discipline firing
        # uses (one reading is how a phantom high would engage a floor that
        # then sells the next dip). A floor that never clears never fires;
        # the caller's exit post remains the backstop.
        if not getattr(position, "profit_floor_cleared", False):
            ticks = int(getattr(position, "_floor_clear_ticks", 0) or 0)
            # Round 2 (M1): clearance is counted in CONSECUTIVE monitor
            # passes, exactly like breach counting (_run_is_broken). Without
            # this, two lone readings bridging a quote outage — the two-
            # phantoms shape — engage a floor the market never really cleared.
            last_pass = getattr(position, "_floor_clear_pass", None)
            if last_pass is not None and (self._pass_seq - last_pass) != 1:
                ticks = 0
            if position.current_price > floor:
                ticks += 1
                position._floor_clear_pass = self._pass_seq
                if ticks >= 2:
                    position.profit_floor_cleared = True
                    position._floor_clear_ticks = 0
                    self.save_position_state()
                    logger.info(
                        f"PROFIT FLOOR ENGAGED for {position.ticker}: price "
                        f"${position.current_price:.2f} confirmed above the "
                        f"caller's level ${floor:.2f}"
                    )
                else:
                    position._floor_clear_ticks = ticks
            else:
                position._floor_clear_ticks = 0
            return False

        def breached(price):
            # Same expression re-run by the confirmation re-quote — never an
            # inverted threshold (the BUG-36 float trap).
            return price <= floor

        if breached(position.current_price):
            return self._breach_confirmed(
                position,
                "floor",
                breached,
                (
                    f"PROFIT FLOOR: {position.ticker} | "
                    f"Price: ${position.current_price:.2f} | "
                    f"Floor: ${floor:.2f} | P&L: {position.pnl_pct:.1f}%"
                ),
            )

        self._clear_breach(position, "floor")
        return False

    def _check_trailing_stop(self, position: Position) -> bool:
        """Check if trailing stop should activate or has been hit."""
        # Day 4 (the operator, 2026-08-03): trails removed — the caller's posted exits
        # are the exit strategy. `enable_trailing_stop` had been a dead config
        # key since Session 9 (written, never read); it is now the master
        # switch, and it gates FIRING as well as arming, deliberately:
        # restored positions carry their per-position rules from the ledger
        # OPEN row (trailing_activation_pct 60) and can restore with a trail
        # already armed, so a config sentinel on the activation threshold
        # reaches neither. This single gate does.
        if not self.config.get("enable_trailing_stop", True):
            return False
        rules = position.management_rules
        activation_pct = rules.get(
            "trailing_activation_pct",
            self.config["trailing_stop_activation_pct"],
        )
        trail_distance = rules.get(
            "trailing_distance_pct",
            self.config["trailing_stop_distance_pct"],
        )

        # Check if we should activate trailing stop
        if not position.trailing_stop_active:
            # Session 15 (BUG-36), and read this before "improving" it.
            #
            # Arming stays on pnl_pct — a statement about NOW — while the
            # trigger comes from the high-water mark, a statement about the
            # past. Review round 2 pointed out that the two can disagree (a
            # jumpy arming tick anchors the trigger to a lagging mark, and
            # since the trigger only ratchets up, the trail stays wider than
            # intended) and suggested arming off the mark instead so both come
            # from one number.
            #
            # That was implemented, and round 3 showed it liquidates
            # positions. The mark is HISTORY, so arming off it arms
            # RETROACTIVELY: a spike during the entry cooldown (the mark
            # ratchets above the cooldown check, arming happens below it)
            # armed at +7.7% ninety seconds after entry and sold immediately;
            # a restored position carrying S's phantom $1.07 armed on the
            # first pass and sold within two. Both are the failure this whole
            # session is about — an exit nobody's price action asked for.
            #
            # So the disagreement stays, deliberately. Its cost is a trail
            # that is too WIDE (it fires late, giving back more), which is
            # bounded and is also what the pre-Session-15 code did. The cost
            # of the alternative is unbounded and immediate.
            if position.pnl_pct >= activation_pct:
                position.trailing_stop_active = True
                # A peak ABOVE the price we are arming at is a peak we never
                # trailed from: either stale (it predates this trail) or
                # phantom (S has carried $1.07 since 2026-07-27). Either way
                # it cannot retroactively protect anything, and using it would
                # put the trigger above the current price — arming straight
                # into a fire. Lower the mark to the honest starting point;
                # this is also what finally clears a poisoned mark off disk.
                #
                # Normal arming never sees this: the trail arms as the
                # position makes new highs, so the mark IS the current price.
                self._reanchor_mark_for_arming(position)
                position.trailing_stop_price = position.high_water_mark * (
                    1 - trail_distance / 100
                )
                logger.info(
                    f"Trailing stop activated for {position.ticker} | "
                    f"Trail price: ${position.trailing_stop_price:.2f}"
                )
                # Session 10f: arming is the single most important thing to
                # survive a restart. Once armed, the trail keeps protecting the
                # position even after price falls back below the activation
                # threshold — a restored-but-disarmed position would not
                # re-arm, and under challenge sizing has no stop at all.
                self.save_position_state()

        # Check if trailing stop hit
        if position.trailing_stop_active:
            # Update trailing stop if price made new high
            new_trail = position.high_water_mark * (1 - trail_distance / 100)
            if new_trail > position.trailing_stop_price:
                position.trailing_stop_price = new_trail
                self.save_position_state()

            if position.current_price <= position.trailing_stop_price:
                return self._breach_confirmed(
                    position,
                    "trail",
                    lambda price: price <= position.trailing_stop_price,
                    (
                        f"TRAILING STOP: {position.ticker} | "
                        f"Price: ${position.current_price:.2f} | "
                        f"Trail: ${position.trailing_stop_price:.2f} | "
                        f"HWM: ${position.high_water_mark:.2f}"
                    ),
                )

            # Back above the trigger: whatever we saw before was not a trend.
            self._clear_breach(position, "trail")

        return False

    def _check_profit_tiers(self, position: Position):
        """Check if any profit-taking tiers have been reached."""
        rules = position.management_rules
        if rules.get("strategy") != "tiered_profit_taking":
            return

        tiers = rules.get("profit_tiers", [])
        # Use a list (not set) so management_rules stays JSON-serializable
        # for trades.json persistence (BUG-20 fix).
        tiers_hit = rules.get("_tiers_hit", [])
        if isinstance(tiers_hit, set):
            tiers_hit = list(tiers_hit)  # migrate any in-memory sets

        for i, tier in enumerate(tiers):
            if i in tiers_hit:
                continue  # Already trimmed at this level

            if position.pnl_pct < tier["gain_pct"]:
                # Below the tier: any in-progress confirmation is void.
                self._clear_breach(position, f"tier:{i}")
                continue

            if position.pnl_pct >= tier["gain_pct"]:
                # Session 15 (BUG-36): a phantom HIGH sells real contracts,
                # exactly as a phantom low closes a real position. Same guard,
                # same reasoning — the tick that trips a tier must be
                # corroborated before anything is sold. Dormant for the live
                # channel (challenge positions carry trailing_stop_only), so
                # this is here for the day caller_a-alerts-style tiers come back.
                def tier_breached(price: float, _gain=tier["gain_pct"]) -> bool:
                    if not position.entry_price:
                        return False
                    pnl = (
                        (price - position.entry_price)
                        / position.entry_price * 100
                    )
                    return pnl >= _gain

                if not self._breach_confirmed(
                    position,
                    f"tier:{i}",
                    tier_breached,
                    (
                        f"PROFIT TIER {tier['gain_pct']}%: {position.ticker} | "
                        f"Price: ${position.current_price:.2f} | "
                        f"P&L: {position.pnl_pct:.1f}%"
                    ),
                ):
                    continue

                # Calculate contracts to trim from percentage
                trim_pct = tier["trim_pct"]
                # Use ORIGINAL contract count as base, not remaining
                contracts_to_sell = max(
                    1, round(position.contracts * trim_pct / 100)
                )
                # Don't sell more than remaining - 1
                contracts_to_sell = min(
                    contracts_to_sell, position.contracts_remaining - 1
                )

                if contracts_to_sell <= 0:
                    logger.info(
                        f"Profit tier {tier['gain_pct']}% hit for {position.ticker} "
                        f"but only {position.contracts_remaining} contract(s) left — "
                        f"skipping tier trim, letting it ride"
                    )
                    tiers_hit.append(i)
                    continue

                logger.info(
                    f"Profit tier hit: {position.ticker} | "
                    f"{tier['gain_pct']}% gain | Trimming {contracts_to_sell} "
                    f"of {position.contracts_remaining}"
                )
                # Session 9 (M4): only mark the tier as hit when the trim
                # actually confirms — a failed trim retries on a later tick.
                #
                # Money-path review 2026-08-04 (B3): off the monitor thread.
                # A trim blocks 50-95s (placement + confirm + cancel), during
                # which every OTHER position's stop/floor checks starve.
                # Single-flight per position: the worker marks the tier hit
                # itself on confirmation; a failed trim leaves it unmarked
                # and the backoff (M2) paces the retry.
                if getattr(position, "_tier_trim_thread_active", False):
                    continue
                position._tier_trim_thread_active = True

                def _run_tier_trim(p=position, n=contracts_to_sell,
                                   tier_idx=i, gain=tier['gain_pct']):
                    try:
                        ok = self._execute_trim(
                            p, n, f"profit_tier_{gain}pct"
                        )
                        if ok:
                            hits = p.management_rules.get("_tiers_hit", [])
                            if tier_idx not in hits:
                                hits.append(tier_idx)
                                p.management_rules["_tiers_hit"] = hits
                            self.save_position_state()
                        else:
                            logger.warning(
                                f"Profit tier {gain}% trim did not fill for "
                                f"{p.ticker} — tier NOT marked hit, will retry"
                            )
                    except Exception as trim_err:  # noqa: BLE001
                        logger.error(
                            f"Tier trim worker error for {p.ticker}: "
                            f"{trim_err}", exc_info=True
                        )
                    finally:
                        p._tier_trim_thread_active = False

                threading.Thread(
                    target=_run_tier_trim, daemon=True,
                    name=f"tier-trim-{position.ticker}",
                ).start()

        rules["_tiers_hit"] = tiers_hit

    def force_exit_all_0dte(self) -> int:
        """
        Force-exit all open positions that expire today (0DTE).
        Called at 3:45 PM ET by the scheduler to prevent expiry-worthless losses.

        0DTE options that reach 4:00 PM unfilled become worthless — selling at
        any price at 3:45 PM is almost always better than a 100% loss.

        Returns the number of positions DISPATCHED for exit, not the number
        closed. Session 16: each exit now runs in its own worker thread, so
        this returns before any of them have finished.

        Why (2026-07-30, XSP 727P × 2 at $0.01): the sweep fired correctly,
        placed two 45-second attempts, could not fill, and gave up at 15:47
        with thirteen minutes of market left. The contract expired worthless
        for -$18. A limit order left RESTING into the close costs nothing and
        is the only strategy that can catch a stray bid on a dying contract —
        but resting means blocking for ~11 minutes, and this method is called
        from the scheduler thread, which also owns the 16:05 EOD summary, the
        expiry reconcile and the daily task latches. Blocking it would trade
        one bug for a worse one, so the wait moved off-thread.
        """
        # Session 9 (H7a): US trading date + normalized expiry comparison
        # (raw string equality missed "0DTE"/"M/D" style expiries), and
        # manual (bot_managed=False) positions are never auto-sold.
        today = market_time.trading_date().isoformat()
        dispatched = 0
        self.last_0dte_sweep_considered = 0

        for key, position in self._positions_snapshot():
            if not position.bot_managed:
                if market_time.normalize_expiry(position.expiry) == today:
                    logger.warning(
                        f"0DTE forced exit SKIPPING manual position {key} "
                        f"(bot_managed=False) — expires today, manage it yourself!"
                    )
                continue
            if market_time.normalize_expiry(position.expiry) != today:
                continue
            if position.contracts_remaining <= 0:
                continue

            # Counted before the dispatch can fail, so the caller can tell
            # "none held" from "held one and did not dispatch it".
            self.last_0dte_sweep_considered += 1

            logger.warning(
                f"0DTE FORCED EXIT: {position.ticker} ${position.strike} "
                f"{position.direction} exp {position.expiry} × "
                f"{position.contracts_remaining} | P&L: {position.pnl_pct:+.1f}% "
                f"— auto-closing at 3:45 PM ET to avoid expiry worthless"
            )

            if self.notifier:
                self.notifier.notify_status(
                    f"⏰ **0DTE FORCED EXIT**: {position.ticker} ${position.strike} "
                    f"{position.direction} | P&L: {position.pnl_pct:+.1f}% "
                    f"— selling before expiry"
                )

            def _failed(pos=position, k=key):
                logger.error(
                    f"0DTE forced exit FAILED for {k} — order may not have placed. "
                    f"Monitor manually!"
                )
                if self.notifier:
                    self.notifier.notify_error(
                        f"0DTE FORCED EXIT FAILED: {pos.ticker} ${pos.strike} "
                        f"— could not close before expiry. Check Robinhood immediately!"
                    )

            spawned = self._spawn_exit_worker(
                key, position, "0dte_forced_exit_3_45pm",
                urgent=True, rest_until_close=True, on_failure=_failed,
            )
            if spawned:
                dispatched += 1

        return dispatched

    # ── Session 16: expiry reaches the ledger ────────────────────────────
    #
    # 2026-07-30: the 0DTE sweep could not fill XSP 727P × 2 (no bid at
    # $0.01), the contract expired worthless, and NOTHING was written. The
    # key's last event in trades.json stayed OPEN, so:
    #   - the EOD summary reported the day as -$10 when it was -$28;
    #   - `daily_pnl` and the engine's circuit breaker never saw the loss;
    #   - the position kept being quoted all evening at $0.01;
    #   - the stale sidecar entry was pruned on the next restart, and the
    #     books never reconciled.
    # An expiry is not a sell — it is the absence of one — so no code path
    # existed to book it. This is that path.

    def _expiry_suspicious_price(self) -> float:
        """Above this last-known mark, an expiry gets a LOUD notification.

        Not a veto. The first draft of this made it one — refuse to book
        anything marked above $0.03 — and adversarial review killed it: a dead
        OTM contract routinely rests at $0.04-$0.30 purely from a one-sided
        book (bid 0.00 / ask 0.40 marks at 0.20), so the veto would have
        refused most ordinary worthless expiries and recreated the exact
        invisible-loss bug this exists to fix, while leaving the dead position
        in the book to re-alert every day.

        The real hazard it was reaching for is genuine: a contract that
        finished IN THE MONEY does not vanish at 16:00 — an equity option
        auto-exercises into shares (index options like XSP cash-settle, which
        is the same problem wearing a different hat: real proceeds we did not
        record). But the mark cannot tell those apart reliably, and silence is
        the worse error of the two. So the loss is always booked, and a mark
        this high additionally says so on the error channel, where a human can
        check Robinhood and correct one row.
        """
        raw = self.config.get("expiry_suspicious_price", 0.15)
        try:
            return float(raw)
        except (TypeError, ValueError):
            logger.warning(
                f"expiry_suspicious_price={raw!r} is not a number — using 0.15"
            )
            return 0.15

    @staticmethod
    def _expiry_close_stamp(expiry_iso: str) -> Optional[str]:
        """16:00 ET on an expiry date — the moment the contract died.

        Used to back-date a booking so it lands in the P&L of the day it
        happened rather than the day it was noticed.
        """
        try:
            d = date.fromisoformat(expiry_iso)
        except (TypeError, ValueError):
            return None
        return datetime.combine(d, dt_time(16, 0), tzinfo=market_time.ET).isoformat()

    def book_expired_worthless(self, key: str, position: Position,
                               last_price: Optional[float] = None,
                               context: str = "expiry",
                               when: Optional[str] = None) -> bool:
        """Write the CLOSE that an expiry never generates. Returns True if booked.

        Deliberately NOT a day trade: `_record_day_trade_after_fill` is not
        called, because nothing was sold. Deliberately booked at $0.00 rather
        than at the last mark: the position produced no proceeds, and the last
        mark is a quote nobody traded at.

        `when` back-dates the event (see `_log_trade`). A back-dated booking
        does NOT touch today's `daily_pnl` or the engine's circuit breaker —
        it belongs to the session it happened in, and both readers of the
        ledger agree: `restore_daily_pnl_from_trade_log` counts only rows whose
        ET trading date is today, and the EOD summary only rows stamped today.

        The ONE case that is refused is a position whose last sell attempt
        ended with an unconfirmed cancel: that order may have filled, so both
        "it expired worthless" and "we sold it" are live possibilities and
        picking one would be inventing a number.
        """
        # Read and zero under the lock. Three threads can now reach this for
        # one position — the 16:05 sweep on the scheduler, an exit worker, and
        # the startup reconcile — and an unsynchronised check-then-act across
        # a config lookup let two of them both book, for two CLOSE rows and
        # double the loss in `daily_pnl`. Claiming the quantity here makes the
        # second caller a no-op.
        with self._lock:
            qty = position.contracts_remaining
            if qty <= 0:
                return False
            unknown = getattr(position, "sell_state_unknown", False)

        # Money-path review 2026-08-04 (C2): if this position had a FAILED
        # exit — the bot told the operator "manual exit required on
        # Robinhood" — then absence at the broker may mean the operator did
        # exactly that, and booking -100% would INVENT a loss over real
        # proceeds (invariant 5's forbidden direction). Ask the broker before
        # writing off. Network, so outside the lock; the claim below re-checks.
        if (
            not unknown
            and not self.paper_trade
            and getattr(position, "had_failed_exit", False)
        ):
            verdict = None
            try:
                verdict = self.executor.position_exists_at_broker(
                    position.ticker, position.strike,
                    position.expiry, position.direction,
                )
            except Exception as probe_err:  # noqa: BLE001
                logger.warning(
                    f"Expiry holdings check errored for {key}: {probe_err} — "
                    f"proceeding with the normal expiry booking"
                )
            if verdict is False:
                logger.error(
                    f"EXPIRY BOOKING DIVERTED for {key}: this position had a "
                    f"failed exit (operator told to sell by hand) and the "
                    f"broker shows no contract — a manual sale is the live "
                    f"reading. Booking an external close (P&L unknown) "
                    f"instead of -100%."
                )
                return self._book_external_close(
                    position, f"expiry ({context})", qty
                )

        with self._lock:
            if position.contracts_remaining <= 0:
                return False  # another thread booked while we checked
            qty = position.contracts_remaining
            if not unknown:
                # Claim it here, inside the lock. A later assignment would
                # leave a window in which a second caller reads the same
                # quantity and books the same loss again.
                position.contracts_remaining = 0

        if unknown:
            # Alerted ONCE, not once per sweep. An unconditional alert here
            # meant a position carrying this flag emitted a fresh error every
            # single day for ever while never being booked — structurally the
            # same re-alerting zombie the price veto was killed for.
            logger.error(
                f"EXPIRY NOT BOOKED: {key} reached expiry after a sell whose "
                f"cancel was never confirmed — that order may have filled. "
                f"Booking $0.00 could write off contracts that actually sold. "
                f"Ledger left alone; reconcile by hand."
            )
            if not position.expiry_reconcile_alerted:
                # 2026-08-04 (C3/D-4): the refusal must survive a restart. The
                # position is pruned after this, the ledger key stays OPEN,
                # and the startup reconcile would otherwise rebuild a clean
                # stub (flag defaults False) and book the -100% this branch
                # exists to refuse. A NOTE row is ignored by every reader
                # (_scan_trade_log filters OPEN/TRIM/CLOSE) except the
                # reconcile, which skips flagged keys.
                self._log_trade(
                    "NOTE", position,
                    reason="sell_state_unknown — do not auto-book this "
                           "expiry; reconcile by hand",
                    pnl=0.0,
                )
                if self.notifier:
                    self.notifier.notify_error(
                        f"🧾 **EXPIRY NEEDS RECONCILING** — "
                        f"**{position.ticker}** ${position.strike} "
                        f"{position.direction} × {qty} reached expiry after a "
                        f"sell order we could not confirm was cancelled.\nIt "
                        f"may have filled. Check Robinhood and add the "
                        f"correct row to trades.json by hand."
                    )
            position.expiry_reconcile_alerted = True
            return False

        mark = position.current_price if last_price is None else last_price
        suspicious = mark is not None and mark > self._expiry_suspicious_price()

        realized_pnl = -position.entry_price * 100 * qty
        position.current_price = 0.0
        position.pnl_pct = -100.0 if position.entry_price > 0 else 0.0
        if when is None:
            with self._lock:  # 2026-08-04 (B7): += is read-modify-write
                self.daily_pnl += realized_pnl
            # Session 9 (C1): the circuit breaker only sees what is recorded
            # here. An expiry is the largest single loss a position can take,
            # so it is the last thing that should be invisible to it.
            if self.decision_engine:
                self.decision_engine.record_realized_pnl(realized_pnl)
        self._log_trade(
            "CLOSE", position,
            reason=f"expired_worthless ({context})",
            pnl=realized_pnl,
            timestamp=when,
        )
        logger.warning(
            f"EXPIRED WORTHLESS: {key} × {qty} — booked ${realized_pnl:+.2f} "
            f"({context}). No sale took place; this is the entry cost written off."
        )
        if not self.notifier:
            return True

        if suspicious:
            # Booked either way — silence is the worse error — but a mark this
            # high is worth a human's eyes, because an in-the-money expiry
            # produces real proceeds (shares for an equity option, cash for an
            # index one) that this row does not record.
            logger.error(
                f"{key} was booked as worthless despite a last mark of "
                f"${mark:.2f} — if it finished in the money, correct the ledger."
            )
            self.notifier.notify_error(
                f"🪦 **EXPIRED — CHECK THIS ONE**: **{position.ticker}** "
                f"${position.strike} {position.direction} × {qty}\n"
                f"Booked **${realized_pnl:+.2f}** as worthless, but its last "
                f"mark was **${mark:.2f}**. If it finished in the money it was "
                f"exercised or cash-settled, and the real proceeds are missing "
                f"from trades.json — check Robinhood and correct that one row."
            )
        else:
            self.notifier.notify_status(
                f"🪦 **EXPIRED WORTHLESS**: **{position.ticker}** "
                f"${position.strike} {position.direction} × {qty}\n"
                f"Booked **${realized_pnl:+.2f}** — no sale, the contract "
                f"reached expiry unsold."
            )
        return True

    def sweep_expired_positions(self, wait_for_in_flight: float = 0.0) -> int:
        """Book every tracked position that has reached expiry. Returns the count.

        Called from the 16:05 ET scheduler slot, BEFORE the EOD summary, so
        the day's number includes the day's expiries.

        Manual (bot_managed=False) positions are skipped: they are the user's
        to account for.

        A sell still in flight is waited on rather than skipped outright.
        `wait_for_in_flight` seconds are spent letting it finish, because that
        sell may be about to succeed and booking $0.00 over it would write off
        contracts that sold. Review round three found the earlier
        skip-and-hope version leaking a whole class of losses: only a
        `rest_until_close` worker books its own expiry, so an ORDINARY exit
        (caller exit, stop, trail) still confirming at 16:05 was skipped here,
        self-booked nowhere, and then missed by the scheduler's day latch. The
        late backstop sweep (16:20) covers whatever outlives the wait.
        """
        today = market_time.trading_date().isoformat()
        booked = 0
        removed = []

        if wait_for_in_flight > 0:
            deadline = time.monotonic() + float(wait_for_in_flight)
            while time.monotonic() < deadline:
                busy = [
                    k for k, p in self._positions_snapshot()
                    if p.bot_managed and p.contracts_remaining > 0
                    and p.exit_in_flight
                    and (market_time.normalize_expiry(p.expiry, allow_past=True)
                         or "9999") <= today
                ]
                if not busy:
                    break
                logger.info(
                    f"Expiry sweep: waiting for {len(busy)} in-flight sell(s) "
                    f"to finish before booking — {', '.join(busy)}"
                )
                time.sleep(1)

        for key, position in self._positions_snapshot():
            if not position.bot_managed:
                continue
            if position.contracts_remaining <= 0:
                continue
            # allow_past so a straggler from a previous session — one that
            # expired while the bot was down and came back through a restore —
            # is caught here too, not just today's expiries. Without it
            # normalize_expiry returns None for anything past and the position
            # reads as "not expired" for ever.
            normalized = market_time.normalize_expiry(
                position.expiry, allow_past=True
            )
            if normalized is None or normalized > today:
                continue
            with self._lock:
                if position.exit_in_flight:
                    logger.warning(
                        f"Expiry sweep: {key} STILL has a sell in flight after "
                        f"waiting — not booking over it (that order may fill). "
                        f"The late sweep will pick it up."
                    )
                    continue
            # A straggler from an earlier session belongs to the day it died,
            # not to today: booking it now would spend today's circuit-breaker
            # budget on last week's loss. Same reasoning as the restart
            # reconcile, which is where stragglers usually come from.
            if normalized == today:
                when = None
            else:
                when = self._expiry_close_stamp(normalized)
                if when is None:
                    # Never silently fall back to "today": that is exactly the
                    # misattribution the back-dating exists to prevent.
                    logger.error(
                        f"Expiry sweep: could not date {key}'s expiry "
                        f"({normalized!r}) — skipped rather than booked into "
                        f"today's P&L. Book it by hand."
                    )
                    continue
            if self.book_expired_worthless(
                key, position, context="expiry sweep", when=when
            ):
                booked += 1
                removed.append(key)
            elif getattr(position, "sell_state_unknown", False):
                # Refused, and it will be refused every time: the contract is
                # dead, so leaving it under management only keeps a dead quote
                # in the monitor and re-runs this branch tomorrow. It has been
                # escalated; the ledger row stays open on purpose.
                removed.append(key)

        if removed:
            with self._lock:
                for key in removed:
                    self.positions.pop(key, None)
            self.save_position_state()

        return booked

    def _spawn_exit_worker(self, key: str, position: Position, reason: str,
                           urgent: bool = False, limit_price: float = None,
                           rest_until_close: bool = False,
                           on_failure=None) -> bool:
        """Session 9 verify-pass: run a monitor-triggered exit off-loop.

        The worker owns position removal on a confirmed close (the monitor
        loop's keys_to_remove no longer sees these exits). Duplicate spawns
        are prevented by exit_in_flight — checked here cheaply to avoid a
        log line every 5s tick while an exit is confirming.

        Returns True if a worker was started, False if one was already in
        flight for this position. Session 16: the 0DTE sweep counts dispatches
        with it, and `on_failure` carries that path's extra escalation, which
        used to run inline because the sweep used to be synchronous.
        """
        with self._lock:
            if position.exit_in_flight:
                return False

        def _run():
            try:
                exited = self._execute_full_exit(
                    position, reason, limit_price=limit_price, urgent=urgent,
                    rest_until_close=rest_until_close,
                )
                if exited:
                    with self._lock:
                        self.positions.pop(key, None)
                    # Session 16: prune the sidecar too. The synchronous sweep
                    # used to do this; a worker that only pops the dict leaves
                    # a closed key on disk to be restored on the next start.
                    self.save_position_state()
                    return
                # The escalation is best-effort and must not be able to skip
                # the booking below — it is a retrying webhook, and an
                # exception in it used to swallow the whole rest of this path
                # and then re-fire itself from the handler.
                if on_failure is not None:
                    try:
                        on_failure()
                    except Exception as notify_err:
                        logger.error(
                            f"0DTE escalation failed for {key}: {notify_err}"
                        )
                # Session 16: a belt to the 16:05 sweep's braces. The rest
                # ends at close−2min by construction, so in practice the clock
                # gate inside refuses here and the sweep does the booking a
                # few minutes later; this covers the cases where it does not —
                # a rest that ran past 16:00 because the broker hung, or a
                # sweep that never ran because the bot was restarting. It
                # books nothing until the session on the expiry date is over.
                if rest_until_close:
                    self._book_expiry_after_failed_exit(key, position)
            except Exception as e:
                # Before Session 16 this ran inline on the scheduler thread,
                # whose handler logs a traceback. A worker that swallows the
                # exception silently would leave a live position with no exit
                # and no word to anyone.
                logger.error(f"Exit worker error for {key}: {e}", exc_info=True)
                try:
                    if on_failure is not None:
                        on_failure()
                except Exception as notify_err:
                    logger.error(f"Exit worker escalation failed: {notify_err}")

        threading.Thread(
            target=_run, daemon=True, name=f"exit-{position.ticker}"
        ).start()
        return True

    def wait_for_exit_workers(self, timeout: float = 20.0) -> bool:
        """Give in-flight exit workers a moment to finish. True if all did.

        Session 16. Exit workers are daemon threads, which was harmless while
        the longest one lived ~90 seconds and a shutdown mid-exit meant a
        cancelled order. A 0DTE sell now RESTS until just before the close, so
        the same shutdown could strand a live good-for-day order at the broker.
        `_shutdown` makes those waits return promptly; this then waits for them
        to actually cancel and tidy up, bounded so a hung broker call cannot
        block shutdown for ever.
        """
        deadline = time.monotonic() + float(timeout)
        alive = [
            t for t in threading.enumerate()
            if t.name.startswith("exit-") and t.is_alive()
        ]
        for t in alive:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=remaining)
        still = [t.name for t in alive if t.is_alive()]
        if still:
            logger.warning(
                f"Exit worker(s) still running at shutdown: {', '.join(still)} "
                f"— an order may be resting at the broker; check Robinhood."
            )
            return False
        return True

    def _book_expiry_after_failed_exit(self, key: str, position: Position):
        """An expiry-driven exit that could not sell: write the loss off — but
        ONLY once the contract is genuinely dead.

        The gate is the clock, and it is the whole point of this method rather
        than a detail of it. Round two of review found the first version
        booking a -100% loss on a position that was still trading: the sweep
        dispatches at 15:45, a `!stop` at 15:50 aborts the rest, the exit
        returns False, and the write-off landed with TEN MINUTES of market
        left — on a contract still held at Robinhood, while the bot's own
        notification was telling the operator "you can still exit by hand
        until then". Worse, the CLOSE row made the key look closed, so the
        next restart restored the live position as unmanaged: no stop, no
        trail, skipped by both sweeps.

        So: nothing is written off until the session on the expiry date is
        over. After that instant the contract cannot trade again, and it no
        longer matters WHY the sell failed — no bid, a broker outage, a
        contract-identity block, a shutdown. Before that instant, none of
        those are expiry and none of them may be booked as one.
        """
        try:
            if position.contracts_remaining <= 0:
                return
            normalized = market_time.normalize_expiry(
                position.expiry, allow_past=True
            )
            if normalized is None:
                return
            try:
                expiry_date = date.fromisoformat(normalized)
            except (TypeError, ValueError):
                return
            died_at = datetime.combine(
                expiry_date, dt_time(16, 0), tzinfo=market_time.ET
            )
            now = market_time.now_et()
            if now < died_at:
                logger.warning(
                    f"{key} could not be sold, but its expiry has not passed "
                    f"yet ({now:%H:%M} ET vs {died_at:%Y-%m-%d %H:%M}) — NOT "
                    f"writing it off. It is still a live position; exit it by "
                    f"hand if you want out."
                )
                return
            if self.book_expired_worthless(
                key, position, context="unsold at expiry"
            ):
                with self._lock:
                    self.positions.pop(key, None)
                self.save_position_state()
        except Exception as e:
            logger.error(f"Could not book the expiry for {key}: {e}", exc_info=True)

    def _execute_full_exit(self, position: Position, reason: str, limit_price: float = None,
                           urgent: bool = False, rest_until_close: bool = False) -> bool:
        """Session 9 verify-pass: per-position exit mutual exclusion.

        Sell confirmation can block for up to ~2× sell_fill_timeout_seconds,
        and three threads can race to exit the same position (monitor
        stop/trail, caller-exit pipeline, 0DTE scheduler). Without this guard
        a second full-quantity sell could be placed for contracts already
        being sold, PDT slots could be double-burned, and partials could be
        double-booked. First exit in wins; concurrent attempts return False.
        """
        queued_caller_exit = False
        skipped = False
        with self._lock:
            if position.exit_in_flight:
                # Day 4 review round 1: a CALLER exit landing here used to be
                # consumed with an info-level log. The in-flight attempt can
                # take seconds (sell attempts + the BUG-40 broker walk), the
                # caller's exit and a price-inferred trigger fire in the same
                # price region, and under the no-trail live config nothing
                # would ever retry the instruction. Queue it; the monitor
                # re-fires it as an instruction once the flight resolves.
                if reason == "caller_exit":
                    position.pending_caller_exit = True
                    position.pending_caller_exit_limit = float(limit_price or 0.0)
                    queued_caller_exit = True
                else:
                    skipped = True
            else:
                position.exit_in_flight = True
        if queued_caller_exit:
            logger.warning(
                f"CALLER EXIT QUEUED for {position.ticker}: another exit "
                f"attempt is mid-flight — the instruction will re-fire the "
                f"moment it resolves without closing the position."
            )
            if self.notifier:
                self.notifier.notify_status(
                    f"⏳ Caller exit for **{position.ticker}** arrived while "
                    f"another exit attempt was mid-flight — queued; it "
                    f"retries as soon as that attempt resolves."
                )
            return False
        if skipped:
            logger.info(
                f"EXIT SKIPPED for {position.ticker}: another exit is "
                f"already in flight (trigger was: {reason})"
            )
            return False
        try:
            return self._execute_full_exit_inner(
                position, reason, limit_price, urgent,
                rest_until_close=rest_until_close,
            )
        finally:
            with self._lock:
                position.exit_in_flight = False

    def _rest_timeout_seconds(self) -> Optional[float]:
        """How long the FINAL sell attempt may rest, or None for "don't rest".

        Session 16. The deadline is the close minus a margin, so the order is
        cancelled and inspected while the market can still report a fill
        rather than at 16:00 sharp with the book already gone.

        Returns None whenever resting makes no sense — outside the session, or
        so close to the deadline that the rest would be shorter than the normal
        timeout. The caller then falls back to the ordinary flow, which is the
        pre-Session-16 behaviour: never a longer wait than before, only ever a
        longer one when there is real market time to wait through.

        MEASURED FROM NOW, so it must be called at the start of the attempt
        that will rest, not at the top of the exit. Review caught the first
        version doing the latter: attempt 1 takes 45 seconds (longer if
        `sell_option_position` has to re-authenticate), and the rest inherited
        a window computed before all of it, pushing the cancel past 16:00 —
        the one thing the margin exists to prevent.
        """
        remaining = market_time.seconds_until_close()
        if remaining is None:
            return None
        margin_min = self.config.get("zero_dte_rest_margin_minutes", 2)
        try:
            margin_s = float(margin_min) * 60.0
        except (TypeError, ValueError):
            logger.warning(
                f"zero_dte_rest_margin_minutes={margin_min!r} is not a number "
                f"— using 2"
            )
            margin_s = 120.0
        rest = remaining - margin_s
        normal = float(self.config.get("sell_fill_timeout_seconds", 45))
        if rest <= normal:
            return None
        # A backstop against a wrong clock rather than a tuning knob. It does
        # NOT solve early closes: on a 13:00 half-day (roughly twice a year)
        # `seconds_until_close`'s hard-coded 16:00 is simply wrong, the sweep
        # still fires at 15:45, and this returns ~13 minutes of resting into a
        # market that shut hours ago. What saves the money there is that the
        # write-off is gated on the clock passing 16:00 anyway, so the position
        # survives the day intact — a resting order into a closed book cannot
        # fill, and cannot lose anything either. A real half-day calendar in
        # `market_time` is the proper fix; this cap only bounds the waiting.
        rest = min(rest, 30 * 60.0)
        return rest

    # ── Exit ladder (2026-08-04, the QCOM $240c caller exit) ─────────────────
    # caller_a posted "0.44 20% out QCOM" into a $0.36 × $0.51 book; the ILLIQUID
    # tier priced the mirror's sell at bid-3% and it filled at $0.36 — the
    # caller booked +20%, the bot -5.3%, and the whole difference was the
    # half-spread donated by hitting the bid on a book showing vol=1 (no
    # stampede to beat). The wide-spread tier's aggression is right for
    # urgent exits; for an INSTRUCTION on a slow book it is exactly wrong.
    #
    # Policy (the operator, 2026-08-04): non-urgent FULL exits on a wide two-sided
    # book rest near mid and walk down — rungs at [start, bid+25% of spread,
    # bid], each resting exit_ladder.step_seconds, then the pre-existing
    # urgent sweep. Everything else is unchanged: urgent exits, 0DTE
    # (their books die fast and the 15:45 sweep owns the endgame), resting
    # Session-16 exits, tight books, one-sided books, paper mode. Caller
    # TRIMS keep today's single-attempt pricing for now — that path is its
    # own machine and gets its own change, deliberately not tonight's.
    # ONE deliberate exception (review round 1, both reviewers): a caller
    # trim against our LAST contract escalates to a full exit (H4a), and
    # that full exit DOES ladder — it is a full close of the position, the
    # instruction semantics are a caller exit's, and on this 1-lot-heavy
    # account refusing it would exclude the commonest trim of all from the
    # very books where the ladder earns its keep. Pinned in the test file.

    def _ladder_settings(self) -> dict:
        raw = self.config.get("exit_ladder") or {}
        if not isinstance(raw, dict):
            raw = {}

        def _num(key, default):
            try:
                return float(raw.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        # Review round 1: bool("false") is True. The live loader hands YAML
        # bools here, but a quoted "false" or an unexpanded ${VAR} placeholder
        # must fail OFF — a config that cannot be read plainly is a config
        # that did not ask for the feature.
        raw_enabled = raw.get("enabled", False)
        if isinstance(raw_enabled, str):
            enabled = raw_enabled.strip().lower() in ("1", "true", "yes", "on")
        else:
            enabled = bool(raw_enabled)
        return {
            "enabled": enabled,
            "min_spread_pct": _num("min_spread_pct", 15.0),
            "step_seconds": _num("step_seconds", 20.0),
        }

    def _ladder_eligible(self, position: Position, urgent: bool,
                         rest_until_close: bool) -> bool:
        """Preconditions that need no quote. The spread test happens at
        engagement, on the same fresh quote rung 1 will price from."""
        settings = self._ladder_settings()
        if not settings["enabled"]:
            return False
        if urgent or rest_until_close or self.paper_trade:
            return False
        # 0DTE: excluded whole-day, not just near the sweep. A dying
        # contract's book can go one-sided between rungs, and BUG-38's
        # aggressive pricing exists precisely for that population.
        if self._is_0dte_today(position):
            return False
        if not market_time.is_market_hours():
            return False
        # Review round 1: on a 13:00 half-day `seconds_until_close` counts to
        # a fictional 16:00, so the budget check below would happily rest
        # rungs into the last real minutes (or past the real close). The
        # half-day hole is codebase-wide and stays open tonight; the ladder
        # at least refuses to be clever inside it — the classic flow there
        # is the pre-change behaviour, whose aggression is what a closing
        # book wants anyway.
        if market_time.is_early_close_day():
            return False
        remaining = market_time.seconds_until_close()
        if remaining is None:
            return False
        sell_timeout = float(self.config.get("sell_fill_timeout_seconds", 45))
        # The ladder must fit: 3 rungs + the urgent sweep + a minute of
        # margin, or the walk-down risks running into the close.
        budget = 3 * settings["step_seconds"] + sell_timeout + 60.0
        return remaining > budget

    def _ladder_rung_price(self, kind: str, quote: dict,
                           anchor: Optional[float]) -> float:
        """One rung's limit on the tick grid, never above ask - tick.

        kind: "start" (mid, lifted to the caller's printed price when that is
        higher — his fill is proof the level trades), "mid25"
        (bid + 25% of spread), "bid".
        anchor: the caller's stated exit price; only rung 1 receives it.

        Lower bound honesty (review round 1): the price is clamped to >= bid
        BEFORE tick-flooring, so an off-grid bid at/above $3.00 can floor one
        tick under itself (bid 3.27 → 3.25). Deliberate: a sell limit at or
        under the bid is marketable and fills at the bid or better, so the
        floor errs toward fillable, never toward expensive.
        """
        bid, ask = quote["bid"], quote["ask"]
        if kind == "start":
            price = quote["mid"]
            if anchor and anchor > price:
                price = anchor
        elif kind == "mid25":
            price = bid + 0.25 * (ask - bid)
        else:
            price = bid
        # Cap below the ask: a sell resting AT the ask sits behind the maker's
        # own offer; one tick under it jumps the queue. Floor at bid — a rung
        # can never price below what hitting the bid would get.
        tick = 0.01 if ask < 3.00 else 0.05
        price = min(price, ask - tick)
        price = max(price, bid)
        return self.executor.floor_to_tick(price)

    def _execute_full_exit_inner(self, position: Position, reason: str, limit_price: float = None,
                                 urgent: bool = False, rest_until_close: bool = False) -> bool:
        """
        Exit the entire remaining position.
        Returns True only on a CONFIRMED full close (Session 9 / C2).
        Returns False if PDT-blocked, unfilled, or only partially filled —
        partial fills are booked and the remainder stays tracked and managed.

        limit_price: if provided (e.g. caller said "Out at 0.50"), use it as the
        sell limit so we match the caller's exit exactly rather than pricing off
        the live bid (which would undershoot by ~2-5%).
        If None, falls back to bid-based pricing in sell_option_position().

        PDT POLICY: We NEVER accept a PDT flag. If closing this position
        same-day would be day trade #4+, we HOLD it overnight and sell at
        next open. One extra day of drawdown < losing instant settlement.

        The only exception: 0DTE positions that are expiring worthless today.
        Those must close (or expire), and since 0DTE day trades were already
        counted at entry, closing them doesn't add a new day trade.
        """
        if position.contracts_remaining <= 0:
            return True

        # --- PDT check for same-day close ---
        # Session 9: trading-date/expiry math via market_time (US/Eastern).
        is_same_day = self._is_same_trading_day(position)
        is_0dte = self._is_0dte_today(position)

        if is_same_day and not is_0dte and self.decision_engine:
            if not position.day_trade_recorded:
                # First sell of this same-day position = new day trade
                remaining = self.decision_engine.get_day_trades_remaining()
                dt_in_window = self.decision_engine.get_day_trades_in_window()

                if remaining <= 0:
                    # BLOCK THE EXIT: hold overnight instead of triggering PDT
                    pdt_msg = (
                        f"⚠️ PDT HOLD: {position.ticker} stop hit ({reason}) but closing "
                        f"would be day trade #{dt_in_window + 1}/{self.decision_engine._max_day_trades}. "
                        f"HOLDING OVERNIGHT to avoid PDT flag. Will sell at next market open. "
                        f"Current P&L: {position.pnl_pct:.1f}%"
                    )
                    logger.warning(pdt_msg)
                    if self.notifier:
                        self.notifier.notify_error(pdt_msg)

                    position.pdt_held = True
                    position.pdt_held_reason = reason
                    position.pdt_sell_next_open = True
                    position.stop_loss_pct = 999
                    position.trailing_stop_active = False

                    log_trade_execution(
                        action="PDT_HOLD",
                        ticker=position.ticker,
                        direction=position.direction,
                        strike=position.strike,
                        expiry=position.expiry,
                        contracts=position.contracts_remaining,
                        price=position.current_price,
                        pnl_pct=position.pnl_pct,
                        reason=pdt_msg,
                    )
                    return False  # Exit blocked

                else:
                    # Session 9 (M2): the day trade is recorded AFTER a
                    # confirmed fill now — a failed sell must not burn a slot.
                    logger.info(
                        f"Same-day close of {position.ticker} ({reason}) will be "
                        f"day trade #{dt_in_window + 1}/"
                        f"{self.decision_engine._max_day_trades} "
                        f"(recorded after confirmed fill)"
                    )
            else:
                # Already recorded as day trade (e.g., we trimmed earlier today)
                # This additional sell doesn't count as a new day trade
                logger.debug(
                    f"Full exit of {position.ticker} — already recorded as day trade today"
                )

        # --- Execute the exit ---
        logger.info(
            f"FULL EXIT: {position.ticker} | Reason: {reason} | "
            f"Contracts: {position.contracts_remaining} | P&L: {position.pnl_pct:.1f}%"
        )

        if limit_price:
            logger.info(
                f"Selling at caller price ${limit_price:.2f} "
                f"(not bid-based fallback)"
            )

        # Paper trade: log the intended exit but do NOT touch Robinhood
        if self.paper_trade:
            sim_pnl = (
                (position.current_price - position.entry_price)
                * 100
                * position.contracts_remaining
            )
            logger.info(
                f"[PAPER] Would exit {position.ticker} | Reason: {reason} | "
                f"Simulated P&L: ${sim_pnl:+.2f}"
            )
            if self.notifier:
                self.notifier.notify_status(
                    f"📝 **PAPER EXIT** *(not executed)*\n"
                    f"**{position.ticker}** ${position.strike} {position.direction} "
                    f"× {position.contracts_remaining} | Reason: {reason} | "
                    f"Simulated P&L: **${sim_pnl:+.2f}**"
                )
            with self._lock:  # 2026-08-04 (B7): += is read-modify-write
                self.daily_pnl += sim_pnl
            # Session 9 (C1): bridge realized P&L to the engine so the daily
            # circuit breaker actually sees losses.
            if self.decision_engine:
                self.decision_engine.record_realized_pnl(sim_pnl)
            position.contracts_remaining = 0
            self._log_trade("CLOSE", position, reason=f"[PAPER] {reason}", pnl=sim_pnl)
            position.exit_backoff_until = 0.0
            position.pending_caller_exit = False
            return True

        # --- Session 9 (C2): place → CONFIRM FILL → cancel/re-price retry ---
        # Placement is NOT a fill. Attempt 1 uses the stated limit (caller
        # price or spread-aware); if unfilled at timeout, cancel (inspecting
        # the cancel race for fills), then attempt 2 re-prices aggressively
        # (urgent=True). P&L/CLOSE are booked ONLY from confirmed fills.
        qty_target = position.contracts_remaining
        total_filled = 0
        weighted_pps_sum = 0.0
        # Review round 1: the SELL FAILED escalation used to hard-code
        # "2 attempts" — false once a ladder has placed up to 3 more. Count
        # what was actually placed; escalation text is load-bearing here
        # (the Session 17 "0/1 filled within 771s" lesson).
        orders_placed = 0
        sell_timeout = self.config.get("sell_fill_timeout_seconds", 45)

        # Session 16: on an expiry-driven exit the LAST attempt rests until
        # just before the close instead of being cancelled after 45 seconds.
        # None means the ordinary two-attempt flow — see _rest_timeout_seconds.
        # Recomputed at the start of attempt 2 rather than here, so whatever
        # attempt 1 spent comes off the rest and not off the safety margin.
        rest_timeout = None

        # ── Ladder phase (2026-08-04; block comment above _ladder_settings) ──
        # Rungs place → confirm → cancel-and-inspect exactly like the classic
        # attempts below, banking fills into the same accumulators so the
        # booking tail (full/partial/none) needs no new cases. If a rung's
        # cancel cannot be CONFIRMED, nothing further may be placed — the
        # resting order could still fill, and a second order is a double
        # sell. That rule is the classic loop's own (Session 9 verify-pass);
        # the ladder inherits it by aborting straight to the booking tail.
        ladder_ran = False
        ladder_aborted_unknown = False
        if self._ladder_eligible(position, urgent, rest_until_close):
            _ls = self._ladder_settings()
            _step_s = _ls["step_seconds"]
            for _rung_idx, _rung_kind in enumerate(("start", "mid25", "bid"), 1):
                if self._shutdown.is_set():
                    logger.warning(
                        f"EXIT LADDER for {position.ticker}: shutdown "
                        f"requested — handing to the standard flow"
                    )
                    break
                _qty_now = qty_target - total_filled
                if _qty_now <= 0:
                    break
                _q = self.executor.get_option_quote(
                    position.ticker, position.expiry,
                    position.strike, position.direction,
                )
                if _q is None:
                    logger.warning(
                        f"EXIT LADDER for {position.ticker}: book went "
                        f"one-sided/unreadable at rung {_rung_idx} — handing "
                        f"to the standard flow"
                    )
                    break
                if _rung_idx == 1 and _q["spread_pct"] < _ls["min_spread_pct"]:
                    logger.info(
                        f"EXIT LADDER for {position.ticker}: spread "
                        f"{_q['spread_pct']:.1f}% is under the "
                        f"{_ls['min_spread_pct']:.0f}% threshold — ordinary "
                        f"flow (no ladder needed on a tight book)"
                    )
                    break
                _rung_price = self._ladder_rung_price(
                    _rung_kind, _q, limit_price if _rung_idx == 1 else None
                )
                if _rung_idx == 1:
                    logger.warning(
                        f"EXIT LADDER engaged for {position.ticker} "
                        f"${position.strike} {position.direction} × {_qty_now}: "
                        f"book ${_q['bid']:.2f}×${_q['ask']:.2f} "
                        f"({_q['spread_pct']:.1f}% spread) — working from "
                        f"${_rung_price:.2f} down, {_step_s:.0f}s per rung, "
                        f"urgent sweep after"
                    )
                    if self.notifier:
                        self.notifier.notify_status(
                            f"🪜 **{position.ticker}** book is wide "
                            f"(${_q['bid']:.2f}×${_q['ask']:.2f}) — working "
                            f"the exit from **${_rung_price:.2f}** down "
                            f"instead of hitting the bid. Falls back to the "
                            f"aggressive sweep in ~"
                            f"{3 * _step_s + self.config.get('sell_fill_timeout_seconds', 45):.0f}s "
                            f"if unfilled."
                        )
                _order_id = self.executor.sell_option_position(
                    ticker=position.ticker,
                    strike=position.strike,
                    expiry=position.expiry,
                    direction=position.direction,
                    contracts=_qty_now,
                    limit_price=_rung_price,
                    urgent=False,
                )
                if not _order_id:
                    logger.warning(
                        f"EXIT LADDER for {position.ticker}: rung {_rung_idx} "
                        f"failed to PLACE — handing to the standard flow (its "
                        f"own guards said why above)"
                    )
                    break
                ladder_ran = True
                orders_placed += 1
                _state, _fq, _pps = self._confirm_sell_fill(_order_id, _step_s)
                if _state != "filled":
                    _c_qty, _c_pps, _c_final = self._cancel_and_inspect(_order_id)
                    if _c_qty > _fq:
                        _fq, _pps = _c_qty, _c_pps
                    if _c_final == "filled":
                        _state = "filled"
                    if _c_final in ("unknown", "error") and _c_qty <= _fq:
                        position.sell_state_unknown = True
                        ladder_aborted_unknown = True
                        logger.error(
                            f"Cancel UNCONFIRMED for {position.ticker} ladder "
                            f"rung {_rung_idx} order {_order_id} "
                            f"(state={_c_final}) — NOT placing further orders; "
                            f"the rung may still be live on Robinhood"
                        )
                        if self.notifier:
                            self.notifier.notify_error(
                                f"🚨 **CANCEL UNCONFIRMED** — ladder sell for "
                                f"**{position.ticker}** ${position.strike} "
                                f"{position.direction} may still be resting "
                                f"on Robinhood. Check manually before "
                                f"intervening."
                            )
                # "filled" with zero quantity is a data glitch, not a zero
                # fill — same re-poll-then-assume the classic loop and the
                # trim path carry (Session 9 verify-pass / Session 16).
                if _state == "filled" and _fq <= 0:
                    time.sleep(1)
                    try:
                        _retry = self.executor.check_order_status(_order_id) or {}
                    except Exception as _e:  # noqa: BLE001
                        logger.warning(f"ladder re-poll of {_order_id} failed: {_e}")
                        _retry = {}
                    try:
                        _rq = float(_retry.get("filled_quantity", 0) or 0)
                    except (TypeError, ValueError):
                        _rq = 0.0
                    try:
                        _rp = float(_retry.get("average_price_per_share", 0) or 0)
                    except (TypeError, ValueError):
                        _rp = 0.0
                    if _rq > 0:
                        _fq, _pps = _rq, _rp
                    else:
                        logger.warning(
                            f"Ladder order {_order_id} reports filled with 0 "
                            f"quantity — assuming the full {_qty_now} @ last "
                            f"polled price"
                        )
                        _fq = float(_qty_now)
                _fc = int(_fq + 0.5)
                if _fc > 0:
                    # Over-report clamp, parity with the trim path (Session 12
                    # nice-to-have #11): a glitched status must not inflate
                    # realized P&L or drive contracts_remaining negative.
                    _fc = min(_fc, _qty_now)
                    if _pps <= 0:
                        logger.warning(
                            f"No per-share fill price for {position.ticker} "
                            f"ladder order {_order_id} — booking at last "
                            f"polled price ${position.current_price:.2f}"
                        )
                        _pps = position.current_price
                    weighted_pps_sum += _pps * _fc
                    total_filled += _fc
                if ladder_aborted_unknown or total_filled >= qty_target:
                    break
                logger.info(
                    f"EXIT LADDER {position.ticker} rung {_rung_idx}/3 "
                    f"(${_rung_price:.2f}, {_step_s:.0f}s): "
                    f"{_fc}/{_qty_now} filled — "
                    + ("stepping down" if _rung_idx < 3 else "urgent sweep next")
                )
        # After an ENGAGED ladder the non-urgent classic attempt 1 would just
        # re-run a worse rung (spread-aware wide-book pricing = bid-3%), so
        # the remainder goes straight to the urgent sweep. An unconfirmed
        # cancel places nothing more at all — booking tail only.
        classic_attempts = (
            () if ladder_aborted_unknown else ((2,) if ladder_ran else (1, 2))
        )

        for sell_attempt in classic_attempts:
            qty_this_attempt = qty_target - total_filled
            if qty_this_attempt <= 0:
                break

            sell_price = limit_price if sell_attempt == 1 else None
            attempt_urgent = urgent if sell_attempt == 1 else True
            if sell_attempt == 2 and rest_until_close:
                rest_timeout = self._rest_timeout_seconds()
            resting = sell_attempt == 2 and rest_timeout is not None
            attempt_timeout = rest_timeout if resting else sell_timeout
            attempt_started = time.monotonic()
            # Session 17 / BUG-38 review round 4. ONE deadline for the whole
            # resting attempt. The re-quote loop and the fill confirmation
            # below were each given a full `attempt_timeout`, so attempt 2
            # could spend the rest window TWICE — re-quoting until 15:59 and
            # then confirming until 16:12, putting the cancel twelve minutes
            # past the close. That is precisely what the margin exists to
            # prevent, and precisely the defect `_rest_timeout_seconds`'
            # docstring records being fixed once already.
            rest_deadline = attempt_started + attempt_timeout

            order_id = self.executor.sell_option_position(
                ticker=position.ticker,
                strike=position.strike,
                expiry=position.expiry,
                direction=position.direction,
                contracts=qty_this_attempt,
                limit_price=sell_price,
                urgent=attempt_urgent,
            )

            if not order_id and resting:
                # Session 17 / BUG-38 review round 3. Refusing to place a
                # blind order into an empty book is right; abandoning the
                # whole rest window because of ONE bad poll is not. A 0/0
                # quote is routinely transient — a momentary gap in a
                # one-sided book — and this is the 0DTE sweep's last chance:
                # nothing retries after it (the sweep is latched once per day,
                # and a 0/0 quote also fails _quote_is_sane so no price-
                # inferred exit can fire either). Before this fix an order was
                # at least placed and left working for ~13 minutes; the
                # refusal turned that into a 2-second give-up and a manual
                # escalation. Re-quote instead, for the same window.
                requotes = 0
                aborted = self._shutdown.is_set()
                # Review round 5: stop while there is still enough window to
                # be worth an order. The first draft slept exactly TO the
                # deadline and then re-quoted once more, so the late-returning
                # book — the very case this loop exists for — produced an
                # order placed at the deadline and cancelled one second later.
                # And a one-second-old order is the one most likely to read
                # non-terminal on cancel, which now latches
                # `sell_state_unknown` and blocks the expiry booking. Place it
                # with time to work, or not at all.
                min_useful = min(30.0, attempt_timeout / 4.0)
                while (not order_id
                       and (rest_deadline - time.monotonic()) > min_useful
                       and not self._shutdown.is_set()):
                    self._shutdown.wait(
                        min(10.0, max(0.0, rest_deadline - time.monotonic()
                                      - min_useful)))
                    if self._shutdown.is_set():
                        aborted = True
                        break
                    requotes += 1
                    order_id = self.executor.sell_option_position(
                        ticker=position.ticker,
                        strike=position.strike,
                        expiry=position.expiry,
                        direction=position.direction,
                        contracts=qty_this_attempt,
                        limit_price=sell_price,
                        urgent=attempt_urgent,
                    )
                if order_id:
                    logger.warning(
                        f"0DTE RESTING ORDER placed after {requotes} re-quote(s) "
                        f"— the book came back for {position.ticker}")
                elif aborted:
                    logger.warning(
                        f"{position.ticker}: re-quoting stopped by shutdown "
                        f"after {requotes} attempt(s)")
                else:
                    # Review round 4: do NOT claim "no market". The loop
                    # re-enters on ANY falsy order_id — an unverifiable
                    # contract, a resolver mismatch, a failed re-auth — and
                    # naming the wrong cause in the first line an operator
                    # reads is how the last incident took hours to diagnose.
                    logger.warning(
                        f"{position.ticker}: could not place a sell for the "
                        f"whole rest window ({requotes} re-quotes). See the "
                        f"per-attempt reasons above — an empty book is only "
                        f"one of the possibilities."
                    )

            if not order_id:
                logger.warning(
                    f"Sell attempt {sell_attempt} failed to PLACE for "
                    f"{position.ticker}"
                    + (" — retrying with urgent pricing" if sell_attempt == 1 else "")
                )
                if sell_attempt == 1:
                    time.sleep(2)
                continue
            orders_placed += 1

            if resting:
                # Round 4: report the time actually LEFT. Re-quoting can have
                # eaten most of the window before an order was placed, and
                # printing the original figure at 15:59 tells the operator
                # they have thirteen minutes to intervene when they have one.
                _left_min = max(0.0, rest_deadline - time.monotonic()) / 60
                logger.warning(
                    f"0DTE RESTING ORDER: {position.ticker} ${position.strike} "
                    f"{position.direction} × {qty_this_attempt} left working for "
                    f"up to {_left_min:.0f} min, until ~"
                    f"{self.config.get('zero_dte_rest_margin_minutes', 2)} min "
                    f"before the close — a dying contract's only chance of a bid"
                )

            # Round 4: confirm within what REMAINS of the resting window, not
            # a fresh copy of it. Non-resting attempts are unaffected.
            #
            # Money-path review 2026-08-04 (C5): a RESTING order that the
            # broker terminates early (exchange cancel, RH risk check, a
            # manual mis-click on the very order the bot invites the operator
            # to manage) used to surrender the whole remaining window — one
            # order lifetime and done, on a dying contract whose only chance
            # of a bid is that window. Rebirth: while real window remains and
            # the shutdown flag is down, replace the terminated order and
            # keep confirming — bounded, so a tight place/reject cycle can't
            # spin (each rebirth also re-prices via sell_option_position's
            # own quote path).
            confirm_timeout = attempt_timeout
            if resting:
                confirm_timeout = max(1.0, rest_deadline - time.monotonic())
            state, filled_qty, fill_pps = self._confirm_sell_fill(
                order_id, confirm_timeout, resting=resting
            )
            if resting:
                rebirths = 0
                _min_useful = min(30.0, attempt_timeout / 4.0)
                while (
                    state == "unfilled"
                    and filled_qty <= 0
                    and rebirths < 3
                    and not self._shutdown.is_set()
                    and (rest_deadline - time.monotonic()) > _min_useful
                ):
                    # Only a broker-terminated order returns this early with
                    # window left — a timeout would have consumed it.
                    rebirths += 1
                    logger.warning(
                        f"0DTE RESTING ORDER for {position.ticker} ended "
                        f"early in a terminal broker state — replacing "
                        f"(rebirth {rebirths}/3, "
                        f"{(rest_deadline - time.monotonic()) / 60:.1f} min "
                        f"of window left)"
                    )
                    self._shutdown.wait(2.0)
                    new_order = self.executor.sell_option_position(
                        ticker=position.ticker,
                        strike=position.strike,
                        expiry=position.expiry,
                        direction=position.direction,
                        contracts=qty_this_attempt,
                        limit_price=sell_price,
                        urgent=attempt_urgent,
                    )
                    if not new_order:
                        break
                    order_id = new_order
                    orders_placed += 1
                    state, filled_qty, fill_pps = self._confirm_sell_fill(
                        order_id,
                        max(1.0, rest_deadline - time.monotonic()),
                        resting=True,
                    )

            if state != "filled":
                # Unfilled/partial at timeout — cancel and inspect: the cancel
                # race may reveal a fill or partial (honor filled_quantity).
                c_qty, c_pps, c_final = self._cancel_and_inspect(order_id)
                if c_qty > filled_qty:
                    filled_qty, fill_pps = c_qty, c_pps
                if c_final == "filled":
                    state = "filled"
                # Session 9 verify-pass: if the cancel could NOT be confirmed
                # (API error / unknown state), the resting order may still be
                # live — placing attempt 2 would risk a double sell. Escalate
                # instead and let the user check Robinhood.
                if c_final in ("unknown", "error") and c_qty <= filled_qty:
                    logger.error(
                        f"Cancel UNCONFIRMED for {position.ticker} sell order "
                        f"{order_id} (state={c_final}) — NOT re-pricing; the "
                        f"order may still be live on Robinhood"
                    )
                    # Session 16: remember it. If this contract then reaches
                    # expiry, the expiry booking must NOT write it off at
                    # $0.00 — this order may have filled.
                    position.sell_state_unknown = True
                    if self.notifier:
                        self.notifier.notify_error(
                            f"🚨 **CANCEL UNCONFIRMED** — sell order for "
                            f"**{position.ticker}** ${position.strike} "
                            f"{position.direction} may still be resting on "
                            f"Robinhood. Check manually before intervening."
                        )
                    filled_contracts = int(filled_qty + 0.5)
                    if filled_contracts > 0:
                        if fill_pps <= 0:
                            fill_pps = position.current_price
                        weighted_pps_sum += fill_pps * filled_contracts
                        total_filled += filled_contracts
                    break

            # Session 9 verify-pass: a "filled" status with 0 reported quantity
            # is a data glitch, not a zero fill — re-poll once, then fall back
            # to the requested quantity (assuming 0 would re-sell contracts
            # that actually sold, which is the worse failure).
            if state == "filled" and filled_qty <= 0:
                time.sleep(1)
                retry = self.executor.check_order_status(order_id)
                retry_qty = float(retry.get("filled_quantity", 0) or 0)
                retry_pps = float(retry.get("average_price_per_share", 0) or 0)
                if retry_qty > 0:
                    filled_qty, fill_pps = retry_qty, retry_pps
                else:
                    logger.warning(
                        f"Order {order_id} reports filled with 0 quantity — "
                        f"assuming full fill of {qty_this_attempt} @ last "
                        f"polled price"
                    )
                    filled_qty = float(qty_this_attempt)

            filled_contracts = int(filled_qty + 0.5)
            if filled_contracts > 0:
                if fill_pps <= 0:
                    logger.warning(
                        f"No per-share fill price reported for {position.ticker} "
                        f"sell order {order_id} — booking at last polled price "
                        f"${position.current_price:.2f}"
                    )
                    fill_pps = position.current_price
                weighted_pps_sum += fill_pps * filled_contracts
                total_filled += filled_contracts

            if total_filled >= qty_target:
                break

            # Session 17 / BUG-38: report the time actually SPENT, not the
            # timeout we intended. On 2026-07-31 this line read "0/1 filled
            # within 771s" for an order that lived seven seconds — it did not
            # merely fail to reveal the bug, it argued against looking for one.
            attempt_elapsed = time.monotonic() - attempt_started
            logger.warning(
                f"Sell attempt {sell_attempt} for {position.ticker}: "
                f"{filled_contracts}/{qty_this_attempt} filled after "
                f"{attempt_elapsed:.0f}s (budget {attempt_timeout:.0f}s)"
                + (" — retrying remainder with urgent pricing" if sell_attempt == 1 else "")
            )

            # Session 16: when attempt 2 is about to REST, the loud "manual
            # exit needed" escalation below is ~11 minutes away — too late to
            # act on. Warn now, while there is still a market to act in. This
            # is deliberately notify_status, not notify_error: nothing has
            # failed yet, and the error channel is for things that have.
            # `_rest_timeout_seconds` is asked again here because attempt 2
            # has not computed its own window yet; the two answers differ by
            # the length of this notification and agree to the printed minute.
            prospective_rest = (
                self._rest_timeout_seconds()
                if (sell_attempt == 1 and rest_until_close) else None
            )
            if prospective_rest is not None:
                deadline_min = prospective_rest / 60.0
                logger.warning(
                    f"0DTE EXIT NOT FILLED YET: {position.ticker} "
                    f"${position.strike} {position.direction} × "
                    f"{qty_target - total_filled} — resting a limit order for "
                    f"up to {deadline_min:.0f} more minutes; manual exit is "
                    f"still possible until then"
                )
                if self.notifier:
                    self.notifier.notify_status(
                        f"⏳ **0DTE EXIT — NO FILL YET**: **{position.ticker}** "
                        f"${position.strike} {position.direction} × "
                        f"{qty_target - total_filled}\n"
                        f"No bid at the first attempt. Resting a limit order "
                        f"for up to **{deadline_min:.0f} min** (until just "
                        f"before the close). You can still exit by hand on "
                        f"Robinhood until then."
                    )

        if total_filled >= qty_target and total_filled > 0:
            # CONFIRMED FULL CLOSE — book P&L from the ACTUAL fill.
            avg_pps = weighted_pps_sum / total_filled
            realized_pnl = (avg_pps - position.entry_price) * 100 * total_filled
            with self._lock:  # 2026-08-04 (B7): += is read-modify-write
                self.daily_pnl += realized_pnl
            if self.decision_engine:
                self.decision_engine.record_realized_pnl(realized_pnl)
            self._record_day_trade_after_fill(position, is_same_day, is_0dte, "close")
            position.current_price = avg_pps
            if position.entry_price > 0:
                position.pnl_pct = (
                    (avg_pps - position.entry_price) / position.entry_price * 100
                )
            position.contracts_remaining = 0
            self._log_trade("CLOSE", position, reason=reason, pnl=realized_pnl)
            # Session 16: see the twin in the trim path — a confirmed fill
            # answers the question an unconfirmed cancel left open.
            position.sell_state_unknown = False
            # Day 4 review round 1: a confirmed fill proves the broker is
            # working — a leftover backoff or queued exit is now stale.
            position.exit_backoff_until = 0.0
            position.pending_caller_exit = False
            logger.info(
                f"EXIT CONFIRMED: {position.ticker} × {total_filled} filled "
                f"@ ${avg_pps:.2f} avg | Realized: ${realized_pnl:+.2f}"
            )
            # Session 12 (GO_LIVE B2): notify_exit existed since Session 9 and
            # was never called from anywhere — the paper branch pings on every
            # simulated exit while a REAL stop-out sold real contracts in
            # silence. First live day would have been indistinguishable from
            # nothing happening.
            if self.notifier:
                self.notifier.notify_exit(
                    position.ticker, reason, position.pnl_pct, realized_pnl
                )
            return True

        if total_filled > 0:
            # PARTIAL FILL — book what actually sold, keep managing the rest.
            avg_pps = weighted_pps_sum / total_filled
            realized_pnl = (avg_pps - position.entry_price) * 100 * total_filled
            with self._lock:  # 2026-08-04 (B7): += is read-modify-write
                self.daily_pnl += realized_pnl
            if self.decision_engine:
                self.decision_engine.record_realized_pnl(realized_pnl)
            # A partial sell is still a sell — it counts as the day trade.
            self._record_day_trade_after_fill(position, is_same_day, is_0dte, "partial close")
            position.contracts_remaining -= total_filled
            # Session 9 verify-pass: log the partial as TRIM, not CLOSE — the
            # restore path classifies by last OPEN/CLOSE event, and a CLOSE
            # here would make the still-open remainder restore as "manual"
            # (no stops, skipped by the 0DTE sweep) after a restart.
            self._log_trade(
                "TRIM", position,
                reason=f"{reason} (PARTIAL EXIT {total_filled}/{qty_target} — "
                       f"{position.contracts_remaining} still open)",
                pnl=realized_pnl,
            )
            # Round 2 (R2-3): a partial exit's remainder also invites a
            # manual finish — the expiry booking must know.
            position.had_failed_exit = True
            logger.error(
                f"PARTIAL SELL: {position.ticker} ${position.strike} "
                f"{position.direction} — {total_filled}/{qty_target} filled "
                f"@ ${avg_pps:.2f}, {position.contracts_remaining} contract(s) "
                f"still open and managed | Trigger: {reason}"
            )
            if self.notifier:
                self.notifier.notify_error(
                    f"🚨 **PARTIAL SELL** — **{position.ticker}** ${position.strike} "
                    f"{position.direction}: {total_filled}/{qty_target} filled, "
                    f"**{position.contracts_remaining} still open** (kept under "
                    f"management)\nTrigger: {reason} | Check Robinhood."
                )
            return False

        # Nothing filled at all.
        #
        # Day 4 (BUG-40): before escalating, ask the broker whether the
        # position even EXISTS. On 2026-08-03 the operator sold AAL by hand; the bot's
        # book still said 3 contracts, its trail fired, every sell failed to
        # place, and the monitor re-fired the confirmed exit every ~11s tick —
        # two attempts and a 🚨 webhook per cycle until a restart. The STARTUP
        # restore already answers this correctly by checking real holdings;
        # this is the same check at runtime, under Session 17's proof rule:
        # only an authenticated, complete, 2xx answer saying "not held" may
        # book the external close. "Can't reach the broker" ≠ "position gone".
        # Review round 1, two guards on the guard:
        #
        # 1. If OUR OWN order state is unknown (a cancel that never
        #    confirmed — `sell_state_unknown`), the walk proving "absent" may
        #    be proving that OUR order filled, not that the operator sold by hand.
        #    Booking that as an external close with P&L 0 falsifies the
        #    ledger, skips the day-trade record and misinforms the operator.
        #    Session 16's expiry booking refuses this exact state; so does
        #    this. The position stays tracked with the backoff.
        #
        # 2. The walk takes seconds. A caller re-entry of the same contract
        #    can merge into this Position meanwhile (`open_position` reuses
        #    the key), and zeroing the merged contracts would orphan a LIVE
        #    holding. The size is snapshotted before the walk and
        #    `_book_external_close` refuses if it moved.
        verdict: Optional[bool] = None
        qty_snapshot = position.contracts_remaining
        if getattr(position, "sell_state_unknown", False):
            logger.warning(
                f"BUG-40 holdings check SKIPPED for {position.ticker}: our "
                f"own order state is unknown (unconfirmed cancel) — absence "
                f"could be our own fill. Keeping the position tracked."
            )
        elif not self.paper_trade:
            try:
                verdict = self.executor.position_exists_at_broker(
                    position.ticker,
                    position.strike,
                    position.expiry,
                    position.direction,
                )
            except Exception as probe_err:  # noqa: BLE001
                logger.warning(
                    f"BUG-40 holdings check errored for {position.ticker}: "
                    f"{probe_err} — treating as undetermined"
                )
        refused_stale = False
        if verdict is False:
            if self._book_external_close(position, reason, qty_snapshot):
                return True
            # Size moved under the walk (re-entry merge) — the evidence is
            # stale. Fall through to the backoff branch as undetermined.
            verdict = None
            refused_stale = True

        # Broker still holds it (True) or we could not tell (None): the
        # position stays tracked, but price-inferred exits back off so the
        # next attempt is minutes away, not the next 11-second tick.
        # Instructions (caller exits, the 0DTE sweep) are not gated by this —
        # each new instruction attempt re-runs the check above.
        backoff_s = 0.0
        try:
            backoff_s = float(self.config.get("sell_fail_backoff_seconds", 300) or 0)
        except (TypeError, ValueError):
            backoff_s = 300.0
        if backoff_s > 0:
            position.exit_backoff_until = time.time() + backoff_s
        # 2026-08-04 (C2): remember that this position had a failed exit and a
        # "manual exit required" escalation — if it later reads as absent at
        # expiry time, a manual sale is a live possibility.
        position.had_failed_exit = True
        if refused_stale:
            held_note = (
                "broker showed the contract gone but the position size "
                "changed mid-check — treating as unverified"
            )
        elif verdict is True:
            held_note = "broker still shows the position"
        else:
            held_note = "broker holdings could not be verified"
        logger.error(
            f"SELL FAILED ({orders_placed} order(s) placed, none filled): "
            f"{position.ticker} ${position.strike} "
            f"{position.direction} × {position.contracts_remaining} | "
            f"Reason was: {reason} | {held_note} | Position remains open — "
            f"MANUAL EXIT NEEDED"
            + (
                f" | price-inferred retries paused {backoff_s:.0f}s"
                if backoff_s > 0
                else ""
            )
        )
        if self.notifier:
            self.notifier.notify_error(
                f"🚨 **SELL FAILED** — could not close **{position.ticker}** "
                f"${position.strike} {position.direction} × {position.contracts_remaining}\n"
                f"Trigger: {reason} | {held_note} | "
                f"**Manual exit required on Robinhood!**"
                + (
                    f"\n_Automatic retries pause for {backoff_s / 60:.0f} min "
                    f"— caller exits still act immediately._"
                    if backoff_s > 0
                    else ""
                )
            )
        return False

    def _book_external_close(self, position: Position, trigger_reason: str,
                             qty_snapshot: Optional[int] = None) -> bool:
        """The broker PROVABLY holds no such contract: it was closed outside
        the bot (a manual sale). Book a CLOSE the restore path will respect,
        say so once, and return True so the caller removes the position.

        Day 4 (BUG-40). Three rules, all learned the hard way:

          - This may ONLY be called on a False verdict from
            position_exists_at_broker — an authenticated, complete walk. A
            fix that makes a position merely LOOK closed is a fail-to-exit
            bug (Session 16), and the restore path classifies by the last
            ledger event, so a wrongly-written CLOSE row would also stop the
            position from ever being restored.

          - The evidence must still describe THIS position. The walk takes
            seconds; a re-entry of the same contract key can merge into this
            Position meanwhile. If the size moved since `qty_snapshot` was
            taken (before the walk), the verdict is stale — refuse (return
            False) and let the caller fall back to the backoff.

          - P&L is NOT invented. The bot never saw the fill; Robinhood's
            history is the truth. pnl_usd is 0.0 with a reason that names it
            external, and nothing feeds the circuit breaker — a fabricated
            figure from the last mark would be fiction with a decimal point
            (Session 16: book and shout, never guess).
        """
        key = self._display_key(position)
        with self._lock:
            qty = position.contracts_remaining
            if qty_snapshot is not None and qty != qty_snapshot:
                logger.error(
                    f"EXTERNAL CLOSE ABORTED for {key}: size changed "
                    f"{qty_snapshot} → {qty} while the broker walk ran "
                    f"(re-entry merge?) — the absence evidence is stale. "
                    f"Position stays tracked."
                )
                return False
            position.contracts_remaining = 0
        logger.error(
            f"EXTERNAL CLOSE: {key} × {qty} — authenticated broker check "
            f"shows no such contract held. Closed outside the bot; booking "
            f"external close with P&L unknown. Trigger was: {trigger_reason}"
        )
        self._log_trade(
            "CLOSE",
            position,
            reason=f"external_close (was: {trigger_reason})",
            pnl=0.0,
        )
        self.save_position_state()
        if self.notifier:
            self.notifier.notify_error(
                f"👋 **{position.ticker}** ${position.strike} "
                f"{position.direction} × {qty} was closed **outside the bot** "
                f"— the broker shows no position, so the {trigger_reason} "
                f"exit stands down and the book is reconciled.\n"
                f"Booked as external close with **P&L unknown** (0 in the "
                f"ledger) — your Robinhood history has the real figure; "
                f"correct trades.json by hand if you want the EOD number "
                f"right."
            )
        return True

    def _execute_trim(self, position: Position, contracts_to_sell: int, reason: str) -> bool:
        """Session 9 verify-pass: same exit mutual exclusion as full exits —
        a trim mid-confirmation must not race a concurrent full exit."""
        with self._lock:
            if position.exit_in_flight:
                logger.info(
                    f"TRIM SKIPPED for {position.ticker}: an exit is already "
                    f"in flight (trigger was: {reason})"
                )
                # Round 2 (L5): full caller exits queue behind a flight;
                # trims deliberately do not (a stale partial re-fired later
                # can over-sell a changed book). But silence here left the
                # book diverged from the caller with no word to anyone.
                if self.notifier and reason == "caller_proportional_trim":
                    self.notifier.notify_status(
                        f"✂️ Caller trim for **{position.ticker}** arrived "
                        f"while another exit attempt was mid-flight — NOT "
                        f"queued (only full exits queue). If the trim still "
                        f"matters, mirror it manually."
                    )
                return False
            # Session 16: a previous trim's cancel was never confirmed, so an
            # order for these contracts may still be live. Selling again could
            # sell them twice. A full EXIT is deliberately still allowed —
            # that path has its own unconfirmed-cancel handling, and refusing
            # to close a position because a trim once glitched would be the
            # worse failure. Same reasoning exempts a trim that IS a close:
            # with one contract left, `_execute_trim_inner` escalates to a
            # full exit (H4a), and blocking that would silently drop the
            # caller's instruction to get out.
            blocked = (
                getattr(position, "trim_blocked_unconfirmed", False)
                and position.contracts_remaining > 1
            )
            if blocked:
                # Latched like the PDT block above it: failed tier trims retry
                # every ~5s by design, and this state persists for the rest of
                # the session, so an unlatched log line is a permanent stream
                # and an unlatched webhook is a flood.
                today_iso = market_time.trading_date().isoformat()
                first_today = position.trim_blocked_notified_date != today_iso
                position.trim_blocked_notified_date = today_iso
                if first_today:
                    logger.warning(
                        f"TRIM BLOCKED for {position.ticker}: an earlier trim's "
                        f"cancel was never confirmed and the order may still be "
                        f"live (trigger was: {reason}). Restart the bot once "
                        f"Robinhood shows the true position."
                    )
                    if self.notifier:
                        self.notifier.notify_error(
                            f"✂️ **TRIM SKIPPED** — **{position.ticker}**: an "
                            f"earlier trim's cancel was never confirmed, so we "
                            f"are not selling again over a possibly-live order. "
                            f"We are now out of step with the caller on this "
                            f"position until you check Robinhood and restart."
                        )
                else:
                    logger.debug(
                        f"TRIM BLOCKED (latched today) for {position.ticker}"
                    )
                return False
            position.exit_in_flight = True
        try:
            return self._execute_trim_inner(position, contracts_to_sell, reason)
        finally:
            with self._lock:
                position.exit_in_flight = False

    def _execute_trim_inner(self, position: Position, contracts_to_sell: int, reason: str) -> bool:
        """
        Trim a specific number of contracts from the position.
        Same-day trims also count as day trades (any sell of a same-day buy).

        Session 9: returns True only when at least one contract's sale is
        CONFIRMED filled; False on failure (nothing booked, nothing marked).
        """
        if contracts_to_sell <= 0:
            return False

        # Session 9 (H4a): a trim against our last contract used to clamp to
        # 0 and silently no-op. The caller is reducing — with one contract
        # left, the only way to follow is a full exit.
        if position.contracts_remaining <= 1:
            logger.info(
                f"Trim requested for {position.ticker} but only "
                f"{position.contracts_remaining} contract(s) left — "
                f"executing full exit instead"
            )
            # Session 9 verify-pass: call the INNER exit — this thread already
            # holds the exit_in_flight guard via the trim wrapper.
            return self._execute_full_exit_inner(position, "caller_trim_last_contract")

        if contracts_to_sell >= position.contracts_remaining:
            contracts_to_sell = position.contracts_remaining - 1

        # PDT check: same-day trim of a non-0DTE position = day trade
        # Session 9: trading-date/expiry math via market_time (US/Eastern).
        is_same_day = self._is_same_trading_day(position)
        is_0dte = self._is_0dte_today(position)

        if is_same_day and not is_0dte and self.decision_engine:
            if not position.day_trade_recorded:
                # First sell of this same-day position would be a day trade
                remaining = self.decision_engine.get_day_trades_remaining()
                if remaining <= 0:
                    # Session 9 verify-pass: latch the notification — since M4,
                    # failed tier trims retry every monitor tick (~5s), which
                    # would spam an @-mention webhook until the PDT window
                    # rolls. Notify once per trading day per position.
                    today_iso = market_time.trading_date().isoformat()
                    first_block_today = position.pdt_trim_blocked_date != today_iso
                    position.pdt_trim_blocked_date = today_iso
                    if first_block_today:
                        logger.warning(
                            f"PDT BLOCK: Trimming {position.ticker} same-day would be a "
                            f"day trade but we're at the limit. Skipping trim."
                        )
                        if self.notifier:
                            self.notifier.notify_error(
                                f"⚠️ Caller trimmed {position.ticker} but we can't follow — "
                                f"same-day sell would trigger PDT. Holding to next day."
                            )
                    else:
                        logger.debug(
                            f"PDT BLOCK (latched today): skipping trim of {position.ticker}"
                        )
                    return False
            # If day_trade_recorded is already True, we can trim freely
            # (this position's round trip was already counted)

        logger.info(
            f"TRIM: {position.ticker} | Selling {contracts_to_sell} of "
            f"{position.contracts_remaining} | Reason: {reason}"
        )

        log_trade_execution(
            action="TRIM",
            ticker=position.ticker,
            direction=position.direction,
            strike=position.strike,
            expiry=position.expiry,
            contracts=contracts_to_sell,
            price=position.current_price,
            pnl_pct=position.pnl_pct,
            reason=reason,
        )

        # Paper trade: log but don't touch Robinhood
        if self.paper_trade:
            sim_pnl = (
                (position.current_price - position.entry_price)
                * 100
                * contracts_to_sell
            )
            logger.info(
                f"[PAPER] Would trim {contracts_to_sell}x {position.ticker} | "
                f"Reason: {reason} | Simulated P&L: ${sim_pnl:+.2f}"
            )
            if self.notifier:
                self.notifier.notify_status(
                    f"📝 **PAPER TRIM** *(not executed)*\n"
                    f"**{position.ticker}** ${position.strike} {position.direction} "
                    f"× {contracts_to_sell} | Reason: {reason} | "
                    f"Simulated P&L: **${sim_pnl:+.2f}**"
                )
            with self._lock:  # 2026-08-04 (B7): += is read-modify-write
                self.daily_pnl += sim_pnl
            # Session 9 (C1): bridge realized P&L to the engine.
            if self.decision_engine:
                self.decision_engine.record_realized_pnl(sim_pnl)
            position.contracts_remaining -= contracts_to_sell
            # Session 12 (GO_LIVE B6): paper trims were never written to the
            # ledger — the paper CLOSE branch logs, this one returned first.
            # Every restart re-read size from the last event and inflated a
            # trimmed position back to full contracts, and the paper week's
            # realised P&L (the evidence for the go-live decision) was missing
            # every trim. Logged AFTER the decrement so the row's
            # contracts_remaining reflects the post-trim book, which is what
            # `_scan_trade_log` reads for size on restore.
            self._log_trade("TRIM", position, reason=f"[PAPER] {reason}", pnl=sim_pnl)
            return True

        # --- Session 9 (C2): place → CONFIRM FILL → cancel-inspect ---
        # Single attempt for trims; on failure return False without booking
        # or marking anything (tiers/caller counts stay retryable).
        order_id = self.executor.sell_option_position(
            ticker=position.ticker,
            strike=position.strike,
            expiry=position.expiry,
            direction=position.direction,
            contracts=contracts_to_sell,
        )

        if not order_id:
            # Day 4 review round 1: without a backoff, a tier trim that fails
            # to place retries every ~6s tick (Session 9 M4 deliberately does
            # not latch tier trims) — the AAL fire→fail loop through the trim
            # door. Same pause as the full-exit path; caller trims are
            # one-shot and unaffected beyond the pause being recorded.
            try:
                _bo = float(self.config.get("sell_fail_backoff_seconds", 300) or 0)
            except (TypeError, ValueError):
                _bo = 300.0
            if _bo > 0:
                position.exit_backoff_until = time.time() + _bo
            position.had_failed_exit = True  # R2-3
            logger.error(
                f"Trim order failed to place for {position.ticker} × "
                f"{contracts_to_sell} ({reason})"
                + (f" — price-inferred retries paused {_bo:.0f}s" if _bo > 0 else "")
            )
            return False

        state, filled_qty, fill_pps = self._confirm_sell_fill(order_id)

        cancel_unconfirmed = False
        if state != "filled":
            # Cancel and inspect — the cancel race may reveal a (partial) fill.
            c_qty, c_pps, c_final = self._cancel_and_inspect(order_id)
            if c_qty > filled_qty:
                filled_qty, fill_pps = c_qty, c_pps
            # Parity with the full-exit path: a cancel that comes back
            # "filled" means the race went the other way and the order is
            # done. Without this a fill reported with no quantity reads as
            # unfilled and the contracts are sold again.
            if c_final == "filled":
                state = "filled"
            # Session 16 (parity with _execute_full_exit_inner): if the cancel
            # could not be CONFIRMED, the order may still be resting at the
            # broker. The full-exit path already refuses to place a second
            # order in that state; the trim path returned a bare False, and a
            # bare False is an instruction to retry — profit tiers re-attempt
            # on the very next ~5s monitor tick (Session 9 / M4), and a caller
            # trim can be re-sent. That is a double sell waiting to happen.
            # Latch the position instead and hand it to a human.
            #
            # `c_qty <= filled_qty` is the full-exit path's test, not
            # `c_qty <= 0`. Review caught the difference: with a partial fill
            # already confirmed by polling, the looser test skipped the latch
            # AND returned before the booking below, so a contract that really
            # sold was never recorded and `contracts_remaining` stayed a lie.
            # A confirmed fill is banked either way; the latch is about
            # whether anything MORE may still be in flight.
            if c_final in ("unknown", "error") and c_qty <= filled_qty:
                cancel_unconfirmed = True
                position.trim_blocked_unconfirmed = True
                position.sell_state_unknown = True
                logger.error(
                    f"Cancel UNCONFIRMED for {position.ticker} trim order "
                    f"{order_id} (state={c_final}) — the order may still be "
                    f"live on Robinhood. Trims for this position are BLOCKED "
                    f"until the bot restarts or you clear it manually."
                )
                if self.notifier:
                    self.notifier.notify_error(
                        f"🚨 **CANCEL UNCONFIRMED** — trim order for "
                        f"**{position.ticker}** ${position.strike} "
                        f"{position.direction} may still be resting on "
                        f"Robinhood. Further trims on this position are "
                        f"blocked; check manually before intervening."
                    )
                # Fall through: whatever polling DID confirm is booked below,
                # exactly as the full-exit path banks its partial before it
                # breaks out of the retry loop.

        # Session 16 (parity): "filled" with a zero quantity is a data glitch,
        # not a zero fill. The full-exit path re-polls once and then assumes
        # the requested quantity, because assuming zero would re-sell
        # contracts that already sold. The trim path treated it as UNFILLED
        # and returned False — which, for a profit tier, means retrying the
        # sale of contracts the broker just sold.
        if state == "filled" and filled_qty <= 0:
            time.sleep(1)
            try:
                retry = self.executor.check_order_status(order_id) or {}
            except Exception as e:
                logger.warning(f"trim re-poll of {order_id} failed: {e}")
                retry = {}
            # Coerced separately: sharing one `try` meant a malformed PRICE
            # threw away a perfectly good QUANTITY and fell through to
            # "assume the full amount", which is a worse answer than the one
            # the broker just gave us.
            try:
                retry_qty = float(retry.get("filled_quantity", 0) or 0)
            except (TypeError, ValueError):
                retry_qty = 0.0
            try:
                retry_pps = float(retry.get("average_price_per_share", 0) or 0)
            except (TypeError, ValueError):
                retry_pps = 0.0
            if retry_qty > 0:
                filled_qty, fill_pps = retry_qty, retry_pps
            else:
                logger.warning(
                    f"Trim order {order_id} reports filled with 0 quantity — "
                    f"assuming the full {contracts_to_sell} @ last polled price"
                )
                filled_qty = float(contracts_to_sell)

        filled_contracts = int(filled_qty + 0.5)
        if filled_contracts <= 0:
            # Round 2 (M2): a trim that PLACES but never fills looped through
            # the other door — the tier is deliberately unlatched (Session 9
            # M4), so without a backoff this is one place/cancel cycle per
            # ~sell_fill_timeout for ever, each blocking the monitor thread.
            try:
                _bo = float(self.config.get("sell_fail_backoff_seconds", 300) or 0)
            except (TypeError, ValueError):
                _bo = 300.0
            if _bo > 0:
                position.exit_backoff_until = time.time() + _bo
            position.had_failed_exit = True  # R2-3
            logger.warning(
                f"TRIM UNFILLED: {position.ticker} × {contracts_to_sell} did "
                f"not fill within timeout — nothing sold, nothing booked "
                f"({reason}); "
                + (
                    "the cancel could NOT be confirmed, so the order may still "
                    "be live"
                    if cancel_unconfirmed else "order cancelled"
                )
                + (f" | price-inferred retries paused {_bo:.0f}s" if _bo > 0 else "")
            )
            return False
        # Session 12 (go-live review, nice-to-have #11): clamp BEFORE booking —
        # an over-reporting order status must not inflate realized P&L or
        # drive contracts_remaining below the true book.
        if filled_contracts > contracts_to_sell:
            logger.warning(
                f"Order status reported {filled_contracts} filled for a "
                f"{contracts_to_sell}-contract trim — clamping to the request."
            )
            filled_contracts = contracts_to_sell

        if fill_pps <= 0:
            logger.warning(
                f"No per-share fill price reported for {position.ticker} trim "
                f"order {order_id} — booking at last polled price "
                f"${position.current_price:.2f}"
            )
            fill_pps = position.current_price

        # Book realized P&L from the ACTUAL fill.
        realized_pnl = (fill_pps - position.entry_price) * 100 * filled_contracts
        with self._lock:  # 2026-08-04 (B7): += is read-modify-write
            self.daily_pnl += realized_pnl
        # Session 9 (C1): bridge realized P&L to the engine.
        if self.decision_engine:
            self.decision_engine.record_realized_pnl(realized_pnl)

        # Session 9 (M2 parity): record day trade AFTER the confirmed fill.
        self._record_day_trade_after_fill(position, is_same_day, is_0dte, "trim")

        position.contracts_remaining -= filled_contracts
        partial_note = (
            "" if filled_contracts == contracts_to_sell
            else f" (PARTIAL {filled_contracts}/{contracts_to_sell})"
        )
        self._log_trade("TRIM", position, reason=f"{reason}{partial_note}", pnl=realized_pnl)
        # Session 16: a confirmed fill re-establishes what the broker holds,
        # so an EARLIER unconfirmed cancel is no longer an open question. Left
        # set, this flag would block the expiry booking for ever and make the
        # loss invisible again — the very bug this session exists to fix.
        #
        # But not when THIS call is the one that could not confirm its cancel.
        # Review round three caught that: polling reports 1 of 2 filled, the
        # cancel then fails, and clearing here would announce the broker's
        # state is known when an order for the other contract may still be
        # live. The full-exit twin only clears on a confirmed FULL close for
        # the same reason.
        if not cancel_unconfirmed:
            position.sell_state_unknown = False
        # Day 4 review round 1: a confirmed fill proves the broker is working.
        position.exit_backoff_until = 0.0
        logger.info(
            f"TRIM CONFIRMED: {position.ticker} × {filled_contracts} filled "
            f"@ ${fill_pps:.2f} | Realized: ${realized_pnl:+.2f}"
            f"{partial_note}"
        )
        # Session 12 (GO_LIVE B2): same story as notify_exit — designed for
        # both modes, never called. A live trim now pings like a paper one.
        if self.notifier:
            trim_pnl_pct = (
                (fill_pps - position.entry_price) / position.entry_price * 100
                if position.entry_price > 0 else 0.0
            )
            self.notifier.notify_trim(
                position.ticker, filled_contracts,
                position.contracts_remaining, trim_pnl_pct,
            )
        return True

    def _log_trade(
        self, action: str, position: Position, reason: str = "", pnl: float = 0,
        timestamp: Optional[str] = None
    ):
        """
        Log trade to JSON file for record keeping.

        `timestamp` overrides the event time (ISO-8601, ET). Every real event
        happens now and leaves it None; Session 16's expiry reconcile is the
        exception, because an expiry booked on Monday morning HAPPENED at
        Friday's close, and dating it now would put Friday's loss in Monday's
        daily P&L and Monday's circuit-breaker budget.

        Session 9 (H13): the read-modify-write cycle is serialized by a
        class-level lock and written atomically (temp file + os.replace).
        A corrupt trades.json is quarantined loudly instead of failing
        forever silently.
        """
        # Session 14: remember closes so a later caller signal for the same
        # ticker can be answered with "we already closed it", not "the entry
        # was missed". Done here because this is the single point every close
        # passes through, paper and live alike.
        if action == "CLOSE":
            self._recently_closed[position.ticker] = {
                "at": market_time.now_et(),
                "reason": reason,
                "pnl_pct": position.pnl_pct,
            }

        entry = {
            "timestamp": timestamp or market_time.now_et().isoformat(),
            "action": action,
            "ticker": position.ticker,
            "direction": position.direction,
            "strike": position.strike,
            "expiry": position.expiry,
            "contracts": position.contracts,
            "contracts_remaining": position.contracts_remaining,
            "entry_price": position.entry_price,
            "current_price": position.current_price,
            "pnl_pct": round(position.pnl_pct, 2),
            "pnl_usd": round(pnl, 2),
            "reason": reason,
            "source": position.source,
            # Session 12 (GO_LIVE B5): every row states which mode wrote it.
            # Simulated and real money must never be indistinguishable in the
            # ledger — see ledger_row_matches_mode for the readers.
            "mode": "paper" if self.paper_trade else "live",
        }

        # BUG-20 fix: persist management config on OPEN so it survives restarts.
        # Without this, _restore_positions_from_robinhood had to guess defaults
        # (30% stop for challenge positions that should have no stop, empty rules).
        if action == "OPEN":
            entry["stop_loss_pct"] = position.stop_loss_pct
            entry["management_rules"] = position.management_rules
            entry["management_style"] = getattr(position, "management_style", "")

        corrupt_path = None
        with TradeManager._trade_log_lock:
            try:
                existing = []
                if self.trade_log_path.exists():
                    try:
                        existing = json.loads(self.trade_log_path.read_text())
                    except json.JSONDecodeError as e:
                        ts = market_time.now_et().strftime("%Y%m%d-%H%M%S")
                        corrupt_path = self.trade_log_path.with_name(
                            f"{self.trade_log_path.name}.corrupt-{ts}"
                        )
                        os.replace(self.trade_log_path, corrupt_path)
                        logger.error(
                            f"trades.json is corrupt ({e}) — quarantined to "
                            f"{corrupt_path}, starting a fresh trade log"
                        )
                        existing = []
                if not isinstance(existing, list):
                    logger.error(
                        f"trades.json root is {type(existing).__name__}, not a "
                        f"list — starting a fresh trade log"
                    )
                    existing = []
                existing.append(entry)
                tmp_path = self.trade_log_path.with_name(
                    self.trade_log_path.name + ".tmp"
                )
                tmp_path.write_text(json.dumps(existing, indent=2))
                os.replace(tmp_path, self.trade_log_path)
            except Exception as e:
                logger.error(f"Failed to log trade: {e}")

        # Notify OUTSIDE the file lock — webhook I/O must not block other
        # trade-log writers.
        if corrupt_path is not None and self.notifier:
            try:
                self.notifier.notify_error(
                    f"🚨 **trades.json CORRUPT** — quarantined to "
                    f"`{corrupt_path.name}` and started a fresh log. "
                    f"Position restore on next restart will NOT see older "
                    f"entries — review the quarantined file!"
                )
            except Exception as e:
                logger.error(f"Failed to send trades.json corruption alert: {e}")

    def get_status_summary(self) -> str:
        """Get a human-readable summary of all positions."""
        snapshot = self._positions_snapshot()
        if not snapshot:
            return "No open positions."

        lines = [f"=== Open Positions ({len(snapshot)}) ==="]
        for key, pos in snapshot:
            trail_status = "ACTIVE" if pos.trailing_stop_active else "inactive"
            # Round 2 (L1): a caller-stated runner floor was invisible in
            # !status — an armed instruction the operator couldn't see.
            _fp = float(getattr(pos, "profit_floor_price", 0.0) or 0.0)
            floor_note = (
                f" | Floor: ${_fp:.2f} "
                f"({'engaged' if getattr(pos, 'profit_floor_cleared', False) else 'pending'})"
                if _fp > 0 else ""
            )
            lines.append(
                f"  {pos.ticker} ${pos.strike}{pos.direction[0].upper()} "
                f"exp {pos.expiry} | "
                f"{pos.contracts_remaining}/{pos.contracts} contracts | "
                f"Entry: ${pos.entry_price:.2f} | "
                f"Now: ${pos.current_price:.2f} | "
                f"P&L: {pos.pnl_pct:+.1f}% | "
                f"Trail: {trail_status}{floor_note}"
            )
        lines.append(f"Daily P&L: ${self.daily_pnl:+.2f}")
        return "\n".join(lines)
