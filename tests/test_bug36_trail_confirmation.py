"""BUG-36: no exit inferred from price may fire on a single reading.

Found in production on 2026-07-29 — the first day the bot traded real money —
not by reading code. XSP $710P, entry $0.26, trail armed with a $0.30 trigger:

    20:50:03  Now: $0.34  +30.8%  HWM: $0.43  Trail: ACTIVE @ $0.30
    20:50:09  Now: $0.40  +53.8%  HWM: $0.43  Trail: ACTIVE @ $0.30
    20:50:14  TRAILING STOP HIT | Price: $0.28 | Trail: $0.30
    20:50:16  EXIT CONFIRMED: XSP × 1 filled @ $0.37 | Realized: $+11.00
    20:57:28  caller_a: "Lets TP 160% XSP @role"

The $0.37 fill two seconds after the $0.28 reading is the proof. An urgent
sell prices off the bid, so it cannot fill at $0.37 into a book at $0.28: mid
went 0.40 -> 0.28 -> (a real book at 0.37+) inside seven seconds. A momentary
one-sided quote ten minutes before the close closed a live position, and the
caller took the same contract off at +160% seven minutes later.

What was NOT at fault:

  - the 60/30 trail parameters the operator set in Session 14. The genuine pullback
    that afternoon bottomed at $0.33 against a $0.30 trigger — the 30%
    distance was holding, exactly as designed, and would have carried the
    trade to the caller's exit.
  - _quote_is_sane. Its 10x band is for a quote belonging to a DIFFERENT
    contract (the 2026-07-28 QQQ incident). $0.28 on a $0.26 contract is
    entirely plausible for this one. The defect is not an implausible price,
    it is an unconfirmed one — which is why the guard here is separate.
  - the sell execution, which got $0.37 out of a $0.28 reading.

Same class as the Session 10f opening-bell spike (see test_open_settle.py),
which armed AND fired TE's trail on the 09:30:01 print. That was fixed with a
60-second window at the open; this covers the other six and a half hours.

Two independent confirmations, and one rule about who they apply to:

  1. N consecutive monitor readings must agree (management.exit_confirmation
     _ticks, default 2). This alone saves the XSP trade — the phantom was gone
     on the very next tick.
  2. One fresh quote at the moment of firing must agree too. Catches a glitch
     spanning both readings.
  3. INSTRUCTIONS ARE NEVER GATED. Caller exits, the deferred exit_at_open,
     the PDT next-day sell and the 15:45 0DTE sweep are decisions already
     taken. Only inferences from price wait for a second opinion.

The high-water mark gets the same treatment, for a different reason: it only
ever goes up, the sidecar persists it, and every future trigger is computed
from it. S has carried a phantom $1.07 since 2026-07-27.
"""
import types
from datetime import datetime

import pytest

from execution.position import Position
from management.trade_manager import TradeManager
from utils import market_time
from utils.market_time import ET


# ── harness ──────────────────────────────────────────────────────────────────

UNAVAILABLE = object()      # a re-quote the broker could not answer


class Quotes:
    """The broker.

    One monitor pass asks for a price once. A confirmation re-quote is the
    SECOND ask inside the same pass, so `requote` models what the book really
    says at the instant we would have sold — which is the whole question the
    2026-07-29 fill answered. `requote=None` means the re-quote agrees with
    the tick, which is what an honest quote does; `UNAVAILABLE` means the
    broker returned nothing.
    """

    def __init__(self, price=0.40):
        self.price = price
        self.requote = None
        self.calls_this_pass = 0
        self.total_calls = 0

    def get_option_price(self, *a, **k):
        self.calls_this_pass += 1
        self.total_calls += 1
        if self.calls_this_pass > 1 and self.requote is not None:
            return None if self.requote is UNAVAILABLE else self.requote
        return self.price

    def __getattr__(self, name):
        return lambda *a, **k: None


@pytest.fixture()
def mid_session(monkeypatch):
    """Mid-afternoon: past the opening settle, market open. The 2026-07-29
    incident was at 15:50 ET, which no time-based guard covers."""
    monkeypatch.setattr(market_time, "seconds_since_open", lambda *a, **k: 3600 * 6)
    monkeypatch.setattr(market_time, "is_market_hours", lambda *a, **k: True)
    monkeypatch.setattr(
        market_time, "now_et", lambda: datetime(2026, 7, 29, 15, 50, tzinfo=ET)
    )


def make_manager(config, quotes, notices=None):
    # Day 4 (2026-08-03): the LIVE config disables trails entirely
    # (enable_trailing_stop: false — the operator removed them). This file tests the
    # trail/confirmation MACHINERY, which stays in the code behind the master
    # switch, so the switch is pinned ON here ("pin anything ambient"). The
    # switch-off behaviour has its own tests in test_day4_trail_removal.py.
    config["management"]["enable_trailing_stop"] = True
    notifier = types.SimpleNamespace(
        notify_status=(notices.append if notices is not None else lambda *_: None),
        notify_error=(notices.append if notices is not None else lambda *_: None),
    )
    m = TradeManager(config, executor=quotes, decision_engine=None,
                     notifier=notifier)
    m.paper_trade = True
    return m


