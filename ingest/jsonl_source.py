"""
JSONL message source - the generic ingestion edge.

The engine consumes plain message dicts; where they come from is an adapter
concern. This source replays newline-delimited JSON, one message per line:

    {"id": "1", "source": "caller_a", "message": "SPY 640c 0DTE @ 1.20", 
     "timestamp": "2026-08-01T14:31:00Z", "images": []}

Real-time sources (webhooks, chat exports, broker alerts) implement the same
iterator contract: yield dicts with at least id/source/message.
"""

import json
from pathlib import Path
from typing import Iterator


def read_messages(path: str | Path) -> Iterator[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if "message" not in msg:
                raise ValueError(f"message field missing on line: {line[:80]}")
            msg.setdefault("source", "unknown")
            msg.setdefault("images", [])
            yield msg
