"""Kalshi trading fees.

Kalshi's fee schedule: a fee is charged only when an order matches immediately (a TAKER):
    fee = round up( 0.07 x multiplier x contracts x price x (1 - price) )
A resting order that later gets hit (a MAKER) pays nothing, unless the series is listed for
maker fees (series fee_type 'quadratic_with_maker_fees'), where the rate is 0.0175 instead of 0.07.

The price is the price of the contract traded (P and 1-P give the same number).
Each fill is rounded up to the next cent on its own, the way Kalshi reports fills
(your live bot's taker fills show 1 cent each).
"""
from __future__ import annotations

import math


def _cents(dollars: float) -> int:
    return int(math.ceil(dollars * 100.0 - 1e-9))


def taker_fee_cents(contracts: float, price_cents: float, multiplier: float = 1.0) -> int:
    if contracts <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    return _cents(0.07 * float(multiplier) * float(contracts) * p * (1.0 - p))


def maker_fee_cents(contracts: float, price_cents: float, rate: float = 0.0) -> int:
    """rate = 0.0175 x multiplier when the series charges maker fees, else 0."""
    if rate <= 0 or contracts <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    return _cents(float(rate) * float(contracts) * p * (1.0 - p))
