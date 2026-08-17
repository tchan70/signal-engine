"""Day 4 (2026-08-03) — trail removal, BUG-39, BUG-40, runner floor, TP posts.

Two live failures on the first Monday after go-live, one decision:

  BUG-39  caller_a posted a brokerage screenshot 14 seconds after the MRVL entry.
          The parser called it what it was ("status update … not an exit or
          trim"), but handle_caller_exit ran the entry-cooldown check BEFORE
          reading the signal type, so the status post was "suppressed" into a
          hard-coded -15% cooldown trail — which sold MRVL at +8.7% while the
          caller held to +60%.

  BUG-40  the operator sold AAL by hand. The bot's book still said 3 contracts; its
          trail fired, the sell could not place, and the monitor re-fired the
          confirmed exit every ~11s tick — two attempts and a 🚨 webhook per
          cycle until a restart. The STARTUP restore checks real holdings;
          the runtime path never did.

  Decision (the operator): trails are REMOVED. `enable_trailing_stop` — a config key
  that had been dead since Session 9 — is now the master switch, gating
  firing as well as arming. Caller-INSTRUCTED stops survive: a runner post's
  stated "N% profit SL" arms a STATIC floor at that level (entry × 1.5 for
  50%), and TP posts execute as exits even when phrased as plans.
"""
import json
import time as _time
_real_sleep = _time.sleep
import types
from datetime import datetime

import pytest

from execution.position import Position
from execution.paper import PaperExecutor as RobinhoodExecutor
from management.trade_manager import TradeManager
from utils import market_time
from utils.market_time import ET


# ── harness (self-contained; modeled on test_bug36's) ────────────────────────

class Quotes:
    def __init__(self, price=0.40):
        self.price = price

    def get_option_price(self, *a, **k):
        return self.price

    def __getattr__(self, name):
        return lambda *a, **k: None


class FailingSellExec(Quotes):
    """A broker whose sells never place, with a scripted holdings verdict."""

    def __init__(self, price=0.28, verdict=None):
        super().__init__(price)
        self.verdict = verdict
        self.sell_calls = 0
        self.verify_calls = 0

    def sell_option_position(self, *a, **k):
        self.sell_calls += 1
        return None

    def position_exists_at_broker(self, *a, **k):
        self.verify_calls += 1
        return self.verdict


@pytest.fixture()
def mid_session(monkeypatch):
    monkeypatch.setattr(market_time, "seconds_since_open", lambda *a, **k: 3600 * 6)
    monkeypatch.setattr(market_time, "is_market_hours", lambda *a, **k: True)
    monkeypatch.setattr(
        market_time, "now_et", lambda: datetime(2026, 8, 3, 12, 5, tzinfo=ET)
    )


def make_manager(config, executor, notices=None, *, trails_on=False, paper=True):
    # The real config ships enable_trailing_stop: false (the operator, 2026-08-03).
    # Tests opt back IN explicitly when they exercise the gated machinery —
    # never implicitly ("pin anything ambient").
    config["management"]["enable_trailing_stop"] = bool(trails_on)
    m = TradeManager(
        config, executor=executor, decision_engine=None,
        notifier=types.SimpleNamespace(
            notify_status=(notices.append if notices is not None else lambda *_: None),
            notify_error=(notices.append if notices is not None else lambda *_: None),
        ),
    )
    m.paper_trade = paper
    return m


def add_pos(m, *, ticker="MRVL", entry=0.23, current=0.23, hwm=None,
            armed=False, trigger=0.0, contracts=1, opened_at=None,
            rules=None, floor=0.0, floor_cleared=None):
    key = f"{ticker}_300.0_2026-08-21_call"
    if floor_cleared is None:
        floor_cleared = bool(floor)   # a floor set below market = engaged
    m.positions[key] = Position(
        ticker=ticker, direction="call", strike=300.0, expiry="2026-08-21",
        contracts=contracts, entry_price=entry, current_price=current,
        high_water_mark=hwm if hwm is not None else max(entry, current),
        pnl_pct=(current - entry) / entry * 100, stop_loss_pct=0.0,
        trailing_stop_active=armed, trailing_stop_price=trigger,
        profit_floor_price=floor, profit_floor_cleared=floor_cleared,
        management_rules=rules if rules is not None else {
            "strategy": "trailing_stop_only",
            "trailing_activation_pct": 60,
            "trailing_distance_pct": 30,
            "follow_caller_exits": True,
        },
        order_id="X", opened_at=opened_at or "2026-08-03T10:00:00-04:00",
        source="caller_a-challenge-challenge", contracts_remaining=contracts,
        caller_contracts=contracts, caller_contracts_remaining=contracts,
    )
    return m.positions[key], key