def add_xsp(m, *, entry=0.26, current=0.40, hwm=0.43, armed=True, trigger=0.30,
            stop=0.0, expiry="2026-07-31", rules=None):
    """XSP 710P as it stood at 20:50:09, one tick before the phantom."""
    key = f"XSP_710.0_{expiry}_put"
    m.positions[key] = Position(
        ticker="XSP", direction="put", strike=710.0, expiry=expiry,
        contracts=1, entry_price=entry, current_price=current,
        high_water_mark=hwm, pnl_pct=(current - entry) / entry * 100,
        stop_loss_pct=stop,
        trailing_stop_active=armed, trailing_stop_price=trigger,
        management_rules=rules if rules is not None else {
            "strategy": "trailing_stop_only",
            "trailing_activation_pct": 60,
            "trailing_distance_pct": 30,
            "follow_caller_exits": True,
        },
        order_id="LIVE", opened_at="2026-07-29T14:37:27-04:00",
        source="caller_a-challenge-challenge", contracts_remaining=1,
    )
    return m.positions[key], key


def catch_exits(m, monkeypatch):
    fired = []
    monkeypatch.setattr(
        m, "_spawn_exit_worker",
        lambda k, p, reason, urgent=False, limit_price=None: fired.append(reason),
    )
    return fired


def tick(m, q, price, requote=None):
    """One monitor pass with the book at `price`."""
    q.price = price
    q.requote = requote
    q.calls_this_pass = 0
    m.check_all_positions()


# ── 1. the incident, replayed ────────────────────────────────────────────────

# The real series from positions.log, 20:47:31 to 20:50:14. The dip in the
# middle is genuine; only the last value is fiction.
INCIDENT = [
    0.39, 0.40, 0.41, 0.42, 0.43, 0.40, 0.39, 0.40, 0.40, 0.40, 0.39, 0.39,
    0.38, 0.37, 0.36, 0.37, 0.38, 0.38, 0.38, 0.36, 0.36, 0.35, 0.34, 0.33,
    0.33, 0.33, 0.33, 0.34, 0.40,
    0.28,   # <- the phantom that closed it
]


def test_the_0729_phantom_no_longer_closes_the_position(config, mid_session,
                                                        monkeypatch):
    """The whole bug, end to end through the monitor."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.39, hwm=0.41)
    fired = catch_exits(m, monkeypatch)

    for price in INCIDENT:
        # The book was healthy throughout — proven by the $0.37 fill the real
        # bot got two seconds after reading $0.28.
        tick(m, q, price, requote=0.37 if price == 0.28 else None)

    assert fired == [], "the phantom tick closed the position again"
    assert m.positions.get(key) is not None


def test_the_genuine_dip_never_even_breached(config, mid_session, monkeypatch):
    """Worth pinning separately: the 30% distance was doing its job. The real
    low was $0.33 against a $0.30 trigger, so nothing about this trade needed
    the confirmation guard until fiction arrived."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.39, hwm=0.41)
    fired = catch_exits(m, monkeypatch)

    for price in INCIDENT[:-1]:          # everything except the phantom
        tick(m, q, price)

    assert fired == []
    assert pos.breach_ticks.get("trail", 0) == 0, "a genuine dip never breached"


def test_the_caller_exit_that_should_have_happened(config, mid_session,
                                                   monkeypatch):
    """The counterfactual, and the point of the whole fix: survive the
    phantom, and seven minutes later the caller's own exit takes the trade
    off — which on a mirror bot is the exit strategy."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.39, hwm=0.41)

    for price in INCIDENT:
        tick(m, q, price, requote=0.37 if price == 0.28 else None)
    assert m.positions.get(key) is not None

    q.price = 0.68                                    # "Lets TP 160% XSP"
    m.handle_caller_exit("XSP", {
        "type": "exit", "notes": "", "raw_message": "1.61 160% all out XSP",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions.get(key) is None, "caller exit did not close the mirror"


# ── 2. the trailing-stop counter ─────────────────────────────────────────────

def test_one_reading_below_the_trigger_does_not_fire(config, mid_session,
                                                     monkeypatch):
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    assert fired == []
    assert pos.breach_ticks["trail"] == 1


def test_two_consecutive_readings_do_fire(config, mid_session, monkeypatch):
    """The guard delays a real trail exit by one tick. It must not cancel one
    — a trail that never fires is not a disaster brake."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28)
    tick(m, q, 0.27)
    assert fired == ["trailing_stop"]


def test_a_recovery_resets_the_count(config, mid_session, monkeypatch):
    """Confirmation means CONSECUTIVE. Two phantoms an hour apart are two
    phantoms, not a confirmed breach."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    assert pos.breach_ticks["trail"] == 1
    tick(m, q, 0.40)
    assert pos.breach_ticks.get("trail", 0) == 0
    tick(m, q, 0.28, requote=0.37)
    assert fired == [], "two isolated phantoms must not add up to a breach"


def test_the_requote_vetoes_a_breach_that_did_not_survive(config, mid_session,
                                                          monkeypatch):
    """Second line of defence: a glitch that spans BOTH readings."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    tick(m, q, 0.28, requote=0.37)      # the book is fine; the feed is not
    assert fired == []
    assert pos.breach_ticks["trail"] == 0, "a veto must reset the count"


