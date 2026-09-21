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
print("2) qualify")
check("YES 70c qualifies", engine.qualify(70, 65, 72, "active", 98)["qualified"])
check("YES 97c qualifies", engine.qualify(97, 96, 98, "active", 98)["qualified"])
check("YES 1c qualifies", engine.qualify(1, 1, 2, "active", 98)["qualified"])
check("YES 98c does not", not engine.qualify(98, 97, 99, "active", 98)["qualified"])
check("YES 99c does not", not engine.qualify(99, 98, 100, "active", 98)["qualified"])
check("bid 98+ disqualifies even if last is stale", not engine.qualify(60, 98, 99, "active", 98)["qualified"])
check("no trade -> falls back to bid", engine.qualify(None, 40, 50, "active", 98)["basis"] == "bid")
check("no price at all -> skip", engine.qualify(None, None, None, "active", 98)["reason"] == "no price")
check("closed market -> skip", not engine.qualify(50, 49, 51, "closed", 98)["qualified"])
check("half-cent 97.5 qualifies", engine.qualify(97.5, 97, 98, "active", 98)["qualified"])

# ------------------------------------------------------------------ 3) engine: taker / maker / money
print("3) engine")
check("contracts $5 @ sell YES 55c = 11.11", engine.contracts_for(5, 55) == 11.11)
book = {"yes": [(20, 5), (55, 4), (60, 3), (75, 100)], "no": [(20, 7), (45, 2), (50, 1)]}
s = engine.book_summary(book, 55)
check("size at/above limit", s["yes_size_at_limit"] == 107)
check("queue ahead = NO bids >= 45", s["queue_ahead"] == 3)
tf = engine.taker_fills(book, 55, 11.11)
check("taker: best bid first, one level", len(tf) == 1 and tf[0]["price_cents"] == 75 and tf[0]["contracts"] == 11.11)
check("taker fee on 11.11 @75c = 15c", tf[0]["fee_cents"] == 15)
tf2 = engine.taker_fills({"yes": [(96, 4), (95, 10)]}, 55, 11.11)
check("taker: walks two levels", [f["price_cents"] for f in tf2] == [96, 95] and close(sum(f["contracts"] for f in tf2), 11.11, 1e-6))
check("taker: nothing below limit", engine.taker_fills({"yes": [(50, 100)]}, 55, 5) == [])
tr = [
    {"id": "a", "ts": T + timedelta(seconds=5), "yes_cents": 55.0, "count": 10, "taker_side": "yes"},
    {"id": "b", "ts": T + timedelta(seconds=30), "yes_cents": 60.0, "count": 4, "taker_side": "no"},
    {"id": "c", "ts": T + timedelta(seconds=60), "yes_cents": 56.0, "count": 3, "taker_side": "yes"},
    {"id": "d", "ts": T + timedelta(seconds=400), "yes_cents": 99.0, "count": 200, "taker_side": "yes"},
    {"id": "e", "ts": T - timedelta(seconds=5), "yes_cents": 99.0, "count": 200, "taker_side": "yes"},
    {"id": "f", "ts": T + timedelta(seconds=5000), "yes_cents": 99.0, "count": 200, "taker_side": "yes"},
]
mf = engine.maker_fills(tr, T, T + timedelta(seconds=1400), 55, 8.0)
check("maker: strictly-above only, buyer must be the taker, inside the window",
      [f["ref"] for f in mf] == ["trade:c", "trade:d"] and [f["contracts"] for f in mf] == [3.0, 5.0], str(mf))
