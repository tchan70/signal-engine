"""Opening-bell settle (Session 10f).

Found in production on 2026-07-27, not by reading code. The tick at 09:30:01 ET
printed TE at $1.02 against a real $0.55:

    14:29:56  TE | Now: $0.55 | P&L:  -8.3% | HWM: $0.60 | Trail: inactive
    14:30:01  TE | Now: $1.02 | P&L: +70.0% | HWM: $1.02 | Trail: ACTIVE @ $0.82
    14:30:07  TRAILING STOP HIT: TE | Price: $0.55 | Trail: $0.82   -> closed -8.3%

S spiked in the same instant and touched $1.00+ on 4 ticks out of 6,171 that
day — every one of them inside a 17-second window at the open. The prints are
the stale overnight mark colliding with an illiquid opening quote.

Two separate harms, so two separate guards:
  - the phantom high ratchets the high-water mark PERMANENTLY (S still carries
    $1.07, 19% above anything it really traded, now persisted in the sidecar)
  - it can arm and immediately fire a trailing stop

Caller exits must NOT be gated — they arrive via handle_caller_exit, not this
loop. The settle suppresses our inferences about price, never the caller's
instructions.
"""
import types
from datetime import datetime, timedelta

import pytest

from execution.position import Position
from management.trade_manager import TradeManager
from utils import market_time
from utils.market_time import ET


class StubExec:
    """Replays a fixed price sequence, one value per poll."""

    def __init__(self, prices):
        self.prices = list(prices)
        self.calls = 0

    def get_option_price(self, *a, **k):
        p = self.prices[min(self.calls, len(self.prices) - 1)]
        self.calls += 1
        return p

    def __getattr__(self, name):
        return lambda *a, **k: None


def make_manager(config, prices, notices=None):
    # Day 4 (2026-08-03): live config disables trails (the operator); this file
    # tests machinery that lives behind the master switch, so pin it ON
    # ("pin anything ambient"). Switch-off behaviour is tested in
    # test_day4_trail_removal.py.
    config["management"]["enable_trailing_stop"] = True
    notifier = types.SimpleNamespace(
        notify_status=(notices.append if notices is not None else lambda *_: None),
        notify_error=(notices.append if notices is not None else lambda *_: None),
    )
    m = TradeManager(config, executor=StubExec(prices), decision_engine=None,
                     notifier=notifier)
    m.paper_trade = True
    return m


def add_te(m):
    """TE exactly as it stood that morning: entry 0.60, mark 0.55, trail idle."""
    key = "TE_7.0_2026-09-18_call"
    m.positions[key] = Position(
        ticker="TE", direction="call", strike=7.0, expiry="2026-09-18",
        contracts=1, entry_price=0.60, current_price=0.55,
        high_water_mark=0.60, pnl_pct=-8.3, stop_loss_pct=0.0,
        management_rules={"strategy": "trailing_stop_only",
                          "trailing_activation_pct": 60,
                          "trailing_distance_pct": 20,
                          "follow_caller_exits": True},
        order_id="PAPER", opened_at="2026-07-24T15:52:41-04:00",
        source="caller_a-challenge-challenge", contracts_remaining=1,
    )
    return key


def at_open(seconds_in):
    """A tz-aware ET datetime `seconds_in` seconds after a Monday 9:30 open."""
    base = datetime(2026, 7, 27, 9, 30, 0, tzinfo=ET)
    return base + timedelta(seconds=seconds_in)


@pytest.fixture()
def frozen(monkeypatch):
    """Pin now_et() so the settle window is deterministic."""
    def _set(seconds_in):
        monkeypatch.setattr(market_time, "now_et", lambda: at_open(seconds_in))
    return _set


# ── the helper ───────────────────────────────────────────────────────────────

def test_seconds_since_open_inside_the_session():
    assert market_time.seconds_since_open(at_open(0)) == 0
    assert market_time.seconds_since_open(at_open(45)) == 45
    assert market_time.seconds_since_open(at_open(3600)) == 3600


def test_seconds_since_open_is_none_outside_the_session():
    pre = datetime(2026, 7, 27, 9, 0, tzinfo=ET)
    post = datetime(2026, 7, 27, 16, 30, tzinfo=ET)
    weekend = datetime(2026, 7, 26, 10, 0, tzinfo=ET)   # a Sunday
    assert market_time.seconds_since_open(pre) is None
    assert market_time.seconds_since_open(post) is None
    assert market_time.seconds_since_open(weekend) is None