def test_a_vetoed_breach_adopts_the_honest_price(config, mid_session,
                                                 monkeypatch):
    """The re-quote is a real quote. Keeping it means P&L, the notification
    and the next sanity reference are the truth rather than the number we
    just refused to trade on."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    tick(m, q, 0.28, requote=0.37)
    assert pos.current_price == 0.37
    assert pos.pnl_pct == pytest.approx(42.3, abs=0.1)


def test_a_confirmed_exit_books_the_requote_not_the_trigger(config, mid_session,
                                                            monkeypatch):
    """The live log said "FULL EXIT ... P&L: 7.7%" off the phantom while the
    fill came back +42.3%. Whatever fires, the number we log and book has to
    be the freshest real one."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.29)      # still under the 0.30 trigger: fires
    tick(m, q, 0.28, requote=0.29)
    assert pos.current_price == 0.29


def test_an_unavailable_requote_does_not_block_the_exit(config, mid_session,
                                                        monkeypatch):
    """Two readings have already agreed. A stop that stops working because the
    broker went quiet is worse than a stop that fires."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28)
    tick(m, q, 0.28, requote=UNAVAILABLE)
    assert fired == ["trailing_stop"]


def test_an_implausible_requote_does_not_block_the_exit(config, mid_session,
                                                        monkeypatch):
    """Same reasoning. An out-of-band re-quote tells us nothing about the
    breach — Session 13 says never INFER from it, and we are not: the
    inference already came from two in-band readings."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28)
    tick(m, q, 0.28, requote=99.00)     # 350x — outside the sanity band
    assert fired == ["trailing_stop"]


# ── 3. the hard stop loss ────────────────────────────────────────────────────

def test_a_single_tick_does_not_trip_the_hard_stop(config, mid_session,
                                                   monkeypatch):
    """The phantom cuts both ways: on 2026-07-27 an opening print closed TE at
    -8.3% on a price that never traded."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, armed=False, trigger=0.0, stop=30.0)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.10, requote=0.38)
    assert fired == []


def test_two_ticks_do_trip_the_hard_stop(config, mid_session, monkeypatch):
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, armed=False, trigger=0.0, stop=30.0)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.10)
    tick(m, q, 0.10)
    assert fired == ["stop_loss"]


def test_a_recovering_price_resets_the_stop_count(config, mid_session,
                                                  monkeypatch):
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, armed=False, trigger=0.0, stop=30.0)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.10, requote=0.38)
    tick(m, q, 0.40)
    tick(m, q, 0.10, requote=0.38)
    assert fired == []


# ── 4. the high-water mark ───────────────────────────────────────────────────

def test_a_lone_spike_does_not_ratchet_the_mark(config, mid_session):
    """The permanent harm. S has carried a phantom $1.07 since 2026-07-27
    because one tick raised the mark and the sidecar wrote it down."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.40, hwm=0.43)

    tick(m, q, 1.02)
    assert pos.high_water_mark == 0.43
    tick(m, q, 0.40)
    assert pos.high_water_mark == 0.43


def test_a_corroborated_high_does_ratchet(config, mid_session):
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.40, hwm=0.43)

    tick(m, q, 0.50)
    tick(m, q, 0.50)
    assert pos.high_water_mark == 0.50


def test_a_rally_tracks_one_tick_behind_the_peak(config, mid_session):
    """Documented cost of the rule: the mark is the highest level two adjacent
    ticks both reached, so a rising series lags by one tick. 0.43 vs 0.42 on a
    30% trail moves the trigger by 0.007 — the harmless direction (wider)."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.40, hwm=0.40, armed=False, trigger=0.0)

    for price in (0.41, 0.42, 0.43):
        tick(m, q, price)
    assert pos.high_water_mark == 0.42


def test_a_spike_inside_a_rally_contributes_only_its_neighbour(config,
                                                               mid_session):
    """A phantom between two honest ticks must not leak its own value into
    the mark — only the level its neighbour also reached.

    $2.00 rather than something wilder on purpose: this has to be a phantom
    that _quote_is_sane ACCEPTS (5x, inside the 10x band), or the test proves
    the Session 13 guard works rather than this one.
    """
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.40, hwm=0.40, armed=False, trigger=0.0)

    tick(m, q, 0.41)
    tick(m, q, 2.00)        # phantom, in band
    tick(m, q, 0.42)
    assert pos.high_water_mark == 0.42
    assert pos.high_water_mark < 2.00, "the spike's own value reached the mark"


def test_the_requote_never_ratchets_the_mark(config, mid_session, monkeypatch):
    """A confirmation quote is a single unconfirmed reading. It may set the
    price; it may not set the peak."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.90)
    tick(m, q, 0.28, requote=0.90)
    assert pos.high_water_mark == 0.43
    assert pos.current_price == 0.90


# ── 5. instructions are never gated ──────────────────────────────────────────

