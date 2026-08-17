"""
Demo driver - replay sample signals through the full pipeline on the
paper executor.

    python run_paper.py                     # offline: uses pre-labelled parses
    ANTHROPIC_API_KEY=... python run_paper.py --llm   # live Claude parsing

Pipeline: ingest -> parse -> decide -> execute (paper) -> manage.
"""

import os
import argparse
import json
import logging

import yaml

from engine.decision_engine import DecisionEngine
from execution.paper import PaperExecutor
from ingest.jsonl_source import read_messages

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("demo")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", default="eval/sample_signals.jsonl")
    ap.add_argument("--llm", action="store_true",
                    help="parse with Claude instead of using bundled labels")
    args = ap.parse_args()

    cfg_path = "config.yaml" if os.path.exists("config.yaml") else "config.example.yaml"
    config = yaml.safe_load(open(cfg_path))
    executor = PaperExecutor(starting_balance=1000.0)
    executor.login()
    engine = DecisionEngine(config)

    parser = None
    if args.llm:
        from parser.signal_parser import SignalParser
        parser = SignalParser(config)

    for msg in read_messages(args.signals):
        if parser:
            parsed = parser.parse(msg["message"], source=msg["source"])
        else:
            parsed = msg.get("expected")
        if not parsed or parsed.get("type") in (None, "noise", "watch"):
            log.info("[%s] no actionable signal: %r", msg["id"], msg["message"][:60])
            continue
        log.info("[%s] parsed: %s", msg["id"], json.dumps(parsed, default=str))
        if parsed.get("type") == "entry":
            executor.set_quote(parsed["ticker"], parsed["expiry"], parsed["strike"],
                               parsed["direction"], bid=parsed["entry_price"] * 0.98,
                               ask=parsed["entry_price"] * 1.02)
            result = executor.place_option_order(
                parsed["ticker"], parsed["expiry"], parsed["strike"],
                parsed["direction"], contracts=1, limit_price=parsed["entry_price"])
            log.info("[%s] order: %s", msg["id"], result)

    log.info("final balance: $%.2f | open positions: %s",
             executor.get_account_balance(), executor.get_open_positions())


if __name__ == "__main__":
    main()
