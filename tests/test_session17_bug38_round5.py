"""BUG-38 review round 5 — defects introduced by the round-4 fixes.

Round 4 fixed the doubled rest window and the snatched cancel verdict, and
introduced three problems of its own. Two are the same shape as everything
else in this session: a guard that is correct about the thing it guards and
wrong about what the caller does next.

1. THE CANCEL SETTLE LOOP COULD DISCARD A FILL IT HAD ALREADY SEEN. `order`
   was reassigned on every poll and nulled in the handler, so a partial fill
   observed in a non-terminal state and then followed by a None — the library
   returns None on a 5xx, and 2026-07-31 was eleven straight minutes of that —
   vanished. On the BUY path (`main.py`'s timeout-cancel) that means live
   contracts never registered: no stop, no trail, no ledger row. Verbatim the
   failure `cancel_order`'s own docstring says it exists to prevent.

2. "NO PRICE" SKIPPED DECISIONS THAT NEVER NEEDED A PRICE. Returning None for
   a one-sided book was right, but the monitor's `continue` sat ABOVE the PDT
   next-day sell and the deferred caller exit — instructions already given.
   `trade_manager` warns about exactly this a hundred lines further down:
   "how a caller's 'all out', queued overnight, gets silently dropped the next
   morning." A one-sided book minutes after 09:30 is ordinary, and 09:30 is
   when `exit_at_open` fires.

3. THE RE-QUOTE LOOP PLACED ORDERS IT WOULD CANCEL A SECOND LATER, by sleeping
   exactly to the deadline and then quoting once more.
"""
import logging
import time
import types

import pytest

from execution.paper import PaperExecutor as RobinhoodExecutor
from management.trade_manager import TradeManager


# ── 1. a fill seen while settling must survive a later bad poll ────────────

def make_executor(settle=3.0):
    e = object.__new__(RobinhoodExecutor)
    e.config = {}
    e.mgmt_config = {"cancel_settle_seconds": settle}
    return e



def test_re_quoting_stops_while_the_order_would_still_have_time_to_work():
    """Round 5. Sleeping exactly TO the deadline and re-quoting once more
    meant the late-returning book — the case the loop exists for — produced
    an order placed at the deadline and cancelled a second later. A
    one-second-old order is also the one most likely to read non-terminal on
    cancel, which now latches `sell_state_unknown` and blocks the expiry
    booking."""
    from conftest import PROJECT_ROOT

    src = (PROJECT_ROOT / "management" / "trade_manager.py").read_text(
        encoding="utf-8")
    assert "min_useful" in src, (
        "the re-quote loop no longer reserves any window for the order it "
        "places")
    assert "(rest_deadline - time.monotonic()) > min_useful" in src


# ── 4. a zero ask must not discard the caller's limit ──────────────────────