fills = [
    {"kind": "taker", "ts": T, "contracts": 4.0, "price_cents": 96.0, "fee_cents": 2},
    {"kind": "maker", "ts": T + timedelta(seconds=100), "contracts": 3.0, "price_cents": 55.0, "fee_cents": 0},
    {"kind": "maker", "ts": T + timedelta(seconds=500), "contracts": 2.0, "price_cents": 55.0, "fee_cents": 0},
]
ag = engine.aggregate(fills, T + timedelta(seconds=150))
check("aggregate respects the cancel time", ag["filled_contracts"] == 7.0 and ag["taker_contracts"] == 4.0 and ag["maker_contracts"] == 3.0)
check("risk = n x (100 - price)", close(ag["risk_cents"], 4 * 4 + 3 * 45))
sm_no = engine.settle(fills, T + timedelta(seconds=150), "no")
check("word NOT said: keep the price, fees off", close(sm_no["pnl_cents"], 4 * 96 - 2 + 3 * 55))
sm_yes = engine.settle(fills, T + timedelta(seconds=150), "yes")
check("word said: lose 100 - price, fees off", close(sm_yes["pnl_cents"], -4 * 4 - 2 - 3 * 45))
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
print("5) whole night, fake Kalshi")
if PG:      # start clean: drop our tables and the fake no-fade copies
    with store.engine().begin() as conn:
        for t in ("nolive_depth", "nolive_trades", "nolive_fills", "nolive_orders", "nolive_markets", "nolive_runs",
                  "nolive_activity", "nolive_state", "days", "orders", "depth"):
            conn.execute(text("drop table if exists %s cascade" % t))
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
    conn.execute(text("insert into orders (event_date, market_ticker, result) values (:d, :m, :r)"), {"d": DATE, "m": EVENT + "-DRONE", "r": "no"})
    conn.execute(text("insert into depth (ts, event_date, market_ticker, best_yes_bid, best_no_bid) values (:t, :d, :m, 22, 75)"),
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
    m("OIL", "Oil / Gas", 20, 18, 22),
    m("DRONE", "Drone", 70, 65, 72),
    m("NVDA", "Nvidia", 99, 98, 100),
    m("TRUMP5", "Trump (5+ times)", 40, 38, 42),
    m("HELI", "Helicopter", None, None, None),
    m("CLOSED", "Closed", 50, 49, 51, status="closed"),
    m("GOLD", "Gold", 96, 95, 97),
]
BOOKS = {
    "OIL": {"yes": [(10, 50)], "no": [(70, 30)]},
    "DRONE": {"yes": [(60, 3), (75, 100)], "no": []},
    "TRUMP5": {"yes": [(30, 5)], "no": [(60, 5)]},
    "GOLD": {"yes": [(95, 10), (96, 4)], "no": []},
}
TAPE = {
    "OIL": [(-200, 40.0, 5, "yes"), (60, 60.0, 4, "yes"), (400, 99.0, 200, "yes")],
    "NVDA": [(-100, 99.0, 3, "yes"), (100, 99.0, 4, "no")],
    "DRONE": [(50, 30.0, 5, "yes")],
    "TRUMP5": [(-30, 45.0, 2, "no"), (30, 58.0, 3, "yes")],
    "GOLD": [],
}
RESULTS = {"TRUMP5": "no", "GOLD": "yes"}       # OIL / DRONE come from the no-fade tables


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
check("7 words seen / 4 qualified / 3 skipped", (run["markets_seen"], run["qualified"], run["skipped"]) == (7, 4, 3), str((run["markets_seen"], run["qualified"], run["skipped"])))
check("fired exactly at 5:32:30", run["late_seconds"] is not None and run["late_seconds"] < 1.0, str(run["late_seconds"]))
check("fee type stored from Kalshi", run["fee_type"] == "quadratic" and run["maker_rate"] == 0.0)

mk = dict((x["market_ticker"].split("-")[-1], x) for x in store.markets_for_run(run["id"]))
check("skip reasons recorded", mk["NVDA"]["skip_reason"].startswith("YES 99") and mk["HELI"]["skip_reason"] == "no price" and "not active" in mk["CLOSED"]["skip_reason"])
check("counting word flagged, still traded", mk["TRUMP5"]["is_counting"] in (1, True) and mk["TRUMP5"]["qualified"] in (1, True))
check("pre-fire book read from no-fade depth", mk["OIL"]["pre_yes_bid_cents"] == 22 and mk["OIL"]["pre_no_bid_cents"] == 75)
check("queue ahead recorded (TRUMP5: NO bid 60 >= 45)", close(mk["TRUMP5"]["queue_ahead"], 5))

