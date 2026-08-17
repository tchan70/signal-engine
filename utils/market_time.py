"""
Market time utilities — single source of truth for all trading-day/time math.

WHY THIS EXISTS (Session 9): the bot runs on a UK host (Europe/London, or UTC
inside Docker) while trading US markets.  Host-local `date.today()` rolls the
"trading day" at UK midnight = 7-8 PM ET, which is the wrong day for anything
that happens outside 14:30-21:00 UK.  Every date/time computation with trading
semantics (PDT windows, daily P&L resets, 0DTE detection, same-day checks,
market-hours gates, expiry normalization) must go through this module so it is
computed in America/New_York regardless of host clock or container TZ.

Requires the `tzdata` package on Windows (zoneinfo has no system DB there) —
already pinned in requirements.txt.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("market_time")

ET = ZoneInfo("America/New_York")

MARKET_OPEN = time(9, 30)
MARKET_CLOSE = time(16, 0)

# ── Early closes (1:00 PM ET half-days) — 2026-08-04, ladder review ──────────
# `seconds_until_close`/`is_market_hours` and everything downstream still
# hard-code 16:00 (the documented half-day hole, deliberately unchanged
# tonight — a proper per-day close belongs in a calmer session). This table
# exists so individual features can at least refuse to be CLEVER on these
# days. Consumers: the exit ladder (`_ladder_eligible` refuses — resting
# limits toward a fictional 16:00 close on a 13:00 day is exactly backwards).
# Rule: day after Thanksgiving, and Christmas Eve when it is a trading
# weekday (2027's Dec 24 is the observed Christmas holiday — no session, so
# no entry). Extend yearly with the holiday table below.
EARLY_CLOSE_DATES: frozenset[date] = frozenset({
    date(2026, 11, 27),   # day after Thanksgiving
    date(2026, 12, 24),   # Christmas Eve (Thursday session)
    date(2027, 11, 26),   # day after Thanksgiving
})


def is_early_close_day(d: date = None) -> bool:
    """True on a 1:00 PM ET half-day. See EARLY_CLOSE_DATES."""
    return (d or trading_date()) in EARLY_CLOSE_DATES


# NYSE/CBOE full-day market holidays (observed dates).
# Extend this dict once a year; unknown years fall back to weekday-only logic
# (a startup warning is logged by `check_holiday_coverage`).
US_MARKET_HOLIDAYS: dict[int, frozenset[date]] = {
    2026: frozenset({
        date(2026, 1, 1),    # New Year's Day
        date(2026, 1, 19),   # Martin Luther King Jr. Day
        date(2026, 2, 16),   # Presidents' Day
        date(2026, 4, 3),    # Good Friday
        date(2026, 5, 25),   # Memorial Day
        date(2026, 6, 19),   # Juneteenth
        date(2026, 7, 3),    # Independence Day (observed — Jul 4 is a Saturday)
        date(2026, 9, 7),    # Labor Day
        date(2026, 11, 26),  # Thanksgiving
        date(2026, 12, 25),  # Christmas
    }),
    2027: frozenset({
        date(2027, 1, 1),    # New Year's Day
        date(2027, 1, 18),   # Martin Luther King Jr. Day
        date(2027, 2, 15),   # Presidents' Day
        date(2027, 3, 26),   # Good Friday
        date(2027, 5, 31),   # Memorial Day
        date(2027, 6, 18),   # Juneteenth (observed — Jun 19 is a Saturday)
        date(2027, 7, 5),    # Independence Day (observed — Jul 4 is a Sunday)
        date(2027, 9, 6),    # Labor Day
        date(2027, 11, 25),  # Thanksgiving
        date(2027, 12, 24),  # Christmas (observed — Dec 25 is a Saturday)
    }),
}


def now_et() -> datetime:
    """Current wall-clock time in US/Eastern (tz-aware)."""
    return datetime.now(ET)


def trading_date(dt: datetime = None) -> date:
    """
    The current US-market calendar date.  Use this EVERYWHERE the code
    previously used `date.today()` with trading semantics.
    """
    return (dt.astimezone(ET) if dt else now_et()).date()


def is_market_holiday(d: date) -> bool:
    holidays = US_MARKET_HOLIDAYS.get(d.year)
    if holidays is None:
        return False  # unknown year — weekday-only fallback
    return d in holidays


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not is_market_holiday(d)


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def previous_trading_day(d: date) -> date:
    prev = d - timedelta(days=1)
    while not is_trading_day(prev):
        prev -= timedelta(days=1)
    return prev


def trading_days_ago(n: int, from_date: date = None) -> date:
    """The trading day `n` trading days before `from_date` (default: today ET)."""
    d = from_date or trading_date()
    for _ in range(n):
        d = previous_trading_day(d)
    return d


def is_market_hours(dt: datetime = None) -> bool:
    """True during US regular trading hours (9:30-16:00 ET on a trading day)."""
    t = (dt.astimezone(ET) if dt else now_et())
    if not is_trading_day(t.date()):
        return False
    return MARKET_OPEN <= t.time() < MARKET_CLOSE


def seconds_since_open(dt: datetime = None) -> Optional[float]:
    """Seconds elapsed since today's 9:30 ET open, or None if not applicable.

    None means "the settling window does not apply": a non-trading day, or
    before the open, or after the close. A float means we are inside the
    session and this many seconds have passed since the bell.

    Session 10f. Added for the opening-bell settle: on 2026-07-27 the first
    quote after the open printed TE at $1.02 against a real $0.55, which
    cleared the 60% trailing-stop activation, armed the trail at $0.82, and
    fired six seconds later when the quote reverted. S spiked the same instant
    and touched $1.00+ on 4 ticks out of 6,171 that day — all 17 seconds of it
    at the open.
    """
    t = (dt.astimezone(ET) if dt else now_et())
    if not is_trading_day(t.date()):
        return None
    if not (MARKET_OPEN <= t.time() < MARKET_CLOSE):
        return None
    open_dt = t.replace(
        hour=MARKET_OPEN.hour, minute=MARKET_OPEN.minute,
        second=0, microsecond=0,
    )
    return (t - open_dt).total_seconds()


def seconds_until_close(dt: datetime = None) -> Optional[float]:
    """Seconds remaining until today's 16:00 ET close, or None if not applicable.

    The mirror of `seconds_since_open`, and it answers the same shape of
    question: None means "there is no session clock running right now" (a
    non-trading day, before the bell, or after the close), a float means we are
    inside the session with this much of it left.

    Session 16. Added for the 0DTE forced exit: a dying contract's only chance
    of a fill is a limit order left resting into the close, and "how long may I
    rest?" is exactly this number minus a safety margin. Callers must treat
    None as "do not rest" rather than as zero — outside the session there is no
    deadline to rest until, and defaulting to 0 would silently turn the rest
    into a no-op that looks like it ran.
    """
    t = (dt.astimezone(ET) if dt else now_et())
    if not is_trading_day(t.date()):
        return None
    if not (MARKET_OPEN <= t.time() < MARKET_CLOSE):
        return None
    close_dt = t.replace(
        hour=MARKET_CLOSE.hour, minute=MARKET_CLOSE.minute,
        second=0, microsecond=0,
    )
    return (close_dt - t).total_seconds()


def is_past_market_close(dt: datetime = None) -> bool:
    """True once today's session is OVER — at/after 16:00 ET on a trading
    day, or any time on a non-trading day... no: a non-trading day has no
    session to be past, so it returns False there (callers gating "book
    today's expiry" must not fire on a weekend for a Friday contract —
    that contract is already `expiry_date < today` by then).

    Money-path review 2026-08-04 (D-7): added for the startup expiry
    reconcile — after the close there is no 16:05 sweep left to defer to.
    Same half-day caveat as seconds_until_close: hard-codes 16:00.
    """
    t = (dt.astimezone(ET) if dt else now_et())
    if not is_trading_day(t.date()):
        return False
    return t.time() >= MARKET_CLOSE


def this_or_next_friday(d: date) -> date:
    """The nearest Friday >= d, adjusted back if that Friday is a holiday."""
    days_ahead = (4 - d.weekday()) % 7  # 0 if d is already Friday
    friday = d + timedelta(days=days_ahead)
    while not is_trading_day(friday):
        friday = previous_trading_day(friday)
    if friday < d:  # holiday adjustment walked before d — push forward instead
        friday = next_trading_day(d)
    return friday


def check_holiday_coverage() -> None:
    """Log a loud warning if the calendar tables don't cover the current year.

    2026-08-04 (ladder review round 2): also covers EARLY_CLOSE_DATES — an
    unknown year there silently means "no half-days", which re-opens the
    exact hole the early-close gate closed (the ladder resting toward a
    fictional 16:00 on a 13:00 day). The comment on the table is not a
    mechanism; this warning is.
    """
    yr = trading_date().year
    for y in (yr, yr + 1):
        if y not in US_MARKET_HOLIDAYS:
            logger.warning(
                f"US_MARKET_HOLIDAYS has no entries for {y} — trading-day math "
                f"falls back to weekday-only. Update utils/market_time.py."
            )
        if not any(d.year == y for d in EARLY_CLOSE_DATES):
            logger.warning(
                f"EARLY_CLOSE_DATES has no entries for {y} — half-day guards "
                f"(exit ladder) silently treat every day as a full session. "
                f"Update utils/market_time.py."
            )


# ── Expiry normalization ─────────────────────────────────────────────────────

_MDY_RE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?$")
_YYMMDD_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})$")


def normalize_expiry(raw, today: date = None, allow_past: bool = False) -> Optional[str]:
    """
    Normalize any expiry representation the parser can emit into an ISO
    YYYY-MM-DD string, or None if it cannot be resolved.

    Handles: date/datetime objects, ISO strings, "0DTE", "1DTE", "WEEKLY"/
    "WEEKLIES", "M/D", "M/D/YY", "M/D/YYYY", "YYMMDD" (Robinhood contract
    style).  All relative terms resolve against the US-market calendar
    (weekend- and holiday-aware).  Dates that resolve to the past roll to the
    next plausible occurrence for M/D forms, and return None otherwise.

    `allow_past` (Session 16) turns off that last rule. The default answers the
    question every ENTRY path asks — "which tradeable date is this?" — and a
    date in the past is not a tradeable date, so None is the right answer and
    a hallucinated 2019 expiry cannot become an order.

    But a position we already HOLD asks the opposite question: "has this
    expired yet?", and there None reads as "no". That is how an expired
    contract could never be identified as expired: the sweep asked
    `normalize_expiry(...) != today`, got None, and skipped the very position
    it was looking for. With `allow_past` a past date resolves to itself, and
    an unanchored "M/D" resolves within the current year instead of rolling
    forward into the next one.

    The flag only affects forms that name an absolute date. Relative forms
    ("0DTE", "1DTE", "WEEKLY") mean "relative to TODAY" by construction and
    still resolve forward — asking whether a held position stamped "0DTE" has
    expired is unanswerable from the string alone.

    ⚠️ That resolution is "expires TODAY", and the expiry sweep books what
    expires today. So a position carrying a literal "0DTE" expiry would be
    written off at the first 16:05 sweep after it was stored, on whatever day
    that happened to be. Nothing stores one — entries normalise to ISO before
    a position exists, and every ledger row is written from that — but a
    hand-edited trades.json could, and it would be believed. Write ISO dates.
    """
    today = today or trading_date()

    def _resolve(d: date) -> Optional[str]:
        """Past dates: themselves when asked about history, None when asked
        what is tradeable."""
        if d >= today or allow_past:
            return d.isoformat()
        return None

    if raw is None:
        return None
    if isinstance(raw, datetime):
        raw = raw.date()
    if isinstance(raw, date):
        return _resolve(raw)

    s = str(raw).strip().upper()
    if not s:
        return None

    if s in ("0DTE", "0D", "TODAY"):
        d = today if is_trading_day(today) else next_trading_day(today)
        return d.isoformat()

    if s in ("1DTE", "1D", "TOMORROW"):
        return next_trading_day(today).isoformat()

    if s in ("WEEKLY", "WEEKLIES", "THIS WEEK"):
        return this_or_next_friday(today).isoformat()

    # ISO YYYY-MM-DD
    try:
        d = date.fromisoformat(s)
        return _resolve(d)
    except ValueError:
        pass

    # M/D, M/D/YY, M/D/YYYY
    m = _MDY_RE.match(s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year_part = m.group(3)
        try:
            if year_part:
                year = int(year_part)
                if year < 100:
                    year += 2000
                d = date(year, month, day)
                return _resolve(d)
            # No year: assume this year, roll forward if it lands in the past
            # (e.g. "1/16" parsed in late December means NEXT January). Asking
            # about history, the roll-forward is exactly wrong — a held
            # position stamped "7/24" means the 7/24 that has just gone.
            d = date(today.year, month, day)
            if d < today and not allow_past:
                d = date(today.year + 1, month, day)
            return d.isoformat()
        except ValueError:
            return None

    # YYMMDD (Robinhood contract symbol style, e.g. 260320)
    m = _YYMMDD_RE.match(s)
    if m:
        try:
            d = date(2000 + int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return _resolve(d)
        except ValueError:
            return None

    return None
