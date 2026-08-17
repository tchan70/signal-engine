"""Session 16: the trim path end to end, with a broker attached.

`test_trim_policy.py` covers the arithmetic thoroughly — half-up rounding, the
signed carry, the runner cap, "most" = 80% — by calling
`_calculate_proportional_trim` directly. What has never been exercised
ANYWHERE is the wiring underneath it: caller signal → `handle_caller_exit`'s
trim branch → `_execute_trim` → sell confirmation → the TRIM ledger row → the
remainder staying under management.

That gap is now live risk rather than theoretical. 34% of caller_a's entries are
multi-contract, and after 2026-07-30 the book holds AAL ×3, so the next
"trimmed 1" runs this code with real money for the first time.

Two defects in that path are fixed here, both by parity with the full-exit
path, which learned each lesson the hard way in Sessions 9-12:

  - An UNCONFIRMED cancel used to return a bare False. False means "retry",
    and profit tiers retry on the next ~5s monitor tick (Session 9 / M4) —
    straight over an order that may still be resting at the broker. The
    full-exit path refuses to re-price in that state; the trim path now
    latches the position instead.
  - "filled" with a zero quantity is a broker data glitch, not a zero fill.
    The full-exit path re-polls once and then assumes the requested quantity,
    because assuming zero re-sells contracts that already sold. The trim path
    read it as UNFILLED and returned False — which for a profit tier means
    re-selling exactly those contracts.
"""
import json
import types
from datetime import datetime

import pytest

from execution.position import Position
from management.trade_manager import TradeManager
from utils import market_time
from utils.market_time import ET


# ── harness ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mid_session(monkeypatch):
    """Thursday 2026-07-30, 12:00 ET.

    Autouse because every test in this file is about what happens when a trim
    EXECUTES. Outside market hours `handle_caller_exit` defers instead — which
    is correct, is tested elsewhere, and would otherwise silently turn all of
    these into assertions about a no-op.
    """
    now = datetime(2026, 7, 30, 14, 30, tzinfo=ET)
    monkeypatch.setattr(market_time, "now_et", lambda: now)
    monkeypatch.setattr(market_time, "is_market_hours", lambda *a, **k: True)
    monkeypatch.setattr(market_time, "seconds_since_open", lambda *a, **k: 3600 * 5)
    return now

class TrimBroker:
    """Scriptable sells. `fills[i]` applies to the i-th order placed."""

    def __init__(self, fills=None, cancel_result=None, status_override=None):
        self.fills = list(fills or [])
        self.orders = []
        self.cancelled = []
        self.cancel_result = cancel_result
        self.status_override = status_override
        self.status_calls = 0

    def sell_option_position(self, **kw):
        oid = f"order-{len(self.orders) + 1}"
        self.orders.append({"id": oid, **kw})
        return oid

    def check_order_status(self, order_id):
        self.status_calls += 1
        if self.status_override is not None:
            return self.status_override(order_id, self.status_calls)
        idx = int(order_id.rsplit("-", 1)[1]) - 1
        fill = self.fills[idx] if idx < len(self.fills) else None
        if fill is None:
            return {"status": "queued", "filled_quantity": 0,
                    "average_price_per_share": 0}
        qty, pps = fill
        return {"status": "filled", "filled_quantity": qty,
                "average_price_per_share": pps}

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        if self.cancel_result is not None:
            return self.cancel_result
        return {"filled_quantity": 0, "average_price_per_share": 0,
                "final_status": "cancelled"}

    def __getattr__(self, name):
        return lambda *a, **k: None


def make_manager(config, broker, notices=None, errors=None, engine=None):
    config["management"]["sell_fill_timeout_seconds"] = 0.05
    notifier = types.SimpleNamespace(
        notify_status=(notices.append if notices is not None else lambda *_: None),
        notify_error=(errors.append if errors is not None else lambda *_: None),
        notify_exit=lambda *a, **k: None,
        notify_trim=lambda *a, **k: None,
    )
    m = TradeManager(config, executor=broker, decision_engine=engine,
                     notifier=notifier)
    m.paper_trade = False
    return m


FUTURE = "2026-08-21"


