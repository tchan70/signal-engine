#!/usr/bin/env python3
"""
Parser eval harness — the validation gate for ANY prompt change (Session 9, review §2b).

WARNING: this costs real API tokens (~20+ Haiku calls per full run — a few cents,
but not free). Do not wire it into anything that runs on a loop.

Usage (from the project root):
    python3 eval/parser_eval.py                    # full run
    python3 eval/parser_eval.py --limit 5          # first 5 cases only
    python3 eval/parser_eval.py --only injection   # cases whose id contains "injection"
    python3 eval/parser_eval.py --config config.yaml

Workflow:
- Run BEFORE and AFTER any edit to SIGNAL_PARSER_PROMPT (or parser code) and
  compare the per-field accuracy tables — this converts prompt editing from
  vibes into regression testing.
- Whenever a real-world misparse is found, grab the raw message from
  logs/parser.log, hand-label the expected fields, and append a new case to
  eval/labeled_signals.jsonl so that misparse can never regress silently.
- Exit code 1 if signal_type accuracy < 0.90 — usable as a pre-merge gate.
- SkillOpt-style prompt optimization can sit on top of this later: this harness
  is the scorer/validation gate. If you do that, hold out a slice of labeled
  cases the optimizer never sees (eval-set overfitting is the failure mode).

labeled_signals.jsonl format — one JSON object per line:
    {"id": str, "message": str, "source": str, "source_priority": "high",
     "expected": {field: value, ...}}
Expected-field semantics:
- Only fields present in "expected" are scored (exact match).
- A list value means "any of these is acceptable"
  (e.g. "signal_type": ["management", "noise"]).
- null means the parser must return None for that field.
- Numeric fields compare as numbers (210 == 210.0); ticker is case-insensitive.
- "expiry" expectations are the raw parser strings ("0DTE", "1DTE", "3/21", ISO).
"""

import argparse
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml
from dotenv import load_dotenv

from parser.signal_parser import SignalParser

CASES_PATH = ROOT / "eval" / "labeled_signals.jsonl"

# Fields compared as floats / ints (210 == 210.0, "3" == 3)
NUMERIC_FIELDS = {"strike", "entry_price", "stop_loss", "target_price", "current_price"}
INT_FIELDS = {"caller_contracts", "trim_contracts"}


def load_config(path: str) -> dict:
    """Mirror main.py's load_config: yaml with ${ENV_VAR} interpolation.

    python-dotenv loads .env first (start.py does this for the live bot).
    """
    load_dotenv(ROOT / ".env")
    with open(path, encoding="utf-8") as f:  # Session 9b: Windows defaults to cp1252
        raw = f.read()

    def replace_env(match):
        return os.environ.get(match.group(1), match.group(0))

    raw = re.sub(r"\$\{(\w+)\}", replace_env, raw)
    return yaml.safe_load(raw)


def load_cases(path: Path) -> list[dict]:
    cases = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"BAD JSONL at {path}:{lineno}: {e}", file=sys.stderr)
                sys.exit(2)
    return cases


def get_actual(signal, fld):
    """Pull a field off the ParsedSignal, unwrapping enums to their str value."""
    val = getattr(signal, fld, None)
    if hasattr(val, "value"):
        val = val.value
    return val


def value_matches(fld, expected, actual) -> bool:
    if isinstance(expected, list):
        return any(value_matches(fld, e, actual) for e in expected)
    if expected is None:
        return actual is None
    if fld in NUMERIC_FIELDS:
        try:
            return actual is not None and abs(float(expected) - float(actual)) < 1e-6
        except (TypeError, ValueError):
            return False
    if fld in INT_FIELDS:
        try:
            return actual is not None and int(expected) == int(actual)
        except (TypeError, ValueError):
            return False
    if fld == "ticker" and isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().upper() == actual.strip().upper()
    return expected == actual


