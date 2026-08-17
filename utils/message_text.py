"""Own-text extraction, shared between main.py and the parser.

Session 9 (M14b) introduced `_own_text` in main.py so keyword scans can't
match words that came from injected [REPLYING TO: ...] / [RECENT CONTEXT ...]
blocks or [PING: ...] / [caller: ...] markers instead of the caller's current
words. BUG-41 round 2 (2026-08-05) needed the same scrub inside the parser's
expiry-year heal — a hallucinated expiry must not be "blessed" by an M/D that
appears only in a PREVIOUS message's quoted context — so the regexes moved
here to a single shared home. main.py delegates; behavior is identical.
"""
import re

# Injected-context markers added by the monitors / pipeline. Line-anchored
# for the block markers (their payload is a single line, and quoted content
# may itself contain "]"), bracket-bounded for the inline markers.
CONTEXT_MARKER_RES = (
    re.compile(r'(?m)^\[REPLYING TO:.*$', re.IGNORECASE),
    re.compile(r'(?m)^\[RECENT CONTEXT.*$', re.IGNORECASE),
    re.compile(r'\[PING:[^\]]*\]', re.IGNORECASE),
    re.compile(r'\[caller:[^\]]*\]', re.IGNORECASE),
)


def own_text(raw: str) -> str:
    """Return only the message's OWN text — injected context stripped."""
    if not raw:
        return ""
    text = raw
    for rx in CONTEXT_MARKER_RES:
        text = rx.sub(" ", text)
    return text.strip()