def test_a_caller_exit_fires_mid_breach(config, mid_session, monkeypatch):
    """The line from Session 13, restated: this guard suppresses OUR
    inferences about price, never the caller's instructions. A caller exit
    arriving between the first and second breach readings must not inherit
    the wait."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)

    tick(m, q, 0.28, requote=0.37)
    assert pos.breach_ticks["trail"] == 1

    m.handle_caller_exit("XSP", {
        "type": "exit", "notes": "", "raw_message": "all out XSP",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions.get(key) is None


def test_a_deferred_exit_at_open_is_not_gated(config, mid_session):
    """It is a caller instruction that has already waited for the bell."""
    import time as _t
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    pos.exit_at_open = True
    pos.exit_at_open_reason = "caller_exit"

    tick(m, q, 0.40)
    _t.sleep(0.3)                       # the deferred exit runs in a worker
    assert m.positions.get(key) is None or pos.contracts_remaining == 0


def test_the_0dte_sweep_is_not_gated(config, mid_session):
    """15:45 ET is a clock, not a price. It fires on the first call.

    Session 16: the sweep DISPATCHES workers rather than selling inline (the
    final attempt may rest an order until just before the close, and that must
    not block the scheduler thread), so the return value counts dispatches and
    the close lands a moment later — same shape as the deferred exit_at_open
    test above. What is being asserted here is unchanged: no confirmation
    ticks were required before it acted.
    """
    import time as _t
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, expiry=market_time.trading_date().isoformat())

    assert m.force_exit_all_0dte() == 1
    for _ in range(40):                 # the exit runs in a worker
        if m.positions.get(key) is None:
            break
        _t.sleep(0.05)
    assert m.positions.get(key) is None


# ── 6. profit tiers ──────────────────────────────────────────────────────────

TIERED = {
    "strategy": "tiered_profit_taking",
    "profit_tiers": [{"gain_pct": 50, "trim_pct": 50}],
}


def test_a_phantom_high_does_not_trim(config, mid_session, monkeypatch):
    """A phantom HIGH sells real contracts, exactly as a phantom low closes a
    real position."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.30, hwm=0.30, armed=False, trigger=0.0,
                       rules=dict(TIERED))
    pos.contracts = 4
    pos.contracts_remaining = 4
    trims = []
    monkeypatch.setattr(m, "_execute_trim",
                        lambda p, n, reason: trims.append(n) or True)

    tick(m, q, 0.60, requote=0.30)      # +130% on one tick
    assert trims == []


def test_a_confirmed_tier_still_trims(config, mid_session, monkeypatch):
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.30, hwm=0.30, armed=False, trigger=0.0,
                       rules=dict(TIERED))
    pos.contracts = 4
    pos.contracts_remaining = 4
    trims = []
    monkeypatch.setattr(m, "_execute_trim",
                        lambda p, n, reason: trims.append(n) or True)

    tick(m, q, 0.60)
    tick(m, q, 0.60)
    assert trims == [2]


# ── 7. the knobs ─────────────────────────────────────────────────────────────

def test_the_guard_can_be_switched_off(config, mid_session, monkeypatch):
    """Both knobs off is the pre-Session-15 behaviour, byte for byte — the
    escape hatch if this ever proves worse than the bug."""
    q = Quotes()
    m = make_manager(config, q)
    m.config["exit_confirmation_ticks"] = 1
    m.config["exit_confirmation_requote"] = False
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    assert fired == ["trailing_stop"]


def test_the_knobs_are_independent(config, mid_session, monkeypatch):
    """A coherent middle setting, worth pinning because it is easy to break:
    fire on one reading, but only if a fresh quote agrees. On the 2026-07-29
    sequence this is still enough to save the trade."""
    q = Quotes()
    m = make_manager(config, q)
    m.config["exit_confirmation_ticks"] = 1
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    assert fired == [], "the re-quote must still veto with ticks disabled"
    tick(m, q, 0.28, requote=0.29)
    assert fired == ["trailing_stop"], "an agreeing re-quote must still fire"


def test_the_high_water_guard_has_its_own_switch(config, mid_session):
    """Decoupled on purpose. The fire delay costs a tick in a genuine
    collapse; the high-water guard costs nothing in a monotone move and
    prevents a permanent corruption. Turning the first down must not silently
    disable the second."""
    q = Quotes()
    m = make_manager(config, q)
    m.config["exit_confirmation_ticks"] = 0
    pos, key = add_xsp(m, current=0.40, hwm=0.43)

    tick(m, q, 1.02)
    assert pos.high_water_mark == 0.43, "the mark guard rode on the wrong knob"

    m.config["confirm_high_water_mark"] = False
    tick(m, q, 1.02)
    assert pos.high_water_mark == 1.02


def test_the_requote_can_be_switched_off_on_its_own(config, mid_session,
                                                    monkeypatch):
    """Ticks without the extra API call, for anyone counting requests."""
    q = Quotes()
    m = make_manager(config, q)
    m.config["exit_confirmation_requote"] = False
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    assert q.calls_this_pass == 1, "re-quote fired despite being disabled"
    tick(m, q, 0.28, requote=0.37)
    assert fired == ["trailing_stop"], "without a re-quote, ticks alone decide"


def test_a_malformed_setting_falls_back_to_two(config):
    q = Quotes()
    m = make_manager(config, q)
    for bad in ("", None, "banana", []):
        m.config["exit_confirmation_ticks"] = bad
        assert m._confirmation_ticks_required() in (0, 2)
    m.config["exit_confirmation_ticks"] = "3"
    assert m._confirmation_ticks_required() == 3


