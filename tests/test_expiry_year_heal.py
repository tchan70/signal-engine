"""BUG-41 (2026-08-05, DJT): model invents a year for a bare M/D expiry.

Live miss: caller_a posted "DJT $16 Call 11/20 • 1 Buy 0.85 BUY LIMIT" and the
parser LLM emitted expiry "2025-11-20" — despite "Today's date: 2026-08-05"
in the prompt, its training-year prior chose the year. The (correct) past-date
drop then discarded a real entry → SKIP "unresolvable expiry: None".

The heal in _validate_and_coerce recovers ONLY the provably-invented-year
case: emitted expiry is ISO-shaped, the caller literally wrote that M/D in
the message, and the emitted year appears nowhere in the message (4-digit or
"/YY" form). Everything else keeps today's fail-closed drop.

Calendar is PINNED (2026-08-05) — date-literal tests rot otherwise.
"""
from datetime import date

import pytest

from parser.signal_parser import SignalParser, SignalType

TODAY = date(2026, 8, 5)  # Wednesday, a trading day

DJT_RAW = "DJT $16 Call 11/20 • 1 Buy 0.85 BUY LIMIT <@&1494187024037445715> [caller: caller_a]"


@pytest.fixture()
def parser(config, monkeypatch):
    cfg = dict(config)
    cfg["anthropic"] = {"api_key": "test-key-not-used"}
    p = SignalParser(cfg)
    # Pin the ambient calendar: today is always 2026-08-05 in these tests.
    monkeypatch.setattr(type(p), "today", property(lambda self: TODAY))
    return p


def _entry(parser, expiry, raw):
    return parser._dict_to_signal(
        {"signal_type": "entry", "ticker": "DJT", "direction": "call",
         "strike": 16, "entry_price": 0.85, "expiry": expiry},
        raw_message=raw, source="s", source_priority="high",
    )


# ── The live DJT case ────────────────────────────────────────────────────────

def test_djt_invented_year_heals_to_caller_md(parser):
    sig = _entry(parser, "2025-11-20", DJT_RAW)
    assert sig.expiry == "2026-11-20"
    assert sig.signal_type == SignalType.ENTRY


def test_heal_is_noted_not_silent(parser):
    data, notes = parser._validate_and_coerce(
        {"signal_type": "entry", "ticker": "DJT", "direction": "call",
         "strike": 16, "expiry": "2025-11-20"},
        raw_message=DJT_RAW,
    )
    assert data["expiry"] == "2026-11-20"
    assert any("expiry year corrected" in n for n in notes)


# ── Fail-closed guards: every doubt keeps the drop ───────────────────────────

def test_caller_wrote_two_digit_year_never_second_guessed(parser):
    # "11/20/25" — the caller DID write a year; a past expiry stays dropped.
    sig = _entry(parser, "2025-11-20", "SPY $500 Call 11/20/25 1.00")
    assert sig.expiry is None


def test_caller_wrote_four_digit_year_never_second_guessed(parser):
    sig = _entry(parser, "2025-11-20", "SPY 500c exp 11/20 2025 lotto")
    assert sig.expiry is None


def test_hallucinated_whole_date_still_drops(parser):
    # M/D appears nowhere in the message → check 2 fails → no heal.
    sig = _entry(parser, "2019-03-15", "TSLA calls looking juicy here")
    assert sig.expiry is None


def test_empty_raw_message_fails_closed(parser):
    sig = _entry(parser, "2025-11-20", "")
    assert sig.expiry is None


def test_non_iso_garbage_takes_normal_drop_path(parser):
    sig = _entry(parser, "garbage", DJT_RAW)
    assert sig.expiry is None


# ── Untouched paths ──────────────────────────────────────────────────────────

def test_future_iso_passes_through_unhealed(parser):
    sig = _entry(parser, "2026-11-20", DJT_RAW)
    assert sig.expiry == "2026-11-20"


def test_bare_md_kept_raw_and_resolvable(parser):
    # The prompt now asks for this form. Validation keeps the RAW form
    # (engine normalizes at decision time — Session 9 H8); it must survive
    # validation un-dropped and resolve to the right date.
    from utils import market_time

    sig = _entry(parser, "11/20", DJT_RAW)
    assert sig.expiry == "11/20"
    assert market_time.normalize_expiry(sig.expiry, today=TODAY) == "2026-11-20"


def test_0dte_untouched(parser):
    sig = _entry(parser, "0DTE", "DJT 16c 0DTE 0.85")
    assert sig.expiry == "0DTE"


# ── Heal-path breadth ────────────────────────────────────────────────────────

def test_zero_padded_md_in_message_heals(parser):
    sig = _entry(parser, "2025-08-21", "IBM $270 Call 08/21 • 1 Buy 0.57")
    assert sig.expiry == "2026-08-21"


def test_md_before_today_rolls_to_next_year(parser):
    # Caller wrote "1/16" in August — the next 1/16 is 2027.
    sig = _entry(parser, "2025-01-16", "AAPL $250 Call 1/16 leaps 3.20")
    assert sig.expiry == "2027-01-16"


