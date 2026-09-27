#!/usr/bin/env python3
"""Read-only pre-flight for the LIVE engine. Places nothing, cancels nothing.

Run this BEFORE ever setting LIVE_DRY_RUN=false, to confirm the Kalshi key works and to see
tonight's would-be orders, with no risk. Reads the key from your .env / Streamlit Secrets --
never asks you to type or paste it here.

    python3 scripts/verify_live_api.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nolive import clock, config as C, engine, live, store  # noqa: E402
from nolive.kalshi import KalshiClient, KalshiPublic, count_needed, market_prices, word_from_market  # noqa: E402


def line(char="-", n=72):
    print(char * n)


def main() -> int:
    print(C.live_summary())
    line("=")

    client = KalshiClient()
    print("Base URL   : %s" % client.base_url)
    print("Key loaded : %s" % client.authenticated)
    if not client.authenticated:
        print("\n!! No usable private key (KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PEM not set).")
        print("   The live engine will stay disabled until both are set. Market-data checks below still run.")
    line()

    if client.authenticated:
        try:
            balance = client.get_balance()
            cash = (balance.get("balance") or 0) / 100.0
            print("AUTH OK. Cash balance: $%.2f" % cash)
            need = C.live_collateral_per_market() * C.LIVE_MAX_MARKETS_PER_DAY
            print("Worst-case resting collateral (all %d markets): $%.2f" % (C.LIVE_MAX_MARKETS_PER_DAY, need))
            if cash < need:
                print("  !! $%.2f short of that worst case." % (need - cash))
        except Exception as exc:
            print("AUTH FAILED: %s" % exc)
            return 1
        line()
        try:
            resting = client.get_resting_orders(series_prefix=C.SERIES)
            print("Currently resting %s orders: %d" % (C.SERIES, len(resting)))
        except Exception as exc:
            print("Could not list resting orders: %s" % exc)
        line()

    pub = KalshiPublic()
    print("TODAY IS %s (%s)" % (clock.today_ct(), clock.fmt(clock.now_ct(), with_seconds=False)))
    print("Live cancel time: %s CT" % C.LIVE_CANCEL_CT)
    try:
        events = pub.get_events(C.SERIES, status="open")
        print("Open events in %s: %d" % (C.SERIES, len(events)))
        for ev in events[:8]:
            print("  %s" % ev.get("event_ticker"))
        today_ticker = None
        for ev in events:
            t = str(ev.get("event_ticker") or "")
            if live.LiveRunner._ticker_date(t) == clock.today_ct():
                today_ticker = t
                break
        if today_ticker:
            markets = [m for m in pub.get_markets(today_ticker)
                      if str(m.get("status") or "").lower() in ("active", "open")]
            print("\nToday's event %s: %d active markets" % (today_ticker, len(markets)))
            print("(read-only -- nothing below is sent to Kalshi)\n")
            for mkt in markets[:20]:
                word = word_from_market(mkt)
                last, bid, ask = market_prices(mkt)
                counting = count_needed(word) >= 2
                q = engine.qualify(last, bid, ask, mkt.get("status"), C.QUALIFY_MAX_YES_CENTS,
                                   is_counting=counting and C.EXCLUDE_COUNTING_WORDS)
                verdict = "WOULD ORDER" if q["qualified"] else ("skip: %s" % q["reason"])
                print("  %-32s last=%s  %s" % (word[:32], last, verdict))
        else:
            print("\nNo event for today yet.")
    except Exception as exc:
        print("Market data failed: %s" % exc)
    line()

    try:
        store.init_db()
        backend = "POSTGRES" if store.using_postgres() else "SQLITE (local only!)"
        print("Storage    : %s" % backend)
        print("live orders on file for today: %d" % len(store.live_orders_for_day(clock.today_ct())))
    except Exception as exc:
        print("Storage check FAILED: %s" % exc)
        return 1

    line("=")
    print("Pre-flight complete. Nothing above placed or cancelled a real order.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
