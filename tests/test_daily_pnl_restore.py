"""Daily P&L survives a restart (Session 10f).

`daily_pnl` lived only in memory, in two places — TradeManager and
DecisionEngine — so every restart silently reset the 40% daily-loss circuit
breaker mid-day. docker-compose runs `restart: on-failure:10`, so a bad day
plus a crash loop handed the bot a fresh 40% of rope up to ten times.

It is rebuilt from trades.json rather than snapshotted: the ledger already
records `pnl_usd` and an ET timestamp on every CLOSE and TRIM, and recomputing
is idempotent, so repeated restarts in one day converge on the same number.
"""
import json

import pytest

from engine.decision_engine import DecisionEngine
from management.trade_manager import TradeManager
from utils import market_time


class SilentExec:
    def __getattr__(self, name):
        def _noop(*a, **k):
            return None
        return _noop


def make_manager(config, engine=None):
    return TradeManager(config, executor=SilentExec(),
                        decision_engine=engine, notifier=None)


def _event(action, pnl, when, ticker="S"):
    return {
        "timestamp": when, "action": action, "ticker": ticker,
        "direction": "call", "strike": 22.0, "expiry": "2026-09-18",
        "contracts": 1, "contracts_remaining": 0, "entry_price": 0.95,
        "current_price": 0.80, "pnl_pct": -15.0, "pnl_usd": pnl,
        "reason": "stop_loss", "source": "caller_a-challenge-challenge",
    }


def _today_at(hhmm):
    d = market_time.trading_date().isoformat()
    return f"{d}T{hhmm}:00.000000-04:00"


def _write(m, events):
    m.trade_log_path.write_text(json.dumps(events), encoding="utf-8")


# ── the core contract ────────────────────────────────────────────────────────

def test_todays_realized_pnl_is_rebuilt(config):
    m = make_manager(config)
    _write(m, [
        _event("CLOSE", -120.0, _today_at("10:15")),
        _event("TRIM", -35.5, _today_at("11:40")),
        _event("CLOSE", 60.0, _today_at("14:02")),
    ])
    assert m.restore_daily_pnl_from_trade_log() == pytest.approx(-95.5)
    assert m.daily_pnl == pytest.approx(-95.5)


def test_the_circuit_breaker_sees_it(config):
    """The breaker gates on DecisionEngine.daily_pnl, a different object — the
    bridge has to be crossed or the restore is cosmetic."""
    eng = DecisionEngine(config)
    m = make_manager(config, engine=eng)
    _write(m, [_event("CLOSE", -410.0, _today_at("10:15"))])
    m.restore_daily_pnl_from_trade_log()
    assert eng.daily_pnl == pytest.approx(-410.0)


def test_a_blown_day_still_halts_after_restart(config):
    """End to end: down 41% of a $1,000 account, restart, breaker must hold."""
    eng = DecisionEngine(config)
    m = make_manager(config, engine=eng)
    _write(m, [_event("CLOSE", -410.0, _today_at("10:15"))])
    m.restore_daily_pnl_from_trade_log()
    threshold = 1000.0 * eng.risk_config["circuit_breaker_daily_loss_pct"] / 100
    assert eng.daily_pnl < -threshold, (
        "restored loss no longer trips the breaker"
    )


def test_restore_is_idempotent_across_repeated_restarts(config):
    """compose allows ten restarts — the accumulator must not compound."""
    eng = DecisionEngine(config)
    m = make_manager(config, engine=eng)
    _write(m, [_event("CLOSE", -120.0, _today_at("10:15"))])
    for _ in range(5):
        m.restore_daily_pnl_from_trade_log()
    assert m.daily_pnl == pytest.approx(-120.0)
    assert eng.daily_pnl == pytest.approx(-120.0)


# ── what must NOT be counted ─────────────────────────────────────────────────

def test_earlier_days_are_excluded(config):
    m = make_manager(config)
    _write(m, [
        _event("CLOSE", -500.0, "2026-03-16T14:58:20.785356-04:00"),
        _event("CLOSE", -40.0, _today_at("10:15")),
    ])
    assert m.restore_daily_pnl_from_trade_log() == pytest.approx(-40.0)


def test_opens_are_not_counted(config):
    """An OPEN carries pnl_usd 0, but counting it at all would be a bug the day
    the ledger format changes."""
    m = make_manager(config)
    opened = _event("OPEN", 999.0, _today_at("09:40"))
    opened["action"] = "OPEN"
    _write(m, [opened, _event("CLOSE", -40.0, _today_at("10:15"))])
    assert m.restore_daily_pnl_from_trade_log() == pytest.approx(-40.0)


def test_naive_legacy_timestamps_are_read_as_eastern(config):
    """Pre-Session-9 rows have no offset. Letting astimezone() guess from the
    container clock would silently mis-bucket them across the date boundary."""
    m = make_manager(config)
    naive_today = f"{market_time.trading_date().isoformat()}T12:00:00.000000"
    _write(m, [_event("CLOSE", -75.0, naive_today)])
    assert m.restore_daily_pnl_from_trade_log() == pytest.approx(-75.0)


def test_unparseable_timestamps_are_skipped_not_fatal(config):
    m = make_manager(config)
    _write(m, [
        _event("CLOSE", -999.0, "not-a-timestamp"),
        _event("CLOSE", -40.0, _today_at("10:15")),
    ])
    assert m.restore_daily_pnl_from_trade_log() == pytest.approx(-40.0)


# ── it must never block startup ──────────────────────────────────────────────

def test_missing_trade_log_returns_zero(config):
    m = make_manager(config)
    assert m.restore_daily_pnl_from_trade_log() == 0.0


def test_corrupt_trade_log_returns_zero(config):
    m = make_manager(config)
    m.trade_log_path.write_text("{ not json", encoding="utf-8")
    assert m.restore_daily_pnl_from_trade_log() == 0.0


def test_empty_ledger_returns_zero(config):
    m = make_manager(config)
    _write(m, [])
    assert m.restore_daily_pnl_from_trade_log() == 0.0


def test_reset_date_is_stamped_so_nothing_zeroes_it(config):
    """DecisionEngine._reset_daily_pnl_if_new_day() fires on the next
    record_realized_pnl(); if the stamp were stale it would wipe the restore."""
    eng = DecisionEngine(config)
    m = make_manager(config, engine=eng)
    _write(m, [_event("CLOSE", -120.0, _today_at("10:15"))])
    m.restore_daily_pnl_from_trade_log()

    eng.record_realized_pnl(-30.0)
    assert eng.daily_pnl == pytest.approx(-150.0), "the restore was reset away"
