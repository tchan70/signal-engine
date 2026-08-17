"""Trim sizing policy (Session 10f).

Two changes, both aimed at mirroring the caller's *proportion* rather than a
fixed contract count:

1. A trim with no stated size used to take exactly 1 contract. That is only
   sensible at 2 — at 5 it mirrors a 20% trim, at 20 a 5% one. It now takes
   half, which is caller_a's own median when he does state a size ("Trimmed 1/2 and
   let 1 runner", "trimmed 1 & leaving 1 runner" of 2).

2. The `max(1, ...)` rounding floor over-mirrored small positions with no
   memory: a caller trimming 1 of 10 (10%) against our 3 rounded up to 1 = 33%,
   every single time. Rounding stays half-up (Session 9: under-mirroring is the
   worse direction) but the signed remainder is now carried on the position, so
   the error cancels across trims instead of compounding.
"""
import types

import pytest

from execution.position import Position
from management.trade_manager import TradeManager


class SilentExec:
    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop


def make_manager(config):
    return TradeManager(config, executor=SilentExec(),
                        decision_engine=None, notifier=None)


def make_position(**kw):
    base = dict(
        ticker="ONDS", direction="call", strike=10.5, expiry="2026-09-18",
        contracts=5, entry_price=0.38, current_price=0.55,
        high_water_mark=0.55, stop_loss_pct=0.0, management_rules={},
        order_id="PAPER", opened_at="2026-07-24T13:04:26-04:00",
        source="caller_a-challenge-challenge", contracts_remaining=5,
        caller_contracts=0, caller_contracts_remaining=0,
    )
    base.update(kw)
    return Position(**base)


def trim(m, pos, count=None, pct=None, notes=""):
    return m._calculate_proportional_trim(
        position=pos, caller_trim_count=count, explicit_trim_pct=pct, notes=notes
    )


# ── 1. a sizeless trim takes half ────────────────────────────────────────────

@pytest.mark.parametrize("held,expected", [
    (2, 1),    # half of 2
    (3, 1),    # 3 // 2
    (4, 2),
    (5, 2),
    (10, 5),
    (20, 10),
])
def test_bare_trim_takes_half(config, held, expected):
    m = make_manager(config)
    pos = make_position(contracts=held, contracts_remaining=held)
    assert trim(m, pos) == expected


def test_bare_trim_on_a_single_contract_is_a_full_exit(config):
    """Unchanged: you can't halve 1, and the caller reducing means we're out."""
    m = make_manager(config)
    pos = make_position(contracts=1, contracts_remaining=1)
    assert trim(m, pos) == 1


def test_bare_trim_always_leaves_a_runner(config):
    m = make_manager(config)
    for held in range(2, 12):
        pos = make_position(contracts=held, contracts_remaining=held)
        assert trim(m, pos) <= held - 1, f"no runner left at {held}"


def test_bare_trim_agrees_with_an_explicit_half(config):
    """A stated 'half' and a bare trim must not disagree."""
    m = make_manager(config)
    for held in (2, 3, 5, 7, 10):
        bare = trim(m, make_position(contracts=held, contracts_remaining=held))
        stated = trim(m, make_position(contracts=held, contracts_remaining=held),
                      notes="trimmed half")
        assert bare == stated, f"disagree at {held}: bare {bare} vs half {stated}"


def test_bare_trim_is_not_the_old_one_contract(config):
    """Regression marker for the actual change."""
    m = make_manager(config)
    pos = make_position(contracts=10, contracts_remaining=10)
    assert trim(m, pos) == 5, "reverted to the fixed 1-contract fallback"


# ── 2. rounding carry ────────────────────────────────────────────────────────

def test_half_up_rounding_is_preserved(config):
    """Session 9's call stands: 50% of 5 takes 3, not 2."""
    m = make_manager(config)
    pos = make_position(contracts=5, contracts_remaining=5,
                        caller_contracts=10, caller_contracts_remaining=5)
    assert trim(m, pos, count=5) == 3
    assert pos.trim_carry == pytest.approx(-0.5)


