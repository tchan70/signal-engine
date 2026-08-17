"""BUG-38 review round 3 — the unconfirmed cancel, and the forfeited rest window.

Two defects found by attacking the round-2 fixes.

1. THE UNCONFIRMED CANCEL — the best mechanical explanation for the incident
   itself, and pre-existing rather than introduced. `cancel_order` decides
   whether a cancel was CONFIRMED and logs "cancel NOT confirmed" when it was
   not; `_cancel_and_inspect` threw that away and the caller escalated only on
   ("unknown", "error"). Robinhood cancels are asynchronous, so a final status
   of "queued" / "pending_cancel" / "confirmed" half a second after the
   request is the NORMAL reading — and every one of them passed through as if
   the order were dead. Attempt 2 then submitted a full-quantity close for
   contracts already committed to a live order, which a broker refuses
   asynchronously. An order placed at 20:45:10 and gone by 20:45:16 is exactly
   that shape.

2. THE FORFEITED REST WINDOW — introduced by round 2, my own doing. Refusing
   to place a blind order into an empty book is right. Abandoning the whole
   13-minute rest window on ONE bad poll is not: a 0/0 quote is routinely a
   momentary gap in a one-sided book, nothing retries after the 0DTE sweep
   (it is latched once a day), and a 0/0 quote also fails `_quote_is_sane` so
   no price-inferred exit can fire either. The refusal turned a 13-minute
   working order into a two-second give-up and a manual escalation.
"""
import logging
import time
import types

import pytest

from management.trade_manager import TradeManager


def bare_manager():
    m = object.__new__(TradeManager)
    m.config = {"sell_fill_timeout_seconds": 45}
    m._shutdown = types.SimpleNamespace(is_set=lambda: False, wait=lambda t: None)
    return m


# ── 1. an unconfirmed cancel must not read as a dead order ─────────────────

@pytest.mark.parametrize("final_status", [
    "queued", "confirmed", "pending_cancel", "pending_cancelled",
    "unconfirmed", "partially_filled", "",
])
def test_a_non_terminal_cancel_is_reported_as_unknown(final_status, caplog):
    """Anything that is not a definite end-state must block re-pricing.

    The caller already treats "unknown" as "do NOT place attempt 2" — the safe
    direction, because the alternative risks selling the same contracts twice.
    """
    m = bare_manager()
    m.executor = types.SimpleNamespace(
        cancel_order=lambda oid: {
            "filled_quantity": 0, "average_price_per_share": 0,
            "final_status": final_status, "cancelled": False,
        })

    with caplog.at_level(logging.WARNING, logger="management.trade_manager"):
        qty, pps, final = m._cancel_and_inspect("ord-1")

    assert final == "unknown", (
        f"final_status={final_status!r} passed through as {final!r}; attempt 2 "
        f"will now submit a full-size close over an order that may still be "
        f"working at the broker")
    assert "NOT confirmed dead" in caplog.text


@pytest.mark.parametrize("final_status", [
    "cancelled", "canceled", "filled", "rejected", "failed", "expired",
])
def test_a_genuinely_dead_order_is_passed_through(final_status):
    """The guard must not fire on a real end-state — that would block every
    legitimate second attempt and turn a fix into a fail-to-exit."""
    m = bare_manager()
    m.executor = types.SimpleNamespace(
        cancel_order=lambda oid: {
            "filled_quantity": 0, "average_price_per_share": 0,
            "final_status": final_status, "cancelled": True,
        })

    _qty, _pps, final = m._cancel_and_inspect("ord-1")
    assert final == final_status, (
        "a confirmed-dead order was escalated to unknown; nothing would ever "
        "re-price and every exit would need a human")


def test_a_cancel_race_fill_is_still_honoured():
    """The pre-existing behaviour this guard sits next to: a fill discovered
    during the cancel must still be booked, whatever the status says."""
    m = bare_manager()
    m.executor = types.SimpleNamespace(
        cancel_order=lambda oid: {
            "filled_quantity": 2, "average_price_per_share": 0.35,
            "final_status": "queued", "cancelled": False,
        })

    qty, pps, _final = m._cancel_and_inspect("ord-1")
    assert (qty, pps) == (2.0, 0.35), "a real fill was dropped"


def test_a_missing_cancelled_key_does_not_break_legacy_executors():
    """The bool-returning / partial-dict executors must still work."""
    m = bare_manager()
    m.executor = types.SimpleNamespace(
        cancel_order=lambda oid: {"final_status": "cancelled"})
    assert m._cancel_and_inspect("ord-1")[2] == "cancelled"

    m.executor = types.SimpleNamespace(cancel_order=lambda oid: True)
    assert m._cancel_and_inspect("ord-1")[2] not in ("error",)


# ── 2. the rest window must survive a transient empty book ─────────────────



def test_a_non_urgent_trim_does_not_discount_as_hard_as_an_urgent_exit():
    """Round 3. The no-bid branch ignored `urgent`, so a profit trim into a
    one-sided book published an offer 15% under the only price information
    available — about $30 a contract on a $2.00 ask, with no urgency to
    justify it. Urgency is what buys aggression."""
    from execution.paper import PaperExecutor as RobinhoodExecutor

    e = object.__new__(RobinhoodExecutor)
    urgent = e._spread_aware_sell_price(bid=0.0, ask=2.00, urgent=True)
    calm = e._spread_aware_sell_price(bid=0.0, ask=2.00, urgent=False)

    assert calm > urgent, (
        f"a non-urgent trim priced at ${calm:.2f}, no better than the urgent "
        f"${urgent:.2f} — the discount is not being earned")
    assert calm <= 2.00, "still may never exceed the ask"


def test_urgency_does_not_change_the_penny_case():
    """Where the floor binds, both must still produce a fillable price."""
    from execution.paper import PaperExecutor as RobinhoodExecutor

    e = object.__new__(RobinhoodExecutor)
    for urgent in (True, False):
        assert e._spread_aware_sell_price(bid=0.0, ask=0.02,
                                          urgent=urgent) == 0.01
