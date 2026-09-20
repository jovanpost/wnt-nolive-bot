"""READ-ONLY lookups into the no-fade bot's tables (same Supabase project).

Same principle as wnt-gap-bot: if no-fade already has the data, we read it instead of asking Kalshi again.
Everything here is SELECT-only and returns an empty answer (never an error) if a table is missing.

What no-fade has that we use:
  days.event_ticker              -> tonight's event ticker
  orders.result                  -> official yes/no result of each word (its nightly settle job fills it)
  depth (best bids, up to 5:28)  -> the last picture of the book BEFORE our 5:32:30 fire time

What no-fade does NOT have (so this bot records it itself in nolive_* tables):
  prices and order books after 5:28 PM, trades after 5:29 PM, and anything about our own paper orders.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import text

from . import store

log = logging.getLogger("nolive.nofade")


def event_ticker(event_date: str) -> str | None:
    try:
        with store.engine().connect() as conn:
            row = conn.execute(text("select event_ticker from days where event_date = :d"),
                               {"d": event_date}).mappings().first()
        val = (row or {}).get("event_ticker")
        return str(val) if val else None
    except Exception as exc:
        log.debug("nofade days lookup failed: %s", exc)
        return None


def results_for(event_date: str) -> dict:
    """{market_ticker: 'yes'|'no'} for every word no-fade has already settled that night."""
    try:
        with store.engine().connect() as conn:
            rows = conn.execute(text("""
                select market_ticker, result from orders
                where event_date = :d and result in ('yes', 'no')
            """), {"d": event_date}).mappings().all()
        return {r["market_ticker"]: str(r["result"]).lower() for r in rows}
    except Exception as exc:
        log.debug("nofade results lookup failed: %s", exc)
        return {}


def pre_book(market_ticker: str, event_date: str, as_of: Any) -> dict | None:
    """Newest no-fade depth snapshot at or before `as_of`: ts + best bids only (small on purpose)."""
    try:
        with store.engine().connect() as conn:
            row = conn.execute(text("""
                select ts, best_yes_bid, best_no_bid from depth
                where market_ticker = :m and event_date = :d and ts <= :t
                order by ts desc limit 1
            """), {"m": market_ticker, "d": event_date, "t": store._ts(as_of)}).mappings().first()
        return store._row(row) if row else None
    except Exception as exc:
        log.debug("nofade depth lookup failed: %s", exc)
        return None
