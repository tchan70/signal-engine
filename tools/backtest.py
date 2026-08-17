"""
Caller-anchored backtest for the caller_a-challenge-challenge channel — Session 9b.

Replays scraped channel history (tools/scrape_history.py output) and answers:
  1. What trades did the caller actually take (reconstructed from HIS posted
     prices — entries, trims, exits, status marks)?
  2. What would the bot have done following them (challenge mirror sizing,
     slippage, proportional trims, optional stop/trail policy on the sparse
     price path of posted marks)?
  3. Where do the deterministic regex parser and Claude's parser disagree?
     (--claude mode; disagreements are where misreads hide)

Honest fidelity limits: prices exist only where the caller posted them, so
independent stop-losses can only trigger on posted marks (conservative), and
fills are assumed at posted price ± slippage. This validates parsing and the
follow-the-caller policy — not intraday execution quality.

Usage (from the project root, venv active):
    python tools/backtest.py                          # regex-only, no API cost
    python tools/backtest.py --claude                 # + real parser, cached
    python tools/backtest.py --balance 500 --slippage-pct 2
    python tools/backtest.py --export-eval            # draft eval cases

Outputs: logs/history/backtest_report.md, backtest_trades.csv,
         (optional) eval/inbox_backtest.jsonl
"""

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

HISTORY_DIR = PROJECT_ROOT / "logs" / "history"


# ── Deterministic regex parser for caller_a's challenge formats ───────────────────
# v2 (Session 9b): tuned against the REAL scraped history. Observed formats:
#   entry:    "BA $242.5 Call 2/13 • 1 Buy 2.11"   "GME $25 Call\n2/6. 2 Buys 0.80"
#   exit:     "All out 5.00" | "4.00 70% all out" | "OUT 20% GME" | "cut BA -10 %"
#             "Out at breakeven" | "1.41 I 25% TSLA all out"  ("I" = typo'd "|")
#   trim:     "Trimmed 2.40" (price!) | "0.67 40% ONDS trimmed 3" | "trimmed most"
#   scale-in: "Scaled in 1 more con avg @ 0.59" | "Bought 1 more, new avg 0.98"
#   stop:     "-25% SL" | "SL hit - 20% cut APP"

TICKER = r"(?P<ticker>[A-Z]{1,6})"
STRIKE = r"\$?(?P<strike>\d{1,5}(?:\.\d{1,2})?)"
DIRECTION = r"(?P<direction>[Cc]alls?|[Pp]uts?|C|P)\b"
EXPIRY = r"(?P<expiry>\d{1,2}/\d{1,2}(?:/\d{2,4})?|0[Dd][Tt][Ee]|1[Dd][Tt][Ee])"
PRICE = r"(?P<price>\d{1,3}(?:\.\d{1,2})?)"

