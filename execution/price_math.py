"""
Broker-agnostic options price math - tick rounding and spread-aware sell
pricing. Extracted from the live executor so every backend (paper, MCP)
prices identically and the BUG-38 regression suite runs against one
implementation. See docs/postmortems/no-bid-sell-pricing.md for the
incident that shaped the no-bid branch.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class PriceMathMixin:
    @staticmethod
    def _round_to_tick(price: float) -> float:
        """
        Round an options order price to the exchange minimum tick size.

        CBOE Penny Pilot Program (covers virtually all liquid options):
        - Options priced BELOW $3.00  → minimum tick $0.01
        - Options priced AT/ABOVE $3.00 → minimum tick $0.05

        Non-penny symbols (thinly traded ETFs, etc.) use $0.05/$0.10 but
        those orders would still round to a valid price here (multiples of
        $0.01 are always valid for $0.05-increment contracts too).

        Always round DOWN so we never submit above our intended limit.
        """
        import math
        if price < 3.00:
            tick = 0.01
        else:
            tick = 0.05
        # Session 9 [H3]: naive floor(price/tick) drops a full tick on
        # grid-valid prices via float error (0.29/0.01 = 28.999... → 0.28,
        # 3.05/0.05 = 60.999... → 3.00). Round the quotient before flooring.
        rounded = math.floor(round(price / tick, 6) + 1e-9) * tick
        return round(rounded, 2)

    def _floored_tick(self, price: float) -> float:
        """Round to a valid tick, but never below one tick.

        Session 17 / BUG-38 review round 2. `_round_to_tick` rounds DOWN and
        has no floor, so every tier in `_spread_aware_sell_price` underflows
        to $0.00 on a $0.01 bid — `0.01 * 0.97 = 0.0097`. $0.00 is not a
        price a broker will hold, and the population it silently disarmed is
        the dying-0DTE one this whole bug is about. Flooring where the price
        is COMPUTED, rather than at the order site, is what lets the caller
        treat "no price" as a genuine refusal instead of a rounding artefact.
        """
        return max(0.01, self._round_to_tick(price))

    def _spread_aware_sell_price(
        self, bid: float, ask: float,
        open_interest: int = 0, volume: int = 0,
        urgent: bool = False,
    ) -> Optional[float]:
        """
        Calculate a sell limit price based on bid/ask spread width, open
        interest, daily volume, and exit urgency.

        **Urgency override** (stop-loss, trailing stop, 0DTE forced exit):
        When urgent=True, always use aggressive pricing regardless of spread
        or liquidity — speed of fill matters more than saving a few cents.

        **Liquidity assessment** (non-urgent exits):
        Combines spread width with open interest and volume to determine how
        liquid the contract really is. A tight spread with 5 OI is deceptive
        (market maker quote, no real depth); a medium spread with 10k OI is
        actually very liquid.

        Tiers (non-urgent):
          - Liquid (tight spread + decent OI/volume): bid + 40% of spread
          - Normal (medium spread or thin OI): bid - 1%
          - Illiquid (wide spread or very low OI+volume): bid - 3%

        All prices rounded to valid tick sizes.
        """
        # ── No bid ────────────────────────────────────────────────────────
        # Session 17 / BUG-38, found live on CELH 2026-07-31. This branch had
        # TWO defects and, being the only silent path in this function, hid
        # both: the incident produced no pricing line at all.
        #
        # 1. `max(0.05, ...)` is the floor BUG-10 was supposed to remove. The
        #    fix was applied at the ORDER site (`max(0.01, sell_price)`, see
        #    sell_option_position) but not here — and a floor downstream
        #    cannot lower a price that already arrived too high. CELH had no
        #    bid and roughly a $0.02 ask, so the bot offered at $0.05: ABOVE
        #    THE MARKET, on a contract with 15 minutes to live. An offer above
        #    the ask cannot fill, which is why the 0DTE sweep — the mechanism
        #    whose entire purpose is dying contracts — had never once filled.
        #
        # 2. With no bid AND no ask there is no market at all, and the old
        #    code still returned $0.05. If the zeros are a QUOTE GLITCH rather
        #    than a dead contract (Session 13 proved this feed lies), that is
        #    a standing offer to sell a possibly-valuable contract for $5.
        #    Refuse instead: return None and let the caller decline to place.
        #    Nothing is lost — an order into a marketless contract could not
        #    have filled anyway — and the giveaway tail is closed.
        #
        # Review round 2 (both reviewers, independently): the refusal was
        # first signalled as 0.0 and the caller tested `sell_price <= 0`.
        # That was a FAIL-TO-EXIT, and a spectacular one — `_round_to_tick`
        # rounds DOWN, so `bid * 0.97` underflows to 0.00 for a bid of $0.01
        # on EVERY tier below. The caller could not tell the sentinel from
        # the underflow, so the bot refused to sell any contract bid at a
        # penny: exactly the dying-0DTE population BUG-38 was raised to save.
        # Hence a sentinel that is not a number (None), and a one-tick floor
        # applied at every return so no tier can underflow again.
        if bid <= 0:
            if ask > 0:
                # Aggressive, but arithmetically incapable of exceeding the
                # ask: 0.85 * ask < ask for every ask > 0, and the $0.01 floor
                # can only exceed the ask if the ask were below one tick,
                # which is not a quotable price.
                # Review round 3: respect `urgent`, like every other tier
                # does. This branch ignored it, so a NON-urgent profit trim
                # into a one-sided book published an offer 15% under the only
                # price information available — about $30 a contract on a
                # $2.00 ask, with no urgency to justify the discount. Urgency
                # is what buys aggression; without it, shave the minimum.
                discount = 0.85 if urgent else 0.95
                price = self._floored_tick(ask * discount)
                logger.warning(
                    f"Spread-aware sell: NO BID (ask=${ask:.2f}) — offering at "
                    f"${price:.2f} (ask-{(1 - discount) * 100:.0f}%"
                    f"{', urgent' if urgent else ''}, floored at one tick). "
                    f"With no bid to hit, an offer at or under the ask is the "
                    f"only thing that can fill."
                )
                return price
            # WARNING, not ERROR (review round 2): this is the ROUTINE outcome
            # of the 15:45 sweep on a contract that is already dead, and
            # errors.log is the first file read during an incident. The
            # operator-facing signal is the throttled ping at the call site.
            logger.warning(
                f"Spread-aware sell: NO MARKET AT ALL (bid=${bid:.2f}, "
                f"ask=${ask:.2f}) — refusing to price. A blind offer here "
                f"would sell for pennies if these zeros are a bad quote "
                f"rather than a dead contract."
            )
            return None

        # Session 9 [M5]: crossed or zero-ask quote guard. The tier logic below
        # assumes a sane bid < ask market — with ask <= 0 or ask < bid, the
        # LIQUID branch's `bid + (ask - bid) * 0.4` prices BELOW bid (40% under
        # on ask=0). Fall back to plain bid-based pricing instead.
        if ask <= 0 or ask < bid:
            price = bid * (0.97 if urgent else 0.99)
            logger.warning(
                f"Spread-aware sell: BAD QUOTE (bid=${bid:.2f}, ask=${ask:.2f}) "
                f"— falling back to bid-based pricing ${price:.2f} "
                f"({'bid-3% urgent' if urgent else 'bid-1%'})"
            )
            return self._floored_tick(price)

        mid = (bid + ask) / 2
        spread_pct = (ask - bid) / mid * 100 if mid > 0 else 100

        # --- Urgent exit: always aggressive, skip liquidity analysis ---
        if urgent:
            price = bid * 0.97
            logger.warning(
                f"Spread-aware sell [URGENT]: pricing at bid-3% ${price:.2f} "
                f"(bid=${bid:.2f}, ask=${ask:.2f}, spread={spread_pct:.1f}%, "
                f"OI={open_interest}, vol={volume}) — speed over price"
            )
            return self._floored_tick(price)

        # --- Liquidity score: combines spread, OI, and volume ---
        # OI < 50 and volume < 20 = thin (likely only market maker quoting)
        # OI > 500 or volume > 200 = decent depth
        thin_liquidity = (open_interest < 50 and volume < 20)
        deep_liquidity = (open_interest > 500 or volume > 200)

        if spread_pct < 10 and not thin_liquidity:
            # Tight spread + real liquidity: aim near mid
            price = bid + (ask - bid) * 0.4
            logger.info(
                f"Spread-aware sell: LIQUID — tight spread ({spread_pct:.1f}%) "
                f"+ depth (OI={open_interest}, vol={volume}) — "
                f"pricing at ${price:.2f} (bid=${bid:.2f}, ask=${ask:.2f})"
            )
        elif spread_pct < 10 and thin_liquidity:
            # Tight spread but no depth — the quote could vanish
            price = bid * 0.99
            logger.info(
                f"Spread-aware sell: THIN — tight spread ({spread_pct:.1f}%) "
                f"but low depth (OI={open_interest}, vol={volume}) — "
                f"pricing at bid-1% ${price:.2f} (bid=${bid:.2f}, ask=${ask:.2f})"
            )
        elif spread_pct < 25 and deep_liquidity:
            # Medium spread but good depth — price conservatively
            price = bid * 0.99
            logger.info(
                f"Spread-aware sell: NORMAL — medium spread ({spread_pct:.1f}%) "
                f"with depth (OI={open_interest}, vol={volume}) — "
                f"pricing at bid-1% ${price:.2f} (bid=${bid:.2f}, ask=${ask:.2f})"
            )
        elif spread_pct < 25:
            # Medium spread, thin depth — lean aggressive
            price = bid * 0.97
            logger.warning(
                f"Spread-aware sell: ILLIQUID — medium spread ({spread_pct:.1f}%) "
                f"+ low depth (OI={open_interest}, vol={volume}) — "
                f"pricing at bid-3% ${price:.2f} (bid=${bid:.2f}, ask=${ask:.2f})"
            )
        else:
            # Wide spread: aggressive regardless of OI
            price = bid * 0.97
            logger.warning(
                f"Spread-aware sell: ILLIQUID — wide spread ({spread_pct:.1f}%) "
                f"(OI={open_interest}, vol={volume}) — "
                f"pricing at bid-3% ${price:.2f} (bid=${bid:.2f}, ask=${ask:.2f})"
            )

        return self._floored_tick(price)