def test_small_position_no_longer_over_trims(config):
    """The headline case: caller trims 1 of 10 (10%), we hold 3.

    Old behaviour: max(1, 0.3) = 1 of 3 = 33%, a 3.3x over-mirror, every time.
    """
    m = make_manager(config)
    pos = make_position(contracts=3, contracts_remaining=3,
                        caller_contracts=10, caller_contracts_remaining=9)
    assert trim(m, pos, count=1) == 0
    assert pos.trim_carry == pytest.approx(0.3)


def test_carry_accumulates_into_a_whole_contract(config):
    m = make_manager(config)
    pos = make_position(contracts=3, contracts_remaining=3,
                        caller_contracts=10, caller_contracts_remaining=9)
    assert trim(m, pos, count=1) == 0                    # 0.30 carried
    assert pos.trim_carry == pytest.approx(0.3)
    pos.caller_contracts_remaining = 8
    # second 10%-ish trim: 3 * (1/9) = 0.33, + 0.30 carried = 0.63 -> half-up 1
    assert trim(m, pos, count=1) == 1
    # having taken a whole contract for 0.63 of a share, we now owe the other way
    assert pos.trim_carry < 0


def test_carry_cancels_rather_than_compounds(config):
    """Ten 10% trims should not strip a 3-lot bare the way the old floor did."""
    m = make_manager(config)
    pos = make_position(contracts=3, contracts_remaining=3,
                        caller_contracts=10, caller_contracts_remaining=10)
    taken = 0
    for _ in range(10):
        pos.caller_contracts_remaining = max(1, pos.caller_contracts_remaining - 1)
        taken += trim(m, pos, count=1)
    # Old floor would have taken 1 every single time and hit the runner cap.
    assert taken < 10, "still over-trimming"
    assert taken >= 2, "under-trimming — the carry never cashed in"


def test_runner_cap_excess_is_not_carried(config):
    """The cap is a policy floor, not a rounding artefact — carrying its excess
    would let the debt grow without bound across repeated large trims."""
    m = make_manager(config)
    pos = make_position(contracts=2, contracts_remaining=2,
                        caller_contracts=10, caller_contracts_remaining=1)
    assert trim(m, pos, count=9) == 1        # wanted 2, capped to keep a runner
    assert pos.trim_carry == 0.0


def test_carry_never_drives_a_negative_trim(config):
    m = make_manager(config)
    pos = make_position(contracts=5, contracts_remaining=5,
                        caller_contracts=10, caller_contracts_remaining=9)
    pos.trim_carry = -0.9
    assert trim(m, pos, count=1) >= 0


def test_explicit_percentage_also_carries(config):
    m = make_manager(config)
    pos = make_position(contracts=3, contracts_remaining=3)
    assert trim(m, pos, pct=10) == 0
    assert pos.trim_carry == pytest.approx(0.3)


# ── 3. long-run fidelity, which is the actual point ──────────────────────────

@pytest.mark.parametrize("our_size", [3, 5, 10, 20])
def test_mirrored_fraction_tracks_the_caller(config, our_size):
    """Caller repeatedly trims 20% of what they hold; we should end up having
    shed roughly the same fraction, at every size."""
    m = make_manager(config)
    pos = make_position(contracts=our_size, contracts_remaining=our_size,
                        caller_contracts=20, caller_contracts_remaining=20)
    caller_start, our_start = 20, our_size
    for _ in range(4):
        if pos.contracts_remaining <= 1:
            break
        caller_trim = max(1, int(pos.caller_contracts_remaining * 0.2 + 0.5))
        pos.caller_contracts_remaining -= caller_trim
        pos.contracts_remaining -= trim(m, pos, count=caller_trim)

    caller_shed = (caller_start - pos.caller_contracts_remaining) / caller_start
    our_shed = (our_start - pos.contracts_remaining) / our_start
    assert abs(our_shed - caller_shed) < 0.35, (
        f"size {our_size}: caller shed {caller_shed:.0%}, we shed {our_shed:.0%}"
    )