def catch_exits(m, monkeypatch):
    fired = []

    def _rec(k, p, reason, urgent=False, limit_price=None,
             rest_until_close=False, on_failure=None):
        fired.append(reason)
        return True   # "worker started" — consumed-flag semantics
    monkeypatch.setattr(m, "_spawn_exit_worker", _rec)
    return fired


def sync_exits(m, monkeypatch):
    """Run monitor-triggered exits synchronously (real code path, no thread)."""
    def _run(key, position, reason, urgent=False, limit_price=None,
             rest_until_close=False, on_failure=None):
        exited = m._execute_full_exit(
            position, reason, limit_price=limit_price, urgent=urgent
        )
        if exited:
            m.positions.pop(key, None)
        return True
    monkeypatch.setattr(m, "_spawn_exit_worker", _run)


def tick(m, q, price):
    q.price = price
    m.check_all_positions()


# ── 1. the master switch ─────────────────────────────────────────────────────

def test_switch_off_a_reached_activation_never_arms(config, mid_session):
    """+100% against a 60% activation — the pre-Day-4 code arms here."""
    q = Quotes()
    m = make_manager(config, q, trails_on=False)
    pos, _ = add_pos(m, entry=0.23, current=0.46, hwm=0.46)
    tick(m, q, 0.46)
    assert pos.trailing_stop_active is False


def test_switch_off_an_already_armed_trail_does_not_fire(config, mid_session,
                                                         monkeypatch):
    """The restored-position case the config sentinel cannot reach: old
    per-position rules AND an armed trail off disk. Three breaching ticks —
    with the switch off, nothing may fire."""
    q = Quotes()
    m = make_manager(config, q, trails_on=False)
    pos, key = add_pos(m, entry=0.23, current=0.35, hwm=0.45,
                       armed=True, trigger=0.32)
    fired = catch_exits(m, monkeypatch)
    for price in (0.30, 0.30, 0.30):
        tick(m, q, price)
    assert fired == []
    assert key in m.positions


def test_switch_on_the_same_scenario_fires(config, mid_session, monkeypatch):
    """The mutation test for the gate: delete the switch check in
    _check_trailing_stop and both this and the test above pass/fail
    identically — this pair only diverges through the gate itself."""
    q = Quotes()
    m = make_manager(config, q, trails_on=True)
    add_pos(m, entry=0.23, current=0.35, hwm=0.45, armed=True, trigger=0.32)
    fired = catch_exits(m, monkeypatch)
    for price in (0.30, 0.30):  # exactly the confirmation requirement
        tick(m, q, price)
    assert fired == ["trailing_stop"]


# ── 2. BUG-39: management signals never take the cooldown branch ─────────────

MRVL_STATUS = {
    "type": "management",
    "notes": "brokerage screenshot showing existing position in drawdown: "
             "-13.04%. This is a status update, not an exit signal.",
    "raw_message": "",
    "source_channel": "caller_a-challenge-challenge",
}


def test_the_mrvl_screenshot_arms_nothing_inside_the_cooldown(config,
                                                              mid_session):
    """The incident, replayed: management signal at position age 14s. Trails
    ENABLED and cooldown pinned to 120 to prove the fix is the ROUTING —
    the signal type is read first — not the dead trail or the zeroed
    cooldown in the live config."""
    config["management"]["entry_stop_cooldown_seconds"] = 120
    q = Quotes()
    m = make_manager(config, q, trails_on=True)
    pos, key = add_pos(m, opened_at="2026-08-03T12:04:46-04:00")  # 14s old
    m.handle_caller_exit("MRVL", dict(MRVL_STATUS))
    assert pos.trailing_stop_active is False, \
        "BUG-39: a status update armed the cooldown trail"
    assert key in m.positions


