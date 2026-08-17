"""
Logging Configuration - Separate log files for each component.

Log files:
  logs/agent.log        - Main application flow
  logs/discord.log      - Raw source messages received (everything the bot sees)
  logs/social.log      - Raw social TA posts received (everything ingested)
  logs/parser.log       - What the AI parser extracts from each signal
  logs/decisions.log    - Decision engine output (scores, sizing, action)
  logs/trades.log       - Execution: orders placed, fills, exits, P&L
  logs/positions.log    - Position monitoring: price checks, trailing stops
  logs/errors.log       - All errors across all components

Log retention policy (enforced at startup via cleanup_old_logs):
  - trades.json, trades.log, decisions.log: kept forever (audit trail)
  - agent.log, errors.log, discord.log, social.log, parser.log: 14 days
  - positions.log: 7 days (highest volume, lowest long-term value)
  - Operational state files (.json except trades.json): never touched
"""

import logging
import json
import os
import sys
import time as _time
from pathlib import Path
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler


def setup_logging(log_dir: str = "logs", level: str = "DEBUG"):
    """Configure all loggers with separate files per component."""
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    # Common format
    detailed_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    simple_fmt = logging.Formatter(
        "%(asctime)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Root logger - console + agent.log
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.DEBUG))

    # Console handler — force UTF-8 on Windows (cp1252 can't handle emoji).
    # LOW-3 (Session 9): under pythonw / as a service, sys.stdout is None —
    # the fileno() trick raised AttributeError before ANY handler existed.
    # Guard it: fall back to a plain StreamHandler, or skip console entirely.
    console = None
    try:
        console = logging.StreamHandler(
            stream=open(sys.stdout.fileno(), mode='w', encoding='utf-8', closefd=False)
        )
    except Exception:
        if sys.stdout is not None:
            console = logging.StreamHandler()  # plain, default encoding
    if console is not None:
        console.setLevel(logging.INFO)
        console.setFormatter(detailed_fmt)
        root.addHandler(console)

    # ── Suppress noisy third-party loggers ───────────────────────────────────
    # These all log at DEBUG by default and collectively generate tens of
    # thousands of lines per hour with zero operational value.
    _SUPPRESS_TO_WARNING = [
        # HTTP stacks (urllib3, requests, httpcore, httpx)
        "urllib3",
        "urllib3.connectionpool",
        "requests",
        "httpcore",           # covers httpcore.http2, httpcore.connection, httpcore.http11
        "httpx",
        # HTTP/2 header compression — HTTP client libraries pull in hpack which logs every
        # header field decoded. At 5-min social polls this is still ~18k lines/hr.
        "hpack",              # covers hpack.hpack and hpack.table
        # asyncio internals
        "asyncio",
        # tzlocal timezone detection
        "tzlocal",
        # Anthropic SDK internal HTTP
        "anthropic._base_client",
    ]
    for _name in _SUPPRESS_TO_WARNING:
        logging.getLogger(_name).setLevel(logging.WARNING)

    # Position monitor fires every 5s and emits a DEBUG line per position.
    # For manually-opened positions ("MANUAL ... not bot-managed") this generates
    # ~1,800 lines/hour per position with zero actionable content — suppress to INFO.
    # Real events (stop hits, fills, errors) are logged at INFO/WARNING/ERROR and
    # will still appear. Full DEBUG detail is in positions.log if needed.
    logging.getLogger("management.trade_manager").setLevel(logging.INFO)
    # ─────────────────────────────────────────────────────────────────────────

    # Rotating file handlers — cap size so logs never grow unbounded.
    # agent.log: 5 MB × 7 backups = 35 MB max (main workhorse log)
    # errors.log: 2 MB × 5 backups = 10 MB max
    # component logs: 2 MB × 3 backups = 6 MB max each
    agent_file = RotatingFileHandler(
        log_path / "agent.log", maxBytes=5*1024*1024, backupCount=7, encoding="utf-8"
    )
    agent_file.setLevel(logging.DEBUG)
    agent_file.setFormatter(detailed_fmt)
    root.addHandler(agent_file)

    error_file = RotatingFileHandler(
        log_path / "errors.log", maxBytes=2*1024*1024, backupCount=5, encoding="utf-8"
    )
    error_file.setLevel(logging.ERROR)
    error_file.setFormatter(detailed_fmt)
    root.addHandler(error_file)

    # Component-specific loggers
    _setup_component_logger("discord.raw", log_path / "discord.log", detailed_fmt)
    _setup_component_logger("social.raw", log_path / "social.log", detailed_fmt)
    _setup_component_logger("parser.output", log_path / "parser.log", detailed_fmt)
    _setup_component_logger("decisions", log_path / "decisions.log", detailed_fmt)
    _setup_component_logger("trades", log_path / "trades.log", detailed_fmt)
    _setup_component_logger("positions", log_path / "positions.log", detailed_fmt)


