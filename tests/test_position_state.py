"""management/trade_manager.py — the position-state sidecar (Session 10f).

The high-water mark and trailing-stop arming are volatile runtime state, not
ledger facts, so they live in logs/position_state.json rather than trades.json.
These tests cover the WRITE side; tests/test_paper_restore.py covers reading it
back into a restored position.

The failure this prevents: a position that ratcheted to +150% and pulled back
to +50% used to restart with its HWM re-anchored to the current price, so the
trail never re-armed — and under challenge sizing (stop_loss_pct 0) that leaves
no stop of any kind.
"""
import json

import pytest

from execution.position import Position
from management.trade_manager import TradeManager


class SilentExec:
    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop


def make_manager(config):
    # Day 4 (2026-08-03): live config disables trails (the operator); this file
    # tests machinery that lives behind the master switch, so pin it ON
    # ("pin anything ambient"). Switch-off behaviour is tested in
    # test_day4_trail_removal.py.
    config["management"]["enable_trailing_stop"] = True
    return TradeManager(config, executor=SilentExec(),
                        decision_engine=None, notifier=None)


def make_position(**kw):
    base = dict(
        ticker="S", direction="call", strike=22.0, expiry="2026-09-18",
        contracts=1, entry_price=0.95, current_price=0.95,
        high_water_mark=0.95, pnl_pct=0.0, stop_loss_pct=0.0,
        trailing_stop_active=False, trailing_stop_price=0.0,
        management_rules={"strategy": "trailing_stop_only",
                          "trailing_activation_pct": 60,
                          "trailing_distance_pct": 20,
                          "follow_caller_exits": True},
        order_id="PAPER", opened_at="2026-07-24T13:04:26-04:00",
        source="caller_a-challenge-challenge", contracts_remaining=1,
    )
    base.update(kw)
    return Position(**base)


# ── round trip ───────────────────────────────────────────────────────────────

def test_save_then_load_round_trip(config):
    m = make_manager(config)
    m.positions["S_22.0_2026-09-18_call"] = make_position(
        high_water_mark=2.375, trailing_stop_active=True, trailing_stop_price=1.90
    )
    m.save_position_state()

    loaded = m.load_position_state()
    rec = loaded["S_22.0_2026-09-18_call"]
    assert rec["high_water_mark"] == 2.375
    assert rec["trailing_stop_active"] is True
    assert rec["trailing_stop_price"] == 1.90
    # identity stamp — without it a re-entry inherits a stale mark
    assert rec["entry_price"] == 0.95
    assert rec["opened_at"] == "2026-07-24T13:04:26-04:00"


def test_saved_file_is_valid_json_on_disk(config):
    m = make_manager(config)
    m.positions["S_22.0_2026-09-18_call"] = make_position(high_water_mark=1.5)
    m.save_position_state()
    assert m.state_path.exists()
    on_disk = json.loads(m.state_path.read_text())
    assert on_disk["S_22.0_2026-09-18_call"]["high_water_mark"] == 1.5


def test_state_file_sits_beside_the_trade_log(config):
    m = make_manager(config)
    assert m.state_path.parent == m.trade_log_path.parent
    assert m.state_path.name == "position_state.json"


def test_save_prunes_closed_positions(config):
    m = make_manager(config)
    m.positions["A_1.0_2026-09-18_call"] = make_position(ticker="A", strike=1.0)
    m.positions["B_2.0_2026-09-18_call"] = make_position(ticker="B", strike=2.0)
    m.save_position_state()
    assert len(m.load_position_state()) == 2

    m.positions.pop("A_1.0_2026-09-18_call")
    m.save_position_state()
    assert set(m.load_position_state()) == {"B_2.0_2026-09-18_call"}


def test_save_with_no_positions_writes_an_empty_map(config):
    m = make_manager(config)
    m.save_position_state()
    assert m.load_position_state() == {}


# ── it must never take the bot down ──────────────────────────────────────────

def test_load_returns_empty_when_file_absent(config):
    m = make_manager(config)
    assert not m.state_path.exists()
    assert m.load_position_state() == {}


def test_load_survives_corrupt_json(config):
    m = make_manager(config)
    m.state_path.write_text("{ this is not json", encoding="utf-8")
    assert m.load_position_state() == {}


def test_load_survives_wrong_root_type(config):
    m = make_manager(config)
    m.state_path.write_text('["a", "list"]', encoding="utf-8")
    assert m.load_position_state() == {}


def test_save_survives_an_unwritable_path(config, monkeypatch):
    """Losing the sidecar costs a high-water mark, not a position — a write
    failure must not propagate into the monitoring loop."""
    m = make_manager(config)
    m.positions["S_22.0_2026-09-18_call"] = make_position()
    m.state_path = m.state_path.parent / "no_such_dir" / "position_state.json"
    m.save_position_state()  # must not raise


def test_write_is_atomic_no_tmp_left_behind(config):
    m = make_manager(config)
    m.positions["S_22.0_2026-09-18_call"] = make_position()
    m.save_position_state()
    leftovers = list(m.state_path.parent.glob("position_state.json.tmp"))
    assert leftovers == []


# ── the write actually fires where it matters ────────────────────────────────

def test_new_high_water_mark_is_persisted_by_the_trailing_check(config):
    """Arming the trail must hit disk immediately — the process may never get
    a clean shutdown to flush it."""
    m = make_manager(config)
    key = "S_22.0_2026-09-18_call"
    pos = make_position(current_price=1.60, high_water_mark=1.60, pnl_pct=68.4)
    m.positions[key] = pos

    assert m._check_trailing_stop(pos) is False  # arms, does not fire
    assert pos.trailing_stop_active is True

    rec = m.load_position_state()[key]
    assert rec["trailing_stop_active"] is True
    assert rec["trailing_stop_price"] == pytest.approx(1.28)


def test_trail_ratchet_is_persisted(config):
    m = make_manager(config)
    key = "S_22.0_2026-09-18_call"
    pos = make_position(current_price=2.00, high_water_mark=2.00, pnl_pct=110.5,
                        trailing_stop_active=True, trailing_stop_price=1.28)
    m.positions[key] = pos

    m._check_trailing_stop(pos)
    assert pos.trailing_stop_price == pytest.approx(1.60)
    assert m.load_position_state()[key]["trailing_stop_price"] == pytest.approx(1.60)



def test_caller_counts_and_pdt_flag_round_trip(config):
    m = make_manager(config)
    key = "S_22.0_2026-09-18_call"
    m.positions[key] = make_position(
        caller_contracts=5, caller_contracts_remaining=3, pdt_sell_next_open=True
    )
    m.save_position_state()
    rec = m.load_position_state()[key]
    assert rec["caller_contracts"] == 5
    assert rec["caller_contracts_remaining"] == 3
    assert rec["pdt_sell_next_open"] is True


def test_caller_scale_in_is_persisted(config):
    """A scale-in changes the caller's count — the next trim's proportion
    depends on it, so it must not wait for a shutdown to be written."""
    m = make_manager(config)
    key = "S_22.0_2026-09-18_call"
    m.positions[key] = make_position(caller_contracts=2, caller_contracts_remaining=2)
    m.handle_caller_scale_in("S", 3, notes="adding", source_channel="caller_a-challenge-challenge")
    assert m.load_position_state()[key]["caller_contracts_remaining"] == 5
