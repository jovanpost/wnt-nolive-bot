"""Turn paper fills into money once Kalshi publishes the official result.

Result order of preference (same principle as the gap bot: don't ask Kalshi if no-fade already knows):
  1. no-fade's orders.result (its nightly settle job already asked Kalshi)
  2. Kalshi itself, for the words no-fade does not have
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from . import clock, engine, nofade, store
from .kalshi import KalshiPublic, market_result

log = logging.getLogger("nolive.settle")


def fetch_results(run: dict, need: list, client) -> dict:
    """{ticker: (result, source)} for the words in `need` that have an official result."""
    found: dict = {}
    nf = nofade.results_for(run["event_date"])
    for m in need:
        if m["market_ticker"] in nf:
            found[m["market_ticker"]] = (nf[m["market_ticker"]], "nofade")
    rest = [m["market_ticker"] for m in need if m["market_ticker"] not in found]
    if rest:
        def one(t):
            try:
                return t, market_result(client.get_market(t))
            except Exception as exc:
                log.warning("result lookup %s failed: %s", t, exc)
                return t, None
        with ThreadPoolExecutor(max_workers=5) as pool:
            for t, res in pool.map(one, rest):
                if res in ("yes", "no", "void"):
                    found[t] = (res, "kalshi")
    return found


def settle_run(run: dict, client=None, now=None) -> dict:
    client = client or KalshiPublic()
    now = now or clock.now_utc()
    markets = [m for m in store.markets_for_run(run["id"]) if m.get("qualified")]
    need = [m for m in markets if m.get("result") not in ("yes", "no", "void")]
    if need:
        for ticker, (res, src) in fetch_results(run, need, client).items():
            store.set_market_result(run["id"], ticker, res, src)
    markets = [m for m in store.markets_for_run(run["id"]) if m.get("qualified")]
    result_by = {m["market_ticker"]: m.get("result") for m in markets}

    orders = store.orders_for_run(run["id"])
    fills_by: dict = {}
    for f in store.fills_for_run(run["id"]):
        fills_by.setdefault(f["market_ticker"], []).append(f)

    n_settled = 0
    for o in orders:
        res = result_by.get(o["market_ticker"])
        if res not in ("yes", "no", "void") or o.get("pnl_cents") is not None:
            continue
        fills = fills_by.get(o["market_ticker"], [])
        agg = engine.aggregate(fills, o["cancel_at"])
        money = engine.settle(fills, o["cancel_at"], res)
        status = "void" if res == "void" else engine.status_for(agg, float(o["contracts"]), o["cancel_at"], now)
        store.update_order(o["id"], result=res, status=status, settled_at=now, **agg, **money)
        n_settled += 1

    pending = sum(1 for v in result_by.values() if v not in ("yes", "no", "void"))
    done = bool(markets) and pending == 0
    if done:
        store.update_run(run["id"], status="settled", settled_at=now)
        store.log_activity("settled", "%s settled: %d orders" % (run["event_date"], len(orders)))
    return {"settled_orders": n_settled, "pending_words": pending, "done": done}


def live_pnl_cents(result: str, filled: float, avg_no_cents: float, fees_cents: float) -> float:
    """Money on a real bought-NO position once Kalshi publishes the result.
    'no'  -> each contract pays $1: gain (100 - price) per contract, minus fees.
    'yes' -> each contract pays $0: lose the price paid per contract, plus fees.
    'void'-> Kalshi returns the money: 0."""
    if result == "void":
        return 0.0
    if result == "no":
        return filled * (100.0 - avg_no_cents) - fees_cents
    return -filled * avg_no_cents - fees_cents


def settle_live(client=None, now=None) -> int:
    """v3.0.7: give every real filled nolive order its official result and dollar P&L.
    Result source: no-fade's settled row first, then Kalshi itself (same order as paper)."""
    client = client or KalshiPublic()
    rows = store.live_orders_to_settle()
    if not rows:
        return 0
    done = 0
    by_date: dict = {}
    for r in rows:
        by_date.setdefault(r["event_date"], []).append(r)
    for date, orders in by_date.items():
        nf = nofade.results_for(date)
        for o in orders:
            res = nf.get(o["market_ticker"])
            if res not in ("yes", "no"):
                try:
                    res = market_result(client.get_market(o["market_ticker"]))
                except Exception as exc:  # noqa: BLE001
                    log.warning("live result lookup %s failed: %s", o["market_ticker"], exc)
                    res = None
            if res not in ("yes", "no", "void"):
                continue
            filled = float(o.get("filled_contracts") or 0)
            px = float(o.get("avg_fill_price_cents") or o.get("no_price_cents") or 0)
            pnl = live_pnl_cents(res, filled, px, float(o.get("fees_cents") or 0))
            store.update_live_order(o["client_order_id"], result=res, realized_pnl_cents=round(pnl, 4))
            done += 1
    if done:
        store.log_activity("live_settled", "%d live order(s) settled" % done)
    return done
