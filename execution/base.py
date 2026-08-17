"""
Executor interface — the abstract contract every execution backend implements.

Session 9: extracted from the original brokerage executor per the hardening review's
migration plan ("Abstract the executor"): define one interface, keep the
private brokerage implementation as the original/fallback
backend, and develop the official Robinhood Agentic Trading MCP pilot
(execution/mcp_executor.py) against the same surface so the switch is an
execution-layer swap, not a rewrite. Signal ingestion, parsing, the decision
engine, and trade management only ever see this interface.

Conventions shared by all implementations:
- Methods NEVER raise to callers for routine API failures — they return the
  documented failure sentinel (None / [] / status="error" dict) and log.
- All prices are per-share option premiums (a $1.22 contract = 1.22, not 122).
- Expiries passed in may be any parser format ("0DTE", "1DTE", "WEEKLY",
  "M/D", ISO); implementations normalize via utils.market_time.normalize_expiry.
"""

from abc import ABC, abstractmethod
from typing import Optional


class BaseExecutor(ABC):
    """Abstract execution backend for single-leg options trading."""

    @abstractmethod
    def login(self, retries: int = 4, backoff: float = 15.0) -> bool:
        """Authenticate with the brokerage.

        Retries with exponential backoff on transient failures. Returns True
        on success, False after all attempts fail. Implementations should set
        an internal logged-in flag consumed by the other methods.
        """

    @abstractmethod
    def logout(self):
        """End the brokerage session. Must never raise."""

    @abstractmethod
    def ping_session(self) -> bool:
        """Lightweight liveness check of the authenticated session.

        Returns True if the session is still valid, False if expired/broken.
        Used by the health-check scheduler to detect stale sessions early.
        """

    @abstractmethod
    def get_account_balance(self) -> Optional[float]:
        """Available buying power (dollars) for opening new positions.

        Session 9 [M7] contract: returns None on API failure — NEVER a 0.0
        sentinel (0.0 is a legitimate balance and must not be conflated with
        "fetch failed"). Callers treat None as "balance unknown".
        """

    @abstractmethod
    def get_open_positions(self) -> list[dict]:
        """All open option positions at the brokerage.

        Each dict contains: ticker, strike (float), expiry (ISO str),
        direction ("call"/"put"), quantity (float), avg_cost (per-share
        float), option_id. Returns [] on total failure; a single malformed
        position is skipped, not fatal (Session 9 [LOW-6]).
        """

    @abstractmethod
    def place_option_order(
        self,
        ticker: str,
        strike: float,
        expiry: str,
        direction: str,  # "call" or "put"
        contracts: int,
        limit_price: Optional[float] = None,
        time_in_force: str = "gfd",  # good for day
        max_cost: float = None,  # Session 9: per-order cost ceiling (None = no cap)
    ) -> Optional[str]:
        """Place an options BUY (open) order.

        limit_price is the caller's per-share entry (None = price off the
        live ask). max_cost, when set, hard-blocks orders whose estimated
        total cost exceeds max_cost by >10% headroom. Returns the order ID
        on success, None on any failure or pre-flight block. Implementations
        must expose the final submitted price via `last_submitted_price`.
        """

    @abstractmethod
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
        """Place an options SELL (close) order.

        urgent=True uses aggressive pricing (fill speed over price) — used
        for stop-loss, trailing-stop, and 0DTE forced exits. limit_price=None
        means price from the live quote. Returns order ID or None.
        """

    @abstractmethod
    def check_order_status(self, order_id: str) -> dict:
        """Current state of an order.

        Session 9 contract — ALWAYS returns all keys:
          {"status": str, "filled_quantity": float,
           "average_price_per_share": float, "total_premium": float,
           "average_price": float}
        average_price is kept equal to total_premium for legacy callers
        (BUG-7 trap — prefer average_price_per_share). Error path:
        status="error" with all numerics 0.0.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict:
        """Cancel an open order and report its VERIFIED final state.

        Session 9 [H4] contract:
          {"cancelled": bool, "final_status": str,
           "filled_quantity": float, "average_price_per_share": float}
        cancelled=True only when the final state is confirmed cancelled.
        If the order (partially) filled in the cancel race, filled_quantity
        reflects it and the caller MUST register the fill.
        """

    @abstractmethod
    def get_option_price(
        self, ticker: str, expiry: str, strike: float, direction: str
    ) -> Optional[float]:
        """Current mid price (per-share) of an option contract, or None."""

    @abstractmethod
    def get_tradable_strikes(
        self, ticker: str, expiry_iso: str, option_type: str
    ) -> list[float]:
        """Sorted tradable strikes for ticker/expiry/type; [] on failure.

        Session 9: lets trade construction validate against the real option
        chain instead of guessing strike increments (review H12).
        """

    @abstractmethod
    def get_daily_closes(self, symbol: str, days: int) -> list[float]:
        """Last `days` daily closing prices (oldest→newest); [] on failure.

        Session 9: input for realized-volatility / regime-gate computations.
        """

    # ── De-facto interface (Session 9 verify-pass) ───────────────────────────
    # Not abstract (the private executor defines it privately), but the orchestrator uses
    # it for paper-fill pricing and restore HWM seeding. A concrete default
    # that raises makes alternative backends (MCP) fail loudly instead of
    # dying with AttributeError.
    def _get_option_market_data(self, ticker: str, expiry: str,
                                strike: float, direction: str):
        raise NotImplementedError(
            f"{type(self).__name__} does not implement _get_option_market_data "
            f"(needed for paper-fill pricing and restore HWM seeding)"
        )

    # ── Exit-ladder support (2026-08-04) ─────────────────────────────────────
    # Concrete SAFE defaults, deliberately not abstract: the ladder is an
    # optimisation, and its own contract is "any doubt → None → the classic
    # sell flow takes over". A backend that never implements these simply
    # never ladders — it must not die with AttributeError mid-exit (review
    # round 1, latent for the MCP pilot).
    def get_option_quote(
        self, ticker: str, expiry: str, strike: float, direction: str
    ) -> Optional[dict]:
        """A sane two-sided book {"bid","ask","mid","spread_pct"}, or None."""
        return None

    def floor_to_tick(self, price: float) -> float:
        """A valid option tick at or below `price`, never below $0.01."""
        import math
        tick = 0.01 if price < 3.00 else 0.05
        return max(0.01, round(math.floor(round(price / tick, 6) + 1e-9) * tick, 2))