# ── 4. the carry has to survive a restart ────────────────────────────────────

def test_trim_carry_round_trips_through_the_sidecar(config):
    """Losing the carry on restart resets the rounding debt to zero, which is
    the same over-trim bias the carry exists to remove."""
    m = make_manager(config)
    key = "ONDS_10.5_2026-09-18_call"
    pos = make_position()
    pos.trim_carry = 0.3
    m.positions[key] = pos
    m.save_position_state()
    assert m.load_position_state()[key]["trim_carry"] == pytest.approx(0.3)


def test_negative_carry_round_trips(config):
    m = make_manager(config)
    key = "ONDS_10.5_2026-09-18_call"
    pos = make_position()
    pos.trim_carry = -0.5
    m.positions[key] = pos
    m.save_position_state()
    assert m.load_position_state()[key]["trim_carry"] == pytest.approx(-0.5)




def test_most_is_read_from_the_callers_own_words(config):
    """His phrasing may not survive into the parser's notes field, so the raw
    message is scanned too."""
    m = make_manager(config)
    pos = make_position(contracts=5, contracts_remaining=5)
    got = m._calculate_proportional_trim(
        position=pos, caller_trim_count=None, explicit_trim_pct=None,
        notes="", raw_message="3.70 I 15% I trimmed most APLD @everyone",
    )
    assert got == 4


def test_almost_does_not_count_as_most(config):
    """\\bmost\\b — 'almost' must not trigger an 80% trim."""
    m = make_manager(config)
    pos = make_position(contracts=5, contracts_remaining=5)
    got = trim(m, pos, notes="almost at target, taking something off")
    assert got == 2, "matched 'almost' as 'most'"


def test_most_beats_the_bare_default_but_loses_to_a_stated_count(config):
    m = make_manager(config)
    bare = trim(m, make_position(contracts=5, contracts_remaining=5))
    most = trim(m, make_position(contracts=5, contracts_remaining=5),
                notes="trimmed most")
    counted = trim(m, make_position(contracts=5, contracts_remaining=5,
                                    caller_contracts=10,
                                    caller_contracts_remaining=9),
                   count=1, notes="trimmed most")
    assert bare == 2 and most == 4, (bare, most)
    assert counted == 1, "an explicit count must win over the wording"


def test_most_always_leaves_a_runner(config):
    m = make_manager(config)
    for held in range(2, 12):
        pos = make_position(contracts=held, contracts_remaining=held)
        assert trim(m, pos, notes="trimmed most") <= held - 1


# ── 6. a dropped caller exit has to reach the user (Session 10f) ─────────────

def test_dropped_caller_exit_notifies(config):
    """Four of these fired during the 07-23/24 run and none reached Discord.
    A dropped exit means the book has diverged from the caller."""
    notices = []
    m = TradeManager(config, executor=SilentExec(), decision_engine=None,
                     notifier=types.SimpleNamespace(
                         notify_status=notices.append,
                         notify_error=notices.append))
    m.handle_caller_exit("XSP", {"source_channel": "caller_a-challenge-challenge"})
    assert notices, "dropped exit was silent"
    assert "XSP" in notices[0]


def test_dropped_exit_message_lists_what_we_do_hold(config):
    notices = []
    m = TradeManager(config, executor=SilentExec(), decision_engine=None,
                     notifier=types.SimpleNamespace(
                         notify_status=notices.append,
                         notify_error=notices.append))
    m.positions["S_22.0_2026-09-18_call"] = make_position(ticker="S", strike=22.0)
    m.handle_caller_exit("XSP", {"source_channel": "caller_a-challenge-challenge"})
    assert "S" in notices[0]


def test_no_notifier_does_not_crash_the_exit_path(config):
    m = TradeManager(config, executor=SilentExec(), decision_engine=None,
                     notifier=None)
    m.handle_caller_exit("XSP", {"source_channel": "caller_a-challenge-challenge"})
