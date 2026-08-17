"""
Notification Module - Sends trade alerts and status updates.
Supports Discord webhooks (primary), console logging, and extensible to Telegram/SMS.
"""

import logging
import json
import time
import requests
from typing import Optional

from engine.decision_engine import TradeDecision

logger = logging.getLogger(__name__)


class Notifier:
    def __init__(self, config: dict):
        self.config = config["notifications"]
        self.confirmation_mode = self.config.get("confirmation_mode", True)
        self.method = self.config.get("notification_method", "console")
        self.webhook_url = self.config.get("discord_webhook_url", "")

        # Paper trading mode — log what WOULD happen without placing real orders.
        # Set paper_trade: true in config.yaml to shadow-trade without executing.
        self.paper_trade = self.config.get("paper_trade", False)

        # Discord @ mention config.
        # mention_user_id: your Discord snowflake ID → <@ID> pings you directly.
        # mention_on: list of event types that get a mention prepended.
        raw_uid = self.config.get("mention_user_id", "") or ""
        # Ignore un-substituted env placeholder (e.g. "${DISCORD_MENTION_USER_ID}")
        self._mention_id = raw_uid if (raw_uid and not raw_uid.startswith("${")) else ""
        self._mention_on: set = set(self.config.get("mention_on", []))
        # LOW-2 (Session 9): pending_confirmations dict removed — it was
        # write-only (never read anywhere; grep-verified).

    def notify_trade_decision(self, decision: TradeDecision) -> bool:
        """
        Notify about a trade decision. 
        Returns True if trade should proceed, False if waiting for confirmation.
        """
        if decision.action == "skip":
            # PDT blocks should be louder — you need to know a signal was missed
            if "PDT" in decision.reason:
                self._send(
                    f"🚫 **PDT BLOCKED**: {decision.ticker} — {decision.reason}",
                    level="important",
                )
            else:
                self._send(
                    f"⏭️ **SKIP**: {decision.ticker} - {decision.reason}",
                    level="info",
                )
            return False

        if decision.action == "execute":
            msg = self._format_trade_alert(decision)

            if self.confirmation_mode:
                # In confirmation mode, notify and wait.
                # Session 9: wording fixed — webhooks cannot receive reactions,
                # so the old "React ✅/❌" instruction was impossible to follow.
                confirm_msg = (
                    f"🔔 **TRADE PENDING**\n{msg}\n\n"
                    f"Auto-executes in 60 seconds.\n"
                    f"Reply `!cancel {decision.ticker}` (or `!cancel all`) in the "
                    f"control channel to abort. `!pause` blocks all new entries."
                )
                self._send(confirm_msg, level="important")
                return False  # Don't execute yet
            else:
                self._send(f"🚀 **EXECUTING TRADE**\n{msg}", level="important")
                return True

        if decision.action == "manage":
            self._send(
                f"📋 **MANAGEMENT**: {decision.ticker} - {decision.reason}",
                level="info",
            )
            return True

        if decision.action == "notify_only":
            self._send(f"📊 {decision.ticker} - {decision.reason}", level="info")
            return False

        # LOW-20 (Session 9): "queue" decisions previously returned False
        # silently — the user never learned a signal was queued/watchlisted.
        if decision.action == "queue":
            self._send(
                f"👀 **WATCHLIST**: {decision.ticker} - {decision.reason}",
                level="info",
            )
            return False

        return False

    def notify_fill(
        self,
        ticker: str,
        contracts: int,
        price: float,
        direction: str,
        extra: str = "",
    ):
        """
        Notify about an order fill (real or paper-trade simulated).

        extra: optional slippage/latency line appended on a second row,
               e.g. "Caller: $1.40 → Submitted: $1.40 → Fill: $1.43 | Slip: +2.1% | Latency: 8s"
        """
        paper_tag = " 📝 *[PAPER]*" if self.paper_trade else ""
        msg = (
            f"✅ **FILLED**{paper_tag}: {contracts}x {ticker} {direction} @ ${price:.2f} "
            f"(Total: ${price * 100 * contracts:.2f})"
        )
        if extra:
            msg += f"\n{extra}"
        self._send(msg, level="important", mention_event="fill")

    def notify_exit(self, ticker: str, reason: str, pnl_pct: float, pnl_usd: float):
        """Notify about a position exit."""
        emoji = "🟢" if pnl_usd >= 0 else "🔴"
        paper_tag = " 📝 *[PAPER]*" if self.paper_trade else ""
        self._send(
            f"{emoji} **EXIT**{paper_tag}: {ticker} | {reason} | "
            f"P&L: {pnl_pct:+.1f}% (${pnl_usd:+.2f})",
            level="important",
            mention_event="exit",
        )

    def notify_trim(
        self, ticker: str, contracts_sold: int, remaining: int, pnl_pct: float
    ):
        """Notify about a position trim."""
        paper_tag = " 📝 *[PAPER]*" if self.paper_trade else ""
        self._send(
            f"✂️ **TRIM**{paper_tag}: {ticker} | Sold {contracts_sold}, "
            f"{remaining} remaining | P&L: {pnl_pct:+.1f}%",
            level="info",
        )

    def notify_error(self, message: str):
        """Notify about an error."""
        self._send(f"⚠️ **ERROR**: {message}", level="error", mention_event="error")

    def notify_login_action(self, message: str):
        """Broker-login events that may need the operator's hands (Session 12).

        The device-approval prompt used to live only in the console — the one
        place the operator is guaranteed not to be looking while monitoring
        from the chat source at work. Sent at 'important' level (retries on failure)
        with the existing `error` mention event, so it pings whoever
        `mention_user_id` names without any new config. The webhook is plain
        HTTPS, so this works during startup before the Discord gateway is up,
        and during mid-session re-auth while a sell is blocked on login.
        """
        self._send(f"🔐 **ROBINHOOD LOGIN**: {message}",
                   level="important", mention_event="error")

    def notify_status(self, status: str):
        """Send a status update."""
        self._send(f"📈 {status}", level="info")

    def notify_paper_trade(self, decision, sim_price: float):
        """
        Send a paper-trade notification showing what WOULD have been executed.
        sim_price: simulated fill price (submitted limit or live ask).
        """
        sim_cost = sim_price * 100 * decision.contracts
        msg = (
            f"📝 **PAPER TRADE** *(not executed)*\n"
            f"**{decision.ticker}** ${decision.strike} {decision.direction.upper()} "
            f"× {decision.contracts} @ ${sim_price:.2f} (${sim_cost:.2f})\n"
            f"Expiry: {decision.expiry} | SL: {decision.stop_loss_pct:.0f}% | "
            f"Conviction: {decision.conviction_score:.0f}/100 ({decision.sizing_tier})\n"
            f"Source: {decision.source_signal.source if decision.source_signal else 'unknown'}"
        )
        self._send(msg, level="important", mention_event="paper_trade")

    def notify_circuit_breaker(self, reason: str):
        """Notify about a circuit breaker trigger."""
        self._send(
            f"🛑 **CIRCUIT BREAKER**: {reason}\nTrading paused.",
            level="critical",
        )

    def format_trade_alert(self, decision: TradeDecision) -> str:
        """Format a trade decision into a readable alert (public — used by auto-trade path)."""
        lines = [
            f"**{decision.ticker}** ${decision.strike} {decision.direction.upper()}",
            f"Expiry: {decision.expiry} {'(0DTE)' if decision.is_0dte else ''}",
            f"Contracts: {decision.contracts}",
            f"Max Cost: ${decision.max_cost:.2f}",
            f"Entry Limit: ${decision.entry_price_limit:.2f}" if decision.entry_price_limit else "Entry: Market",
            f"Stop Loss: {decision.stop_loss_pct:.0f}%",
            f"Conviction: {decision.conviction_score:.0f}/100 ({decision.sizing_tier})",
            f"Source: {decision.source_signal.source if decision.source_signal else 'unknown'}",
        ]
        return "\n".join(lines)

    # Legacy alias — keep private name working in case it's called elsewhere
    _format_trade_alert = format_trade_alert

    def _mention_prefix(self, event: str) -> str:
        """
        Return a Discord mention string to prepend to a message, or empty string.

        Checks whether `event` matches the configured mention_on list.
        "all" in mention_on triggers for every event type.

        Discord requires both the mention in the message content AND the
        `allowed_mentions` field in the payload — otherwise the ping is
        silently suppressed by Discord's mention safety filter.
        """
        if not self._mention_id:
            return ""
        if "all" in self._mention_on or event in self._mention_on:
            return f"<@{self._mention_id}> "
        return ""

    def _send(self, message: str, level: str = "info", mention_event: str = ""):
        """Send a notification via the configured method."""
        # Always log
        log_fn = getattr(logger, level if level != "important" else "info", logger.info)
        log_fn(f"[NOTIFY] {message}")

        if self.method == "discord" and self.webhook_url:
            mention = self._mention_prefix(mention_event) if mention_event else ""
            self._send_discord(message, mention=mention, level=level)
        elif self.method == "console":
            print(f"\n{'='*50}\n{message}\n{'='*50}\n")

    def _send_discord(self, message: str, mention: str = "", level: str = "info"):
        """Send via Discord webhook.

        mention: optional '<@USER_ID> ' prefix — when present, the payload's
                 allowed_mentions.users includes the ID so Discord actually
                 delivers the ping instead of silently stripping it.

        M13 (Session 9):
        - allowed_mentions is ALWAYS sent with "parse": [] so caller text
          echoed into notification bodies (e.g. a quoted "@everyone" alert)
          can never ping the server — only the explicitly configured user
          mention is ever honoured.
        - level != "info" messages (important/error/critical, e.g.
          "🚨 SELL FAILED") retry up to 3 times with backoff, honouring
          Retry-After on 429. Final failure is logged loudly.
        """
        # Discord webhook message limit is 2000 chars
        full_message = mention + message
        if len(full_message) > 1900:
            full_message = full_message[:1900] + "..."

        payload: dict = {
            "content": full_message,
            # Never let echoed caller text (@everyone/@here/role pings) fire —
            # only the explicitly configured user mention, when present.
            "allowed_mentions": {
                "parse": [],
                "users": [self._mention_id] if (mention and self._mention_id) else [],
            },
        }

        attempts = 3 if level != "info" else 1
        for attempt in range(1, attempts + 1):
            wait = 2.0 * attempt  # default backoff
            try:
                resp = requests.post(
                    self.webhook_url,
                    json=payload,
                    timeout=10,
                )
                if resp.status_code in (200, 204):
                    return
                if resp.status_code == 429:
                    # Honour Retry-After (header is seconds; may be fractional)
                    try:
                        retry_after = float(
                            resp.headers.get("Retry-After")
                            or resp.json().get("retry_after", wait)
                        )
                        wait = min(max(retry_after, 0.5), 30.0)
                    except Exception:
                        pass
                logger.warning(
                    f"Discord webhook attempt {attempt}/{attempts} failed: "
                    f"HTTP {resp.status_code}"
                )
            except Exception as e:
                logger.warning(
                    f"Discord webhook attempt {attempt}/{attempts} error: {e}"
                )
            if attempt < attempts:
                time.sleep(min(wait, 30.0))

        logger.error(
            f"Discord webhook FAILED after {attempts} attempt(s) — "
            f"notification LOST: {message[:200]!r}"
        )
