"""Tracked open position - shared by all execution backends."""
from dataclasses import dataclass, field
from dataclasses import field


@dataclass
class Position:
    """Tracked open position."""
    ticker: str
    direction: str  # call / put
    strike: float
    expiry: str
    contracts: int
    entry_price: float
    current_price: float = 0.0
    high_water_mark: float = 0.0
    pnl_pct: float = 0.0
    stop_loss_pct: float = 30.0
    trailing_stop_active: bool = False
    trailing_stop_price: float = 0.0
    # Day 4: a STATIC stop at the profit level the CALLER
    # stated ("4 runners with a 50% profit SL" → entry × 1.5). Never ratchets,
    # no high-water-mark involvement, and deliberately NOT gated by
    # `enable_trailing_stop` — that switch removes price exits the bot
    # invents, not ones the caller instructs. 0.0 = not armed.
    profit_floor_price: float = 0.0
    # Day 4 review round 1: a floor stored at/above the market ENGAGES only
    # after two consecutive readings above the level (else arming off a
    # stale price would sell instantly). Persisted with the floor itself.
    profit_floor_cleared: bool = False
    # Day 4 (BUG-40): after a sell that failed to PLACE while the broker
    # still (or possibly still) holds the contract, price-inferred exits for
    # this position are paused until this monotonic-ish epoch. Runtime-only:
    # deliberately NOT persisted — a restart re-reconciles against the broker
    # anyway, which is a strictly better answer than a saved backoff.
    exit_backoff_until: float = 0.0
    # Day 4 review round 1: a caller exit that arrived while another exit
    # attempt held exit_in_flight used to be silently consumed ("EXIT
    # SKIPPED", info-level). Now it queues here and the monitor re-fires it
    # as an instruction once the in-flight attempt resolves without closing
    # the position. Runtime-only.
    pending_caller_exit: bool = False
    pending_caller_exit_limit: float = 0.0
    # 2026-08-04 (C2): set when a sell for this position failed and the
    # operator was told "manual exit required" — the precondition for a
    # manual sale the expiry booking must not write off at -100%. Runtime-
    # only: a restart re-reconciles against the broker at startup anyway.
    had_failed_exit: bool = False
    management_rules: dict = field(default_factory=dict)
    order_id: str = ""
    opened_at: str = ""
    source: str = ""
    contracts_remaining: int = 0  # After trims

    # Caller position tracking for proportional trimming
    caller_contracts: int = 0       # How many the caller originally bought
    caller_contracts_remaining: int = 0  # How many the caller still holds
    
    # Conviction tracking (for runner decisions)
    conviction_score: float = 0.0

    # Management style: "challenge", "managed", or "fire_and_forget".
    # Persisted to trades.json on OPEN so restarts don't lose it.
    management_style: str = ""

    # Whether this position was opened by the bot (True) or manually by the user (False).
    # Manually-restored positions are tracked for awareness but never auto-managed.
    bot_managed: bool = True

    # Session 10f: fractional remainder from proportional trims. The old
    # max(1, ...) floor over-mirrored small positions — a caller trimming 1 of
    # 10 (10%) against 3 of ours rounded up to 1 = 33%, every time, with no
    # memory. Carrying the remainder lets repeated small trims accumulate to a
    # whole contract instead of drifting.
    trim_carry: float = 0.0

    # Session 10f: a caller exit that arrived while the market was shut.
    # Options don't trade outside 9:30-16:00 ET, so the choice is between
    # booking a fill at a stale mark (which is fiction) and deferring. We
    # defer, and the monitor fires it once the session is open and settled.
    exit_at_open: bool = False
    exit_at_open_reason: str = ""
    # 2026-08-04 (C1/F1): a deferred caller TRIM carries its quantity so the
    # open-time firing can run the trim path — without these it degraded to a
    # full exit of the caller's held remainder.
    exit_at_open_trim_contracts: int = 0
    exit_at_open_trim_pct: float = 0.0
    exit_at_open_trim_notes: str = ""

    # PDT protection: position held overnight to avoid day trade flag
    pdt_held: bool = False              # True if we wanted to exit but PDT blocked it
    pdt_held_reason: str = ""           # Why we wanted to exit (stop_loss, trailing_stop, etc.)
    pdt_sell_next_open: bool = False    # Sell this at next market open
    day_trade_recorded: bool = False    # True if this position already counted as a day trade today
                                        # (multiple sells of same position = 1 round trip)

    # Session 15 (BUG-36): confirmation state for the AUTOMATED, price-
    # inferred exits, keyed by what is being confirmed ("stop", "trail",
    # "tier:0"): how many monitor readings have now agreed that the threshold
    # is breached, and the monitor pass each was seen on. The pass index is
    # what makes "consecutive" mean consecutive — a pass where this position
    # could not be evaluated at all (no quote, quote out of band, opening
    # settle, entry cooldown) leaves a gap, and a gap voids the run. Without
    # it two unrelated glitches minutes apart would confirm each other.
    #
    # `pending_high` is the highest reading still awaiting corroboration, so
    # a new high must be seconded before it ratchets the mark permanently.
    #
    # Deliberately absent from _state_record: a restart mid-breach starts the
    # count again, which is the correct blunt behaviour. None of this is a
    # ledger fact — losing it costs at most one extra ~6s tick.
    breach_ticks: dict = field(default_factory=dict)
    breach_pass: dict = field(default_factory=dict)
    pending_high: float = 0.0
    pending_high_pass: int = 0

    # Session 9: exit mutual exclusion. Sell confirmation can block for ~90s;
    # three threads (monitor stop/trail, caller-exit pipeline, 0DTE scheduler)
    # can all try to exit the same position. Set/cleared under TradeManager._lock.
    exit_in_flight: bool = False
    # Session 9: latch so a PDT-blocked tier trim notifies once per day instead
    # of every 5s monitor tick (failed tier trims retry by design since M4).
    pdt_trim_blocked_date: str = ""
    # Session 16: a trim whose cancel could NOT be confirmed. The order may
    # still be resting at the broker, so the usual "a failed trim retries on
    # the next tick" behaviour (profit tiers, M4) would risk selling the same
    # contracts twice. Latched until a human intervenes, exactly as the full-
    # exit path already refuses to re-price over an unconfirmed cancel.
    # Runtime-only, like exit_in_flight: after a restart the broker is
    # authoritative again and the position restores from what is actually held.
    trim_blocked_unconfirmed: bool = False
    # Latch for the notification above, same shape as pdt_trim_blocked_date:
    # the block persists all session and tier trims retry every ~5s.
    trim_blocked_notified_date: str = ""
    # Session 16: set when ANY sell for this position ended with a cancel we
    # could not confirm. The broker's true state is unknown — that order may
    # have filled — so no code may invent one. Its consumer is the expiry
    # booking, which refuses to write a $0.00 close over it: "expired
    # worthless" and "actually sold" are both live, and guessing writes a
    # false number into the ledger the circuit breaker reads.
    sell_state_unknown: bool = False
    # Latch for the "this expiry needs reconciling by hand" alert, so a
    # position carrying sell_state_unknown asks once rather than every sweep.
    expiry_reconcile_alerted: bool = False

    @property
    def cost_basis(self) -> float:
        return self.entry_price * 100 * self.contracts

    @property
    def current_value(self) -> float:
        return self.current_price * 100 * self.contracts_remaining


