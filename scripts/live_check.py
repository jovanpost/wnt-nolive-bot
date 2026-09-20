#!/usr/bin/env python3
"""30-second check against REAL Kalshi data. Read-only: no database, no Telegram, no orders.

    python3 scripts/live_check.py                 # newest open event, else the newest settled one
    python3 scripts/live_check.py KXWORLDNEWSMENTION-26SEP18

It shows what the bot would do at 5:32:30 with the prices and books that exist right now:
which words qualify, and what an instant (taker) match would look like.
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nolive import config as C, engine  # noqa: E402
from nolive.kalshi import KalshiPublic, count_needed, market_prices, word_from_market  # noqa: E402


def ticker_date(t):
    try:
        return datetime.strptime("20" + t.split("-")[1], "%Y%b%d")
    except Exception:
        return datetime.min


def main():
    k = KalshiPublic()
    print(C.summary())
    print()
    s = k.get_series(C.SERIES)
    print("series fee_type = %r, fee_multiplier = %r  (plain 'quadratic' means resting orders pay no fee)" % (s.get("fee_type"), s.get("fee_multiplier")))

    if len(sys.argv) > 1:
        event = sys.argv[1]
    else:
        evs = k.get("/events", {"series_ticker": C.SERIES, "status": "open", "limit": 20}).get("events") or []
        label = "open"
        if not evs:
            evs = k.get("/events", {"series_ticker": C.SERIES, "status": "settled", "limit": 20}).get("events") or []
            label = "settled (nothing is open right now)"
        if not evs:
            print("no events found")
            return
        event = max((e["event_ticker"] for e in evs), key=ticker_date)
        print("newest %s event: %s" % (label, event))
    print()

    markets = k.get_markets(event)
    print("%d markets in %s" % (len(markets), event))
    print()
    print("%-34s %-8s %6s %6s %6s  %s" % ("word", "status", "last", "bid", "ask", "would the bot order?"))
    first_q = None
    for m in markets:
        last, bid, ask = market_prices(m)
        q = engine.qualify(last, bid, ask, m.get("status"), C.SKIP_YES_AT_OR_ABOVE)
        w = word_from_market(m)
        if q["qualified"] and first_q is None:
            first_q = m
        print("%-34s %-8s %6s %6s %6s  %s%s" % (w[:34], str(m.get("status"))[:8], last, bid, ask,
                                                "YES: sell YES %dc" % C.LIMIT_YES_CENTS if q["qualified"] else "no (%s)" % q["reason"],
                                                "  [counting word]" if count_needed(w) >= 2 else ""))
    if first_q is None:
        print("\nNo word qualifies right now (markets already settled or at 98c+). Try again on a live evening.")
        return

    t = first_q["ticker"]
    print("\n--- order book for %s" % t)
    book = k.get_orderbook(t, depth=15)
    print("YES bids (price c, size), best last:", book["yes"][-6:])
    print("NO bids  (price c, size), best last:", book["no"][-6:])
    summ = engine.book_summary(book, C.LIMIT_YES_CENTS)
    print("YES size that would match a SELL YES %dc right now: %s   |   NO size queued ahead of us: %s" % (
        C.LIMIT_YES_CENTS, summ["yes_size_at_limit"], summ["queue_ahead"]))
    want = engine.contracts_for(C.PAPER_DOLLARS, C.LIMIT_YES_CENTS)
    fills = engine.taker_fills(book, C.LIMIT_YES_CENTS, want)
    print("order size $%g -> %.2f contracts; instant taker fills: %s" % (C.PAPER_DOLLARS, want, fills or "none (it would rest)"))

    print("\n--- last trades for %s (oldest first)" % t)
    trades = k.get_trades(t, max_pages=1)[-6:]
    for tr in trades:
        print("  %s  YES %.2fc  x %.2f  taker=%s" % (tr["ts"].strftime("%H:%M:%S UTC"), tr["yes_cents"], tr["count"], tr["taker_side"]))
    if not trades:
        print("  (no trades returned)")
    print("\nIf the numbers above look like what you see on Kalshi, the parsing is right.")


if __name__ == "__main__":
    main()