# ── the actual regression ────────────────────────────────────────────────────

def test_the_0727_spike_no_longer_closes_the_position(config, frozen):
    """Replay of the exact sequence that closed TE."""
    frozen(1)                                   # 09:30:01, inside the settle
    m = make_manager(config, prices=[1.02])
    key = add_te(m)
    m.check_all_positions()
    pos = m.positions.get(key)
    assert pos is not None, "position closed on the opening-bell print again"
    assert pos.trailing_stop_active is False, "trail armed on a phantom quote"


def test_the_phantom_high_does_not_ratchet_the_hwm(config, frozen):
    """The subtler harm: a one-tick high raises the mark permanently, and the
    sidecar then persists it."""
    frozen(1)
    m = make_manager(config, prices=[1.02])
    key = add_te(m)
    m.check_all_positions()
    assert m.positions[key].high_water_mark == 0.60


def test_after_the_window_normal_behaviour_resumes(config, frozen):
    """A real +70% an hour into the session must still arm the trail.

    Session 15 (BUG-36): arming still keys off P&L, so it happens on the
    first honest reading. What changed is the high-water mark, which now
    needs a corroborating reading before it ratchets — this settle guard
    covers 60 seconds, and the same phantom prints at 15:50.

    The trigger on that first pass is anchored to min(mark, price), so an
    uncorroborated print cannot set a trigger above the price and arm the
    trail straight into a fire.
    """
    frozen(3600)
    m = make_manager(config, prices=[1.02])
    key = add_te(m)
    m.check_all_positions()
    pos = m.positions[key]
    assert pos.high_water_mark == 0.60, "one reading must not ratchet the mark"
    assert pos.trailing_stop_active is True, "arming must not wait for a second tick"
    assert pos.trailing_stop_price == pytest.approx(0.48)   # min(0.60, 1.02)*0.8

    m.check_all_positions()
    assert pos.high_water_mark == 1.02, "a corroborated high must ratchet"
    assert pos.trailing_stop_price == pytest.approx(0.816)  # 1.02 * 0.8


@pytest.mark.parametrize("secs,gated", [
    (0, True), (1, True), (30, True), (59, True), (60, False), (120, False),
])
def test_the_window_boundary(config, frozen, secs, gated):
    frozen(secs)
    m = make_manager(config, prices=[1.02])
    key = add_te(m)
    # Two passes: outside the window the mark needs a corroborating reading
    # before it ratchets, and arming is computed from the mark (BUG-36).
    # Inside the window neither happens, however many passes run.
    m.check_all_positions()
    m.check_all_positions()
    armed = m.positions[key].trailing_stop_active
    assert armed is (not gated)


def test_a_stop_loss_is_also_held_during_the_settle(config, frozen):
    """The spike cuts both ways — a phantom LOW would trip a hard stop."""
    frozen(1)
    m = make_manager(config, prices=[0.10])
    key = add_te(m)
    m.positions[key].stop_loss_pct = 30.0
    m.check_all_positions()
    assert m.positions.get(key) is not None, "stopped out on an opening print"


# ── the caller must never be gated ───────────────────────────────────────────