# ── 8. state hygiene ─────────────────────────────────────────────────────────

def test_the_counters_are_not_persisted(config, mid_session):
    """Runtime scaffolding, not state worth restoring. A restart mid-breach
    starting the count again is the correct blunt behaviour."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)

    tick(m, q, 0.28, requote=0.37)
    m.save_position_state()
    rec = m.load_position_state()[key]
    assert "breach_ticks" not in rec
    assert "pending_high" not in rec


def test_a_legacy_position_without_the_field_still_works(config, mid_session,
                                                         monkeypatch):
    """Restored objects and anything built before Session 15."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    pos.breach_ticks = None                    # as if the field never existed
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28)
    tick(m, q, 0.28)
    assert fired == ["trailing_stop"]


def test_a_gap_in_the_passes_voids_the_run(config, mid_session, monkeypatch):
    """Found by adversarial review, and the most dangerous thing in the first
    draft of this fix. Counting readings alone, "consecutive" was a lie:
    every early `continue` in the monitor loop — no quote, an out-of-band
    quote, the opening settle, the entry cooldown — skips the price checks
    without touching the counter. So a phantom at 15:50 left a 1 on the
    board, an eight-minute quote outage preserved it, and the next phantom
    "confirmed" it. Two single readings, minutes apart, closing a position.
    """
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    assert pos.breach_ticks["trail"] == 1

    q.get_option_price = lambda *a, **k: None       # the broker goes quiet
    for _ in range(100):                            # ~8 minutes of nothing
        m.check_all_positions()

    del q.get_option_price
    tick(m, q, 0.28, requote=0.37)
    assert fired == [], "two readings eight minutes apart confirmed each other"
    assert pos.breach_ticks["trail"] == 1, "the stale run was not discarded"


def test_the_opening_settle_does_not_bank_a_breach(config, monkeypatch):
    """The same hole, in the shape it would actually have happened in. A dip
    below the trigger at the end of one day, healthy prints through the next
    morning's settle window — which skips the checks — and the first
    post-settle reading closes the position on what is really one tick."""
    from utils import market_time as mt
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    monkeypatch.setattr(mt, "is_market_hours", lambda *a, **k: True)
    monkeypatch.setattr(mt, "now_et",
                        lambda: datetime(2026, 7, 29, 15, 50, tzinfo=ET))
    monkeypatch.setattr(mt, "seconds_since_open", lambda *a, **k: 3600 * 6)
    tick(m, q, 0.29, requote=0.37)                 # yesterday's one-tick dip
    assert pos.breach_ticks["trail"] == 1

    monkeypatch.setattr(mt, "seconds_since_open", lambda *a, **k: 5)
    for price in (0.35, 0.36, 0.35):               # healthy, inside the settle
        tick(m, q, price)

    monkeypatch.setattr(mt, "seconds_since_open", lambda *a, **k: 3600 * 6)
    tick(m, q, 0.28, requote=0.37)
    assert fired == [], "the settle window banked a breach instead of voiding it"


def test_the_stop_fires_at_the_exact_threshold_cent(config, mid_session,
                                                    monkeypatch):
    """Found by adversarial review. The branch tests P&L; an early draft of
    the re-quote tested price against entry*(1-pct/100). Algebraically equal,
    not equal in IEEE754: at entry $0.40 with a 30% stop the trigger price is
    exactly $0.28, and 0.28 <= 0.4*0.7 is False because 0.4*0.7 is
    0.27999999999999997. The stop was breached on every tick and vetoed on
    every tick, for ever, logging "did not survive a second look" about a
    price identical to the one that triggered it. Thirteen (entry, price)
    pairs do this at the default 30% stop alone.
    """
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.40, current=0.40, hwm=0.40,
                       armed=False, trigger=0.0, stop=30.0)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28)
    tick(m, q, 0.28)
    assert fired == ["stop_loss"], "the float boundary vetoed a real stop"


@pytest.mark.parametrize("entry,price", [
    (0.10, 0.07), (0.20, 0.14), (0.40, 0.28), (0.70, 0.49), (0.80, 0.56),
    (1.30, 0.91), (1.40, 0.98), (1.50, 1.05),
])
def test_the_float_boundary_across_real_premiums(config, mid_session,
                                                 monkeypatch, entry, price):
    """The same defect at every premium this account actually trades."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=entry, current=entry, hwm=entry,
                       armed=False, trigger=0.0, stop=30.0)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, price)
    tick(m, q, price)
    assert fired == ["stop_loss"]


def test_choppy_tape_still_ratchets_the_mark(config, mid_session):
    """Found by adversarial review. The first draft cleared the pending high
    on any reading at or below the mark, so a sawtooth never produced two
    ADJACENT highs and the mark never moved: a position up 60% carried a
    trail computed from its opening mark. Clearing on a gap in the passes is
    right; clearing on a dip is not."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.26, current=0.30, hwm=0.30,
                       armed=False, trigger=0.0)

    for price in (0.31, 0.29, 0.33, 0.29, 0.35, 0.29, 0.38, 0.29, 0.42):
        tick(m, q, price)

    assert pos.high_water_mark > 0.30, "the mark froze on choppy tape"
    assert pos.high_water_mark == pytest.approx(0.38)