ENTRY_RE = re.compile(
    TICKER + r"\s+" + STRIKE + r"\s+" + DIRECTION +
    r"[\s.,•·|-]*" + EXPIRY + r"?[\s.,•·|-]*(?P<contracts>\d{1,2})\s+[Bb]uys?\s+" + PRICE
)
# Dated swing format ("CRM $195 / 06 Feb 26 (W) Put 100 2.23") — usually tagged
# "not challenge account"; parsed so it is COUNTED, but never traded in the sim.
ENTRY_DATED_RE = re.compile(
    TICKER + r"\s+" + STRIKE + r"\s+\d{1,2}\s+[A-Za-z]{3}\s+\d{2}\s*(?:\(W\))?\s*"
    r"(?P<direction>[Cc]alls?|[Pp]uts?)\s*(?:100)?\s*@?\s*" + PRICE
)
SCALE_RE = re.compile(
    r"(?:[Ss]caled?\s+in\s+(?P<n1>\d{1,2})|[Bb]ought\s+(?P<n2>\d{1,2})\s+more"
    r"|[Aa]dded?\s+(?P<n3>\d{1,2})\s+more)"
)
AVG_RE = re.compile(r"(?:new\s+avg|avg)\s*@?\s*(?P<avg>\d{1,3}\.\d{1,2})", re.I)
EXIT_WORD_RE = re.compile(
    # Every "out" form requires adjacent EVIDENCE (ticker, price, pct, at/of,
    # runner phrasing) so prose like "locked me out" / "fill out" never fires.
    r"(?i:\ball\s*out\b)"                          # "All out"/"ALL OUT"/"ALL out"
    r"|(?i:\bout\b\s*[+-]?\d)"                     # "Out 2.93 50%" / "out -10% IREN"
    r"|(?i:\bout\s+of\s+[A-Za-z]{2,6}\b)"          # "Out of Googl" / "out of SOUN"
    r"|(?i:\bout\b)(?=\s+[A-Z]{2,6}\b)"            # "OUT CIFR" / "out OKLO"
    r"|(?i:\bout\s+(?:at|@))"                      # "out APP at 1.00" / "Out at Be"
    r"|(?i:\bout\s+(?:this\s+|last\s+runner\b))"   # "OUT this DDOG" / "Out last runner LYFT"
    r"|(?:\d\.\d{2}|[A-Z]{2,6})\s+(?i:out\b)"      # "0.50 SOUN out"
    r"|(?i:\bcut(?:ting)?\b)"                      # "cut BA -10 %" / "Cutting CIFR"
)
# "out of Googl": the ticker can be mixed-case — capture it directly
OUT_OF_RE = re.compile(r"(?i)\bout\s+of\s+(?P<tk>[A-Za-z]{2,6})\b")
LOSS_CONTEXT_RE = re.compile(r"(?i)\blos(?:e|ing|s|t)\b|\bred\b|-\s*\d+(?:\.\d+)?\s*%")
TRIM_WORD_RE = re.compile(r"[Tt]rimm?(?:ed|ing)?|\b[Ss]old\b")
PCT_RE = re.compile(r"(?P<pct>[+-]?\s?\d{1,4}(?:\.\d+)?)\s*%")
DEC_PRICE_RE = re.compile(r"(?<![\d.%])(?P<p>\d{1,3}\.\d{1,2})(?!\s*%)(?![\d.])")
# Requires SL or an explicit stop-loss phrase — bare "stop" matches prose
# ("they are trying to stop me!!!" is not a stop update).
STOP_RE = re.compile(r"(?:\bSL\b|[Ss]top\s*loss|[Ss]top\s+(?:at|@|to)\s*\d)")
MARK_RE = re.compile(
    r"^(?P<price>\d{1,3}\.\d{1,2})\s*[|I]?\s*(?P<pct>[+-]?\d{1,4}(?:\.\d+)?)\s*%"
)

# Tokens that look like tickers but never are (from the real history)
_TICKER_STOPWORDS = {
    "ALL", "OUT", "CUT", "THE", "AND", "FOR", "NOW", "NEW", "AVG", "CON",
    "CONS", "MOST", "MORE", "HIT", "BE", "SL", "DTE", "RH", "ER", "CPI",
    "GG", "MB", "OK", "PM", "AM", "ET", "RISK", "OFF", "TMR", "WAR", "NO",
    "EVERYONE", "HERE", "BUY", "BUYS", "CALL", "PUT", "CALLS", "PUTS", "I",
    "A", "W", "GM", "TY", "LOL", "IT", "ITS", "WE", "ATE", "THIS", "THAT",
    "ONE", "TODAY", "TMRW", "LAST", "RUNNER", "PORT", "CASH", "ONLY",
}
# Plans are not fills: "if TE doesnt reclaim 6.20 today, we need to cut it"
CONDITIONAL_RE = re.compile(
    r"(?i)\b(?:if|unless|might|may|gonna|going\s+to|about\s+to|need(?:s)?\s+to|"
    r"we\s+need|watch(?:ing)?|planning|will\s+(?:cut|exit|sell))\b"
)


def _find_ticker(text: str):
    for m in re.finditer(r"(?<![A-Za-z0-9$])([A-Z]{2,6})(?![a-z0-9])", text):
        tok = m.group(1)
        if tok not in _TICKER_STOPWORDS:
            return tok
    return None


def _find_pct(text: str):
    m = PCT_RE.search(text)
    return float(m.group("pct").replace(" ", "")) if m else None


def _find_price(text: str):
    m = DEC_PRICE_RE.search(text)
    return float(m.group("p")) if m else None