orders = store.orders_for_run(run["id"])
check("4 words x 5 cancel times = 20 paper orders", len(orders) == 20, str(len(orders)))
by = dict(((o["market_ticker"].split("-")[-1], o["variant_id"]), o) for o in orders)
check("order size = $5 / 45c = 11.11", all(close(o["contracts"], 11.11) for o in orders))
check("OIL 5:35 cancels with only the first maker fill", close(by[("OIL", "c1735")]["filled_contracts"], 4.0) and by[("OIL", "c1735")]["status"] == "partial · cancelled", str(by[("OIL", "c1735")]["status"]))
check("OIL 5:40 gets both maker fills (all 11.11)", close(by[("OIL", "c1740")]["filled_contracts"], 11.11) and by[("OIL", "c1740")]["status"] == "filled")
check("DRONE instant taker fill at 75c", close(by[("DRONE", "c1735")]["taker_contracts"], 11.11) and close(by[("DRONE", "c1735")]["maker_contracts"], 0) and by[("DRONE", "c1735")]["taker_fee_cents"] == 15)
check("TRUMP5 rests, maker only, 3 contracts", close(by[("TRUMP5", "c1755")]["maker_contracts"], 3.0) and by[("TRUMP5", "c1755")]["taker_fee_cents"] == 0 and by[("TRUMP5", "c1755")]["status"] == "partial · cancelled")
check("GOLD: two taker levels, 96c then 95c, fees 2c + 3c", close(by[("GOLD", "c1735")]["taker_fee_cents"], 5) and close(by[("GOLD", "c1735")]["taker_contracts"], 11.11))
check("no fee on maker fills", all(float(o["maker_fee_cents"] or 0) == 0 for o in orders))

fills = store.fills_for_run(run["id"])
check("fills: OIL 2, DRONE 1, TRUMP5 1, GOLD 2", len(fills) == 6, str(len(fills)))
trades_n = store.counts()["nolive_trades"]
check("raw tape stored once per trade: 4 after the fire + 3 in the gap + 1 after the fire on a skipped word", trades_n == 8, str(trades_n))
with store.engine().begin() as conn:
    pre_tr = conn.execute(text("select count(*) from nolive_trades where market_ticker = :m"), {"m": EVENT + "-NVDA"}).scalar()
    pre_rows = conn.execute(text("select count(*), count(distinct market_ticker) from nolive_depth where kind = 'pre'")).one()
    orphan = conn.execute(text("select count(*) from nolive_depth where run_id is null")).scalar()
check("NVDA (not ordered): its gap trade AND its post-fire trade are both saved", pre_tr == 2, str(pre_tr))
with store.engine().begin() as conn:
    poll_words = conn.execute(text("select count(distinct market_ticker) from nolive_depth where kind = 'poll'")).scalar()
    nvda_poll = conn.execute(text("select count(*) from nolive_depth where kind = 'poll' and market_ticker = :m"), {"m": EVENT + "-NVDA"}).scalar()
check("after the fire, books are kept for ALL 7 words (ordered or not)", poll_words == 7, str(poll_words))
check("a skipped word gets about one book a minute until 5:55", 20 <= nvda_poll <= 26, str(nvda_poll))
check("gap books: every word (7), about every 30s from 5:28", pre_rows[1] == 7 and 56 <= pre_rows[0] <= 70, str(tuple(pre_rows)))
check("gap books linked to tonight's run after the fire", orphan == 0, str(orphan))
check("gap trades never became fills (they happened before our order)", len([f for f in store.fills_for_run(run["id"]) if f["ref"] in ("trade:%s-OIL#0" % EVENT,)]) == 0)
depth_n = store.counts()["nolive_depth"]
check("book pictures recorded every minute (4 words x ~24)", depth_n > 60, str(depth_n))
check("Telegram: fire + close messages", len(notifier.sent) == 2 and "fired" in notifier.sent[0] and "cancelled" in notifier.sent[1], str([x[:30] for x in notifier.sent]))