def _run_cases(parser, cases, workers):
    """Parse every case, returning results in INPUT order.

    Session 10f: this loop was serial — 270 cases at a second or two each is
    7-10 minutes for a gate you are meant to run before every deploy.

    The subtlety is prompt caching. The ~8k-token system prompt is sent with
    `cache_control: ephemeral` (signal_parser.py), which bills repeat reads at
    about a tenth of input price. Firing N workers from cold defeats that
    entirely: they all race past each other before the first response lands,
    every one misses, and each pays full price for the whole prefix. With 8
    workers that is 8 full-price reads instead of 1.

    So: one warm-up call serially, THEN fan out. The cache is populated by the
    time the pool starts, every worker hits it, and the 5-minute TTL refreshes
    on each hit — so a ~1 minute run stays warm throughout. Same token cost as
    the serial version, a fraction of the wall time.
    """
    if not cases:
        return []

    def _parse(case):
        return parser.parse_text_signal(
            message=case["message"],
            source=case["source"],
            source_priority=case.get("source_priority", "high"),
        )

    # Warm the cache on a single case before fanning out.
    print(f"Warming the prompt cache on 1 case, then {workers} workers...")
    results = [None] * len(cases)
    results[0] = _parse(cases[0])
    done = 1
    print(f"  cache warm  [{done}/{len(cases)}]")

    rest = list(enumerate(cases))[1:]
    if rest and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_parse, c): i for i, c in rest}
            for fut in as_completed(futures):
                i = futures[fut]
                results[i] = fut.result()
                done += 1
                if done % 25 == 0 or done == len(cases):
                    print(f"  parsed [{done}/{len(cases)}]", flush=True)
    else:
        for i, c in rest:
            results[i] = _parse(c)
            done += 1
            if done % 25 == 0 or done == len(cases):
                print(f"  parsed [{done}/{len(cases)}]", flush=True)
    return results