def test_a_phantom_is_spent_not_banked(config, mid_session):
    """A phantom can second a genuine high that follows it — min() caps the
    mark at the real price, so the phantom's own value never lands. What it
    must NOT do is sit at its level for the rest of the session handing out
    free corroboration to every later reading."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.40, hwm=0.43, armed=False, trigger=0.0)

    tick(m, q, 2.00)          # phantom, in band
    assert pos.high_water_mark == 0.43
    tick(m, q, 0.44)          # real; the phantom seconds it, capped at 0.44
    assert pos.high_water_mark == 0.44
    tick(m, q, 0.46)          # must now need real corroboration again
    assert pos.high_water_mark == 0.44, "the phantom was still handing out seconds"
    tick(m, q, 0.47)
    assert pos.high_water_mark == 0.46


def test_arming_and_the_trigger_come_from_the_same_number(config, mid_session):
    """Found by adversarial review. Arming used to key off the raw price while
    the trigger was computed from the (lagging) mark, so a jumpy tick armed
    the trail and anchored it to a stale, much lower level — and since the
    trigger only ratchets up, that gap was permanent. Modelled on this exact
    path it turned a +19% exit into a -31% one."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.26, current=0.30, hwm=0.30,
                       armed=False, trigger=0.0)

    for price in (0.30, 0.45, 0.31, 0.30, 0.29):
        tick(m, q, price)
        if pos.trailing_stop_active:
            assert pos.trailing_stop_price == pytest.approx(
                pos.high_water_mark * 0.7
            ), "trigger anchored to a different number than the arming test"


def test_a_sustained_rise_still_arms_at_the_right_level(config, mid_session):
    """The control: arming off the mark must not mean never arming."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.26, current=0.30, hwm=0.30,
                       armed=False, trigger=0.0)

    tick(m, q, 0.45)
    tick(m, q, 0.45)
    assert pos.high_water_mark == 0.45
    assert pos.trailing_stop_active is True
    assert pos.trailing_stop_price == pytest.approx(0.315)


def test_no_requote_while_an_exit_is_in_flight(config, mid_session,
                                               monkeypatch):
    """Found by adversarial review. The re-quote ran on every tick once the
    count was reached, including for a position already being sold — a
    blocking broker call inside the monitor loop, which is the one place that
    stalls every other position's stop checks."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    catch_exits(m, monkeypatch)

    tick(m, q, 0.28)
    tick(m, q, 0.28)
    pos.exit_in_flight = True
    before = q.total_calls
    for _ in range(5):
        tick(m, q, 0.28)
    assert q.total_calls - before == 5, "re-quoted while an exit was in flight"


def test_a_veto_reaches_discord(config, mid_session, monkeypatch):
    """"The trail decided to exit and was overruled" is exactly the event a
    human should see. It can only happen at an exit moment, so it is not
    chatty."""
    notices = []
    q = Quotes()
    m = make_manager(config, q, notices=notices)
    pos, key = add_xsp(m)
    catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    tick(m, q, 0.28, requote=0.37)
    assert any("did not survive" in n for n in notices)


def test_a_confirmation_quote_does_not_raise_the_unmanaged_alarm(config,
                                                                 mid_session,
                                                                 monkeypatch):
    """Found by adversarial review. Routing the re-quote through
    _quote_is_sane made an out-of-band CONFIRMATION quote count towards the
    consecutive-rejection alarm and announce "this position is NOT being
    price-managed" — false here: the tick was fine and the exit fires
    anyway."""
    notices = []
    q = Quotes()
    m = make_manager(config, q, notices=notices)
    pos, key = add_xsp(m)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28)
    tick(m, q, 0.28, requote=99.00)
    assert fired == ["trailing_stop"]
    assert not any("NOT being price-managed" in n for n in notices)
    assert getattr(pos, "_quote_reject_total", 0) == 0


def test_a_blank_yaml_value_does_not_disable_the_guard(config):
    """Found by adversarial review. `int(x or 0)` turned a present-but-empty
    YAML key into 0 — the whole guard off, silently. A garbage string failed
    safe; a blank line failed open."""
    q = Quotes()
    m = make_manager(config, q)
    for blank in (None, ""):
        m.config["exit_confirmation_ticks"] = blank
        assert m._confirmation_ticks_required() == 2


def test_the_position_log_price_and_pnl_agree(config, mid_session,
                                              monkeypatch):
    """Found by adversarial review. positions.log took the price from the tick
    and the P&L from the position, so after a re-quote superseded the tick the
    row showed one quote's price beside another quote's P&L. This is the
    record both the 2026-07-28 and 2026-07-29 incidents were reconstructed
    from; it has to be internally consistent."""
    rows = []
    monkeypatch.setattr(
        "management.trade_manager.log_position_check",
        lambda **kw: rows.append(kw),
    )
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m)
    catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)
    assert rows, "nothing was logged"
    row = rows[-1]
    implied = (row["current_price"] - row["entry_price"]) / row["entry_price"] * 100
    assert implied == pytest.approx(row["pnl_pct"], abs=0.1)


# ── 9. defects found in the third review round ───────────────────────────────