def regex_parse(content: str) -> dict:
    """Classify one message with deterministic rules tuned on real history.
    Returns a dict with key "type" plus type-specific fields."""
    flat = " ".join(content.split())                       # collapse newlines
    # Strip pings and Discord role/user/channel mention markup (July messages
    # use <@&ROLE_ID> instead of @everyone — the raw IDs must not feed the
    # price/ticker scanners).
    flat = re.sub(r"@everyone|@here|<@&\d+>|<@!?\d+>|<#\d+>", " ", flat).strip()

    m = ENTRY_RE.search(flat)
    if m:
        d = m.groupdict()
        return {
            "type": "entry", "ticker": d["ticker"],
            "strike": float(d["strike"]),
            "direction": "call" if d["direction"].lower().startswith("c") else "put",
            "expiry": (d["expiry"] or "").upper() or None,
            "price": float(d["price"]),
            "contracts": int(d["contracts"]),
        }
    m = ENTRY_DATED_RE.search(flat)
    if m:
        d = m.groupdict()
        low = content.lower()
        excluded = "not challenge" in low or "swing" in low
        return {
            # Dated format ("CRM $195 / 06 Feb 26 (W) Put 100 2.23") is a real
            # challenge entry UNLESS tagged "not challenge"/"swing" (ADBE case).
            # Contract count isn't stated in this format — assume 1.
            "type": "entry_other" if excluded else "entry",
            "ticker": d["ticker"], "strike": float(d["strike"]),
            "direction": "call" if d["direction"].lower().startswith("c") else "put",
            "expiry": None, "price": float(d["price"]), "contracts": 1,
            "not_challenge": excluded,
        }
    m = SCALE_RE.search(flat)
    if m:
        n = int(m.group("n1") or m.group("n2") or m.group("n3") or 1)
        avg = AVG_RE.search(flat)
        return {
            "type": "scale_in", "contracts": n,
            "new_avg": float(avg.group("avg")) if avg else None,
            "ticker": _find_ticker(flat),
        }
    if TRIM_WORD_RE.search(flat) and "trim" in flat.lower() or re.search(r"\b[Ss]old\b", flat):
        # Count: integer directly after the trim word ONLY (a decimal there is a
        # price — "Trimmed 2.40" trims at $2.40, it does not sell 2 contracts).
        cm = re.search(r"[Tt]rimm?(?:ed|ing)?\s+(?P<n>\d{1,2})(?![\d.])", flat)
        sm = re.search(r"\b[Ss]old\s+(?P<n>\d{1,2})(?![\d.])", flat)
        n = int((cm or sm).group("n")) if (cm or sm) else None
        return {
            "type": "trim", "ticker": _find_ticker(flat),
            "price": _find_price(flat), "pct": _find_pct(flat),
            "contracts": n,                                # None → default later
            "most": bool(re.search(r"trimm\w*\s+most", flat, re.I)),
        }
    if EXIT_WORD_RE.search(flat):
        # A plan is only a plan when the conditional comes BEFORE the action
        # word ("if TE doesnt reclaim... we need to cut"). Commentary AFTER a
        # completed action ("Cut MRK — need to reassess...") is still an exit.
        cond = CONDITIONAL_RE.search(flat)
        if cond and cond.start() < EXIT_WORD_RE.search(flat).start():
            return {"type": "noise", "conditional_exit_language": True}
        of = OUT_OF_RE.search(flat)
        tk = None
        if of and of.group("tk").upper() not in _TICKER_STOPWORDS:
            tk = of.group("tk").upper()
        return {
            "type": "exit", "ticker": tk or _find_ticker(flat),
            "price": _find_price(flat), "pct": _find_pct(flat),
            "breakeven": bool(re.search(r"break\s*even|\bat\s+be\b", flat, re.I)),
            "loss_context": bool(LOSS_CONTEXT_RE.search(flat)),
        }
    if STOP_RE.search(flat):
        return {"type": "stop_update", "pct": _find_pct(flat),
                "level": _find_price(flat)}
    m = MARK_RE.match(flat)
    if m:
        return {
            "type": "mark", "ticker": _find_ticker(flat),
            "price": float(m.group("price")), "pct": float(m.group("pct")),
        }
    return {"type": "noise"}


# ── Trade reconstruction ─────────────────────────────────────────────────────

@dataclass
class CallerTrade:
    ticker: str
    strike: float
    direction: str
    expiry: str
    opened_at: str
    entry_price: float
    contracts: int
    marks: list = field(default_factory=list)       # (ts, price) observations
    trims: list = field(default_factory=list)       # (ts, contracts, price)
    exit_price: float = None
    closed_at: str = None
    unresolved: bool = False                        # closed without a usable price

    @property
    def caller_pnl(self) -> float:
        """Caller's realized $ P&L from his own posted prices."""
        pnl, remaining = 0.0, self.contracts
        for _, n, px in self.trims:
            pnl += (px - self.entry_price) * 100 * n
            remaining -= n
        if self.exit_price is not None and remaining > 0:
            pnl += (self.exit_price - self.entry_price) * 100 * remaining
        return pnl