def add_aal(m, *, contracts=3, entry=0.20, current=0.30, caller=3):
    """AAL 17c ×3 — the position actually open on the night of 2026-07-30."""
    key = f"AAL_17.0_{FUTURE}_call"
    m.positions[key] = Position(
        ticker="AAL", direction="call", strike=17.0, expiry=FUTURE,
        contracts=contracts, entry_price=entry, current_price=current,
        high_water_mark=current, pnl_pct=(current - entry) / entry * 100,
        stop_loss_pct=0.0,
        management_rules={"strategy": "trailing_stop_only",
                          "trailing_activation_pct": 60,
                          "trailing_distance_pct": 30,
                          "follow_caller_exits": True},
        management_style="challenge",
        order_id="LIVE", opened_at="2026-07-30T12:59:41-04:00",
        source="caller_a-challenge-challenge", contracts_remaining=contracts,
        caller_contracts=caller, caller_contracts_remaining=caller,
    )
    return m.positions[key], key


def caller_trim(m, count=None, notes="", raw=""):
    m.handle_caller_exit("AAL", {
        "type": "trim",
        "trim_contracts": count,
        "notes": notes,
        "raw_message": raw,
        "source_channel": "caller_a-challenge-challenge",
    })


def ledger(m):
    return json.loads(m.trade_log_path.read_text()) if m.trade_log_path.exists() else []


# ── 1. the wiring, end to end ────────────────────────────────────────────────

def test_a_caller_trim_of_one_of_three_sells_one_and_keeps_two(config):
    broker = TrimBroker(fills=[(1, 0.30)])
    m = make_manager(config, broker)
    pos, key = add_aal(m)

    caller_trim(m, count=1)

    assert len(broker.orders) == 1
    assert broker.orders[0]["contracts"] == 1
    assert pos.contracts_remaining == 2
    assert key in m.positions                 # remainder still managed
    rows = ledger(m)
    assert [r["action"] for r in rows] == ["TRIM"]
    assert rows[0]["contracts_remaining"] == 2