def test_a_spike_during_the_entry_cooldown_does_not_arm_and_sell(config,
                                                                 monkeypatch):
    """Round 3, and the reason arming still keys off P&L.

    An intermediate version armed off the high-water mark so that arming and
    the trigger shared one number. But the mark is HISTORY, and the mark
    ratchets ABOVE the entry-cooldown check while arming happens below it. A
    spike inside the cooldown therefore armed retroactively the moment the
    cooldown expired, with a trigger far above the current price, and sold at
    +7.7% ninety seconds after entry — a position the caller still held.

    Day 4 (2026-08-03): the live config now sets entry_stop_cooldown_seconds
    to 0 (trails removed), so the cooldown this scenario depends on is pinned
    here — the guard still matters whenever a cooldown exists ("pin anything
    ambient", flip-day lesson).
    """
    from utils import market_time as mt
    q = Quotes()
    config["management"]["entry_stop_cooldown_seconds"] = 120
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.26, current=0.30, hwm=0.30,
                       armed=False, trigger=0.0)
    pos.opened_at = "2026-07-29T15:49:30-04:00"      # 30s old: inside cooldown
    fired = catch_exits(m, monkeypatch)

    monkeypatch.setattr(mt, "is_market_hours", lambda *a, **k: True)
    monkeypatch.setattr(mt, "seconds_since_open", lambda *a, **k: 3600 * 6)
    monkeypatch.setattr(mt, "now_et",
                        lambda: datetime(2026, 7, 29, 15, 50, tzinfo=ET))
    tick(m, q, 0.45)
    tick(m, q, 0.45)                                  # a corroborated spike

    monkeypatch.setattr(mt, "now_et",                 # cooldown has expired
                        lambda: datetime(2026, 7, 29, 15, 55, tzinfo=ET))
    for price in (0.28, 0.28, 0.28):
        tick(m, q, price)

    assert fired == [], "armed retroactively off a cooldown-era peak and sold"
    assert m.positions.get(key) is not None


def test_a_restored_phantom_peak_does_not_arm_and_sell(config, mid_session,
                                                       monkeypatch):
    """Round 3, same root cause, the shape that would have hit first. S is on
    disk right now carrying a phantom $1.07 high-water mark from
    2026-07-27. Arming off the mark sold it within two passes of startup."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.55, current=0.55, hwm=1.07,
                       armed=False, trigger=0.0)
    fired = catch_exits(m, monkeypatch)

    for price in (0.55, 0.55, 0.55):
        tick(m, q, price)

    assert fired == [], "a restored phantom peak armed the trail and sold"


def test_arming_never_sets_a_trigger_above_the_price(config, mid_session):
    """The belt to that braces: whatever the mark says, arming must not
    produce a trigger the current price is already through."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.26, current=0.42, hwm=5.00,
                       armed=False, trigger=0.0)

    tick(m, q, 0.42)
    if pos.trailing_stop_active:
        assert pos.trailing_stop_price < pos.current_price


def test_a_stale_pending_high_cannot_second_a_later_phantom(config,
                                                            mid_session,
                                                            monkeypatch):
    """Round 3. The pass-gap rule only voids a run when a pass is SKIPPED, so
    on a position evaluated every pass a phantom sat in pending_high
    indefinitely and vouched for a second phantom twenty passes later: the
    mark ratcheted on a single reading, and the trail then fired on the
    genuine book. Round 2's defect wearing a different hat."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, current=0.40, hwm=0.43, armed=False, trigger=0.0)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 1.07)                                  # phantom #1
    for price in (0.35, 0.38, 0.41, 0.33, 0.36, 0.39, 0.34, 0.37,
                  0.40, 0.35, 0.38, 0.33, 0.36, 0.39, 0.41, 0.34,
                  0.37, 0.40, 0.35, 0.38):            # 20 passes of real tape
        tick(m, q, price)
    tick(m, q, 0.60)                                  # phantom #2

    assert pos.high_water_mark <= 0.43, (
        "a twenty-pass-old phantom corroborated a new one"
    )
    assert fired == []


def test_choppy_tape_still_seconds_within_the_age_limit(config, mid_session):
    """The control for the rule above: the age limit must not be so tight
    that ordinary chop stops ratcheting again."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.26, current=0.30, hwm=0.30,
                       armed=False, trigger=0.0)

    for price in (0.31, 0.29, 0.33, 0.29, 0.35):
        tick(m, q, price)
    assert pos.high_water_mark > 0.30


@pytest.mark.parametrize("knob", [
    "confirm_high_water_mark", "exit_confirmation_requote",
])
@pytest.mark.parametrize("blank", [None, ""])
def test_a_blank_boolean_knob_fails_safe(config, knob, blank):
    """Round 3. `bool(config.get(name, True))` reads a present-but-blank YAML
    key as None and turns the guard OFF. An operator mid-edit should not
    silently disable a live-money guard."""
    q = Quotes()
    m = make_manager(config, q)
    m.config[knob] = blank
    assert m._guard_flag(knob) is True


@pytest.mark.parametrize("value,expected", [
    (False, False), ("false", False), ("no", False), ("off", False),
    (0, False), (True, True), ("true", True), ("yes", True), (1, True),
])
def test_explicit_boolean_knob_values_are_honoured(config, value, expected):
    q = Quotes()
    m = make_manager(config, q)
    m.config["confirm_high_water_mark"] = value
    assert m._guard_flag("confirm_high_water_mark") is expected


