"""Intent is not a fill (Session 10f).

2026-07-27, 09:01 ET — 29 minutes before the bell:

    "So good, we are going to TP DAL today"   ->  parsed as signal_type="exit"

We held no DAL, so nothing happened. Had we, the bot would have closed the
position on a *plan*, 32 minutes before his actual "1.55 200% all out DAL" at
+200%.

The parser prompt already teaches this ("Plans are not fills") and still got it
wrong, which is why the defence sits downstream too — the same belt-and-braces
shape as the `trimmed N` override.

Day 4 (the operator, 2026-08-03): ONE exemption — TP. After "Lets TP AAL 30%" was
held as a suggestion while the caller took profit, TP posts now EXECUTE, plan
phrasing or not, provided the TP wording is in the caller's OWN text (never
the parser's notes, which paraphrase holds with the words "take profit").
Acting on TP intent fails flat-with-profit; acting on entry intent fails
holding-something-he-never-bought — the asymmetry is deliberate. The
non-TP verbs (cut / sell / trim / close...) keep the original guard.

The pattern is deliberately narrow: the intent word must GOVERN the exit verb
("going to TP", "need to cut"), not merely appear in the message. Validated
against all 539 scraped caller messages — it flags exactly one, the message the
prompt itself uses as its canonical example.
"""
import types
from datetime import datetime

import pytest

from execution.position import Position
from management.trade_manager import INTENT_RE, TradeManager
from utils import market_time
from utils.market_time import ET


@pytest.fixture(autouse=True)
def _in_market_hours(monkeypatch):
    monkeypatch.setattr(
        market_time, "now_et", lambda: datetime(2026, 7, 27, 12, 0, tzinfo=ET)
    )


class SilentExec:
    def __getattr__(self, name):
        return lambda *a, **k: None


def make_manager(config, notices=None):
    m = TradeManager(
        config, executor=SilentExec(), decision_engine=None,
        notifier=types.SimpleNamespace(
            notify_status=(notices.append if notices is not None else lambda *_: None),
            notify_error=(notices.append if notices is not None else lambda *_: None),
        ),
    )
    m.paper_trade = True
    return m


def add_dal(m, contracts=1):
    key = "DAL_100.0_2026-09-18_call"
    m.positions[key] = Position(
        ticker="DAL", direction="call", strike=100.0, expiry="2026-09-18",
        contracts=contracts, entry_price=0.49, current_price=1.55,
        high_water_mark=1.55, pnl_pct=216.0, stop_loss_pct=0.0,
        management_rules={"follow_caller_exits": True},
        order_id="PAPER", opened_at="2026-07-23T09:37:00-04:00",
        source="caller_a-challenge-challenge", contracts_remaining=contracts,
    )
    return key


# ── the pattern ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "So good, we are going to TP DAL today",                    # the 07-27 message
    "if TE doesnt reclaim 6.20 today, we need to cut it",       # the prompt's example
    "planning to sell this tomorrow",
    "might cut if it loses 6",
    "will trim into strength",
    "gonna take profit here soon",
    "about to close this one out",
])
def test_plans_are_recognised_as_intent(text):
    assert INTENT_RE.search(text)


@pytest.mark.parametrize("text", [
    "1.55 200% all out DAL",
    "ALL OUT UBER @everyone",
    "Lets cut SOFI",
    "Cutting Xsp dont want to risk this into INTC er today",
    "Sold 4/6 ONDS at -5% Taking some risks off Only have 2 ONDS",
    "0.67 40% ONDS trimmed 3 @everyone",
    "0.65 I 25% SOFI trimmed 1 & leaving 1 runner",
    "Out at be",
    "cut BA -10 %",
    "SOLD XSP hedge at Breakeven",
    "Trimmed 1/2 and let 1 runner",
    "S $22 Call 9/18 1 Buy 0.95",
])
def test_real_fills_and_entries_are_not_intent(text):
    """A false positive here means ignoring a genuine exit — the expensive
    direction. Every one of these is a phrasing he actually uses."""
    assert not INTENT_RE.search(text)


