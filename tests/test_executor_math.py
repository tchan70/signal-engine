"""Executor price math — tick rounding (H3) and sell pricing (M5/H2)."""
import pytest

from execution.paper import PaperExecutor as RobinhoodExecutor


@pytest.fixture()
def ex():
    # Bypass __init__ (no credentials/session needed for pure math)
    return RobinhoodExecutor.__new__(RobinhoodExecutor)


# H3: grid-valid prices must survive rounding (float error used to drop a tick)
@pytest.mark.parametrize(
    "price,expected",
    [
        (0.29, 0.29), (0.57, 0.57), (1.13, 1.13),   # penny grid
        (3.05, 3.05), (4.10, 4.10),                  # nickel grid
        (0.294, 0.29), (3.07, 3.05),                 # off-grid floors down
        (2.99, 2.99),                                # boundary below $3
    ],
)
def test_round_to_tick(ex, price, expected):
    assert ex._round_to_tick(price) == pytest.approx(expected)


def test_spread_pricing_zero_ask_guard(ex):
    # M5: stale quote with ask=0 must NOT price 40% below bid
    price = ex._spread_aware_sell_price(bid=1.00, ask=0.0,
                                        open_interest=1000, volume=500)
    assert price >= 0.90


def test_spread_pricing_crossed_quote_guard(ex):
    price = ex._spread_aware_sell_price(bid=1.00, ask=0.80,
                                        open_interest=1000, volume=500)
    assert price >= 0.90


def test_spread_pricing_normal_liquid(ex):
    # Liquid, tight spread: bid + 40% of spread
    price = ex._spread_aware_sell_price(bid=1.00, ask=1.05,
                                        open_interest=1000, volume=500)
    assert 1.00 <= price <= 1.05


def test_spread_pricing_urgent_is_aggressive(ex):
    urgent = ex._spread_aware_sell_price(bid=1.00, ask=1.05, urgent=True)
    assert urgent <= 1.00
