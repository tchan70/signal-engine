"""
PaperExecutor - in-memory execution backend for demos, tests, and dry runs.

Implements the full BaseExecutor contract with simulated fills:
- Orders fill immediately at the requested limit (or last quote).
- Quotes come from a seedable in-memory book (set_quote) so tests and the
  backtester can drive deterministic price paths.
- Balance and positions are tracked exactly as a brokerage would report them.

This is the default backend. Live backends (e.g. the official Robinhood
Agentic Trading MCP adapter in mcp_executor.py) implement the same surface,
so switching is a config change, not a rewrite.
"""

import itertools
import logging
from typing import Optional

from execution.base import BaseExecutor
from execution.price_math import PriceMathMixin

logger = logging.getLogger(__name__)


class PaperExecutor(PriceMathMixin, BaseExecutor):
    TICK = 0.01

    def __init__(self, starting_balance: float = 1000.0):
        self._balance = starting_balance
        self._positions: list[dict] = []
        self._orders: dict[str, dict] = {}
        self._quotes: dict[tuple, dict] = {}
        self._ids = itertools.count(1)
        self._logged_in = False

    # -- session -----------------------------------------------------------
    def login(self, retries: int = 4, backoff: float = 15.0) -> bool:
        self._logged_in = True
        return True

    def logout(self):
        self._logged_in = False

    def ping_session(self) -> bool:
        return self._logged_in

    # -- account -----------------------------------------------------------
    def get_account_balance(self) -> Optional[float]:
        return self._balance if self._logged_in else None

    def get_open_positions(self) -> list[dict]:
        return [dict(p) for p in self._positions]

    # -- quotes ------------------------------------------------------------
    def set_quote(self, ticker: str, expiry: str, strike: float,
                  direction: str, bid: float, ask: float, mark: float = None):
        """Seed the simulated book (tests / backtests / demo driver)."""
        key = (ticker.upper(), str(expiry), float(strike), direction.lower())
        self._quotes[key] = {
            "bid": bid, "ask": ask,
            "mark": mark if mark is not None else round((bid + ask) / 2, 4),
        }

    def _quote(self, ticker, expiry, strike, direction):
        return self._quotes.get(
            (ticker.upper(), str(expiry), float(strike), direction.lower()))

    def get_option_quote(self, ticker, expiry, strike, direction):
        q = self._quote(ticker, expiry, strike, direction)
        return dict(q) if q else None

    def get_option_price(self, ticker, expiry, strike, direction):
        q = self._quote(ticker, expiry, strike, direction)
        return q["mark"] if q else None

    def _get_option_market_data(self, ticker, expiry, strike, direction):
        return self.get_option_quote(ticker, expiry, strike, direction)

    def get_tradable_strikes(self, ticker, expiry, direction):
        return sorted({k[2] for k in self._quotes
                       if k[0] == ticker.upper() and k[1] == str(expiry)
                       and k[3] == direction.lower()})

    def get_daily_closes(self, symbol: str, days: int) -> list[float]:
        return []

    def floor_to_tick(self, price: float) -> float:
        return max(self.TICK, int(price / self.TICK) * self.TICK)

    # -- orders ------------------------------------------------------------
    def place_option_order(self, ticker, expiry, strike, direction,
                           contracts, limit_price) -> dict:
        if not self._logged_in:
            return {"status": "error", "detail": "not logged in"}
        cost = limit_price * 100 * contracts
        if cost > self._balance:
            return {"status": "rejected", "detail": "insufficient buying power"}
        oid = f"paper-{next(self._ids)}"
        self._balance -= cost
        self._positions.append({
            "ticker": ticker.upper(), "expiry": str(expiry),
            "strike": float(strike), "direction": direction.lower(),
            "contracts": contracts, "avg_price": limit_price,
        })
        self._orders[oid] = {"status": "filled", "avg_fill_price": limit_price,
                             "side": "buy", "contracts": contracts}
        logger.info("PAPER FILL buy %s %sx %s %s %s @ %.2f",
                    ticker, contracts, strike, direction, expiry, limit_price)
        return {"status": "filled", "order_id": oid,
                "avg_fill_price": limit_price}

    def sell_option_position(self, ticker, expiry, strike, direction,
                             contracts, limit_price) -> dict:
        if not self._logged_in:
            return {"status": "error", "detail": "not logged in"}
        for p in self._positions:
            if (p["ticker"] == ticker.upper() and p["expiry"] == str(expiry)
                    and p["strike"] == float(strike)
                    and p["direction"] == direction.lower()):
                if contracts > p["contracts"]:
                    return {"status": "rejected", "detail": "oversell"}
                p["contracts"] -= contracts
                if p["contracts"] == 0:
                    self._positions.remove(p)
                self._balance += limit_price * 100 * contracts
                oid = f"paper-{next(self._ids)}"
                self._orders[oid] = {"status": "filled",
                                     "avg_fill_price": limit_price,
                                     "side": "sell", "contracts": contracts}
                logger.info("PAPER FILL sell %s %sx @ %.2f", ticker,
                            contracts, limit_price)
                return {"status": "filled", "order_id": oid,
                        "avg_fill_price": limit_price}
        return {"status": "rejected", "detail": "no such position"}

    def check_order_status(self, order_id: str) -> dict:
        return self._orders.get(order_id, {"status": "unknown"})

    def cancel_order(self, order_id: str) -> dict:
        o = self._orders.get(order_id)
        if not o:
            return {"status": "unknown"}
        if o["status"] == "filled":
            return {"status": "rejected", "detail": "already filled"}
        o["status"] = "cancelled"
        return {"status": "cancelled"}