def test_a_caller_exit_at_the_open_still_fires(config, frozen):
    """the operator's condition: settle the automated checks, but if a signal comes in
    at the open, act on it."""
    frozen(1)                                   # inside the settle window
    m = make_manager(config, prices=[0.55])
    key = add_te(m)
    m.handle_caller_exit("TE", {
        "type": "exit", "notes": "", "raw_message": "cut TE",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions.get(key) is None, "caller exit was suppressed by the settle"


def test_a_caller_exit_before_the_open_is_unaffected_by_the_helper(config, frozen):
    """seconds_since_open() returns None pre-market; that must not be read as
    'inside the window'."""
    monday_premarket = datetime(2026, 7, 27, 9, 0, tzinfo=ET)
    assert market_time.seconds_since_open(monday_premarket) is None


# ── the settle must not swallow the whole session ────────────────────────────

def test_positions_are_still_monitored_after_the_window(config, frozen):
    frozen(300)
    m = make_manager(config, prices=[0.70])
    key = add_te(m)
    m.check_all_positions()
    assert m.positions[key].current_price == 0.70, "price must update immediately"
    m.check_all_positions()                      # BUG-36: corroborating tick
    assert m.positions[key].high_water_mark == 0.70


def test_none_from_the_helper_means_not_gated(config, monkeypatch):
    """Outside market hours the monitor should behave normally, not freeze."""
    monkeypatch.setattr(market_time, "seconds_since_open", lambda *a, **k: None)
    m = make_manager(config, prices=[0.70])
    key = add_te(m)
    m.check_all_positions()
    m.check_all_positions()                      # BUG-36: corroborating tick
    assert m.positions[key].high_water_mark == 0.70


# ── market-hours guard on exits (Session 10f) ────────────────────────────────
#
# Entries have had one since Session 9. Exits never did, so a caller exit that
# arrived pre-market executed immediately — in paper, booking P&L off the
# previous session's stale mark, which is a fill that could not have happened.
#
# Live case, 2026-07-27 09:01 ET: "So good, we are going to TP DAL today"
# parsed as an exit 29 minutes before the bell. Had we held DAL we would have
# booked a fictional close, and his real "1.55 200% all out DAL" at 09:33 would
# have found an empty book.

PRE_MARKET = datetime(2026, 7, 27, 9, 1, tzinfo=ET)
MID_SESSION = datetime(2026, 7, 27, 12, 0, tzinfo=ET)
FRIDAY_NIGHT = datetime(2026, 7, 24, 20, 0, tzinfo=ET)


@pytest.fixture()
def clock(monkeypatch):
    def _set(dt):
        monkeypatch.setattr(market_time, "now_et", lambda: dt)
    return _set


def _caller_exit(m, kind="exit"):
    # NB: a real fill phrasing. "we are going to TP TE today" would now be
    # downgraded to management by the intent guard — see test_intent_guard.py.
    m.handle_caller_exit("TE", {
        "type": kind, "notes": "", "raw_message": "1.02 70% all out TE",
        "source_channel": "caller_a-challenge-challenge",
    })


def test_a_premarket_exit_is_not_booked_at_a_stale_mark(config, clock):
    """The DAL scenario. Must defer, not close."""
    clock(PRE_MARKET)
    notices = []
    m = make_manager(config, prices=[0.55], notices=notices)
    key = add_te(m)
    _caller_exit(m)
    assert m.positions.get(key) is not None, "closed while the market was shut"
    assert m.positions[key].exit_at_open is True
    assert any("market shut" in n.lower() or "next open" in n.lower() for n in notices)


def test_a_premarket_trim_defers_too(config, clock):
    clock(PRE_MARKET)
    m = make_manager(config, prices=[0.55])
    key = add_te(m)
    _caller_exit(m, kind="trim")
    assert m.positions[key].exit_at_open is True


def test_a_friday_night_exit_survives_the_weekend(config, clock):
    clock(FRIDAY_NIGHT)
    m = make_manager(config, prices=[0.55])
    key = add_te(m)
    _caller_exit(m)
    assert m.positions[key].exit_at_open is True


def test_an_in_session_exit_is_unaffected(config, clock):
    clock(MID_SESSION)
    m = make_manager(config, prices=[0.55])
    key = add_te(m)
    _caller_exit(m)
    assert m.positions.get(key) is None, "an in-hours exit should just execute"


def test_the_deferred_exit_fires_after_the_settle(config, frozen):
    frozen(120)                                  # open + 2 min, settle passed
    m = make_manager(config, prices=[0.55])
    key = add_te(m)
    m.positions[key].exit_at_open = True
    m.positions[key].exit_at_open_reason = "caller_exit"
    m.check_all_positions()
    import time as _t
    _t.sleep(0.3)                                # exit runs in a worker thread
    assert m.positions.get(key) is None or m.positions[key].contracts_remaining == 0


def test_the_deferred_exit_does_NOT_fire_during_the_settle(config, frozen):
    """Otherwise the queued exit sells straight into the phantom opening print
    the settle exists to ignore."""
    frozen(5)                                    # inside the settle window
    m = make_manager(config, prices=[1.02])
    key = add_te(m)
    m.positions[key].exit_at_open = True
    m.positions[key].exit_at_open_reason = "caller_exit"
    m.check_all_positions()
    import time as _t
    _t.sleep(0.3)
    assert m.positions.get(key) is not None
    assert m.positions[key].exit_at_open is True, "flag cleared before firing"



def test_stop_updates_are_not_deferred(config, clock):
    """They change state, they place no order — no reason to wait for the bell."""
    clock(PRE_MARKET)
    m = make_manager(config, prices=[0.55])
    key = add_te(m)
    m.handle_caller_exit("TE", {
        "type": "stop_update", "stop_level": 40, "notes": "",
        "raw_message": "moving stop to 40", "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions[key].exit_at_open is False