def _setup_component_logger(name: str, filepath: Path, formatter: logging.Formatter):
    """Create a rotating logger that writes to a specific file (2 MB × 3 backups)."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    handler = RotatingFileHandler(
        filepath, maxBytes=2*1024*1024, backupCount=3, encoding="utf-8"
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    # Don't propagate to root to avoid double-logging
    logger.propagate = False


# --- Convenience logging functions ---

def log_discord_message(
    channel_name: str,
    author: str,
    content: str,
    attachments: int = 0,
    embeds: int = 0,
    channel_id: int = 0,
    message_id: int = 0,
):
    """Log a raw source message exactly as received."""
    logger = logging.getLogger("discord.raw")
    logger.info(
        f"CHANNEL: #{channel_name} ({channel_id}) | "
        f"AUTHOR: {author} | "
        f"MSG_ID: {message_id} | "
        f"ATTACHMENTS: {attachments} | "
        f"EMBEDS: {embeds}\n"
        f"--- CONTENT START ---\n"
        f"{content}\n"
        f"--- CONTENT END ---"
    )


def log_social_post(
    handle: str,
    tweet_id: str,
    text: str,
    images: int = 0,
    created_at: str = "",
):
    """Log a raw social TA post exactly as received."""
    logger = logging.getLogger("social.raw")
    logger.info(
        f"HANDLE: @{handle} | "
        f"TWEET_ID: {tweet_id} | "
        f"IMAGES: {images} | "
        f"CREATED: {created_at}\n"
        f"--- TEXT START ---\n"
        f"{text}\n"
        f"--- TEXT END ---"
    )


def log_parser_result(
    source: str,
    raw_input: str,
    parsed_result: dict,
    had_image: bool = False,
):
    """Log what the AI parser extracted from a signal."""
    logger = logging.getLogger("parser.output")
    logger.info(
        f"SOURCE: {source} | HAS_IMAGE: {had_image}\n"
        f"--- RAW INPUT ---\n"
        f"{raw_input[:500]}\n"
        f"--- PARSED RESULT ---\n"
        f"{json.dumps(parsed_result, indent=2)}\n"
        f"--- END ---"
    )


def log_decision(
    ticker: str,
    action: str,
    conviction: float,
    sizing: str,
    reason: str,
    full_decision: dict = None,
):
    """Log a decision engine output."""
    logger = logging.getLogger("decisions")
    logger.info(
        f"TICKER: {ticker} | ACTION: {action} | "
        f"CONVICTION: {conviction:.0f}/100 | SIZING: {sizing}\n"
        f"REASON: {reason}\n"
        f"--- FULL DECISION ---\n"
        f"{json.dumps(full_decision, indent=2, default=str) if full_decision else 'N/A'}\n"
        f"--- END ---"
    )


def log_trade_execution(
    action: str,  # OPEN, CLOSE, TRIM, FILL, ORDER_PLACED, ORDER_FAILED
    ticker: str,
    direction: str,
    strike: float,
    expiry: str,
    contracts: int,
    price: float = 0,
    order_id: str = "",
    pnl_pct: float = 0,
    pnl_usd: float = 0,
    reason: str = "",
    # Slippage / latency fields (populated on FILL events)
    caller_price: float = 0,       # What the caller stated in their message
    submitted_price: float = 0,    # What we actually sent to Robinhood (post-adjustment)
    latency_s: float = 0,          # Seconds from message arrival → order submitted
):
    """Log a trade execution event."""
    logger = logging.getLogger("trades")
    slippage_str = ""
    if action == "FILL" and caller_price > 0:
        caller_slip = (price - caller_price) / caller_price * 100
        submit_slip = (price - submitted_price) / submitted_price * 100 if submitted_price else 0
        slippage_str = (
            f" | CALLER: ${caller_price:.2f} → SUBMITTED: ${submitted_price:.2f} → FILL: ${price:.2f}"
            f" | SLIP_FROM_CALLER: {caller_slip:+.1f}%"
            f" | SLIP_FROM_SUBMIT: {submit_slip:+.1f}%"
            f" | LATENCY: {latency_s:.1f}s"
        )
    logger.info(
        f"ACTION: {action} | {contracts}x {ticker} ${strike} {direction} "
        f"exp {expiry} | PRICE: ${price:.2f} | "
        f"ORDER: {order_id} | P&L: {pnl_pct:+.1f}% (${pnl_usd:+.2f}) | "
        f"REASON: {reason}"
        f"{slippage_str}"
    )


def log_position_check(
    ticker: str,
    direction: str,
    strike: float,
    entry_price: float,
    current_price: float,
    pnl_pct: float,
    hwm: float,
    trailing_active: bool,
    trailing_price: float,
    contracts_remaining: int,
):
    """Log a position monitoring tick."""
    logger = logging.getLogger("positions")
    logger.debug(
        f"{ticker} ${strike}{direction[0].upper()} | "
        f"Entry: ${entry_price:.2f} → Now: ${current_price:.2f} | "
        f"P&L: {pnl_pct:+.1f}% | HWM: ${hwm:.2f} | "
        f"Trail: {'ACTIVE @ $' + f'{trailing_price:.2f}' if trailing_active else 'inactive'} | "
        f"Contracts: {contracts_remaining}"
    )


# ── Log Cleanup ────────────────────────────────────────────────────────────────

# Retention tiers (days). Files not listed here are never touched.
_RETENTION_DAYS = {
    # High-volume, low long-term value
    "positions.log": 7,
    # Medium-volume, useful for weekly reviews
    "agent.log": 14,
    "errors.log": 14,
    "discord.log": 14,
    "social.log": 14,
    "parser.log": 14,
    # trades.log and decisions.log: NOT listed → kept forever (audit trail)
    # trades.json, pdt_tracker.json, social_seen.json, etc.: NOT listed → kept forever
}


def cleanup_old_logs(log_dir: str = "logs") -> dict:
    """
    Delete log files and their rotated backups that exceed the retention policy.

    Called once at startup. Uses file modification time (mtime) to determine age.
    Rotated backups (e.g. positions.log.1, agent.log.3) inherit the retention
    policy of their base file.

    Returns a summary dict: {"deleted": [...], "kept": int, "errors": [...]}
    """
    log_path = Path(log_dir)
    if not log_path.exists():
        return {"deleted": [], "kept": 0, "errors": []}

    now = _time.time()
    deleted = []
    errors = []
    kept = 0

    for filepath in sorted(log_path.iterdir()):
        if not filepath.is_file():
            continue

        name = filepath.name

        # Match base log files and their rotated backups (e.g. positions.log.1)
        base_name = name
        # Strip rotation suffix: "positions.log.1" → "positions.log"
        parts = name.rsplit(".", 1)
        if len(parts) == 2 and parts[1].isdigit():
            base_name = parts[0]

        if base_name not in _RETENTION_DAYS:
            kept += 1
            continue

        max_age_secs = _RETENTION_DAYS[base_name] * 86400
        try:
            file_age = now - filepath.stat().st_mtime
            if file_age > max_age_secs:
                filepath.unlink()
                age_days = file_age / 86400
                deleted.append(f"{name} ({age_days:.0f}d old)")
            else:
                kept += 1
        except Exception as e:
            errors.append(f"{name}: {e}")

    return {"deleted": deleted, "kept": kept, "errors": errors}


def prune_log_file(filepath: Path, max_age_days: int) -> int:
    """
    Remove lines older than max_age_days from a single log file (in-place).

    Parses the timestamp prefix of each line (format: YYYY-MM-DD HH:MM:SS or
    HH:MM:SS). Lines without a recognisable timestamp are kept (they're usually
    continuation lines from multi-line log entries).

    Returns the number of lines removed.

    This is more surgical than deleting entire files — useful if you want to
    trim a large active log without losing recent entries. Not called by default
    (cleanup_old_logs uses file-level deletion which is simpler and sufficient
    given RotatingFileHandler already caps individual file sizes).
    """
    if not filepath.exists():
        return 0

    # LOW-4 (Session 9): %(asctime)s writes LOCAL time — the cutoff must be
    # local too (was datetime.utcnow(), off by the UTC offset).
    cutoff = datetime.now() - timedelta(days=max_age_days)
    kept_lines = []
    removed = 0
    tmp_path = filepath.with_name(filepath.name + ".tmp")

    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Try to parse timestamp from start of line
                try:
                    # Detailed format: "2026-03-13 14:30:22 [INFO] ..."
                    ts_str = line[:19]
                    ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                    if ts < cutoff:
                        removed += 1
                        continue
                except (ValueError, IndexError):
                    pass  # No timestamp or different format — keep the line
                kept_lines.append(line)

        if removed > 0:
            # LOW-4 (Session 9): write to a temp file then atomically replace —
            # a crash mid-rewrite must never truncate the original log.
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(kept_lines)
            os.replace(tmp_path, filepath)
    except Exception as e:
        # Don't crash startup over log cleanup — but DO say what went wrong
        # instead of silently swallowing a half-finished rewrite.
        logging.getLogger(__name__).error(
            f"prune_log_file failed for {filepath}: {e}"
        )
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass
        return 0

    return removed