def test_the_intent_word_must_govern_the_exit_verb():
    """Merely containing both words isn't enough, or half his exits would match."""
    assert not INTENT_RE.search("out of DAL, will update later")
    assert not INTENT_RE.search("cut it, might re-enter tomorrow")


# ── the downgrade ────────────────────────────────────────────────────────────

def test_a_tp_plan_now_executes(config):
    """Day 4 (the operator, 2026-08-03): the 07-27 DAL message SELLS now. Under the
    old policy the bot held; live on 2026-08-03 the same policy held AAL
    through "Lets TP AAL 30%" while the caller took profit, and the operator chose
    the other trade-off: act on TP, accept the abandoned-plan risk."""
    m = make_manager(config)
    key = add_dal(m)
    m.handle_caller_exit("DAL", {
        "type": "exit", "notes": "",
        "raw_message": "So good, we are going to TP DAL today",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions.get(key) is None, "a TP post must execute"


def test_a_non_tp_plan_still_holds(config):
    """The original guard, on every verb except TP."""
    notices = []
    m = make_manager(config, notices)
    key = add_dal(m)
    m.handle_caller_exit("DAL", {
        "type": "exit", "notes": "",
        "raw_message": "if DAL doesnt reclaim 100 today, we need to cut it",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions.get(key) is not None, "closed a +216% position on a plan"
    assert any("plan" in n.lower() for n in notices)


def test_a_planned_trim_is_also_held(config):
    m = make_manager(config)
    key = add_dal(m, contracts=4)
    m.handle_caller_exit("DAL", {
        "type": "trim", "notes": "",
        "raw_message": "will trim a couple here shortly",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions[key].contracts_remaining == 4


def test_the_real_exit_that_followed_still_closes_it(config):
    """Day 4: the TP plan sells immediately; the confirmed fill 32 minutes
    later finds the book already flat and must be a clean no-op, not a crash
    or a phantom re-exit."""
    m = make_manager(config)
    key = add_dal(m)
    m.handle_caller_exit("DAL", {
        "type": "exit", "notes": "",
        "raw_message": "So good, we are going to TP DAL today",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions.get(key) is None, "the TP plan should have sold"
    m.handle_caller_exit("DAL", {
        "type": "exit", "notes": "",
        "raw_message": "1.55 200% all out DAL",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions.get(key) is None


def test_notes_are_scanned_when_there_is_no_raw_message(config):
    m = make_manager(config)
    key = add_dal(m)
    m.handle_caller_exit("DAL", {
        "type": "exit", "notes": "caller says he is going to TP later",
        "raw_message": "", "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions.get(key) is not None


def test_intent_beats_the_market_hours_deferral(config, monkeypatch):
    """A non-TP plan received pre-market must not be queued for the open."""
    monkeypatch.setattr(
        market_time, "now_et", lambda: datetime(2026, 7, 27, 9, 1, tzinfo=ET)
    )
    m = make_manager(config)
    key = add_dal(m)
    m.handle_caller_exit("DAL", {
        "type": "exit", "notes": "",
        "raw_message": "if DAL doesnt reclaim 100 today, we need to cut it",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions[key].exit_at_open is False, "queued an exit on a plan"


def test_a_tp_plan_premarket_queues_for_the_open(config, monkeypatch):
    """Day 4: TP executes, and pre-market that means the market-hours guard
    QUEUES it — the deferral working as designed for an instruction, not the
    intent gate suppressing a plan."""
    monkeypatch.setattr(
        market_time, "now_et", lambda: datetime(2026, 7, 27, 9, 1, tzinfo=ET)
    )
    m = make_manager(config)
    key = add_dal(m)
    m.handle_caller_exit("DAL", {
        "type": "exit", "notes": "",
        "raw_message": "So good, we are going to TP DAL today",
        "source_channel": "caller_a-challenge-challenge",
    })
    assert m.positions[key].exit_at_open is True, "a TP instruction must queue"