def reconstruct(signals: list) -> tuple:
    """signals: chronological [(record, parsed)] → (trades, anomalies)."""
    open_trades: dict = {}   # ticker -> CallerTrade
    done, anomalies = [], []

    def resolve_ticker(t, sig=None):
        """Attribute a ticker-less signal to an open trade.

        Explicit ticker wins. Otherwise, when the signal carries BOTH a price
        and a stated % ("3.50 50% trimmed"), attribute by CONSISTENCY: the pct
        must roughly equal price/entry-1 for the candidate trade. This is what
        stops a CRM trim at 3.50 (+57% on a 2.23 entry) from being pinned on
        an INTC position entered at 0.95 (+268% — impossible per the caller's
        own stated %). Single-open fallback only as a last resort.
        """
        if t and t in open_trades:
            return t
        if sig and sig.get("price") is not None and sig.get("pct") is not None:
            consistent = [
                k for k, tr in open_trades.items()
                if tr.entry_price > 0 and abs(
                    (sig["price"] / tr.entry_price - 1) * 100 - sig["pct"]) <= 20
            ]
            if len(consistent) == 1:
                return consistent[0]
            if len(consistent) > 1:
                return None  # ambiguous — safer to drop than to guess
            if open_trades:
                return None  # price/pct fit NO open trade — do not misattribute
        if len(open_trades) == 1:
            return next(iter(open_trades))
        return t if t in open_trades else None

    for rec, sig in signals:
        ts = rec["created_at"]
        kind = sig["type"]
        if kind == "entry":
            tk = sig["ticker"]
            if tk in open_trades:
                tr = open_trades[tk]
                same_contract = (tr.strike == sig["strike"]
                                 and tr.direction == sig["direction"])
                # Duplicate post guard: identical re-alert within ~15 min
                # ("SOFI... 2 Buys 0.52" posted twice a minute apart) must not
                # double the position.
                if (same_contract and sig["price"] == tr.entry_price
                        and sig["contracts"] == tr.contracts
                        and ts[:13] == tr.opened_at[:13]):
                    anomalies.append((ts, f"duplicate {tk} entry ignored",
                                      rec["content"][:80]))
                    continue
                if same_contract:  # true scale-in: average up/down
                    total = tr.contracts + sig["contracts"]
                    tr.entry_price = (tr.entry_price * tr.contracts
                                      + sig["price"] * sig["contracts"]) / total
                    tr.contracts = total
                    continue
                # DIFFERENT contract on the same ticker: the old trade's exit
                # was missed (e.g. "OUT CIFR" formats). Retire it unresolved
                # instead of merging a July call into a February put.
                tr.unresolved = True
                tr.exit_price = tr.marks[-1][1] if tr.marks else tr.entry_price
                tr.closed_at = ts
                anomalies.append((ts, f"{tk} re-entered with different contract "
                                      f"— prior trade retired (missed exit?)",
                                  rec["content"][:80]))
                done.append(open_trades.pop(tk))
            open_trades[tk] = CallerTrade(
                ticker=tk, strike=sig["strike"], direction=sig["direction"],
                expiry=sig.get("expiry") or "?", opened_at=ts,
                entry_price=sig["price"], contracts=sig["contracts"],
            )
        elif kind == "scale_in":
            # "Scaled in 1 more con avg @ 0.59" / "Bought 1 more, new avg 0.98"
            tk = resolve_ticker(sig.get("ticker"))
            if not tk:
                anomalies.append((ts, "scale-in with unresolvable ticker", rec["content"][:80]))
                continue
            tr = open_trades[tk]
            n = sig.get("contracts") or 1
            if sig.get("new_avg"):
                tr.entry_price = sig["new_avg"]  # caller states his new average
                # An average-in happens at ~market — usable as a price mark, and
                # it invalidates older (pre-averaging) marks for exit fallback.
                tr.marks.append((ts, sig["new_avg"]))
            tr.contracts += n
        elif kind == "trim":
            tk = resolve_ticker(sig.get("ticker"), sig)
            if not tk:
                anomalies.append((ts, "trim with unresolvable ticker", rec["content"][:80]))
                continue
            tr = open_trades[tk]
            px = sig.get("price")
            if px is None and sig.get("pct") is not None:
                px = round(tr.entry_price * (1 + sig["pct"] / 100), 2)
            if px is None:
                anomalies.append((ts, f"trim {tk} without price", rec["content"][:80]))
                px = tr.entry_price
            already = sum(t[1] for t in tr.trims)
            n = sig.get("contracts")
            if n is None:  # "Trimmed 2.40" / "trimmed most" — no explicit count
                n = max(1, int((tr.contracts - already) / 2)) if sig.get("most") else 1
            n = min(n, tr.contracts - already)
            if n > 0:
                tr.trims.append((ts, n, px))
                tr.marks.append((ts, px))
        elif kind == "exit":
            tk = resolve_ticker(sig.get("ticker"), sig)
            if not tk:
                anomalies.append((ts, "exit with unresolvable ticker", rec["content"][:80]))
                continue
            tr = open_trades.pop(tk)
            # Plausibility guard: a "price" scraped from prose can be a STOCK
            # level ("we are losing 21.50 badly" on a $1.80 option). An option
            # exit outside [2%, 10x] of entry is not a fill price.
            px = sig.get("price")
            if px is not None and tr.entry_price > 0 and not (
                    0.02 * tr.entry_price <= px <= 10 * tr.entry_price):
                anomalies.append((ts, f"exit {tk}: implausible price {px} vs "
                                      f"entry {tr.entry_price} — ignored",
                                  rec["content"][:80]))
                px = None
            # Direction guard: a cut/losing exit can't be ABOVE entry — that
            # number is a stock level, not the option fill ("Cutting CIFR —
            # losing 21.50 badly" on a $2.50 option).
            if (px is not None and sig.get("loss_context")
                    and sig.get("pct") is None and px > tr.entry_price * 1.05):
                anomalies.append((ts, f"exit {tk}: price {px} above entry in a "
                                      f"loss-context message — ignored",
                                  rec["content"][:80]))
                px = None
            if sig.get("breakeven"):
                tr.exit_price = tr.entry_price
            elif px is not None:
                tr.exit_price = px
            elif sig.get("pct") is not None:
                # "OUT 20% GME" / "cut BA -10 %" — derive from caller's stated %
                tr.exit_price = round(tr.entry_price * (1 + sig["pct"] / 100), 2)
            elif tr.marks:
                # "Cut HIMS" with no numbers — the last posted mark is an
                # ESTIMATE, not a fill. Priced for the listing but excluded
                # from headline stats (unresolved).
                tr.exit_price = tr.marks[-1][1]
                tr.unresolved = True
                anomalies.append((ts, f"exit {tk} priced from last mark "
                                      f"({tr.exit_price}) — estimate only",
                                  rec["content"][:80]))
            else:
                tr.exit_price, tr.unresolved = tr.entry_price, True
                anomalies.append((ts, f"exit {tk} without price — booked flat", rec["content"][:80]))
            tr.closed_at = ts
            if tr.exit_price:
                tr.marks.append((ts, tr.exit_price))
            done.append(tr)
        elif kind == "mark":
            tk = resolve_ticker(sig.get("ticker"))
            if tk:
                open_trades[tk].marks.append((ts, sig["price"]))

        # Correction detector: "fixed: LYFT not SOFI my bad" — the previous
        # alert's ticker was wrong. Undecidable deterministically; surface it.
        low = rec["content"].lower()
        if ("my bad" in low or "fixed:" in low) and re.search(
                r"\bnot\s+[A-Z]{2,6}\b", rec["content"]):
            anomalies.append((ts, "CALLER CORRECTION — prior alert may be wrong "
                                  "(review manually)", rec["content"][:80]))

    for tk, tr in open_trades.items():  # never-closed trades
        tr.unresolved = True
        anomalies.append((tr.opened_at, f"{tk} entry never closed in history", ""))
        done.append(tr)
    return done, anomalies


