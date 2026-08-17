"""eval/parser_eval.py — parallel case runner (Session 10f).

The eval was serial: 270 cases at a second or two each is 7-10 minutes for a
gate meant to run before every deploy. The calls are independent and the parser
holds no mutable state across them, so they parallelise — but naively, that
would have thrown away the prompt-cache saving. The ~8k-token system prompt is
sent with `cache_control: ephemeral`; N workers starting cold all miss it and
each pays full price for the whole prefix.

Hence: warm on one case, then fan out. These tests pin the two properties that
matter — exactly one call before the pool, and results in INPUT order. A silent
reordering would attach PASS/FAIL to the wrong case ids, which is worse than
being slow.
"""
import importlib.util
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "parser_eval", ROOT / "eval" / "parser_eval.py"
)
parser_eval = importlib.util.module_from_spec(_spec)
sys.modules["parser_eval"] = parser_eval
_spec.loader.exec_module(parser_eval)


class FakeParser:
    """Records call ordering and peak concurrency; returns the case id back."""

    def __init__(self, delay=0.02):
        self.delay = delay
        self.lock = threading.Lock()
        self.started = []
        self.in_flight = 0
        self.peak_concurrency = 0
        self.first_finished_at = None

    def parse_text_signal(self, message, source, source_priority="high"):
        with self.lock:
            self.started.append(message)
            self.in_flight += 1
            self.peak_concurrency = max(self.peak_concurrency, self.in_flight)
        # later cases finish sooner, so input order != completion order
        time.sleep(self.delay * (1 + (len(self.started) % 3)))
        with self.lock:
            self.in_flight -= 1
            if self.first_finished_at is None:
                self.first_finished_at = time.monotonic()
        return f"parsed:{message}"


def cases(n):
    return [{"id": f"c{i}", "message": f"m{i}", "source": "caller_a-challenge-challenge"}
            for i in range(n)]


# ── ordering: the property that would silently corrupt the report ────────────

def test_results_come_back_in_input_order():
    c = cases(30)
    out = parser_eval._run_cases(FakeParser(), c, workers=8)
    assert out == [f"parsed:m{i}" for i in range(30)]


def test_order_matches_the_serial_run_exactly():
    c = cases(20)
    serial = parser_eval._run_cases(FakeParser(), c, workers=1)
    parallel = parser_eval._run_cases(FakeParser(), c, workers=8)
    assert serial == parallel


def test_completion_order_really_does_differ_from_input_order():
    """Otherwise the ordering test above proves nothing."""
    fake = FakeParser()
    parser_eval._run_cases(fake, cases(30), workers=8)
    assert fake.started != [f"m{i}" for i in range(30)] or fake.peak_concurrency > 1


# ── the cache warm-up ────────────────────────────────────────────────────────

def test_exactly_one_call_precedes_the_pool():
    """Fanning out cold would make every worker miss the ~8k-token cached
    prefix and pay full input price for it."""
    fake = FakeParser(delay=0.05)
    parser_eval._run_cases(fake, cases(12), workers=8)
    # the warm-up case is dispatched and completed alone
    assert fake.started[0] == "m0"


def test_warmup_finishes_before_any_other_call_starts():
    order = []
    lock = threading.Lock()

    class Tracking(FakeParser):
        def parse_text_signal(self, message, source, source_priority="high"):
            with lock:
                order.append(("start", message))
            time.sleep(0.03)
            with lock:
                order.append(("end", message))
            return f"parsed:{message}"

    parser_eval._run_cases(Tracking(), cases(10), workers=8)
    # first two events must be the warm-up case starting and finishing
    assert order[0] == ("start", "m0")
    assert order[1] == ("end", "m0"), (
        f"a second call started before the warm-up finished: {order[:4]}"
    )


# ── it is actually parallel ──────────────────────────────────────────────────

def test_the_pool_really_runs_concurrently():
    fake = FakeParser(delay=0.05)
    parser_eval._run_cases(fake, cases(24), workers=8)
    assert fake.peak_concurrency > 1, "no concurrency — still serial"


def test_workers_1_stays_serial():
    fake = FakeParser(delay=0.01)
    parser_eval._run_cases(fake, cases(10), workers=1)
    assert fake.peak_concurrency == 1


def test_parallel_is_faster_than_serial():
    c = cases(24)
    t0 = time.monotonic()
    parser_eval._run_cases(FakeParser(delay=0.02), c, workers=1)
    serial = time.monotonic() - t0
    t0 = time.monotonic()
    parser_eval._run_cases(FakeParser(delay=0.02), c, workers=8)
    parallel = time.monotonic() - t0
    assert parallel < serial * 0.7, f"serial {serial:.2f}s vs parallel {parallel:.2f}s"


# ── edges ────────────────────────────────────────────────────────────────────

def test_empty_case_list():
    assert parser_eval._run_cases(FakeParser(), [], workers=8) == []


def test_single_case_is_just_the_warmup():
    fake = FakeParser()
    out = parser_eval._run_cases(fake, cases(1), workers=8)
    assert out == ["parsed:m0"]
    assert len(fake.started) == 1


def test_more_workers_than_cases():
    out = parser_eval._run_cases(FakeParser(), cases(3), workers=32)
    assert out == ["parsed:m0", "parsed:m1", "parsed:m2"]


def test_a_failing_case_propagates_rather_than_silently_scoring_wrong():
    class Boom(FakeParser):
        def parse_text_signal(self, message, source, source_priority="high"):
            if message == "m5":
                raise RuntimeError("api exploded")
            return f"parsed:{message}"

    with pytest.raises(RuntimeError):
        parser_eval._run_cases(Boom(), cases(10), workers=4)
