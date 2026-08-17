"""Shared fixtures for the Session 9 test suite.

Run from the project root:  python -m pytest tests/ -q
The suite covers the money-math and date logic changed in Session 9 — run it
before every deploy and after any edit to engine/, management/, execution/,
parser/, or utils/market_time.py.
"""
import sys
from pathlib import Path

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def _scratch_cwd(tmp_path_factory):
    """One throwaway directory for the whole session.

    Session-scoped on purpose: an earlier version of the fixture below asked
    for `tmp_path`, which meant a fresh directory per test — 640 of them, and
    on Windows that took the suite from 17 seconds to over five minutes.
    """
    return tmp_path_factory.mktemp("cwd")


@pytest.fixture(autouse=True)
def _isolate_from_live_state(_scratch_cwd, monkeypatch):
    """No test starts in the project root.

    Session 13. `run_tests.bat` runs from the project root, so anything that
    resolves a RELATIVE path is reading and writing the running bot's state:
    DiscordMonitor's `./logs/discord_seen.json` and `./logs/channel_history.json`,
    the position-state sidecar, the trade log.

    Both halves of that showed up on 2026-07-29:

      - `test_unmonitored_channel_does_not_consume_the_ring` asserts a fresh
        dedup ring and got 2,000 real message IDs loaded off disk. So the suite
        passed on any machine that had never run the bot and failed on the one
        that had — the exact inversion of what a test is for.
      - the suite rewrote `logs/channel_history.json`. Nothing was corrupted
        that time, but a test writing a different shape would be editing the
        live bot's memory of recent messages.

    The `config` fixture below has always given its tests a private directory,
    and still does — it runs after this one and chdirs again. This only covers
    the tests that ask for nothing, which are exactly the ones nobody thought
    about, and a shared scratch directory is enough for them: they are not
    writing ledgers, they were simply inheriting the repo root.
    """
    monkeypatch.chdir(_scratch_cwd)


@pytest.fixture()
def config(tmp_path, monkeypatch):
    """The shipped config.example.yaml, loaded raw (env placeholders left as
    strings), with CWD moved to a per-test tmp dir so ./logs artifacts never
    collide between tests (these are the tests that write trades.json and the
    position sidecar, so they need a private directory, not the shared scratch
    one)."""
    with open(PROJECT_ROOT / "config.example.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    monkeypatch.chdir(tmp_path)
    return cfg


class StubExecutor:
    """Minimal executor stub for engine/manager tests."""

    def __init__(self, closes=None, strikes=None):
        self._closes = closes or []
        self._strikes = strikes or []

    def get_daily_closes(self, symbol, days):
        return self._closes[-days:]

    def get_tradable_strikes(self, ticker, expiry_iso, option_type):
        return self._strikes

    def get_account_balance(self):
        return 1000.0


@pytest.fixture()
def stub_executor():
    return StubExecutor()


@pytest.fixture()
def fast_sell(config):
    """Short sell-confirmation timeout for money-path tests.

    Session 17: promoted from test_session16_expiry_and_rest.py so the BUG-38
    diagnostics tests can drive the same real sell path without importing a
    fixture out of another test module.
    """
    config["management"]["sell_fill_timeout_seconds"] = 0.05
    return config


@pytest.fixture()
def fast_rest(config):
    """The resting path's real branching, in fractions of a second.

    Session 17: promoted from test_session16_expiry_and_rest.py so the BUG-38
    round-3 tests can drive the same real rest window. The margin sits just
    under the 15 minutes `at_1545` leaves on the clock, so
    `_rest_timeout_seconds` still takes its real branch and still returns a
    window LONGER than a normal attempt. Nothing under test is stubbed.
    """
    config["management"]["sell_fill_timeout_seconds"] = 0.05
    config["management"]["zero_dte_rest_margin_minutes"] = 14.99   # ≈0.6s rest
    return config


@pytest.fixture()
def at_1545(monkeypatch):
    """15:45 ET on a trading day — the 0DTE sweep's own slot, 15 min to close.

    Session 17: promoted from test_session16_expiry_and_rest.py. Without this
    the rest window cannot open at all (`seconds_until_close` returns None
    outside the session), so any test of resting behaviour silently exercises
    the ordinary two-attempt path instead.
    """
    from datetime import datetime
    from utils import market_time
    from utils.market_time import ET

    now = datetime(2026, 7, 30, 15, 45, tzinfo=ET)
    monkeypatch.setattr(market_time, "now_et", lambda: now)
    monkeypatch.setattr(market_time, "is_market_hours", lambda *a, **k: True)
    monkeypatch.setattr(market_time, "is_trading_day", lambda *a, **k: True)
    return now