def test_veto_notices_are_throttled(config, mid_session, monkeypatch):
    """Round 3. A price oscillating around the trigger vetoes every other
    pass: 100 Discord posts in twenty minutes, each a blocking POST inside
    the monitor loop that stalls every other position's stop check. The log
    line stays unthrottled; the notification does not."""
    notices = []
    q = Quotes()
    m = make_manager(config, q, notices=notices)
    pos, key = add_xsp(m)
    catch_exits(m, monkeypatch)

    for _ in range(60):
        tick(m, q, 0.28, requote=0.37)

    held = [n for n in notices if "did not survive" in n]
    assert len(held) == 1, f"{len(held)} veto notices for one position"


# ── 10. defects found in the fourth review round ─────────────────────────────

def test_a_descending_glitch_cannot_disqualify_the_honest_requote(
        config, mid_session, monkeypatch):
    """Round 4, and the one that made layer 2 useless in exactly the case it
    exists for.

    The band was measured against `current_price`, which by confirmation time
    IS the suspect tick — the monitor writes it in before the checks run. So a
    DESCENDING glitch dragged the band down with it, the honest re-quote came
    back "implausible", and an implausible re-quote does not veto. The bot
    sold at $0.02 into a real $0.55 book.
    """
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.50, current=0.55, hwm=0.60, trigger=0.42)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.08, requote=0.55)      # 6.9x drop: inside the 10x tick band
    tick(m, q, 0.02, requote=0.55)      # within 10x of 0.08, so also accepted
    assert fired == [], "sold into a healthy book on two descending phantoms"


def test_the_confirmation_band_still_rejects_a_wrong_contract(config,
                                                              mid_session,
                                                              monkeypatch):
    """The control: anchoring on entry must not make the band useless. A
    quote from a different contract entirely is still refused as evidence,
    and refusing evidence still means firing on the readings we have."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.26)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28)
    tick(m, q, 0.28, requote=43.33)     # the 2026-07-28 QQQ price
    assert fired == ["trailing_stop"]


def test_a_runner_lock_does_not_liquidate_on_a_stale_peak(config, mid_session,
                                                          monkeypatch):
    """Round 4 invariant, restated for the Day-4 runner rewrite.

    The original defect: the runner path armed a profit-lock TRAIL from
    entry, the monitor's next ratchet recomputed the trigger from a stale
    high-water mark, and "keep the runners" inverted into an immediate exit.

    Day 4 (the operator, 2026-08-03): the runner path arms a STATIC floor at the
    caller's stated level and reads nothing from the high-water mark, so a
    poisoned mark cannot produce a trigger at all. The remaining hazard is
    the caller's own level sitting above OUR market (his fill ran further
    than ours): entry 0.50 × (1+50%) = 0.75 against a 0.60 market would sell
    within two confirmed passes. The arming guard refuses that case, holds
    with nothing armed, and says so. The invariant this test has always
    pinned — a hold instruction must never invert into an exit — is
    unchanged."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.50, current=0.60, hwm=1.00,
                       armed=False, trigger=0.0)
    pos.contracts = 4
    pos.contracts_remaining = 4
    fired = catch_exits(m, monkeypatch)

    m.handle_caller_exit("XSP", {
        "type": "management", "notes": "4 runners with a 50% profit SL",
        "raw_message": "4 runners with a 50% profit SL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert pos.trailing_stop_active is False, \
        "runner posts must not arm trails any more (Day 4)"
    # Review round 1: the level is STORED (losing a stated stop is the BUG-20
    # class) but must not have CLEARED — engaged-above-market is the exit
    # this test exists to forbid.
    assert getattr(pos, "profit_floor_price", 0.0) == pytest.approx(0.75)
    assert getattr(pos, "profit_floor_cleared", False) is False
    for price in (0.60, 0.60, 0.60):
        tick(m, q, price)

    assert fired == [], "a runner hold turned into an exit"
    assert m.positions.get(key) is not None


def test_every_arming_path_leaves_the_trigger_below_the_market(config,
                                                               mid_session):
    """The invariant behind both fixes above, stated once. Whatever arms a
    trail, the trigger it leaves behind must not already be through the
    price — that is an exit disguised as protection."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, entry=0.26, current=0.42, hwm=5.00,
                       armed=False, trigger=0.0)

    tick(m, q, 0.42)
    assert pos.trailing_stop_active is True
    assert pos.trailing_stop_price < pos.current_price
    assert pos.high_water_mark == pytest.approx(0.42), "poisoned mark not cleared"


def test_each_threshold_counts_separately(config, mid_session, monkeypatch):
    """A trail breach must not confirm a stop breach, or vice versa."""
    q = Quotes()
    m = make_manager(config, q)
    pos, key = add_xsp(m, stop=30.0)
    fired = catch_exits(m, monkeypatch)

    tick(m, q, 0.28, requote=0.37)             # under the trail, above the stop
    assert pos.breach_ticks.get("trail") == 1
    assert pos.breach_ticks.get("stop", 0) == 0
    assert fired == []
