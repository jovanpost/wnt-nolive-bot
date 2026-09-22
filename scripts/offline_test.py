#!/usr/bin/env python3
"""Offline self-test for the nolive paper bot. No network, no Supabase, no Telegram.

Runs a whole fake night (5:31 PM -> 6:06 PM Central) against a scripted fake Kalshi and a throwaway
SQLite file, then checks every number by hand-computed expectations.

    python3 scripts/offline_test.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
# Optional: TEST_DATABASE_URL=postgresql://... runs the same night against a real Postgres.
PG = os.environ.get("TEST_DATABASE_URL")
KEEP = os.environ.get("OFFLINE_TEST_DB")      # keep the finished fake night here (used to smoke-test the dashboard)
DB = KEEP or tempfile.mktemp(suffix=".db")
if PG:
    os.environ["DATABASE_URL"] = PG
else:
    os.environ.pop("DATABASE_URL", None)
    os.environ["SQLITE_PATH"] = DB

from sqlalchemy import text  # noqa: E402

from nolive import analytics, clock, config as C, engine, fees, kalshi, pipeline, settle, store  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append(name)
        print("  FAIL  %s  %s" % (name, detail))


def close(a, b, tol=0.02):
    return a is not None and b is not None and abs(float(a) - float(b)) <= tol


DATE = "2026-09-18"
T = clock.fire_at(DATE).astimezone(timezone.utc)          # 22:32:30 UTC
EVENT = "KXWORLDNEWSMENTION-26SEP18"


# ------------------------------------------------------------------ 1) fees
print("1) fees")
check("taker 10ct @40c = 17c", fees.taker_fee_cents(10, 40) == 17)
check("taker 12.5ct @40c = 21c", fees.taker_fee_cents(12.5, 40) == 21)
check("taker tiny fill rounds up to 1c", fees.taker_fee_cents(0.2, 60) == 1)
check("taker symmetric in P", fees.taker_fee_cents(7, 30) == fees.taker_fee_cents(7, 70))
check("no fee at 0 contracts", fees.taker_fee_cents(0, 50) == 0)
check("maker fee is 0 when the series has none", fees.maker_fee_cents(10, 55, 0.0) == 0)
check("maker fee 0.0175 x 10 x .55 x .45 = 5c", fees.maker_fee_cents(10, 55, 0.0175) == 5)

# ------------------------------------------------------------------ 2) qualify
print("2) qualify (v2: 30c-or-below, counting words excluded)")
check("YES 20c qualifies", engine.qualify(20, 18, 22, "active", 30)["qualified"])
check("YES 30c qualifies (at the cap)", engine.qualify(30, 29, 31, "active", 30)["qualified"])
check("YES 1c qualifies", engine.qualify(1, 1, 2, "active", 30)["qualified"])
check("YES 31c does not", not engine.qualify(31, 30, 32, "active", 30)["qualified"])
check("YES 70c does not", not engine.qualify(70, 65, 72, "active", 30)["qualified"])
check("bid above cap disqualifies even if last is stale", not engine.qualify(20, 32, 33, "active", 30)["qualified"])
check("no trade -> falls back to bid", engine.qualify(None, 20, 25, "active", 30)["basis"] == "bid")
check("no price at all -> skip", engine.qualify(None, None, None, "active", 30)["reason"] == "no price")
check("closed market -> skip", not engine.qualify(20, 19, 21, "closed", 30)["qualified"])
check("half-cent 29.5 qualifies", engine.qualify(29.5, 29, 30, "active", 30)["qualified"])
check("counting word excluded even at a cheap price", engine.qualify(10, 9, 11, "active", 30, True)["reason"] == "counting word (excluded)")
check("counting word flag off -> normal price rule applies", engine.qualify(10, 9, 11, "active", 30, False)["qualified"])

# ------------------------------------------------------------------ 3) engine: taker / maker / money
print("3) engine")
check("contracts $3 @ sell YES 30c = 4.29", engine.contracts_for(3, 30) == 4.29)
check("order_size: small request is not capped", engine.order_size(3, 30, 50) == {"contracts": 4.29, "capped": False, "raw": 4.29})
sz_big = engine.order_size(500, 30, 50)
check("order_size: big request gets capped at 50", sz_big["contracts"] == 50 and sz_big["capped"] and close(sz_big["raw"], 714.29, 0.01))
book = {"yes": [(10, 5), (35, 4), (40, 3), (75, 100)], "no": [(20, 7), (65, 2), (70, 1)]}
s = engine.book_summary(book, 30)
check("size at/above limit", s["yes_size_at_limit"] == 107)
check("queue ahead = NO bids >= 70", s["queue_ahead"] == 1)
tf = engine.taker_fills(book, 30, 4.29)
check("taker: best bid first, one level", len(tf) == 1 and tf[0]["price_cents"] == 75 and tf[0]["contracts"] == 4.29)
check("taker fee on 4.29 @75c = 6c", tf[0]["fee_cents"] == 6)
tf2 = engine.taker_fills({"yes": [(36, 2), (35, 10)]}, 30, 4.29)
check("taker: walks two levels", [f["price_cents"] for f in tf2] == [36, 35] and close(sum(f["contracts"] for f in tf2), 4.29, 1e-6))
check("taker: nothing below limit", engine.taker_fills({"yes": [(25, 100)]}, 30, 5) == [])
tr = [
    {"id": "a", "ts": T + timedelta(seconds=5), "yes_cents": 30.0, "count": 10, "taker_side": "yes"},
    {"id": "b", "ts": T + timedelta(seconds=30), "yes_cents": 35.0, "count": 4, "taker_side": "no"},
    {"id": "c", "ts": T + timedelta(seconds=60), "yes_cents": 31.0, "count": 3, "taker_side": "yes"},
    {"id": "d", "ts": T + timedelta(seconds=400), "yes_cents": 99.0, "count": 200, "taker_side": "yes"},
    {"id": "e", "ts": T - timedelta(seconds=5), "yes_cents": 99.0, "count": 200, "taker_side": "yes"},
    {"id": "f", "ts": T + timedelta(seconds=5000), "yes_cents": 99.0, "count": 200, "taker_side": "yes"},
]
mf = engine.maker_fills(tr, T, T + timedelta(seconds=1400), 30, 8.0)
check("maker: strictly-above only, buyer must be the taker, inside the window",
      [f["ref"] for f in mf] == ["trade:c", "trade:d"] and [f["contracts"] for f in mf] == [3.0, 5.0], str(mf))
fills = [
    {"kind": "taker", "ts": T, "contracts": 4.0, "price_cents": 71.0, "fee_cents": 2},
    {"kind": "maker", "ts": T + timedelta(seconds=100), "contracts": 3.0, "price_cents": 30.0, "fee_cents": 0},
    {"kind": "maker", "ts": T + timedelta(seconds=500), "contracts": 2.0, "price_cents": 30.0, "fee_cents": 0},
]
ag = engine.aggregate(fills, T + timedelta(seconds=150))
check("aggregate respects the cancel time", ag["filled_contracts"] == 7.0 and ag["taker_contracts"] == 4.0 and ag["maker_contracts"] == 3.0)
check("risk = n x (100 - price)", close(ag["risk_cents"], 4 * 29 + 3 * 70))
sm_no = engine.settle(fills, T + timedelta(seconds=150), "no")
check("word NOT said: keep the price, fees off", close(sm_no["pnl_cents"], 4 * 71 - 2 + 3 * 30))
sm_yes = engine.settle(fills, T + timedelta(seconds=150), "yes")
check("word said: lose 100 - price, fees off", close(sm_yes["pnl_cents"], -4 * 29 - 2 - 3 * 70))
check("void = zero", engine.settle(fills, T, "void")["pnl_cents"] == 0.0)

# ------------------------------------------------------------------ 4) kalshi parsers
print("4) kalshi parsers")
k = kalshi.KalshiPublic()
k.get = lambda endpoint, params=None, **kw: {"orderbook_fp": {"yes_dollars": [["0.6000", "3.00"], ["0.5500", "10.00"]],
                                                              "no_dollars": [["0.4000", "7.00"]]}}
ob = k.get_orderbook("X")
check("orderbook dollars -> cents, ascending, best bid last", ob["yes"] == [(55.0, 10.0), (60.0, 3.0)] and ob["no"] == [(40.0, 7.0)], str(ob))
k.paginate = lambda endpoint, key, params=None, max_pages=3: [
    {"trade_id": "t2", "created_time": "2026-09-18T23:00:31.888014Z", "yes_price_dollars": "0.9900", "count_fp": "6.23", "taker_side": "no"},
    {"trade_id": "t1", "created_time": "2026-09-18T22:53:10.04941Z", "yes_price_dollars": "0.5500", "count_fp": "2.00", "taker_side": "yes"}]
trd = k.get_trades("X", min_ts=1)
check("trades parsed and returned oldest first", [t["id"] for t in trd] == ["t1", "t2"] and trd[0]["yes_cents"] == 55.0 and trd[1]["count"] == 6.23)
check("count_needed", kalshi.count_needed("Iran (3+ times)") == 3 and kalshi.count_needed("Oil / Gas") == 1)


# ------------------------------------------------------------------ 5) a whole fake night
print("5) whole night, fake Kalshi (v2 rule: 30c-or-below, counting words excluded, $3 -> 4.29ct, cap 50)")
if PG:      # start clean: drop our tables and the fake no-fade copies
    with store.engine().begin() as conn:
        for tname in ("nolive_depth", "nolive_trades", "nolive_fills", "nolive_orders", "nolive_markets", "nolive_runs",
                      "nolive_activity", "nolive_state", "days", "orders", "depth"):
            conn.execute(text("drop table if exists %s cascade" % tname))
store.init_db()
with store.engine().begin() as conn:      # tables that the no-fade bot owns (fake copies, read-only for us)
    if PG:   # same column types as the real no-fade schema
        conn.execute(text("create table days (event_date varchar(10) primary key, event_ticker varchar(64))"))
        conn.execute(text("create table orders (id serial primary key, event_date varchar(10), market_ticker varchar(128), result varchar(8))"))
        conn.execute(text("create table depth (id serial primary key, ts timestamptz, event_date varchar(10), market_ticker varchar(128), best_yes_bid integer, best_no_bid integer)"))
    else:
        conn.execute(text("create table days (event_date text primary key, event_ticker text)"))
        conn.execute(text("create table orders (id integer primary key autoincrement, event_date text, market_ticker text, result text)"))
        conn.execute(text("create table depth (id integer primary key autoincrement, ts text, event_date text, market_ticker text, best_yes_bid integer, best_no_bid integer)"))
    conn.execute(text("insert into days values (:d, :e)"), {"d": DATE, "e": EVENT})
    conn.execute(text("insert into orders (event_date, market_ticker, result) values (:d, :m, :r)"), {"d": DATE, "m": EVENT + "-OIL", "r": "yes"})
    conn.execute(text("insert into depth (ts, event_date, market_ticker, best_yes_bid, best_no_bid) values (:t, :d, :m, 18, 78)"),
                 {"t": (T - timedelta(minutes=4)) if PG else (T - timedelta(minutes=4)).isoformat(), "d": DATE, "m": EVENT + "-OIL"})


def m(tk, word, last, bid, ask, status="active"):
    d = {"ticker": EVENT + "-" + tk, "title": word, "yes_sub_title": word, "status": status}
    if last is not None:
        d["last_price_dollars"] = "%.4f" % (last / 100.0)
    if bid is not None:
        d["yes_bid_dollars"] = "%.4f" % (bid / 100.0)
    if ask is not None:
        d["yes_ask_dollars"] = "%.4f" % (ask / 100.0)
    return d


MARKETS = [
    m("OIL", "Oil / Gas", 20, 18, 22),        # qualifies: 20c <= 30c cap
    m("DRONE", "Drone", 70, 65, 72),          # too expensive under v2 (was fine under the old 98c rule)
    m("NVDA", "Nvidia", 99, 98, 100),         # too expensive
    m("TRUMP5", "Trump (5+ times)", 20, 18, 22),  # cheap enough on price alone, but a counting word: excluded
    m("HELI", "Helicopter", None, None, None),    # no price
    m("CLOSED", "Closed", 50, 49, 51, status="closed"),
    m("ICE", "ICE", 25, 23, 27),              # qualifies: 25c <= 30c cap
]
BOOKS = {
    "OIL": {"yes": [(10, 50)], "no": [(65, 2), (70, 1)]},
    "ICE": {"yes": [(35, 4)], "no": [(60, 3)]},
}
TAPE = {
    "OIL": [(-200, 40.0, 5, "yes"), (60, 40.0, 4, "yes"), (400, 99.0, 200, "yes")],
    "ICE": [(-150, 28.0, 3, "yes"), (50, 32.0, 2, "yes")],
    "NVDA": [(-100, 99.0, 3, "yes"), (100, 99.0, 4, "no")],
    "TRUMP5": [(-30, 45.0, 2, "no"), (30, 58.0, 3, "yes")],
    "DRONE": [], "HELI": [], "CLOSED": [],
}
RESULTS = {"ICE": "no"}       # OIL's result comes from the no-fade fake table above; only ICE needs Kalshi


class FakeKalshi:
    def __init__(self, clock_ref):
        self.clock = clock_ref
        self.calls = {"markets": 0, "book": 0, "trades": 0, "market": 0}

    def get_events(self, series, status="open"):
        return [{"event_ticker": EVENT}]

    def get_series(self, series):
        return {"fee_type": "quadratic", "fee_multiplier": 1}

    def get_markets(self, event_ticker):
        self.calls["markets"] += 1
        return [dict(x) for x in MARKETS]

    def get_orderbook(self, ticker, depth=15):
        self.calls["book"] += 1
        b = BOOKS.get(ticker.split("-")[-1], {"yes": [], "no": []})
        return {"yes": list(b["yes"]), "no": list(b["no"])}

    def get_trades(self, ticker, min_ts=None, limit=1000, max_pages=3):
        self.calls["trades"] += 1
        out = []
        for i, (sec, price, cnt, side) in enumerate(TAPE.get(ticker.split("-")[-1], [])):
            ts = T + timedelta(seconds=sec)
            if ts <= self.clock[0] and (min_ts is None or ts.timestamp() >= min_ts):
                out.append({"id": "%s#%d" % (ticker, i), "ts": ts, "yes_cents": price, "count": float(cnt), "taker_side": side})
        return out

    def get_market(self, ticker):
        self.calls["market"] += 1
        res = RESULTS.get(ticker.split("-")[-1])
        return {"result": res} if res else {"result": ""}


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, text_, quiet=False):
        self.sent.append(text_)


now_ref = [clock.record_from(DATE).astimezone(timezone.utc) - timedelta(seconds=30)]     # 5:27:30 PM CT


def advance(seconds):
    now_ref[0] = now_ref[0] + timedelta(seconds=seconds)


fake = FakeKalshi(now_ref)
notifier = FakeNotifier()
runner = pipeline.Runner(client=fake, now_fn=lambda: now_ref[0], sleep_fn=advance, notifier=notifier)

restarted = False
end = clock.last_cancel_at(DATE).astimezone(timezone.utc) + timedelta(seconds=60)
while now_ref[0] < end:
    runner.tick()
    if not restarted and now_ref[0] >= T + timedelta(seconds=200):
        # simulate the Streamlit app restarting mid-window: a brand-new Runner must rebuild from the database
        restarted = True
        before = store.counts()
        runner = pipeline.Runner(client=fake, now_fn=lambda: now_ref[0], sleep_fn=advance, notifier=notifier)
        runner.tick()
        after = store.counts()
        check("restart: no duplicate orders or fills", before["nolive_orders"] == after["nolive_orders"] and before["nolive_fills"] == after["nolive_fills"], "%s vs %s" % (before, after))
    advance(1)

run = store.get_run(DATE)
check("run exists and is closed", run is not None and run["status"] == "closed", str(run and run["status"]))
check("7 words seen / 2 qualified / 5 skipped", (run["markets_seen"], run["qualified"], run["skipped"]) == (7, 2, 5), str((run["markets_seen"], run["qualified"], run["skipped"])))
check("fired exactly at 5:32:30", run["late_seconds"] is not None and run["late_seconds"] < 1.0, str(run["late_seconds"]))
check("fee type stored from Kalshi", run["fee_type"] == "quadratic" and run["maker_rate"] == 0.0)

mk = dict((x["market_ticker"].split("-")[-1], x) for x in store.markets_for_run(run["id"]))
check("skip reasons recorded: too expensive", mk["NVDA"]["skip_reason"] == "YES 99c > 30c cap" and mk["DRONE"]["skip_reason"] == "YES 70c > 30c cap", str((mk["NVDA"]["skip_reason"], mk["DRONE"]["skip_reason"])))
check("skip reasons recorded: no price / closed", mk["HELI"]["skip_reason"] == "no price" and "not active" in mk["CLOSED"]["skip_reason"])
check("counting word excluded even though its price (20c) would qualify", mk["TRUMP5"]["skip_reason"] == "counting word (excluded)" and mk["TRUMP5"]["is_counting"] in (1, True) and mk["TRUMP5"]["qualified"] in (0, False), str(mk["TRUMP5"]))
check("OIL and ICE qualify", mk["OIL"]["qualified"] in (1, True) and mk["ICE"]["qualified"] in (1, True))
check("pre-fire book read from no-fade depth", mk["OIL"]["pre_yes_bid_cents"] == 18 and mk["OIL"]["pre_no_bid_cents"] == 78)
check("queue ahead recorded (OIL: NO bid 70 >= 70c)", close(mk["OIL"]["queue_ahead"], 1) and close(mk["ICE"]["queue_ahead"], 0), str((mk["OIL"]["queue_ahead"], mk["ICE"]["queue_ahead"])))

orders = store.orders_for_run(run["id"])
check("2 qualifying words x 5 cancel times = 10 paper orders", len(orders) == 10, str(len(orders)))
by = dict(((o["market_ticker"].split("-")[-1], o["variant_id"]), o) for o in orders)
check("order size = $3 / 70c = 4.29, not capped (cap is 50)", all(close(o["contracts"], 4.29) and not o["size_capped"] for o in orders))
check("OIL 5:35 cancels with only the first maker fill (partial)", close(by[("OIL", "c1735")]["filled_contracts"], 4.0) and by[("OIL", "c1735")]["status"] == "partial · cancelled", str(by[("OIL", "c1735")]["status"]))
check("OIL 5:40 gets both maker fills (fully filled)", close(by[("OIL", "c1740")]["filled_contracts"], 4.29) and by[("OIL", "c1740")]["status"] == "filled")
check("ICE gets an instant taker fill at 35c (better than our 30c limit)", close(by[("ICE", "c1735")]["taker_contracts"], 4.0) and by[("ICE", "c1735")]["taker_fee_cents"] == 7, str(by[("ICE", "c1735")]))
check("ICE's remainder fills as maker at our 30c limit, same in every version", all(close(by[("ICE", v["id"])]["maker_contracts"], 0.29) for v in pipeline.C.VARIANTS))
check("no fee on maker fills", all(float(o["maker_fee_cents"] or 0) == 0 for o in orders))

fills = store.fills_for_run(run["id"])
check("fills: OIL 2 (both maker), ICE 2 (1 taker + 1 maker)", len(fills) == 4, str(len(fills)))
trades_n = store.counts()["nolive_trades"]
check("raw tape stored once per trade: OIL 3 + ICE 2 + NVDA 2 + TRUMP5 2 = 9", trades_n == 9, str(trades_n))
with store.engine().begin() as conn:
    pre_tr = conn.execute(text("select count(*) from nolive_trades where market_ticker = :m"), {"m": EVENT + "-NVDA"}).scalar()
    pre_rows = conn.execute(text("select count(*), count(distinct market_ticker) from nolive_depth where kind = 'pre'")).one()
    orphan = conn.execute(text("select count(*) from nolive_depth where run_id is null")).scalar()
check("gap trades saved even for a word we did NOT order (NVDA)", pre_tr == 2, str(pre_tr))
check("gap books: every word (7), about every 30s from 5:28", pre_rows[1] == 7 and 56 <= pre_rows[0] <= 70, str(tuple(pre_rows)))
check("gap books linked to tonight's run after the fire", orphan == 0, str(orphan))
check("gap trades never became fills (they happened before our order)", len([f for f in store.fills_for_run(run["id"]) if f["ref"] in ("trade:%s-OIL#0" % EVENT,)]) == 0)
depth_n = store.counts()["nolive_depth"]
check("book pictures recorded every minute for ALL 7 words (ordered or not)", depth_n > 60, str(depth_n))
with store.engine().begin() as conn:
    poll_words = conn.execute(text("select count(distinct market_ticker) from nolive_depth where kind = 'poll'")).scalar()
    nvda_poll = conn.execute(text("select count(*) from nolive_depth where kind = 'poll' and market_ticker = :m"), {"m": EVENT + "-NVDA"}).scalar()
check("after the fire, books are kept for ALL 7 words (ordered or not)", poll_words == 7, str(poll_words))
check("a skipped word gets about one book a minute until 5:55", 20 <= nvda_poll <= 26, str(nvda_poll))
check("Telegram: fire + close messages", len(notifier.sent) == 2 and "fired" in notifier.sent[0] and "cancelled" in notifier.sent[1], str([x[:30] for x in notifier.sent]))

# ---- settlement at 6:06 PM
now_ref[0] = clock.at(DATE, "18:06").astimezone(timezone.utc)
runner.tick()
run = store.get_run(DATE)
check("run settled", run["status"] == "settled", run["status"])
orders = store.orders_for_run(run["id"])
by = dict(((o["market_ticker"].split("-")[-1], o["variant_id"]), o) for o in orders)
check("OIL 5:35: word said, lose 4.0 x 70c = -280c", close(by[("OIL", "c1735")]["pnl_cents"], -280.0, 0.05), str(by[("OIL", "c1735")]["pnl_cents"]))
check("OIL 5:40: word said, lose 4.29 x 70c = -300.3c", close(by[("OIL", "c1740")]["pnl_cents"], -300.3, 0.05))
check("ICE: not said, taker 4.0@35c - 7c fee = +133c", close(by[("ICE", "c1750")]["taker_pnl_cents"], 133.0, 0.05))
check("ICE: not said, maker 0.29@30c = +8.7c", close(by[("ICE", "c1750")]["maker_pnl_cents"], 8.7, 0.05))
check("ICE total (any version, all fully filled early) = +141.7c", close(by[("ICE", "c1755")]["pnl_cents"], 141.7, 0.05))
srcs = dict((x["market_ticker"].split("-")[-1], x["result_source"]) for x in store.markets_for_run(run["id"]) if x["qualified"])
check("results: no-fade first (OIL), Kalshi for the rest (ICE)", srcs == {"OIL": "nofade", "ICE": "kalshi"}, str(srcs))
check("Kalshi asked only for the 1 word no-fade lacked", fake.calls["market"] == 1, str(fake.calls["market"]))
check("settled Telegram summary sent once", len(notifier.sent) == 3 and "settled" in notifier.sent[2])

rows = analytics.variant_rows(store.all_orders())
r35 = [r for r in rows if r["variant_id"] == "c1735"][0]
r40 = [r for r in rows if r["variant_id"] == "c1740"][0]
check("scoreboard 5:35 net = (-280 + 141.7)/100 = -$1.38", close(r35["net"], -1.383, 0.02), str(r35["net"]))
check("scoreboard 5:40 net = (-300.3 + 141.7)/100 = -$1.59", close(r40["net"], -1.586, 0.02), str(r40["net"]))
check("taker/maker split on scoreboard", close(r40["taker_pnl"], 1.33, 0.02) and close(r40["maker_pnl"], -2.916, 0.02))
check("best by $ = 5:35 (OIL's partial fill there loses less)", analytics.best_of(rows)["by_net"]["variant_id"] == "c1735")
buckets = analytics.bucket_rows(store.all_orders(), "c1740")
check("bucket table sums to the scoreboard", close(sum(b["net $"] for b in buckets), r40["net"], 0.02))
check("OIL (20c) and ICE (25c) land in different 5c-wide buckets", buckets[4]["words"] == 1 and buckets[5]["words"] == 1, str([b["words"] for b in buckets]))

# ---- ticking again does nothing (idempotent), and a fresh start after the fact settles nothing twice
before = store.counts()
now_ref[0] = now_ref[0] + timedelta(hours=1)
runner.last_settle_try = None
runner.tick()
check("second settle pass is a no-op", store.counts() == before and len(notifier.sent) == 3)

print("6) missed window")
DATE2 = "2026-09-21"                      # a Monday
EVENT2 = "KXWORLDNEWSMENTION-26SEP21"
with store.engine().begin() as conn:
    conn.execute(text("insert into days values (:d, :e)"), {"d": DATE2, "e": EVENT2})
now_ref[0] = clock.at(DATE2, "18:40").astimezone(timezone.utc)
n2 = FakeNotifier()
r2 = pipeline.Runner(client=fake, now_fn=lambda: now_ref[0], sleep_fn=advance, notifier=n2)
r2.tick()
run2 = store.get_run(DATE2)
check("late wake-up is recorded as missed, no orders", run2 is not None and run2["status"] == "missed" and store.orders_for_run(run2["id"]) == [])
check("missed alert sent", len(n2.sent) == 1 and "not awake" in n2.sent[0])

# ------------------------------------------------------------------ 7) no event tonight (weekend)
print("7) no event")
DATE3 = "2026-09-26"                      # a Saturday
now_ref[0] = clock.at(DATE3, "17:32:30").astimezone(timezone.utc)
FakeKalshi.get_events = lambda self, series, status="open": []
n3 = FakeNotifier()
r3 = pipeline.Runner(client=fake, now_fn=lambda: now_ref[0], sleep_fn=advance, notifier=n3)
r3.tick()
run3 = store.get_run(DATE3)
check("no event: run recorded as no_event, weekend stays quiet", run3 is not None and run3["status"] == "no_event" and n3.sent == [])

# ------------------------------------------------------------------ 7b) finding tonight's event without no-fade's help
print("7b) event lookup fallback")
check("event ticker date parsing", pipeline.Runner._ticker_date("KXWORLDNEWSMENTION-26SEP18") == "2026-09-18")


class OnlyKalshi:
    def get_events(self, series, status="open"):
        return [{"event_ticker": "KXWORLDNEWSMENTION-26SEP17"}, {"event_ticker": "KXWORLDNEWSMENTION-26SEP30"}]


r4 = pipeline.Runner(client=OnlyKalshi(), now_fn=lambda: now_ref[0], sleep_fn=advance, notifier=FakeNotifier())
check("Kalshi fallback picks the event whose date is today", r4._find_event_ticker("2026-09-30") == "KXWORLDNEWSMENTION-26SEP30")
check("Kalshi fallback returns None when today has no event", r4._find_event_ticker("2026-10-01") is None)

# ------------------------------------------------------------------ 8) paused
print("8) pause")
r3.set_paused(True)
check("pause flag stored", store.get_state("paused") is True and r3.paused())
r3.set_paused(False)

print()
print("%d checks, %d failed" % (CHECKS[0], len(FAILS)))
if FAILS:
    print("FAILED:", ", ".join(FAILS))
    sys.exit(1)
print("ALL GOOD")
if not KEEP:
    try:
        os.remove(DB)
    except OSError:
        pass