def test_a_real_exit_inside_the_cooldown_still_arms_the_trail_when_enabled(
        config, mid_session):
    """The cooldown branch's original job, preserved for configs that keep
    both the cooldown and trails."""
    config["management"]["entry_stop_cooldown_seconds"] = 120
    q = Quotes()
    m = make_manager(config, q, trails_on=True)
    pos, key = add_pos(m, opened_at="2026-08-03T12:04:46-04:00")
    m.handle_caller_exit("MRVL", {
        "type": "exit", "notes": "", "raw_message": "0.25 out MRVL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert pos.trailing_stop_active is True
    assert key in m.positions


def test_a_real_exit_inside_the_cooldown_executes_when_trails_are_off(
        config, mid_session):
    """With no trail to fall back on, suppressing the exit would leave the
    position with no exit path and no instruction — so mirror the caller."""
    config["management"]["entry_stop_cooldown_seconds"] = 120
    q = Quotes()
    m = make_manager(config, q, trails_on=False)
    pos, key = add_pos(m, opened_at="2026-08-03T12:04:46-04:00")
    m.handle_caller_exit("MRVL", {
        "type": "exit", "notes": "", "raw_message": "0.25 out MRVL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert key not in m.positions, "the exit should have been honoured"


def test_cooldown_zero_disables_the_branch(config, mid_session):
    """The live config's setting: an exit 5 seconds after entry executes."""
    config["management"]["entry_stop_cooldown_seconds"] = 0
    q = Quotes()
    m = make_manager(config, q, trails_on=True)
    pos, key = add_pos(m, opened_at="2026-08-03T12:04:55-04:00")  # 5s old
    m.handle_caller_exit("MRVL", {
        "type": "exit", "notes": "", "raw_message": "0.25 out MRVL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert key not in m.positions


# ── 3. TP posts execute ──────────────────────────────────────────────────────

def test_a_tp_management_post_is_promoted_to_a_full_exit(config, mid_session):
    """The AAL message, replayed against a held position."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    m.handle_caller_exit("AAL", {
        "type": "management", "notes": "suggesting a partial take profit",
        "raw_message": "Lets TP AAL 30%",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert key not in m.positions, "a TP post must sell"


def test_tp_in_the_parsers_notes_alone_does_not_promote(config, mid_session):
    """The parser routinely paraphrases holds with the words "take profit"
    ("not taking profit yet"). Only the caller's OWN text may sell."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    m.handle_caller_exit("AAL", {
        "type": "management",
        "notes": "caller_a suggesting to take profit (TP) on 30% of AAL",
        "raw_message": "",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert key in m.positions, "a notes paraphrase must never sell"


def test_a_tp_post_stating_a_partial_still_trims_not_exits(config,
                                                           mid_session):
    """Promotion composes with the trimmed-N override: "N/M" states a partial
    on its face, and selling all three would be the money bug."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30, contracts=3)
    m.handle_caller_exit("AAL", {
        "type": "management", "notes": "",
        "raw_message": "TP time — sold 1/3 AAL here",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert key in m.positions, "a stated partial must not close the book"
    assert m.positions[key].contracts_remaining == 2


# ── 4. the runner profit floor ───────────────────────────────────────────────

def test_the_floor_fires_only_after_two_confirmed_readings(config,
                                                           mid_session,
                                                           monkeypatch):
    """Price-inferred at fire time → the BUG-36 guard applies unchanged."""
    q = Quotes()
    m = make_manager(config, q, trails_on=False)
    pos, key = add_pos(m, entry=1.00, current=1.62, floor=1.50)
    fired = catch_exits(m, monkeypatch)
    tick(m, q, 1.45)
    assert fired == [], "one reading must not fire (BUG-36)"
    tick(m, q, 1.45)
    assert fired == ["profit_floor"]


def test_a_recovery_between_readings_resets_the_floor_count(config,
                                                            mid_session,
                                                            monkeypatch):
    q = Quotes()
    m = make_manager(config, q, trails_on=False)
    pos, key = add_pos(m, entry=1.00, current=1.62, floor=1.50)
    fired = catch_exits(m, monkeypatch)
    for price in (1.45, 1.55, 1.45):
        tick(m, q, price)
    assert fired == [], "non-consecutive breaches must not confirm"


def test_the_floor_is_not_gated_by_the_master_switch(config, mid_session,
                                                     monkeypatch):
    """The switch removes price exits the bot INVENTS. The floor is the
    caller's stated instruction — trails_on=False everywhere here."""
    q = Quotes()
    m = make_manager(config, q, trails_on=False)
    pos, key = add_pos(m, entry=1.00, current=1.62, floor=1.50)
    fired = catch_exits(m, monkeypatch)
    tick(m, q, 1.45)
    tick(m, q, 1.45)
    assert fired == ["profit_floor"]


def test_the_floor_never_ratchets(config, mid_session, monkeypatch):
    """Static means static: a run to 3x does not move the caller's level."""
    q = Quotes()
    m = make_manager(config, q, trails_on=False)
    pos, key = add_pos(m, entry=1.00, current=1.62, floor=1.50)
    fired = catch_exits(m, monkeypatch)
    for price in (2.00, 3.00, 2.50):
        tick(m, q, price)
    assert pos.profit_floor_price == pytest.approx(1.50)
    assert fired == []


def test_the_floor_survives_the_sidecar_round_trip(config, mid_session):
    """Losing the caller's stated stop on a restart would silently disarm an
    instruction — the BUG-20 class of failure."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_pos(m, entry=1.00, current=1.62, floor=1.50)
    m.save_position_state()
    rec = m.load_position_state()[key]
    assert rec["profit_floor_price"] == pytest.approx(1.50)
    assert rec["profit_floor_cleared"] is True


# ── 5. BUG-40: the sell-failure loop ─────────────────────────────────────────

def _armed_failing_setup(config, monkeypatch, verdict, notices=None):
    ex = FailingSellExec(price=0.28, verdict=verdict)
    m = make_manager(config, ex, notices, trails_on=True, paper=False)
    pos, key = add_pos(m, entry=0.20, current=0.35, hwm=0.45,
                       armed=True, trigger=0.32, contracts=3)
    sync_exits(m, monkeypatch)
    monkeypatch.setattr(_time, "sleep", lambda s: None)  # attempt-1 retry pause
    return m, ex, pos, key


def test_a_provably_absent_position_books_an_external_close_once(
        config, mid_session, monkeypatch):
    """The AAL incident with the fix: broker says (authenticated) the
    contract is not held → one external CLOSE row, one notification, the
    position leaves the book, and the next ticks do nothing at all."""
    notices = []
    m, ex, pos, key = _armed_failing_setup(config, monkeypatch, False, notices)
    for price in (0.30, 0.30):
        tick(m, ex, price)

    assert key not in m.positions, "the book must reconcile"
    rows = json.loads(m.trade_log_path.read_text())
    closes = [r for r in rows if r.get("action") == "CLOSE"]
    assert len(closes) == 1
    assert closes[0]["reason"].startswith("external_close")
    assert closes[0]["pnl_usd"] == 0.0, "P&L must not be invented"
    assert sum("outside the bot" in n for n in notices) == 1
    assert "AAL" not in m._recently_closed  # ticker here is MRVL
    assert pos.ticker in m._recently_closed, \
        "a later caller exit should get the 'already closed' wording"

    sells_before = ex.sell_calls
    for price in (0.30, 0.30, 0.30):
        tick(m, ex, price)
    assert ex.sell_calls == sells_before, "the loop must be dead"


def test_an_undetermined_verdict_keeps_the_position_and_backs_off(
        config, mid_session, monkeypatch):
    """"Can't reach the broker" is NOT proof of absence (Session 17). The
    position stays tracked, and the ~11s fire→fail loop becomes one attempt
    per backoff window."""
    notices = []
    m, ex, pos, key = _armed_failing_setup(config, monkeypatch, None, notices)
    for price in (0.30, 0.30):
        tick(m, ex, price)

    assert key in m.positions, "an unverifiable position must never be dropped"
    assert ex.sell_calls == 2, "attempt 1 + urgent attempt 2"
    assert pos.exit_backoff_until > _time.time()
    rows = json.loads(m.trade_log_path.read_text()) if m.trade_log_path.exists() else []
    assert not [r for r in rows if r.get("action") == "CLOSE"], \
        "no CLOSE may be booked without proof"

    sells_before = ex.sell_calls
    for price in (0.30, 0.30, 0.30, 0.30):
        tick(m, ex, price)
    assert ex.sell_calls == sells_before, \
        "BUG-40: the monitor re-fired inside the backoff window"


def test_a_still_held_position_also_backs_off(config, mid_session,
                                              monkeypatch):
    m, ex, pos, key = _armed_failing_setup(config, monkeypatch, True)
    for price in (0.30, 0.30):
        tick(m, ex, price)
    assert key in m.positions
    assert pos.exit_backoff_until > _time.time()


def test_the_backoff_expires_and_the_exit_retries(config, mid_session,
                                                  monkeypatch):
    """The backoff is a pause, not a surrender: once it lapses, two fresh
    confirmed readings fire the exit again."""
    m, ex, pos, key = _armed_failing_setup(config, monkeypatch, None)
    for price in (0.30, 0.30):
        tick(m, ex, price)
    assert ex.sell_calls == 2
    pos.exit_backoff_until = _time.time() - 1
    for price in (0.30, 0.30):
        tick(m, ex, price)
    assert ex.sell_calls == 4, "after backoff the exit must try again"


# ── 6. position_exists_at_broker: the proof rule ─────────────────────────────

class _Resp:
    def __init__(self, status, body=None, raise_json=False):
        self.status_code = status
        self._body = body
        self._raise = raise_json

    def json(self):
        if self._raise:
            raise ValueError("not json")
        return self._body


def test_negated_tp_intent_is_still_demoted(config, mid_session):
    """The two Day-4 changes must not compound: "Not going to TP" matches
    INTENT_RE on "going to … TP", and the verb-only exemption executed it.
    The directive test sees the leading "not" and the plan is held."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    m.handle_caller_exit("AAL", {
        "type": "exit", "notes": "",
        "raw_message": "Not going to TP AAL yet, letting it ride",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert key in m.positions


def test_a_tp_runner_post_without_stop_talk_still_holds(config, mid_session):
    """Pins the runner guard in tp_directive on its own: this phrasing has
    no SL/stop wording, so nothing else can mask a hold. (The variant WITH
    "50% profit SL" below is additionally caught by the stop-talk guard.)"""
    q = Quotes(price=1.62)
    m = make_manager(config, q)
    pos, key = add_pos(m, entry=1.00, current=1.62)
    m.handle_caller_exit("MRVL", {
        "type": "management", "notes": "",
        "raw_message": "TP hit — leaving 4 runners, letting them ride",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert key in m.positions, "a runner hold sold on its TP prefix"


def test_tp_hit_runner_post_holds_and_stores_the_floor(config, mid_session):
    """"TP hit — leaving 4 runners with a 50% profit SL": the naive match
    sold it, defeating the runner branch built in the same diff. Runner
    wording wins; the caller's stated level is stored."""
    q = Quotes(price=1.62)
    m = make_manager(config, q)
    pos, key = add_pos(m, entry=1.00, current=1.62)
    m.handle_caller_exit("MRVL", {
        "type": "management", "notes": "",
        "raw_message": "TP hit — leaving 4 runners with a 50% profit SL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert key in m.positions, "a runner hold sold on its TP prefix"
    assert pos.profit_floor_price == pytest.approx(1.50)
    # Round 2 (H2): NEVER cleared at arming — one arming-time reading is the
    # single-reading trust BUG-36 forbids. The monitor engages it (two
    # consecutive readings above), which for a genuinely-below-market floor
    # costs ~12 seconds.
    assert pos.profit_floor_cleared is False


def test_the_runner_level_is_read_from_the_callers_own_text(config,
                                                            mid_session):
    """First draft read only the parser's notes — a paraphrase that dropped
    the number lost the caller's stated stop entirely."""
    q = Quotes(price=1.62)
    m = make_manager(config, q)
    pos, key = add_pos(m, entry=1.00, current=1.62)
    m.handle_caller_exit("MRVL", {
        "type": "management",
        "notes": "Caller trimmed most and is leaving runners with a profit stop",
        "raw_message": "4 runners with a 50% profit SL @here",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert pos.profit_floor_price == pytest.approx(1.50)


def test_a_pending_floor_engages_only_after_two_readings_above(config,
                                                               mid_session,
                                                               monkeypatch):
    """A floor stored at/above the market must not fire on arrival (that
    would invert a hold into an exit off a stale price) — and must not be
    discarded either (that silently disarms an instruction). It engages
    after two consecutive readings above the level, then fires like any
    price-inferred exit."""
    q = Quotes()
    m = make_manager(config, q, trails_on=False)
    pos, key = add_pos(m, entry=1.00, current=0.80, floor=1.50,
                       floor_cleared=False)
    fired = catch_exits(m, monkeypatch)

    for price in (0.80, 0.70, 0.80):        # below the level: nothing
        tick(m, q, price)
    assert fired == []
    assert pos.profit_floor_cleared is False

    tick(m, q, 1.60)                        # one reading above: not yet
    assert pos.profit_floor_cleared is False
    tick(m, q, 1.45)                        # dip resets the clearance run
    tick(m, q, 1.60)
    tick(m, q, 1.60)                        # two consecutive: engaged
    assert pos.profit_floor_cleared is True
    assert fired == []

    tick(m, q, 1.45)                        # breach 1/2
    tick(m, q, 1.45)                        # breach 2/2 → fires
    assert fired == ["profit_floor"]


def test_own_unknown_order_state_blocks_the_external_close(config,
                                                           mid_session,
                                                           monkeypatch):
    """An unconfirmed cancel means OUR order may have filled — the walk
    proving 'absent' could be proving our own fill. No external close, no
    fabricated 'closed outside the bot'; keep tracked with the backoff
    (Session 16's expiry booking refuses the same state)."""
    m, ex, pos, key = _armed_failing_setup(config, monkeypatch, False)
    pos.sell_state_unknown = True
    for price in (0.30, 0.30):
        tick(m, ex, price)
    assert key in m.positions
    assert ex.verify_calls == 0, "the walk must not even run"
    rows = json.loads(m.trade_log_path.read_text()) if m.trade_log_path.exists() else []
    assert not [r for r in rows if r.get("action") == "CLOSE"]
    assert pos.exit_backoff_until > _time.time()


def test_external_close_refuses_stale_size_evidence(config, mid_session):
    """The walk takes seconds; a re-entry can merge into the Position
    meanwhile. If the size moved since the snapshot, the absence evidence
    is stale — refuse, keep tracked."""
    q = Quotes()
    m = make_manager(config, q, paper=False)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30, contracts=3)
    ok = m._book_external_close(pos, "trailing_stop", qty_snapshot=2)
    assert ok is False
    assert pos.contracts_remaining == 3, "must not zero merged contracts"
    rows = json.loads(m.trade_log_path.read_text()) if m.trade_log_path.exists() else []
    assert not [r for r in rows if r.get("action") == "CLOSE"]


def test_a_caller_exit_during_a_flight_queues_and_refires(config, mid_session,
                                                          monkeypatch):
    """A caller exit landing while another exit attempt holds exit_in_flight
    used to be consumed with an info log. It queues, and the monitor
    re-fires it as an instruction once the flight resolves."""
    notices = []
    q = Quotes(price=0.30)
    m = make_manager(config, q, notices)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    pos.exit_in_flight = True

    m.handle_caller_exit("AAL", {
        "type": "exit", "notes": "",
        "raw_message": "1.55 out AAL", "source_channel": "caller_a-challenge-challenge",
    })
    assert key in m.positions
    assert pos.pending_caller_exit is True
    assert any("queued" in n.lower() for n in notices)

    fired = catch_exits(m, monkeypatch)
    pos.exit_in_flight = False
    tick(m, q, 0.30)
    assert fired == ["caller_exit"]
    assert pos.pending_caller_exit is False, "the flag must be consumed"


def test_instructions_ignore_the_backoff(config, mid_session):
    """The design's central claim, pinned: the sell-failure backoff gates
    price-INFERRED exits only. A caller exit during the backoff window
    still closes the position immediately."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    pos.exit_backoff_until = _time.time() + 300
    m.handle_caller_exit("AAL", {
        "type": "exit", "notes": "",
        "raw_message": "1.55 out AAL", "source_channel": "caller_a-challenge-challenge",
    })
    assert key not in m.positions, "an instruction waited on the backoff"


def test_a_confirmed_fill_clears_backoff_and_pending(config, mid_session):
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    pos.exit_backoff_until = _time.time() + 300
    m.handle_caller_exit("AAL", {
        "type": "exit", "notes": "",
        "raw_message": "1.55 out AAL", "source_channel": "caller_a-challenge-challenge",
    })
    assert key not in m.positions
    assert pos.exit_backoff_until == 0.0
    assert pos.pending_caller_exit is False


def _wait_tier_trim(pos, timeout=5.0):
    """B3 made tier trims asynchronous — wait for the worker to finish."""
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if not getattr(pos, "_tier_trim_thread_active", False):
            return
        _real_sleep(0.01)
    raise AssertionError("tier-trim worker did not finish")


def test_a_failed_tier_trim_backs_off_instead_of_looping(config, mid_session,
                                                         monkeypatch):
    """Tier trims are price-inferred and deliberately un-latched (Session 9
    M4) — without a backoff they reproduce the AAL fire→fail loop through
    the trim door."""
    ex = FailingSellExec(price=2.00)
    m = make_manager(config, ex, trails_on=False, paper=False)
    rules = {"strategy": "tiered_profit_taking",
             "profit_tiers": [{"gain_pct": 50, "trim_pct": 50}],
             "follow_caller_exits": True}
    pos, key = add_pos(m, entry=1.00, current=2.00, contracts=4, rules=rules)
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    tick(m, ex, 2.00)
    tick(m, ex, 2.00)      # tier confirmed → trim placed → fails
    _wait_tier_trim(pos)
    assert ex.sell_calls >= 1, "the tier trim never attempted"
    sells = ex.sell_calls
    assert pos.exit_backoff_until > _time.time()

    for _ in range(3):
        tick(m, ex, 2.00)
        _wait_tier_trim(pos)
    assert ex.sell_calls == sells, "the trim loop survived the backoff"


# ── 8. review round 2: the fixes to the fixes ────────────────────────────────
# Round 2 attacked round 1's fixes and found: the negation window was
# one-directional and same-line only; SL-less target talk sold; gerund/past
# tense TP was silently ignored; the floor cleared off ONE arming-time
# reading; clearance ticks weren't pass-aware; a placed-but-unfilled trim
# still looped; a queued caller exit could fire into a closed market; and the
# deferred exit_at_open flag was consumed by an in-flight exit.

@pytest.mark.parametrize("raw", [
    "TP not hit yet",                          # negation AFTER the token
    "TP later, holding for now",               # deferral after the token
    "Not going to\nTP yet, needs more time",   # line-broken negation
    "TP at 1.50",                              # target talk, no SL token
    "TP target 2.00",
    "TP at .50",                               # round 3 F2: bare-point decimal
    "TP around .30",
    "TP the rest at .80",                      # round 4 R4-1a: adjacency gap
    "Took profit at .50, TP at .80 for the rest",  # R4-1b: per-match exemption
    "TP'd half, TP at 1.20 for the rest",      # R4-1b: token-spanning edge
])
def test_round2_hold_shapes_do_not_sell(config, mid_session, raw):
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    m.handle_caller_exit("AAL", {
        "type": "management", "notes": "",
        "raw_message": raw, "source_channel": "caller_a-challenge-challenge",
    })
    assert key in m.positions, f"hold-shaped TP sold: {raw!r}"


@pytest.mark.parametrize("raw", [
    "Lets TP AAL 30%",             # the original incident
    "Lets TP 160% XSP @role",      # gain-percent vocabulary stays a directive
    "Taking profit on AAL here",   # gerund (round 2 M5)
    "Took profit on AAL",          # past tense
    "Took profit at 1.50",         # round 3 F3: past tense + price = a FILL
    "TP'd AAL at .55",             # past tense + sub-dollar price
])
def test_directive_tp_phrasings_still_sell(config, mid_session, raw):
    """The other half of every hold-guard: the real instructions must keep
    executing, or the guards have silently reintroduced the AAL bug."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    m.handle_caller_exit("AAL", {
        "type": "management", "notes": "",
        "raw_message": raw, "source_channel": "caller_a-challenge-challenge",
    })
    assert key not in m.positions, f"directive TP failed to sell: {raw!r}"


def test_floor_is_never_cleared_at_arming(config, mid_session):
    """Round 2 H2: one arming-time reading deciding 'cleared' is the
    single-reading trust BUG-36 forbids — a phantom at arming would engage a
    floor above the real market, which then sells the runner on the next two
    honest readings. Always pending; the monitor engages it."""
    q = Quotes(price=99.0)   # even an absurdly high quote must not clear it
    m = make_manager(config, q)
    pos, key = add_pos(m, entry=1.00, current=1.62)
    m.handle_caller_exit("MRVL", {
        "type": "management", "notes": "",
        "raw_message": "4 runners with a 50% profit SL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert pos.profit_floor_price == pytest.approx(1.50)
    assert pos.profit_floor_cleared is False


def test_a_pass_gap_voids_the_clearance_run(config, mid_session, monkeypatch):
    """Round 2 M1: two lone readings bridging a quote outage must not engage
    the floor — clearance is consecutive-pass counting, like breaches."""
    q = Quotes()
    m = make_manager(config, q, trails_on=False)
    pos, key = add_pos(m, entry=1.00, current=0.80, floor=1.50,
                       floor_cleared=False)
    catch_exits(m, monkeypatch)

    tick(m, q, 1.60)                 # clearance 1/2
    tick(m, q, None)                 # quote outage: pass runs, position skipped
    tick(m, q, 1.60)                 # NOT consecutive — must restart the run
    assert pos.profit_floor_cleared is False, \
        "two lone readings bridged an outage and engaged the floor"
    tick(m, q, 1.60)                 # consecutive with the previous pass
    assert pos.profit_floor_cleared is True


def test_runner_level_is_word_order_tolerant(config, mid_session):
    """Round 2 L2: "profit SL 50%" states the same number as "50% profit SL"."""
    q = Quotes(price=1.62)
    m = make_manager(config, q)
    pos, key = add_pos(m, entry=1.00, current=1.62)
    m.handle_caller_exit("MRVL", {
        "type": "management", "notes": "",
        "raw_message": "leaving 2 runners, profit SL 50%",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert pos.profit_floor_price == pytest.approx(1.50)


class PlacesButNeverFillsExec(Quotes):
    """Round 2 M2: an order that places, never fills, cancels clean."""

    def __init__(self, price=2.00):
        super().__init__(price)
        self.sell_calls = 0

    def sell_option_position(self, *a, **k):
        self.sell_calls += 1
        return f"ord-{self.sell_calls}"

    def check_order_status(self, order_id):
        return {"status": "queued", "filled_quantity": 0.0,
                "average_price_per_share": 0.0}

    def cancel_order(self, order_id):
        return {"cancelled": True, "filled_quantity": 0.0,
                "average_price_per_share": 0.0, "final_status": "cancelled"}


def test_a_placed_but_unfilled_tier_trim_backs_off(config, mid_session,
                                                   monkeypatch):
    """Round 2 M2: the fail-to-PLACE path got a backoff in round 1; the
    place-then-timeout path looped through the other door — one place/cancel
    cycle per timeout, forever, each blocking the monitor thread."""
    ex = PlacesButNeverFillsExec(price=2.00)
    config["management"]["sell_fill_timeout_seconds"] = 0.05
    m = make_manager(config, ex, trails_on=False, paper=False)
    rules = {"strategy": "tiered_profit_taking",
             "profit_tiers": [{"gain_pct": 50, "trim_pct": 50}],
             "follow_caller_exits": True}
    pos, key = add_pos(m, entry=1.00, current=2.00, contracts=4, rules=rules)
    monkeypatch.setattr(_time, "sleep", lambda s: None)

    tick(m, ex, 2.00)
    tick(m, ex, 2.00)      # tier confirmed → trim placed → times out → cancel
    _wait_tier_trim(pos)
    assert ex.sell_calls >= 1
    placed = ex.sell_calls
    assert pos.exit_backoff_until > _time.time(), \
        "a placed-but-unfilled trim set no backoff"
    for _ in range(3):
        tick(m, ex, 2.00)
        _wait_tier_trim(pos)
    assert ex.sell_calls == placed, "the place/cancel loop survived"


def test_a_queued_exit_drained_after_hours_defers_to_the_open(config,
                                                              monkeypatch):
    """Round 2 M3: a queue drained after 16:00 must convert to exit_at_open,
    not sell into a shut book and burn the instruction."""
    monkeypatch.setattr(market_time, "seconds_since_open", lambda *a, **k: 3600 * 6)
    monkeypatch.setattr(market_time, "is_market_hours", lambda *a, **k: False)
    monkeypatch.setattr(
        market_time, "now_et", lambda: datetime(2026, 8, 3, 16, 30, tzinfo=ET)
    )
    q = Quotes(price=0.30)
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    pos.pending_caller_exit = True
    pos.exit_in_flight = False

    fired = catch_exits(m, monkeypatch)
    tick(m, q, 0.30)
    assert fired == [], "sold into a closed market"
    assert pos.pending_caller_exit is False
    assert pos.exit_at_open is True
    assert pos.exit_at_open_reason == "caller_exit"

    # Round 3 (F1): one pass later the deferred block used to fire the
    # converted exit into the same shut book, logging "market is open and
    # settled" — the conversion just re-routed the bug through the other
    # door. The hours gate must hold it, flags intact, for as long as the
    # market stays shut.
    for _ in range(3):
        tick(m, q, 0.30)
    assert fired == [], "the deferred block fired into the closed market"
    assert pos.exit_at_open is True

    # And once the market opens, it fires exactly once.
    monkeypatch.setattr(market_time, "is_market_hours", lambda *a, **k: True)
    tick(m, q, 0.30)
    assert fired == ["caller_exit"]
    assert pos.exit_at_open is False


def test_a_deferred_exit_survives_an_in_flight_attempt(config, mid_session,
                                                       monkeypatch):
    """Round 2 M4: the deferred exit_at_open flag was cleared BEFORE the
    spawn, so a spawn refused on exit_in_flight consumed the instruction —
    the exact class round 1 fixed for live caller exits."""
    q = Quotes(price=0.30)
    m = make_manager(config, q)
    pos, key = add_pos(m, ticker="AAL", entry=0.20, current=0.30)
    pos.exit_at_open = True
    pos.exit_at_open_reason = "caller_exit"
    monkeypatch.setattr(
        m, "_spawn_exit_worker",
        lambda *a, **k: False,   # refused: another exit is in flight
    )
    tick(m, q, 0.30)
    assert pos.exit_at_open is True, "the deferred instruction was consumed"
    assert pos.exit_at_open_reason == "caller_exit"


def test_a_reposted_runner_note_does_not_demote_an_engaged_floor(config,
                                                                 mid_session):
    """Round 3 F4: an edit/backfill replay of the same runner note reset an
    ENGAGED floor to pending; with price at/below the level it could never
    re-clear and the caller's stop was silently dead."""
    q = Quotes(price=0.29)
    m = make_manager(config, q)
    pos, key = add_pos(m, entry=0.20, current=0.29, floor=0.30,
                       floor_cleared=True)
    m.handle_caller_exit("MRVL", {
        "type": "management", "notes": "",
        "raw_message": "4 runners with a 50% profit SL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert pos.profit_floor_cleared is True, "repost demoted an engaged floor"
    assert pos.profit_floor_price == pytest.approx(0.30)


def test_a_breakeven_runner_stop_is_a_real_level(config, mid_session):
    """Round 3 F5: "0% profit SL" = a stop at entry. A falsy-`or` dropped it
    and the notice then claimed no level was stated."""
    q = Quotes(price=1.62)
    m = make_manager(config, q)
    pos, key = add_pos(m, entry=1.00, current=1.62)
    m.handle_caller_exit("MRVL", {
        "type": "management", "notes": "",
        "raw_message": "runners with a 0% profit SL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert pos.profit_floor_price == pytest.approx(1.00)


def test_a_signed_runner_level_is_no_level(config, mid_session):
    """Round 4 R4-2: "-10% profit SL" must not arm a floor at entry×1.10 —
    a signed level parses as no level, the safe hold."""
    q = Quotes(price=1.62)
    m = make_manager(config, q)
    pos, key = add_pos(m, entry=1.00, current=1.62)
    m.handle_caller_exit("MRVL", {
        "type": "management", "notes": "",
        "raw_message": "runners with a -10% profit SL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert pos.profit_floor_price == 0.0