def main():
    ap = argparse.ArgumentParser(
        description="Replay labeled signals through SignalParser and score per-field "
        "exact match. Costs real API tokens."
    )
    ap.add_argument("--limit", type=int, default=None, help="run at most N cases")
    ap.add_argument(
        "--only", type=str, default=None, help="run only cases whose id contains this substring"
    )
    ap.add_argument(
        "--config", type=str, default=str(ROOT / "config.yaml"), help="path to config.yaml"
    )
    # Session 10d: cost control. Iterating on the prompt only needs the cases
    # that FAILED last time — a full 259-case run is for pre-deploy gates.
    ap.add_argument(
        "--failed-only", action="store_true",
        help="re-run only the cases that failed in the last run "
             "(reads eval/last_run.json; ~5%% of a full run's cost)",
    )
    # Session 10f: the API calls are independent and the parser holds no
    # mutable state across them, so they parallelise cleanly. Kept modest by
    # default — this is a gate, not a load test, and Anthropic rate limits
    # apply per-account.
    ap.add_argument(
        "--workers", type=int, default=8,
        help="parallel parse workers (default 8; 1 = serial). The prompt cache "
             "is warmed on one case first either way, so token cost is "
             "unchanged.",
    )
    args = ap.parse_args()

    config = load_config(args.config)
    api_key = (config.get("anthropic") or {}).get("api_key", "")
    if not api_key or api_key.startswith("${"):
        print(
            "ANTHROPIC_API_KEY is not set (config.anthropic.api_key unresolved). "
            "Set it in .env or the environment.",
            file=sys.stderr,
        )
        sys.exit(2)

    parser = SignalParser(config)

    cases = load_cases(CASES_PATH)
    if args.failed_only:
        last_run = Path(__file__).resolve().parent / "last_run.json"
        try:
            with open(last_run, encoding="utf-8") as f:
                failed_ids = {r["id"] for r in json.load(f).get("failures", [])}
        except (OSError, json.JSONDecodeError, KeyError) as e:
            print(f"--failed-only: cannot read {last_run} ({e}) — run a full "
                  f"eval first.", file=sys.stderr)
            sys.exit(2)
        if not failed_ids:
            print("--failed-only: last run had ZERO failures — nothing to re-run. "
                  "(Run a full eval for a fresh gate.)")
            sys.exit(0)
        cases = [c for c in cases if c["id"] in failed_ids]
        print(f"--failed-only: re-running {len(cases)} previously-failed case(s)")
    if args.only:
        cases = [c for c in cases if args.only in c["id"]]
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        print("No cases matched the filters.", file=sys.stderr)
        sys.exit(2)

    print(f"Running {len(cases)} case(s) against text_model={parser.text_model}\n")
    _t0 = time.time()

    field_totals = defaultdict(lambda: [0, 0])  # field -> [correct, total]
    n_pass = 0
    rows = []

    signals = _run_cases(parser, cases, max(1, args.workers))
    print()

    for case, signal in zip(cases, signals):
        mismatches = []
        for fld, expected in case["expected"].items():
            actual = get_actual(signal, fld)
            ok = value_matches(fld, expected, actual)
            field_totals[fld][1] += 1
            if ok:
                field_totals[fld][0] += 1
            else:
                mismatches.append(f"{fld}: expected {expected!r}, got {actual!r}")
        if get_actual(signal, "signal_type") == "parse_error":
            mismatches.append(f"PARSE_ERROR: {signal.notes}")
        passed = not mismatches
        n_pass += passed
        rows.append((case["id"], passed, mismatches))

    print("=== PER-CASE RESULTS ===")
    id_width = max(len(r[0]) for r in rows)
    for case_id, passed, mismatches in rows:
        status = "PASS" if passed else "FAIL"
        detail = "" if passed else "  |  " + "; ".join(mismatches)
        print(f"{status}  {case_id:<{id_width}}{detail}")

    print(f"\nCases passed: {n_pass}/{len(rows)}")

    print("\n=== PER-FIELD ACCURACY ===")
    for fld in sorted(field_totals, key=lambda f: (f != "signal_type", f)):
        correct, total = field_totals[fld]
        pct = correct / total if total else 0.0
        print(f"{fld:<18} {correct:>3}/{total:<3}  {pct:6.1%}")

    # ── Session 9b: persist results so runs can be fetched and compared ──────
    # eval/last_run.json = full detail of THIS run; eval/runs.jsonl = one
    # summary line per run (append-only history for before/after comparisons).
    st_correct, st_total = field_totals.get("signal_type", (0, 0))
    result = {
        "ran_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        # Session 10f: "does this still take long?" was unanswerable because
        # nothing recorded how long it took.
        "duration_seconds": round(time.time() - _t0, 1),
        "workers": max(1, args.workers),
        "model": parser.text_model,
        "cases": len(rows),
        "cases_passed": n_pass,
        "signal_type_accuracy": round(st_correct / st_total, 4) if st_total else 0.0,
        "per_field": {f: {"correct": c, "total": t}
                      for f, (c, t) in field_totals.items()},
        "failures": [
            {"id": cid, "mismatches": mm} for cid, ok, mm in rows if not ok
        ],
    }
    out_dir = Path(__file__).resolve().parent
    # Session 10d: a --failed-only subset must not clobber the canonical
    # full-run record (the next --failed-only reads it, and its accuracy
    # number describes the FULL set). Subset runs get their own file.
    result["mode"] = "failed-only" if args.failed_only else "full"
    out_name = "last_failed_run.json" if args.failed_only else "last_run.json"
    with open(out_dir / out_name, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    summary_line = {k: v for k, v in result.items() if k != "failures"}
    summary_line["failure_ids"] = [r["id"] for r in result["failures"]]
    with open(out_dir / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(summary_line, ensure_ascii=False) + "\n")
    print(f"\nResults written to {out_dir / out_name}")

    st_acc = st_correct / st_total if st_total else 0.0
    if st_acc < 0.9:
        print(
            f"\nGATE FAILED: signal_type accuracy {st_acc:.1%} < 90% — "
            f"do NOT ship this prompt/parser change.",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"\nGATE PASSED: signal_type accuracy {st_acc:.1%} >= 90%")
    sys.exit(0)


if __name__ == "__main__":
    main()
