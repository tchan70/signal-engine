"""engine/decision_engine.py — circuit breaker (C1), sizing floor (H1),
PDT window (M3), stop-loss units (H9), regime hook, balance guards (D14)."""
from datetime import date

import pytest

from engine.decision_engine import DecisionEngine
from parser.signal_parser import Direction, ParsedSignal, SignalType, Urgency
from utils.market_time import (
    is_trading_day,
    next_trading_day,
    trading_date,
    trading_days_ago,
)


def make_engine(config):
    return DecisionEngine(config)


def entry_signal(**kw):
    base = dict(
        signal_type=SignalType.ENTRY,
        ticker="GLW",
        direction=Direction.CALL,
        strike=190.0,
        expiry="0DTE",
        entry_price=2.75,
        caller_contracts=1,
        urgency=Urgency.IMMEDIATE,
        source="caller_a-challenge-challenge",
        source_priority="high",
        raw_message="test",
    )
    base.update(kw)
    return ParsedSignal(**base)


# ── C1: circuit breaker is actually alive now ────────────────────────────────

def test_record_realized_pnl_accumulates(config):
    eng = make_engine(config)
    eng.record_realized_pnl(-250.0)
    eng.record_realized_pnl(-175.0)
    assert eng.daily_pnl == pytest.approx(-425.0)


def test_circuit_breaker_trips_after_bridged_losses(config):
    eng = make_engine(config)
    eng.record_realized_pnl(-450.0)  # -45% of $1k > 40% breaker
    d = eng.evaluate(entry_signal(), account_balance=1000.0,
                     existing_positions=[], sizing_mode="challenge")
    assert d.action == "skip"
    assert "circuit" in d.reason.lower() or "breaker" in d.reason.lower() \
        or "daily loss" in d.reason.lower()


def test_exit_signals_bypass_gates(config):
    # D13: exits must route to management even with the breaker tripped
    eng = make_engine(config)
    eng.record_realized_pnl(-999.0)
    sig = entry_signal(signal_type=SignalType.EXIT)
    d = eng.evaluate(sig, account_balance=1000.0, existing_positions=[])
    assert d.action != "skip"


# ── H1/M1: sizing floor and caps ─────────────────────────────────────────────

def test_unaffordable_contract_returns_zero(config):
    eng = make_engine(config)
    # Starter-tier budget ~$100; a $9.00 contract = $900 must NOT be forced in
    sizing = eng._determine_sizing(
        entry_signal(entry_price=9.00, sizing_hint="starter",
                     expiry="2026-08-21", urgency=Urgency.STANDARD),
        account_balance=1000.0, conviction=50.0, sizing_mode="percentage",
    )
    contracts = sizing[0] if isinstance(sizing, tuple) else sizing.get("contracts")
    assert contracts == 0


def test_affordable_contract_still_sizes(config):
    eng = make_engine(config)
    sizing = eng._determine_sizing(
        entry_signal(entry_price=0.50, expiry="2026-08-21",
                     urgency=Urgency.STANDARD),
        account_balance=1000.0, conviction=50.0, sizing_mode="percentage",
    )
    contracts = sizing[0] if isinstance(sizing, tuple) else sizing.get("contracts")
    assert contracts >= 1


# ── D14: balance guards ──────────────────────────────────────────────────────

@pytest.mark.parametrize("balance", [None, 0.0, -5.0])
def test_bad_balance_skips_not_crashes(config, balance):
    eng = make_engine(config)
    d = eng.evaluate(entry_signal(), account_balance=balance,
                     existing_positions=[], sizing_mode="challenge")
    assert d.action == "skip"


# ── M3: PDT window = today + previous 4 trading days ─────────────────────────

def test_pdt_window_excludes_fifth_prior_day(config):
    eng = make_engine(config)
    today = trading_date()
    inside = trading_days_ago(4)
    outside = trading_days_ago(5)
    eng._day_trade_dates = [outside.isoformat()]
    assert eng.get_day_trades_in_window() == 0
    eng._day_trade_dates = [inside.isoformat(), today.isoformat()]
    assert eng.get_day_trades_in_window() == 2


def test_pdt_malformed_entry_survives(config):
    eng = make_engine(config)
    eng._day_trade_dates = ["not-a-date", trading_date().isoformat()]
    assert eng.get_day_trades_in_window() == 1
    eng.record_day_trade()  # D12: must not raise on the malformed entry
    assert eng.get_day_trades_in_window() == 2


# ── H9: caller stop-loss unit reinterpretation ───────────────────────────────

def test_stop_loss_price_level_converted(config):
    eng = make_engine(config)
    sig = entry_signal(stop_loss=0.90, entry_price=1.20, expiry="2026-08-21")
    stop = eng._determine_stop_loss(sig, conviction=50.0)
    assert 5 <= stop <= 90
    assert stop == pytest.approx(25.0, abs=1.0)  # (1 - 0.9/1.2) * 100


def test_stop_loss_sane_percent_passthrough(config):
    eng = make_engine(config)
    sig = entry_signal(stop_loss=30, entry_price=1.20, expiry="2026-08-21")
    assert eng._determine_stop_loss(sig, conviction=50.0) == pytest.approx(30.0)


def test_stop_loss_absurd_percent_clamped(config):
    eng = make_engine(config)
    sig = entry_signal(stop_loss=250, entry_price=1.20, expiry="2026-08-21")
    assert eng._determine_stop_loss(sig, conviction=50.0) <= 90


# ── Regime hook ──────────────────────────────────────────────────────────────

def test_regime_multiplier_scales_percentage_sizing(config):
    eng = make_engine(config)
    sig = entry_signal(entry_price=0.50, expiry="2026-08-21",
                       urgency=Urgency.STANDARD)
    normal = eng._determine_sizing(sig, 1000.0, 50.0, "percentage")
    eng.set_regime(0.3, "stressed")
    stressed = eng._determine_sizing(sig, 1000.0, 50.0, "percentage")
    n = normal[0] if isinstance(normal, tuple) else normal.get("contracts")
    s = stressed[0] if isinstance(stressed, tuple) else stressed.get("contracts")
    assert s <= n


# ── H8: unresolvable expiry skips the entry ──────────────────────────────────

def test_unresolvable_expiry_skips(config):
    eng = make_engine(config)
    d = eng.evaluate(entry_signal(expiry="lolwut"), account_balance=1000.0,
                     existing_positions=[], sizing_mode="challenge")
    assert d.action == "skip"


def test_expiry_normalized_to_iso_and_0dte_derived(config):
    """Session 10f: this asserted `trading_date()` flat, so it failed every
    weekend — "0DTE" correctly rolls to the next session when today isn't one,
    and the gate is run before every deploy including on Saturdays."""
    eng = make_engine(config)
    d = eng.evaluate(entry_signal(expiry="0DTE"), account_balance=1000.0,
                     existing_positions=[], sizing_mode="challenge")
    today = trading_date()
    expected = today if is_trading_day(today) else next_trading_day(today)
    if d.action == "execute":
        assert d.expiry == expected.isoformat()
        # ...and it is only genuinely 0DTE when that resolved day IS today.
        assert d.is_0dte is (expected == today)
