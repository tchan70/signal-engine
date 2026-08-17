"""utils/market_time.py — the foundation every Session 9 date fix rests on."""
from datetime import date

from utils.market_time import (
    is_market_holiday,
    is_trading_day,
    next_trading_day,
    normalize_expiry,
    this_or_next_friday,
    trading_days_ago,
)

# A known Wednesday with no nearby holiday
WED = date(2026, 7, 22)


def test_holidays_2026():
    assert is_market_holiday(date(2026, 7, 3))       # July 4th observed
    assert is_market_holiday(date(2026, 4, 3))       # Good Friday
    assert not is_market_holiday(date(2026, 7, 6))


def test_trading_day_skips_weekend_and_holiday():
    assert is_trading_day(WED)
    assert not is_trading_day(date(2026, 7, 25))     # Saturday
    assert not is_trading_day(date(2026, 7, 3))      # holiday
    # Thursday Jul 2 → next trading day skips the Jul 3 holiday AND the weekend
    assert next_trading_day(date(2026, 7, 2)) == date(2026, 7, 6)


def test_trading_days_ago_holiday_aware():
    # From Mon Jul 6, 2 trading days back crosses the Jul 3 holiday: Jul 2, Jul 1
    assert trading_days_ago(2, date(2026, 7, 6)) == date(2026, 7, 1)


def test_pdt_window_shape():
    # FINRA window = today + previous 4 trading days → cutoff 4 back
    cutoff = trading_days_ago(4, WED)
    assert cutoff == date(2026, 7, 16)  # Thu prior week


def test_normalize_0dte_and_1dte():
    assert normalize_expiry("0DTE", today=WED) == "2026-07-22"
    assert normalize_expiry("1DTE", today=WED) == "2026-07-23"
    # 1DTE on a Friday rolls over the weekend
    assert normalize_expiry("1DTE", today=date(2026, 7, 24)) == "2026-07-27"
    # 0DTE "today" on a Saturday resolves to Monday, never a dead date
    assert normalize_expiry("0DTE", today=date(2026, 7, 25)) == "2026-07-27"


def test_normalize_weekly():
    assert normalize_expiry("WEEKLY", today=WED) == "2026-07-24"
    # Already Friday → same day, not next week
    assert normalize_expiry("weekly", today=date(2026, 7, 24)) == "2026-07-24"


def test_good_friday_weekly_adjustment():
    # Week of Good Friday 2026 (Apr 3): Friday is a holiday → Thursday Apr 2
    assert this_or_next_friday(date(2026, 3, 30)) == date(2026, 4, 2)


def test_normalize_month_day_year_roll():
    # "1/16" said in late December means NEXT January (LOW-3 fix)
    assert normalize_expiry("1/16", today=date(2026, 12, 28)) == "2027-01-16"
    assert normalize_expiry("7/31", today=WED) == "2026-07-31"


def test_normalize_explicit_and_contract_formats():
    assert normalize_expiry("2026-08-21", today=WED) == "2026-08-21"
    assert normalize_expiry("260821", today=WED) == "2026-08-21"   # YYMMDD
    assert normalize_expiry("8/21/26", today=WED) == "2026-08-21"


def test_normalize_rejects_garbage_and_past():
    assert normalize_expiry("2026-01-16", today=WED) is None   # past ISO
    assert normalize_expiry("banana", today=WED) is None
    assert normalize_expiry(None, today=WED) is None
    assert normalize_expiry("", today=WED) is None


# ── Session 16: asking about history rather than about tradeability ──────────
#
# The default answers "which tradeable date is this?", so a past date is None.
# Every entry path depends on that: it is what stops a hallucinated 2019 expiry
# becoming an order. But a position we already HOLD asks the opposite question,
# and there None reads as "not expired" — which is how an expired contract
# could never be recognised as expired, and how a -$18 expiry stayed invisible
# to the ledger on 2026-07-30.

def test_the_default_still_refuses_the_past():
    """Pinned deliberately: flipping this default would let a stale expiry
    through the entry path."""
    assert normalize_expiry("2026-01-16", today=WED) is None
    assert normalize_expiry(date(2026, 1, 16), today=WED) is None
    assert normalize_expiry("260116", today=WED) is None
    assert normalize_expiry("1/16/26", today=WED) is None


def test_allow_past_resolves_a_past_date_to_itself():
    assert normalize_expiry("2026-01-16", today=WED, allow_past=True) == "2026-01-16"
    assert normalize_expiry(date(2026, 1, 16), today=WED, allow_past=True) == "2026-01-16"
    assert normalize_expiry("260116", today=WED, allow_past=True) == "2026-01-16"
    assert normalize_expiry("1/16/26", today=WED, allow_past=True) == "2026-01-16"


def test_allow_past_does_not_roll_an_unanchored_month_day_forward():
    """'7/16' on a position we hold means the 16th that has just gone, not the
    one a year from now — the roll-forward is right for entries and exactly
    wrong for history."""
    assert normalize_expiry("7/16", today=WED) == "2027-07-16"
    assert normalize_expiry("7/16", today=WED, allow_past=True) == "2026-07-16"


def test_allow_past_leaves_future_and_garbage_alone():
    assert normalize_expiry("2026-08-21", today=WED, allow_past=True) == "2026-08-21"
    assert normalize_expiry("banana", today=WED, allow_past=True) is None
    assert normalize_expiry(None, today=WED, allow_past=True) is None


def test_allow_past_is_deliberately_inert_for_relative_forms():
    """'0DTE' means "relative to today" by construction, so asking whether a
    held position stamped that way has expired is unanswerable from the string
    alone. Nothing stores them — entries normalise to ISO before a position
    exists — and if a hand-edited ledger reintroduces one it reads as
    'expires today', which writes nothing off. Pinned so the inertness is a
    decision rather than an accident."""
    assert normalize_expiry("0DTE", today=WED, allow_past=True) == "2026-07-22"
    assert normalize_expiry("1DTE", today=WED, allow_past=True) == "2026-07-23"
    assert normalize_expiry("WEEKLY", today=WED, allow_past=True) == "2026-07-24"