# ---- settlement at 6:06 PM
now_ref[0] = clock.at(DATE, "18:06").astimezone(timezone.utc)
runner.tick()
run = store.get_run(DATE)
check("run settled", run["status"] == "settled", run["status"])
orders = store.orders_for_run(run["id"])
by = dict(((o["market_ticker"].split("-")[-1], o["variant_id"]), o) for o in orders)
check("OIL 5:35: word said, lose 4 x 45c = -180c", close(by[("OIL", "c1735")]["pnl_cents"], -180.0, 0.05), str(by[("OIL", "c1735")]["pnl_cents"]))
check("OIL 5:40: word said, lose 11.11 x 45c = -499.95c", close(by[("OIL", "c1740")]["pnl_cents"], -499.95, 0.05))
check("DRONE: not said, +11.11 x 75c - 15c = +818.25c (taker)", close(by[("DRONE", "c1750")]["pnl_cents"], 818.25, 0.05) and close(by[("DRONE", "c1750")]["taker_pnl_cents"], 818.25, 0.05))
check("TRUMP5: not said, +3 x 55c = +165c (maker)", close(by[("TRUMP5", "c1745")]["pnl_cents"], 165.0, 0.05) and close(by[("TRUMP5", "c1745")]["maker_pnl_cents"], 165.0, 0.05))
check("GOLD: said, -(4x4 + 7.11x5) - 5c fees = -56.55c", close(by[("GOLD", "c1740")]["pnl_cents"], -56.55, 0.05), str(by[("GOLD", "c1740")]["pnl_cents"]))
srcs = dict((x["market_ticker"].split("-")[-1], x["result_source"]) for x in store.markets_for_run(run["id"]) if x["qualified"])
check("results: no-fade first, Kalshi for the rest", srcs == {"OIL": "nofade", "DRONE": "nofade", "TRUMP5": "kalshi", "GOLD": "kalshi"}, str(srcs))
check("Kalshi asked only for the 2 words no-fade lacked", fake.calls["market"] == 2, str(fake.calls["market"]))
check("settled Telegram summary sent once", len(notifier.sent) == 3 and "settled" in notifier.sent[2])

rows = analytics.variant_rows(store.all_orders())
r35 = [r for r in rows if r["variant_id"] == "c1735"][0]
r40 = [r for r in rows if r["variant_id"] == "c1740"][0]
check("scoreboard 5:35 net = (-180 + 818.25 + 165 - 56.55)/100 = $7.47", close(r35["net"], 7.4670, 0.01), str(r35["net"]))
check("scoreboard 5:40 net = (-499.95 + 818.25 + 165 - 56.55)/100 = $4.27", close(r40["net"], 4.2675, 0.01), str(r40["net"]))
check("taker/maker split on scoreboard", close(r40["taker_pnl"], (818.25 - 56.55) / 100.0, 0.01) and close(r40["maker_pnl"], (-499.95 + 165.0) / 100.0, 0.01))
check("best by $ = 5:35 (only OIL differs)", analytics.best_of(rows)["by_net"]["variant_id"] == "c1735")
buckets = analytics.bucket_rows(store.all_orders(), "c1740")
check("bucket table sums to the scoreboard", close(sum(b["net $"] for b in buckets), r40["net"], 0.01))

# ---- ticking again does nothing (idempotent), and a fresh start after the fact settles nothing twice
before = store.counts()
now_ref[0] = now_ref[0] + timedelta(hours=1)
runner.last_settle_try = None
runner.tick()
check("second settle pass is a no-op", store.counts() == before and len(notifier.sent) == 3)

# ------------------------------------------------------------------ 6) app asleep through the window -> 'missed', never a late fire
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
