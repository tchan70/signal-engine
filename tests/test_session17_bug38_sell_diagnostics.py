"""BUG-38c/d — the two log defects that made the CELH incident unreadable.

On 2026-07-31 a resting 0DTE sell order was placed at 20:45:10 and was gone by
20:45:16 — six seconds into a wait budgeted at 771. The only two lines the bot
produced about it were:

    Sell attempt 2 for CELH: 0/1 filled within 771s
    SELL FAILED (2 attempts, no fill) — MANUAL EXIT NEEDED

The first is false: nothing waited 771 seconds. It prints the INTENDED timeout,
so it did not merely fail to reveal the problem, it argued against there being
one — the order looks like it rested for thirteen minutes in a quiet market. The
second says the outcome without the cause.

Nothing recorded that the broker had handed the order straight back, which is
the single fact that distinguishes "nobody wanted it" from "our order was
refused". These tests pin both diagnostics, because the next incident of this
shape has to be answerable from the log alone.
"""
import logging
import time
import types

import pytest



from management.trade_manager import TradeManager


def make_manager():
    m = object.__new__(TradeManager)
    m.config = {"sell_fill_timeout_seconds": 45}
    m._shutdown = types.SimpleNamespace(is_set=lambda: False, wait=lambda t: None)
    return m


class Broker:
    """Returns a fixed order state on every poll."""

    def __init__(self, status, filled=0.0):
        self.status = status
        self.filled = filled
        self.polls = 0

    def check_order_status(self, order_id):
        self.polls += 1
        return {"status": self.status,
                "filled_quantity": self.filled,
                "average_price_per_share": 0.0}


# ── BUG-38c: say WHY a resting wait ended early ────────────────────────────

@pytest.mark.parametrize("state", ["cancelled", "rejected", "failed", "expired"])
def test_a_broker_terminated_order_says_so(caplog, state):
    """The fact that was missing on 2026-07-31.

    "no fill" reads as a quiet market. "the broker handed it back after 0s of
    a 771s wait" is a completely different diagnosis, and it is the true one.
    """
    m = make_manager()
    m.executor = Broker(state)

    with caplog.at_level(logging.WARNING, logger="management.trade_manager"):
        result = m._confirm_sell_fill("ord-1", timeout=771, resting=True)

    assert result[0] == "unfilled"
    assert f"broker state '{state}'" in caplog.text
    assert "771s wait" in caplog.text
    assert "did not survive to the end of the wait" in caplog.text

    # Review round 2: state the state, do NOT assert the cause. The bot tells
    # the operator "you can still exit by hand on Robinhood" during exactly
    # this window, so a manual cancel lands here too — an earlier draft
    # claimed "this was NOT our cancel" and would have blamed the broker for
    # the operator's own action.
    assert "NOT our cancel" not in caplog.text
    assert "manual cancel" in caplog.text, (
        "the possible causes should be offered, not one of them asserted")


def test_the_elapsed_time_is_reported_not_the_budget(caplog, monkeypatch):
    """It must be visible that the order died IMMEDIATELY, not at the end."""
    m = make_manager()
    m.executor = Broker("rejected")
    clock = {"t": 1000.0}
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    with caplog.at_level(logging.WARNING, logger="management.trade_manager"):
        m._confirm_sell_fill("ord-1", timeout=771, resting=True)

    assert "after 0s of a 771s wait" in caplog.text, (
        f"expected the true elapsed time; got: {caplog.text}")


def test_an_ordinary_timeout_is_not_reported_as_a_broker_refusal(caplog):
    """A genuinely quiet market must NOT accuse the broker."""
    m = make_manager()
    m.executor = Broker("queued")

    with caplog.at_level(logging.WARNING, logger="management.trade_manager"):
        result = m._confirm_sell_fill("ord-1", timeout=0.05, resting=False)

    assert result[0] == "unfilled"
    assert "broker state" not in caplog.text


def test_a_partial_fill_is_still_reported_on_a_terminal_state(caplog):
    m = make_manager()
    m.executor = Broker("cancelled", filled=1.0)

    with caplog.at_level(logging.WARNING, logger="management.trade_manager"):
        state, qty, _ = m._confirm_sell_fill("ord-1", timeout=100, resting=True)

    assert state == "partial" and qty == 1.0
    assert "filled 1" in caplog.text


# ── BUG-38d: the attempt line must report time spent ───────────────────────