def test_bare_yy_in_price_does_not_block_heal(parser):
    # "0.25" contains "25" — a price digit-collision must not turn a real
    # heal into a drop.
    sig = _entry(parser, "2025-11-20", "DJT $16 Call 11/20 tp 0.25")
    assert sig.expiry == "2026-11-20"


# ── Round 2 (adversarial review, 2026-08-05): confirmed-defect regressions ───

def test_substring_md_never_blesses_heal(parser):
    # Review F1: "2/20" is a SUBSTRING of "12/20" — a hallucinated 2025-02-20
    # must NOT heal off a message whose only date is 12/20.
    sig = _entry(parser, "2025-02-20", "SPY $600 Call 12/20 • 1 Buy 1.50")
    assert sig.expiry is None


def test_substring_md_never_blesses_heal_day_prefix(parser):
    # Review F1b: "1/6" ⊂ "11/6".
    sig = _entry(parser, "2025-01-06", "NVDA $190 Call 11/6 • 1 Buy 2.10")
    assert sig.expiry is None


def test_day_25_heals_despite_invented_2025(parser):
    # Review F2: "/25" is the DAY in "11/25", not a year — the exact invented
    # year (2025) on a 25th-of-month expiry must still heal. This is the
    # original BUG-41 recurring on the most common invented year.
    sig = _entry(parser, "2025-11-25", "AAPL $250 Call 11/25 • 2 Buys 1.50")
    assert sig.expiry == "2026-11-25"


def test_invented_2020_on_djt_message_heals(parser):
    # Review F2: "/20" is the DAY in "11/20" — invented year 2020 must heal.
    sig = _entry(parser, "2020-11-20", DJT_RAW)
    assert sig.expiry == "2026-11-20"


def test_year_digits_inside_bigger_number_do_not_block(parser):
    # Review F2: "$20250" contains "2025" but not as a token.
    sig = _entry(parser, "2025-11-20", "NDX $20250 Call 11/20 • 1 Buy 12.00")
    assert sig.expiry == "2026-11-20"


def test_year_written_as_separate_token_blocks_heal(parser):
    sig = _entry(parser, "2025-11-20", "SPY 500c exp 11/20 2025 lotto")
    assert sig.expiry is None


def test_context_md_never_blesses_heal(parser):
    # Review F3: the M/D lives ONLY in an injected [RECENT CONTEXT] block —
    # a previous message's trade must not bless the current signal's heal.
    raw = (
        '[RECENT CONTEXT — caller_a: "DJT $16 Call 11/20 • 1 Buy 0.85"]\n'
        "adding 1 more GOOG @everyone [caller: caller_a]"
    )
    sig = _entry(parser, "2025-11-20", raw)
    assert sig.expiry is None


def test_own_text_md_heals_with_context_present(parser):
    # Converse of the above: context blocks must not BREAK a legitimate heal
    # when the caller's own line does contain the M/D.
    raw = (
        '[REPLYING TO: "some earlier chatter 2025"]\n'
        "DJT $16 Call 11/20 • 1 Buy 0.85 [caller: caller_a]"
    )
    sig = _entry(parser, "2025-11-20", raw)
    assert sig.expiry == "2026-11-20"


def test_caller_wrote_different_year_than_emitted_still_drops(parser):
    # Review (money-path #2): caller wrote 1/15/28; model emitted 2026-01-15.
    # The written year differs from the emitted one — must NOT heal to 2027.
    sig = _entry(parser, "2026-01-15", "TSLA 1/15/28 300c leaps 12.00")
    assert sig.expiry is None


# ── Round 3: decimal-quote collisions (review N1) ────────────────────────────

def test_bid_ask_quote_never_blesses_heal(parser):
    # "2.10/2.30" contains "10/2" — behind a decimal point, not a date the
    # caller wrote. Must drop.
    sig = _entry(parser, "2025-10-02", "SPX 6300C bid/ask 2.10/2.30 grabbing some")
    assert sig.expiry is None


def test_spread_quote_never_blesses_heal(parser):
    # "0.9/1.10" contains "9/1".
    sig = _entry(parser, "2025-09-01", "TSLA 420c 0.9/1.10 on the spread, in")
    assert sig.expiry is None


def test_ratio_never_blesses_heal(parser):
    # "1.5/5" contains "5/5".
    sig = _entry(parser, "2025-05-05", "PLTR $150 Call r/r 1.5/5 looks great")
    assert sig.expiry is None


def test_sentence_ending_period_still_heals(parser):
    # The "." guard is lookbehind-only: an M/D at the end of a sentence is
    # still the caller's date.
    sig = _entry(parser, "2025-11-20", "DJT $16 calls expiring 11/20. Buy 0.85")
    assert sig.expiry == "2026-11-20"


# ── Round 2: cross-source dedup fingerprint (main.py) ────────────────────────