# ── Bot simulation (challenge mirror mode) ───────────────────────────────────

def simulate(trades, balance, multiplier, slippage_pct, expensive_pct=60,
             stop_loss_pct=None):
    """Mirror the caller with the bot's sizing rules. Returns (rows, summary).

    stop_loss_pct: if set, apply the bot's hard stop against POSTED MARKS only
    (conservative sparse-path simulation)."""
    rows, equity, peak, max_dd = [], balance, balance, 0.0
    skipped = []
    for tr in trades:
        want = max(1, round(tr.contracts * multiplier))
        buy_px = round(tr.entry_price * (1 + slippage_pct / 100), 2)
        cost1 = buy_px * 100
        if cost1 > equity * expensive_pct / 100:
            want = 1
        while want > 0 and want * cost1 > equity:
            want -= 1
        if want <= 0:
            skipped.append((tr.ticker, tr.opened_at,
                            f"unaffordable (${cost1:.0f}/contract vs ${equity:.0f})"))
            continue

        # Sparse-path stop check: did a posted mark cross the stop first?
        stopped_at = None
        if stop_loss_pct:
            stop_px = buy_px * (1 - stop_loss_pct / 100)
            for ts, px in sorted(tr.marks):
                if px <= stop_px:
                    stopped_at = (ts, px)
                    break

        pnl = 0.0
        if stopped_at:
            sell_px = round(stopped_at[1] * (1 - slippage_pct / 100), 2)
            pnl = (sell_px - buy_px) * 100 * want
            outcome = f"stopped @ {sell_px:.2f}"
        else:
            remaining = want
            for _, n_caller, px in tr.trims:
                frac = n_caller / tr.contracts
                n = min(remaining, max(1, int(want * frac + 0.5))) if remaining > 1 else 0
                if n:
                    sell_px = round(px * (1 - slippage_pct / 100), 2)
                    pnl += (sell_px - buy_px) * 100 * n
                    remaining -= n
            if remaining > 0:
                sell_px = round((tr.exit_price or tr.entry_price)
                                * (1 - slippage_pct / 100), 2)
                pnl += (sell_px - buy_px) * 100 * remaining
            outcome = "mirrored"

        # Honest accounting: trades whose exit price is a GUESS (unresolved)
        # must not move the headline equity curve — they're listed, not booked.
        if not tr.unresolved:
            equity += pnl
            peak = max(peak, equity)
            max_dd = max(max_dd, (peak - equity) / peak * 100 if peak else 0)
        rows.append({
            "opened_at": tr.opened_at, "ticker": tr.ticker,
            "contract": f"${tr.strike} {tr.direction} {tr.expiry}",
            "caller_contracts": tr.contracts, "bot_contracts": want,
            "entry": buy_px, "caller_pnl": round(tr.caller_pnl, 2),
            "bot_pnl": round(pnl, 2), "equity": round(equity, 2),
            "outcome": outcome, "unresolved": tr.unresolved,
        })

    resolved = [r for r in rows if not r["unresolved"]]
    guessed = [r for r in rows if r["unresolved"]]
    wins = [r for r in resolved if r["bot_pnl"] > 0]
    summary = {
        "trades_total": len(rows),
        "trades_resolved": len(resolved),
        "coverage_pct": round(len(resolved) / len(rows) * 100, 1) if rows else 0.0,
        "skipped": len(skipped),
        # Headline stats: RESOLVED trades only — unresolved exits are guesses
        # and are excluded from P&L, equity, and win rate.
        "win_rate": round(len(wins) / len(resolved) * 100, 1) if resolved else 0.0,
        "total_pnl": round(sum(r["bot_pnl"] for r in resolved), 2),
        "final_equity": round(equity, 2),
        "return_pct": round((equity - balance) / balance * 100, 1) if balance else 0,
        "max_drawdown_pct": round(max_dd, 1),
        "avg_win": round(sum(r["bot_pnl"] for r in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(r["bot_pnl"] for r in resolved if r["bot_pnl"] <= 0)
                          / max(1, len(resolved) - len(wins)), 2),
        "unresolved_excluded": len(guessed),
        "unresolved_est_pnl_excluded": round(sum(r["bot_pnl"] for r in guessed), 2),
    }
    return rows, skipped, summary


# ── Claude-parser comparison (--claude) ──────────────────────────────────────

def claude_parse_all(records, cache_path: Path):
    """Run the REAL SignalParser over every record, with an on-disk cache so
    reruns are free. Requires ANTHROPIC_API_KEY (+ config.yaml) locally."""
    import yaml
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    import os

    def interp(o):
        if isinstance(o, dict):
            return {k: interp(v) for k, v in o.items()}
        if isinstance(o, list):
            return [interp(v) for v in o]
        if isinstance(o, str) and o.startswith("${") and o.endswith("}"):
            return os.environ.get(o[2:-1], o)
        return o

    cfg = interp(yaml.safe_load(open(PROJECT_ROOT / "config.yaml", encoding="utf-8")))
    from parser.signal_parser import SignalParser
    parser = SignalParser(cfg)

    cache = {}
    if cache_path.exists():
        for line in open(cache_path, encoding="utf-8"):
            try:
                e = json.loads(line)
                cache[e["id"]] = e["parsed"]
            except (json.JSONDecodeError, KeyError):
                continue

    out = {}
    with open(cache_path, "a", encoding="utf-8") as cf:
        for i, rec in enumerate(records):
            if rec["id"] in cache:
                out[rec["id"]] = cache[rec["id"]]
                continue
            sig = parser.parse_text_signal(
                rec["content"], source="caller_a-challenge-challenge", source_priority="high"
            )
            parsed = sig.to_dict()
            cf.write(json.dumps({"id": rec["id"], "parsed": parsed}) + "\n")
            cf.flush()
            out[rec["id"]] = parsed
            if (i + 1) % 25 == 0:
                print(f"  claude-parsed {i + 1}/{len(records)}")
    return out


def diff_parsers(records, claude_results):
    """Regex vs Claude on the action-determining fields."""
    # regex kind → the set of Claude signal_types considered equivalent
    KIND_MAP = {
        "entry": {"entry"},
        "entry_other": {"entry", "noise"},          # swing posts: either is fine
        "scale_in": {"entry", "management"},        # parser tags scale-ins both ways
        "exit": {"exit"},
        "trim": {"trim"},
        "mark": {"management", "noise", "technical_analysis"},
        "stop_update": {"stop_update", "management"},
        "noise": {"noise", "management", "technical_analysis"},
    }
    diffs = []
    for rec in records:
        rx = regex_parse(rec["content"])
        cl = claude_results.get(rec["id"], {})
        cl_type = cl.get("signal_type", "?")
        rx_type = rx["type"]
        equivalents = KIND_MAP.get(rx_type, {rx_type})
        # marks/management/noise distinctions are low-stakes — flag only
        # disagreements involving an actionable class on either side
        actionable = {"entry", "exit", "trim", "stop_update"}
        if cl_type not in equivalents and (rx_type in actionable or cl_type in actionable):
            diffs.append({
                "id": rec["id"], "created_at": rec["created_at"],
                "content": rec["content"][:120],
                "regex": rx_type, "claude": cl_type,
                "claude_ticker": cl.get("ticker"),
                "regex_ticker": rx.get("ticker"),
            })
    return diffs


# ── Report ───────────────────────────────────────────────────────────────────

def write_report(path, args, n_msgs, kind_counts, trades, anomalies, rows,
                 skipped, summary, summary_nostop, diffs):
    L = []
    L.append("# Backtest report — caller_a-challenge-challenge (caller-anchored)\n")
    L.append(f"Input: {args.input} | messages: {n_msgs} | "
             f"balance ${args.balance} | multiplier {args.multiplier} | "
             f"slippage {args.slippage_pct}%\n")
    L.append("## Message classification (regex)\n")
    for k, v in sorted(kind_counts.items(), key=lambda kv: -kv[1]):
        L.append(f"- {k}: {v}")
    L.append(f"\n## Caller trades reconstructed: {len(trades)} "
             f"({sum(1 for t in trades if t.unresolved)} with unresolved prices)\n")
    L.append("## Bot simulation — pure mirror (follow caller exactly)\n")
    for k, v in summary_nostop.items():
        L.append(f"- {k}: {v}")
    L.append(f"\n## Bot simulation — with {args.stop_loss_pct}% hard stop "
             f"(sparse-path: stops only trigger on posted marks)\n")
    for k, v in summary.items():
        L.append(f"- {k}: {v}")
    if skipped:
        L.append("\n## Skipped entries (unaffordable at sim equity)\n")
        for tk, ts, why in skipped:
            L.append(f"- {ts} {tk}: {why}")
    if anomalies:
        L.append(f"\n## Reconstruction anomalies ({len(anomalies)}) — "
                 f"review these, they are also parser edge cases\n")
        for ts, what, snippet in anomalies[:40]:
            L.append(f"- {ts}: {what} | `{snippet}`")
    if diffs is not None:
        L.append(f"\n## Regex vs Claude disagreements on actionable classes: "
                 f"{len(diffs)}\n")
        for d in diffs[:60]:
            L.append(f"- {d['created_at']} regex={d['regex']} claude={d['claude']} "
                     f"| `{d['content']}`")
        L.append("\n(Each disagreement is either a regex gap or a Claude misread "
                 "— label them into eval/labeled_signals.jsonl.)")
    L.append("\n---\n*Fidelity: fills at posted prices ± slippage; independent "
             "stops evaluated against posted marks only. This validates parsing "
             "and follow-the-caller policy, not intraday execution.*")
    path.write_text("\n".join(L), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default=str(HISTORY_DIR / "caller_a-challenge_messages.jsonl"))
    ap.add_argument("--balance", type=float, default=500.0)
    ap.add_argument("--multiplier", type=float, default=1.0)
    ap.add_argument("--slippage-pct", type=float, default=2.0)
    ap.add_argument("--stop-loss-pct", type=float, default=30.0,
                    help="Bot hard stop for the sparse-path policy sim")
    ap.add_argument("--author", default="caller_a",
                    help='Only reconstruct trades from this author_display '
                         '("all" disables the filter). The live bot filters '
                         'to caller_a — the backtest must too.')
    ap.add_argument("--claude", action="store_true",
                    help="Also run the real Claude parser (cached; costs API tokens)")
    ap.add_argument("--export-eval", action="store_true",
                    help="Write draft eval cases from confident regex parses")
    args = ap.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        sys.exit(f"No history file at {in_path} — run tools/scrape_history.py first.")

    records = [json.loads(l) for l in open(in_path, encoding="utf-8") if l.strip()]
    records.sort(key=lambda r: r["created_at"])
    total = len(records)
    # Session 9b: mirror the live bot's author filter — multiple people post in
    # the challenge channel (caller_b/Creation/Skinee banter must not trade).
    if args.author.lower() != "all":
        records = [r for r in records
                   if r.get("author_display", "") == args.author]
    print(f"Loaded {total} messages ({len(records)} from {args.author!r})")

    parsed = [(rec, regex_parse(rec["content"])) for rec in records]
    kind_counts = {}
    for _, sig in parsed:
        kind_counts[sig["type"]] = kind_counts.get(sig["type"], 0) + 1

    actionable = [(r, s) for r, s in parsed if s["type"] != "noise"]
    trades, anomalies = reconstruct(actionable)

    rows_ns, skipped_ns, summary_nostop = simulate(
        trades, args.balance, args.multiplier, args.slippage_pct)
    rows, skipped, summary = simulate(
        trades, args.balance, args.multiplier, args.slippage_pct,
        stop_loss_pct=args.stop_loss_pct)

    diffs = None
    if args.claude:
        print("Running Claude parser (cached) ...")
        cl = claude_parse_all(records, HISTORY_DIR / "parsed_cache.jsonl")
        diffs = diff_parsers(records, cl)

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    with open(HISTORY_DIR / "backtest_trades.csv", "w", newline="",
              encoding="utf-8") as f:
        if rows:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    if args.export_eval:
        ev = PROJECT_ROOT / "eval" / "inbox_backtest.jsonl"
        with open(ev, "w", encoding="utf-8") as f:
            n = 0
            for rec, sig in parsed:
                if sig["type"] in ("entry", "exit", "trim"):
                    exp = {"signal_type": sig["type"]}
                    if sig.get("ticker"):
                        exp["ticker"] = sig["ticker"]
                    if sig["type"] == "entry":
                        exp.update({"strike": sig["strike"],
                                    "direction": sig["direction"],
                                    "caller_contracts": sig["contracts"]})
                    f.write(json.dumps({
                        "id": f"bt-{rec['id']}", "message": rec["content"],
                        "source": "caller_a-challenge-challenge",
                        "source_priority": "high", "expected": exp,
                        "note": "draft label from regex — REVIEW before merging",
                    }, ensure_ascii=False) + "\n")
                    n += 1
        print(f"Wrote {n} draft eval cases to {ev} (review before merging!)")

    report = HISTORY_DIR / "backtest_report.md"
    write_report(report, args, len(records), kind_counts, trades, anomalies,
                 rows, skipped, summary, summary_nostop, diffs)
    print(f"\nReport: {report}")
    print(f"Trades CSV: {HISTORY_DIR / 'backtest_trades.csv'}")
    print("\nSummary (pure mirror):", json.dumps(summary_nostop, indent=2))


if __name__ == "__main__":
    main()
