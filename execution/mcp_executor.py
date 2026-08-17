"""
MCPExecutor — skeleton for the official Robinhood Agentic Trading MCP pilot.

Server: https://agent.robinhood.com/mcp/trading (hosted MCP server, HTTP
transport — NOT a REST API). Trading happens in a separate, ring-fenced
"agentic account" funded with a user-set budget; agents get read-only access
to main-account data but can only place orders in the agentic account.
Launched May 2026 (equities), options rolling out since (single-leg only),
US-only for equities/options so far — verify account eligibility (region +
options rollout) before any implementation work.

Pilot plan (per the 2026-07 hardening review):
  1. Verify eligibility (US account + options access granted).
  2. Fund a small agentic-account budget — a hard, Robinhood-enforced
     ceiling no parser hallucination can exceed.
  3. Run this executor in parallel with an existing brokerage path
     (the existing brokerage path stays the fallback) for a couple of weeks,
     comparing fills, latency, and order-status fidelity.
  4. Cut over only after the comparison holds up. The swap is execution-layer
     only — signals, parsing, decision engine, and trade management are
     untouched because both executors implement BaseExecutor.

What's known about the MCP (beta caveats):
  - No published request/response schemas or rate limits — the trade
    manager's 5s position / 1s order-status polling cadence may not be
    tolerated; discover empirically.
  - Interactive OAuth on a desktop browser is required for setup; redirect
    handling reportedly only reliable on localhost, and the headless token
    refresh lifecycle for a 24/7 unattended bot is undocumented (today's
    login is fully headless).
  - No paper trading / sandbox — all testing is live-money, and the server
    can't even be exercised while markets are closed.
  - Safety controls (trade previews, manual approval, spend alerts) may sit
    between this code and the fill if enabled on the account.

Implementing this requires an MCP client library (e.g. the `mcp` python
package, using its streamable-HTTP client transport) — deliberately NOT
added to requirements.txt yet; it becomes a dependency only when the pilot
starts.
"""

import logging

from typing import Optional

from execution.base import BaseExecutor

logger = logging.getLogger(__name__)

# Official hosted MCP endpoint (HTTP transport).
DEFAULT_MCP_URL = "https://agent.robinhood.com/mcp/trading"

_TODO = "MCP pilot not implemented — needs an MCP client (see module docstring)"


class MCPExecutor(BaseExecutor):
    """Robinhood Agentic Trading MCP execution backend (Session 9 skeleton).

    Config (config.yaml):
        executor:
          mcp_url: https://agent.robinhood.com/mcp/trading  # optional override
    """

    def __init__(self, config: dict):
        self.config = config
        self.mcp_url = config.get("executor", {}).get("mcp_url") or DEFAULT_MCP_URL
        self.logged_in = False
        # Mirrors the original brokerage executor: final submitted price of the last buy,
        # read by main.py for slippage tracking.
        self.last_submitted_price: float = 0.0
        logger.info(f"MCPExecutor initialized (url={self.mcp_url}) — skeleton only, no trading")

    def login(self, retries: int = 4, backoff: float = 15.0) -> bool:
        raise NotImplementedError(f"login: OAuth session bootstrap against {self.mcp_url} — {_TODO}")

    def logout(self):
        raise NotImplementedError(f"logout: close the MCP client session — {_TODO}")

    def ping_session(self) -> bool:
        raise NotImplementedError(f"ping_session: MCP session/token liveness probe — {_TODO}")

    def get_account_balance(self) -> Optional[float]:
        raise NotImplementedError(f"get_account_balance: read agentic-account budget/buying power — {_TODO}")

    def get_open_positions(self) -> list[dict]:
        raise NotImplementedError(f"get_open_positions: list agentic-account option positions — {_TODO}")

    def place_option_order(
        self,
        ticker: str,
        strike: float,
        expiry: str,
        direction: str,
        contracts: int,
        limit_price: Optional[float] = None,
        time_in_force: str = "gfd",
        max_cost: float = None,
    ) -> Optional[str]:
        raise NotImplementedError(f"place_option_order: single-leg buy-to-open via MCP tool call — {_TODO}")

    def sell_option_position(
        self,
        ticker: str,
        strike: float,
        expiry: str,
        direction: str,
        contracts: int,
        limit_price: Optional[float] = None,
        urgent: bool = False,
    ) -> Optional[str]:
        raise NotImplementedError(f"sell_option_position: single-leg sell-to-close via MCP tool call — {_TODO}")

    def check_order_status(self, order_id: str) -> dict:
        raise NotImplementedError(f"check_order_status: map MCP order state to the Session 9 status dict — {_TODO}")

    def cancel_order(self, order_id: str) -> dict:
        raise NotImplementedError(f"cancel_order: cancel + verify final state (H4 contract) via MCP — {_TODO}")

    def get_option_price(
        self, ticker: str, expiry: str, strike: float, direction: str
    ) -> Optional[float]:
        raise NotImplementedError(f"get_option_price: contract quote via MCP market-data tool — {_TODO}")

    def get_tradable_strikes(
        self, ticker: str, expiry_iso: str, option_type: str
    ) -> list[float]:
        raise NotImplementedError(f"get_tradable_strikes: option-chain lookup via MCP — {_TODO}")

    def get_daily_closes(self, symbol: str, days: int) -> list[float]:
        raise NotImplementedError(f"get_daily_closes: daily historicals via MCP market-data tool — {_TODO}")
