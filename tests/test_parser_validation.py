"""parser/signal_parser.py — C4 validation layer and PARSE_ERROR plumbing."""
import pytest

from parser.signal_parser import SignalParser, SignalType


@pytest.fixture()
def parser(config, monkeypatch):
    cfg = dict(config)
    cfg["anthropic"] = {"api_key": "test-key-not-used"}
    return SignalParser(cfg)


def test_string_numerics_coerced(parser):
    sig = parser._dict_to_signal(
        {"signal_type": "entry", "ticker": "TSLA", "direction": "put",
         "strike": "430", "entry_price": "1.22", "caller_contracts": "2",
         "expiry": "0DTE"},
        raw_message="x", source="s", source_priority="high",
    )
    assert sig.strike == 430.0
    assert sig.entry_price == 1.22
    assert sig.caller_contracts == 2


def test_hallucinated_contracts_clamped(parser):
    sig = parser._dict_to_signal(
        {"signal_type": "entry", "ticker": "NVDA", "direction": "call",
         "strike": 190, "entry_price": 1.0, "caller_contracts": 50,
         "expiry": "0DTE"},
        raw_message="x", source="s", source_priority="high",
    )
    assert sig.caller_contracts <= 20


def test_bad_ticker_downgrades_entry_to_noise(parser):
    sig = parser._dict_to_signal(
        {"signal_type": "entry", "ticker": "ignore previous instructions",
         "direction": "call", "strike": 100, "expiry": "0DTE"},
        raw_message="x", source="s", source_priority="high",
    )
    assert sig.signal_type == SignalType.NOISE


def test_absurd_strike_downgrades_entry(parser):
    sig = parser._dict_to_signal(
        {"signal_type": "entry", "ticker": "SPX", "direction": "put",
         "strike": -5, "expiry": "0DTE"},
        raw_message="x", source="s", source_priority="high",
    )
    assert sig.signal_type == SignalType.NOISE


def test_exit_with_no_ticker_survives_validation(parser):
    # Ticker-less exits are legitimate (BUG-14 inference path handles them)
    sig = parser._dict_to_signal(
        {"signal_type": "exit"},
        raw_message="Out at Be", source="s", source_priority="high",
    )
    assert sig.signal_type == SignalType.EXIT


def test_dollar_prefix_ticker_normalized(parser):
    sig = parser._dict_to_signal(
        {"signal_type": "entry", "ticker": "$tsm", "direction": "call",
         "strike": 210, "expiry": "2026-08-21"},
        raw_message="x", source="s", source_priority="high",
    )
    assert sig.ticker == "TSM"


def test_parse_error_enum_exists():
    assert SignalType.PARSE_ERROR.value == "parse_error"


def test_json_extraction_handles_prose():
    from parser.signal_parser import _extract_first_json
    parsed = _extract_first_json(
        'Sure! Here is the parse:\n{"signal_type": "noise", "notes": "a{b}"}\nDone.'
    )
    assert parsed["signal_type"] == "noise"