def test_the_trim_books_pnl_from_the_actual_fill_not_the_last_mark(config):
    """The mark said $0.30; it filled at $0.26. Book the fill."""
    broker = TrimBroker(fills=[(1, 0.26)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m)

    caller_trim(m, count=1)

    assert ledger(m)[0]["pnl_usd"] == pytest.approx((0.26 - 0.20) * 100 * 1)
    assert m.daily_pnl == pytest.approx(6.0)


def test_the_callers_own_count_is_tracked_separately_from_ours(config):
    broker = TrimBroker(fills=[(1, 0.30)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m, caller=3)

    caller_trim(m, count=1)

    assert pos.caller_contracts_remaining == 2
    assert pos.caller_contracts == 3


def test_a_bare_trim_takes_half_through_the_real_wiring(config):
    """The policy tests prove half; this proves half reaches the broker."""
    broker = TrimBroker(fills=[(2, 0.30)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m, contracts=4, caller=4)

    caller_trim(m, count=None, notes="trimming here", raw="trimming here")

    assert broker.orders[0]["contracts"] == 2
    assert pos.contracts_remaining == 2


def test_a_trim_of_our_last_contract_becomes_a_full_exit(config):
    """H4a. The caller is reducing; with one contract the only way to follow
    is to close. It must book CLOSE, not TRIM, or the restore path would keep
    treating a closed position as open."""
    broker = TrimBroker(fills=[(1, 0.30)])
    m = make_manager(config, broker)
    pos, key = add_aal(m, contracts=1, caller=2)

    caller_trim(m, count=1)

    rows = ledger(m)
    assert [r["action"] for r in rows] == ["CLOSE"]
    assert rows[0]["reason"] == "caller_trim_last_contract"
    assert pos.contracts_remaining == 0
    assert key not in m.positions


def test_a_trim_never_takes_the_last_contract(config):
    """The runner cap: trimming 3 of our 3 leaves 1."""
    broker = TrimBroker(fills=[(2, 0.30)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m, contracts=3, caller=3)

    caller_trim(m, count=3)

    assert broker.orders[0]["contracts"] == 2
    assert pos.contracts_remaining == 1


def test_a_trim_then_a_full_exit_of_the_remainder(config):
    broker = TrimBroker(fills=[(1, 0.30), (2, 0.28)])
    m = make_manager(config, broker)
    pos, key = add_aal(m)

    caller_trim(m, count=1)
    m.handle_caller_exit("AAL", {
        "type": "exit", "notes": "", "raw_message": "all out AAL",
        "source_channel": "caller_a-challenge-challenge",
    })

    assert [r["action"] for r in ledger(m)] == ["TRIM", "CLOSE"]
    assert key not in m.positions


def test_the_trail_state_survives_a_trim(config):
    """The remainder keeps its earned high-water mark and arming — a trim is
    not a new position."""
    broker = TrimBroker(fills=[(1, 0.30)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m)
    pos.high_water_mark = 0.44
    pos.trailing_stop_active = True
    pos.trailing_stop_price = 0.31

    caller_trim(m, count=1)

    assert pos.high_water_mark == 0.44
    assert pos.trailing_stop_active is True
    assert pos.trailing_stop_price == 0.31


# ── 2. fills that go wrong ───────────────────────────────────────────────────

def test_a_trim_that_never_fills_changes_nothing(config):
    broker = TrimBroker(fills=[None])
    m = make_manager(config, broker)
    pos, _ = add_aal(m)

    caller_trim(m, count=1)

    assert pos.contracts_remaining == 3
    assert ledger(m) == []
    assert m.daily_pnl == 0.0
    assert broker.cancelled == ["order-1"]


def test_a_partial_trim_books_only_what_sold(config):
    broker = TrimBroker(fills=[(1, 0.30)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m, contracts=4, caller=4)

    caller_trim(m, count=2)

    rows = ledger(m)
    assert len(rows) == 1
    assert "PARTIAL 1/2" in rows[0]["reason"]
    assert pos.contracts_remaining == 3


def test_an_over_reporting_broker_cannot_inflate_the_book(config):
    """Session 12: clamp before booking, or contracts_remaining goes below the
    true book and P&L is inflated."""
    broker = TrimBroker(fills=[(9, 0.30)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m)

    caller_trim(m, count=1)

    assert pos.contracts_remaining == 2
    assert ledger(m)[0]["pnl_usd"] == pytest.approx(10.0)   # 1 contract, not 9


# ── 3. the two parity fixes ──────────────────────────────────────────────────

def test_an_unconfirmed_cancel_blocks_further_trims(config):
    """The double-sell hazard. The order may still be resting; a bare False
    would have the profit tier retry it ~5 seconds later."""
    errors = []
    broker = TrimBroker(fills=[None], cancel_result={"final_status": "error"})
    m = make_manager(config, broker, errors=errors)
    pos, _ = add_aal(m)

    assert m._execute_trim(pos, 1, "profit_tier_50pct") is False
    assert pos.trim_blocked_unconfirmed is True
    assert any("CANCEL UNCONFIRMED" in str(e) for e in errors)

    # The retry the tier logic would make on the next tick:
    assert m._execute_trim(pos, 1, "profit_tier_50pct") is False
    assert len(broker.orders) == 1            # no second order was placed


def test_a_confirmed_cancel_leaves_trimming_available(config):
    """Only an UNCONFIRMED cancel latches — an ordinary no-fill must stay
    retryable, which is what profit tiers depend on."""
    broker = TrimBroker(fills=[None, (1, 0.30)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m)

    assert m._execute_trim(pos, 1, "profit_tier_50pct") is False
    assert pos.trim_blocked_unconfirmed is False
    assert m._execute_trim(pos, 1, "profit_tier_50pct") is True
    assert pos.contracts_remaining == 2


def test_a_blocked_position_can_still_be_closed_completely(config):
    """Refusing to EXIT because a trim once glitched would be the worse
    failure — the full-exit path has its own unconfirmed-cancel handling."""
    broker = TrimBroker(fills=[(3, 0.30)])
    m = make_manager(config, broker)
    pos, key = add_aal(m)
    pos.trim_blocked_unconfirmed = True

    m.handle_caller_exit("AAL", {
        "type": "exit", "notes": "", "raw_message": "all out AAL",
        "source_channel": "caller_a-challenge-challenge",
    })

    assert pos.contracts_remaining == 0
    assert key not in m.positions


def test_filled_with_zero_quantity_is_a_glitch_not_a_zero_fill(config):
    """Assuming zero would re-sell contracts the broker already sold."""
    def status(order_id, call_no):
        # First poll: filled but no quantity. Re-poll: still nothing.
        return {"status": "filled", "filled_quantity": 0,
                "average_price_per_share": 0}

    broker = TrimBroker(status_override=status)
    m = make_manager(config, broker)
    pos, _ = add_aal(m)

    assert m._execute_trim(pos, 1, "caller_proportional_trim") is True
    assert pos.contracts_remaining == 2
    assert [r["action"] for r in ledger(m)] == ["TRIM"]


def test_the_zero_quantity_repoll_prefers_a_real_answer(config):
    """If the second poll DOES report the fill, use its price rather than the
    last mark."""
    state = {"n": 0}

    def status(order_id, call_no):
        state["n"] += 1
        if state["n"] == 1:
            return {"status": "filled", "filled_quantity": 0,
                    "average_price_per_share": 0}
        return {"status": "filled", "filled_quantity": 1,
                "average_price_per_share": 0.27}

    broker = TrimBroker(status_override=status)
    m = make_manager(config, broker)
    pos, _ = add_aal(m)

    assert m._execute_trim(pos, 1, "caller_proportional_trim") is True
    assert ledger(m)[0]["pnl_usd"] == pytest.approx((0.27 - 0.20) * 100)


# ── 4. PDT and mutual exclusion ──────────────────────────────────────────────

def _engine(remaining=0):
    return types.SimpleNamespace(
        record_realized_pnl=lambda v: None,
        record_day_trade=lambda: None,
        get_day_trades_remaining=lambda: remaining,
        get_day_trades_in_window=lambda: 3,
        _max_day_trades=3,
    )


def test_a_pdt_blocked_trim_notifies_once_per_day(config):
    """Session 9: failed tier trims retry every ~5s by design, so an
    unlatched notification is a webhook flood."""
    errors = []
    broker = TrimBroker(fills=[(1, 0.30)])
    m = make_manager(config, broker, errors=errors, engine=_engine(remaining=0))
    pos, _ = add_aal(m)
    pos.opened_at = market_time.now_et().isoformat()      # same-day → day trade

    for _ in range(5):
        assert m._execute_trim(pos, 1, "profit_tier_50pct") is False

    assert broker.orders == []
    assert len([e for e in errors if "PDT" in str(e)]) == 1


def test_a_trim_is_refused_while_an_exit_is_in_flight(config):
    broker = TrimBroker(fills=[(1, 0.30)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m)
    pos.exit_in_flight = True

    assert m._execute_trim(pos, 1, "caller_proportional_trim") is False
    assert broker.orders == []


# ── 5. what a restart makes of a trimmed position ────────────────────────────


def test_the_carry_survives_the_sidecar_round_trip(config):
    """A fractional remainder is the whole point of the carry; losing it on a
    restart reintroduces the compounding bias it exists to cancel."""
    broker = TrimBroker(fills=[(1, 0.30)])
    m = make_manager(config, broker)
    pos, key = add_aal(m, contracts=3, caller=10)

    caller_trim(m, count=1)                   # 1/10 of the caller's book
    assert pos.trim_carry != 0.0
    m.save_position_state()

    saved = m.load_position_state()
    assert saved[key]["trim_carry"] == pytest.approx(pos.trim_carry)


# ── 6. what the first review pass showed was untested ────────────────────────

def test_a_partial_fill_survives_an_unconfirmed_cancel(config):
    """The regression the latch introduced, caught in review.

    Polling confirms one contract sold; the cancel then cannot be verified.
    The first version of the latch used `c_qty <= 0`, which both skipped the
    latch AND returned before the booking — so a contract that really sold was
    never recorded and `contracts_remaining` stayed a lie. Whatever is
    confirmed must be banked, exactly as the full-exit path banks its partial
    before breaking out of the retry loop.
    """
    errors = []

    def status(order_id, call_no):
        return {"status": "queued", "filled_quantity": 1,
                "average_price_per_share": 0.30}

    broker = TrimBroker(
        status_override=status,
        cancel_result={"filled_quantity": 1, "average_price_per_share": 0.30,
                       "final_status": "unknown"},
    )
    m = make_manager(config, broker, errors=errors)
    pos, _ = add_aal(m, contracts=4, caller=4)

    assert m._execute_trim(pos, 2, "profit_tier_50pct") is True
    assert pos.contracts_remaining == 3                  # the sold one is gone
    rows = ledger(m)
    assert [r["action"] for r in rows] == ["TRIM"]
    assert rows[0]["pnl_usd"] == pytest.approx((0.30 - 0.20) * 100)
    # ...and the position is still latched, because MORE may be in flight.
    assert pos.trim_blocked_unconfirmed is True
    assert any("CANCEL UNCONFIRMED" in str(e) for e in errors)


def test_an_unconfirmed_trim_marks_the_sell_state_unknown(config):
    """Consumed by the expiry booking: with an order possibly still live,
    'expired worthless' and 'we sold it' are both live answers."""
    broker = TrimBroker(fills=[None], cancel_result={"final_status": "error"})
    m = make_manager(config, broker)
    pos, _ = add_aal(m)

    m._execute_trim(pos, 1, "profit_tier_50pct")
    assert pos.sell_state_unknown is True


def test_the_blocked_trim_warning_is_latched(config):
    """Tier trims retry every ~5s and this state lasts the session, so an
    unlatched notification is a permanent webhook flood — the same lesson
    pdt_trim_blocked_date already encodes one branch above."""
    errors = []
    broker = TrimBroker(fills=[None], cancel_result={"final_status": "error"})
    m = make_manager(config, broker, errors=errors)
    pos, _ = add_aal(m)

    m._execute_trim(pos, 1, "profit_tier_50pct")       # latches
    errors.clear()
    for _ in range(10):
        m._execute_trim(pos, 1, "profit_tier_50pct")

    assert len(errors) == 1


def test_a_latched_position_can_still_follow_the_caller_out_of_its_last_contract(config):
    """With one contract left a 'trim' IS a close (H4a). Blocking it would
    silently drop the caller's instruction to get out."""
    broker = TrimBroker(fills=[None, (1, 0.30)],
                        cancel_result={"final_status": "error"})
    m = make_manager(config, broker)
    pos, key = add_aal(m, contracts=2, caller=2)

    m._execute_trim(pos, 1, "profit_tier_50pct")       # fails, latches
    assert pos.trim_blocked_unconfirmed is True
    assert pos.contracts_remaining == 2

    # Caller trims again; we are down to our last, so this escalates to a close.
    pos.contracts_remaining = 1
    assert m._execute_trim(pos, 1, "caller_proportional_trim") is True
    assert pos.contracts_remaining == 0
    assert [r["action"] for r in ledger(m)] == ["CLOSE"]


def test_a_malformed_fill_price_does_not_discard_a_good_quantity(config):
    """One `try` around both coercions threw away a perfectly good quantity
    because the price was junk, then fell through to 'assume the full
    amount' — a worse answer than the one the broker just gave."""
    state = {"n": 0}

    def status(order_id, call_no):
        state["n"] += 1
        if state["n"] == 1:
            return {"status": "filled", "filled_quantity": 0,
                    "average_price_per_share": 0}
        return {"status": "filled", "filled_quantity": 1,
                "average_price_per_share": "not-a-number"}

    broker = TrimBroker(status_override=status)
    m = make_manager(config, broker)
    pos, _ = add_aal(m, contracts=4, caller=4)

    assert m._execute_trim(pos, 2, "caller_proportional_trim") is True
    assert pos.contracts_remaining == 3          # the ONE the broker reported
    # No price came back, so it books at the last mark rather than inventing one.
    assert ledger(m)[0]["pnl_usd"] == pytest.approx((0.30 - 0.20) * 100)


def test_an_unconfirmed_cancel_is_not_cleared_by_its_own_partial_fill(config):
    """Review round three. The flag is set when THIS call could not confirm
    its cancel, then a confirmed partial cleared it a hundred lines later in
    the same call — announcing the broker's state was known while an order for
    the other contract might still be live. The expiry booking believes that
    flag, so clearing it wrongly writes $0.00 over contracts that may have
    sold. The full-exit twin only clears on a confirmed FULL close."""
    def status(order_id, call_no):
        return {"status": "queued", "filled_quantity": 1,
                "average_price_per_share": 0.30}

    broker = TrimBroker(
        status_override=status,
        cancel_result={"filled_quantity": 1, "average_price_per_share": 0.30,
                       "final_status": "unknown"},
    )
    m = make_manager(config, broker)
    pos, _ = add_aal(m, contracts=4, caller=4)

    assert m._execute_trim(pos, 2, "profit_tier_50pct") is True
    assert pos.contracts_remaining == 3            # the confirmed one is booked
    assert pos.sell_state_unknown is True          # ...and the rest is unknown


def test_a_clean_trim_after_an_earlier_glitch_does_clear_the_flag(config):
    """The other direction: a confirmed fill with a confirmed cancel really
    does re-establish what the broker holds, and leaving the flag set would
    block the expiry booking for ever."""
    broker = TrimBroker(fills=[(1, 0.30)])
    m = make_manager(config, broker)
    pos, _ = add_aal(m)
    pos.sell_state_unknown = True                  # left over from yesterday

    assert m._execute_trim(pos, 1, "caller_proportional_trim") is True
    assert pos.sell_state_unknown is False
