#!/usr/bin/env python3
"""Offline self-test for the LIVE (real-money) engine. No network, no Supabase, no Telegram, no real
Kalshi key -- a fake signed client stands in for Kalshi's order endpoints.

This is the test that matters most: it exists to prove, before any real dollar is risked, that the
one rule the whole live design hangs on actually holds -- an order is NEVER sent twice for the same
(day, ticker, price, size), under a fresh run, a restart, or a retried tick.

    python3 scripts/offline_live_test.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("DATABASE_URL", None)
os.environ["SQLITE_PATH"] = tempfile.mktemp(suffix=".db")

from nolive import clock, config as C, live, store  # noqa: E402
from nolive.kalshi import KalshiError  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append(name)
        print("  FAIL  %s  %s" % (name, detail))


store.init_db()

DATE = "2026-09-26"
EVENT = "KXWORLDNEWSMENTION-26SEP26"


def m(tk, word, last, status="active"):
    return {"ticker": EVENT + "-" + tk, "title": word, "yes_sub_title": word, "status": status,
            "last_price_dollars": "%.4f" % (last / 100.0) if last is not None else None}


MARKETS = [
    m("OIL", "Oil / Gas", 20),                  # qualifies (<=30c)
    m("ICE", "ICE", 25),                        # qualifies (<=30c)
    m("NVDA", "Nvidia", 99),                    # too expensive, excluded
    m("TRUMP5", "Trump (5+ times)", 10),        # cheap but a counting word: excluded
    m("CLOSED", "Closed", 20, status="finalized"),  # not active: excluded
]


class FakePublic:
    """Stands in for KalshiPublic: only reads, never places or cancels anything."""
    def __init__(self):
        self.calls = {"events": 0, "markets": 0, "book": 0}

    def get_events(self, series, status="open"):
        self.calls["events"] += 1
        return [{"event_ticker": EVENT}]

    def get_markets(self, event_ticker):
        self.calls["markets"] += 1
        return [dict(x) for x in MARKETS]

    def get_orderbook(self, ticker, depth=10):
        self.calls["book"] += 1
        return {"yes": [], "no": []}   # empty book: take-if-cheap never fires in this test


class FakeSignedClient:
    """Stands in for the real, signed KalshiClient. Records every order it is ever asked to place
    or cancel so the test can assert none of them repeat."""
    def __init__(self):
        self.orders_sent = []          # list of (client_order_id, ticker, no_price_cents, count)
        self.cancelled = []
        self.resting = []
        self.balance_cents = 100000    # $1000
        self.next_order_id = 1
        self.reject_next = False
        self.authenticated = True

    def get_balance(self):
        return {"balance": self.balance_cents}

    def create_no_order(self, ticker, no_price_cents, count, client_order_id, post_only=True,
                        expiration_epoch=None):
        if self.reject_next:
            self.reject_next = False
            raise KalshiError(400, "insufficient balance", "/portfolio/events/orders")
        for oid, tk, px, ct in self.orders_sent:
            if oid == client_order_id:
                # Real Kalshi would refuse this as a duplicate. The test client raises the same
                # way, so a bug that calls create_no_order twice for the same order is caught here
                # even if the store-level guard in live.py is bypassed.
                raise KalshiError(400, "order already exists for client_order_id", "/portfolio/events/orders")
        order_id = "order-%d" % self.next_order_id
        self.next_order_id += 1
        self.orders_sent.append((client_order_id, ticker, no_price_cents, count))
        self.resting.append({"ticker": ticker, "order_id": order_id})
        return {"order_id": order_id, "client_order_id": client_order_id, "fill_count": 0,
                "remaining_count": count, "avg_fill_price_cents": None, "fee_cents": None, "raw": {}}

    def get_resting_orders(self, series_prefix=None):
        return list(self.resting)

    def batch_cancel(self, order_ids):
        ok = 0
        for oid in order_ids:
            self.cancelled.append(oid)
            self.resting = [o for o in self.resting if o["order_id"] != oid]
            ok += 1
        return ok, []

    def get_fills(self, ticker=None, limit=200):
        return []


print("1) dry_run: no real orders are ever sent")
C.LIVE_DRY_RUN = True
C.LIVE_SMOKE = False
C.LIVE_DOLLARS_PER_WORD = 5.0
C.LIVE_CANCEL_CT = "17:55"
pub = FakePublic()
signed = FakeSignedClient()
runner = live.LiveRunner(client=signed, public=pub)
runner.place_all(EVENT, DATE)
check("dry_run sends nothing real", len(signed.orders_sent) == 0, signed.orders_sent)
rows = store.live_orders_for_day(DATE)
qualifying = [r for r in rows if not live._is_smoke_row(r)]
check("dry_run recorded exactly the 2 qualifying, non-counting, active words",
      len(qualifying) == 2, [r["market_ticker"] for r in qualifying])
check("dry_run rows are marked dry_run", all(r["status"] == "dry_run" for r in qualifying), qualifying)
check("counting word (Trump 5+) never got a row", not any("TRUMP5" in r["market_ticker"] for r in rows))
check("closed market never got a row", not any("CLOSED" in r["market_ticker"] for r in rows))
check("too-expensive market (Nvidia) never got a row", not any("NVDA" in r["market_ticker"] for r in rows))

print("2) idempotency: firing the exact same night again places nothing new")
rows_before = len(store.live_orders_for_day(DATE))
runner2 = live.LiveRunner(client=signed, public=pub)   # a fresh Runner instance, as after a restart
runner2.place_all(EVENT, DATE)   # claim_live_run should refuse: this event_date is already claimed
rows_after = len(store.live_orders_for_day(DATE))
check("second place_all call for the same night adds no rows", rows_before == rows_after,
      (rows_before, rows_after))
check("second place_all call sent nothing real", len(signed.orders_sent) == 0)

print("3) client_order_id is deterministic across independent Runner instances")
coid_a = live.client_order_id(DATE, EVENT + "-OIL")
coid_b = live.client_order_id(DATE, EVENT + "-OIL")
check("same (date, ticker, price, size) -> same client_order_id", coid_a == coid_b)
C.LIVE_DOLLARS_PER_WORD = 6.0
coid_c = live.client_order_id(DATE, EVENT + "-OIL")
check("a different $/word -> a different client_order_id (no silent reuse across a config change)",
      coid_a != coid_c)
C.LIVE_DOLLARS_PER_WORD = 5.0

print("4) smoke mode: exactly one tiny REAL order per qualifying word, alongside the dry_run row")
DATE2 = "2026-09-27"
EVENT2 = EVENT.replace("26SEP26", "26SEP27")
MARKETS2 = [m("OIL", "Oil / Gas", 20), m("ICE", "ICE", 25)]
MARKETS[:] = MARKETS2   # FakePublic.get_markets reads MARKETS by closure... use a fresh instance instead


class FakePublic2(FakePublic):
    def get_events(self, series, status="open"):
        return [{"event_ticker": EVENT2}]

    def get_markets(self, event_ticker):
        return [dict(x) for x in MARKETS2]


C.LIVE_SMOKE = True
C.LIVE_SMOKE_CONTRACTS = 1
pub2 = FakePublic2()
signed2 = FakeSignedClient()
runner3 = live.LiveRunner(client=signed2, public=pub2)
runner3.place_all(EVENT2, DATE2)
check("smoke sends exactly 2 real orders (one per qualifying word)", len(signed2.orders_sent) == 2,
      signed2.orders_sent)
check("smoke orders are 1 contract each", all(ct == 1 for _, _, _, ct in signed2.orders_sent),
      signed2.orders_sent)
rows2 = store.live_orders_for_day(DATE2)
smoke_rows = [r for r in rows2 if live._is_smoke_row(r)]
check("both smoke rows recorded in the DB", len(smoke_rows) == 2, smoke_rows)
check("smoke rows are tagged mode=smoke, dry_run=False",
      all(r["mode"] == "smoke" and not r["dry_run"] for r in smoke_rows), smoke_rows)
# firing again must not re-send the smoke orders either
runner3b = live.LiveRunner(client=signed2, public=pub2)
runner3b.place_all(EVENT2, DATE2)
check("re-firing the same night sends no duplicate smoke orders", len(signed2.orders_sent) == 2)
C.LIVE_SMOKE = False

print("5) live mode: real orders sized at $LIVE_DOLLARS_PER_WORD, post_only, correct NO price")
DATE3 = "2026-09-28"
EVENT3 = EVENT.replace("26SEP26", "26SEP28")
MARKETS3 = [m("OIL", "Oil / Gas", 20), m("ICE", "ICE", 25)]


class FakePublic3(FakePublic):
    def get_events(self, series, status="open"):
        return [{"event_ticker": EVENT3}]

    def get_markets(self, event_ticker):
        return [dict(x) for x in MARKETS3]


C.LIVE_DRY_RUN = False
pub3 = FakePublic3()
signed3 = FakeSignedClient()
runner4 = live.LiveRunner(client=signed3, public=pub3)
runner4.place_all(EVENT3, DATE3)
check("live mode sent exactly 2 real orders", len(signed3.orders_sent) == 2, signed3.orders_sent)
expected_no_price = 100 - C.LIMIT_YES_CENTS   # 70c: sell YES 30c = buy NO 70c
check("live order NO price matches the (already-validated) v2 rule",
      all(px == expected_no_price for _, _, px, _ in signed3.orders_sent), signed3.orders_sent)
sized = C.live_sized_contracts()["contracts"]
check("live order size matches $%.2f/word" % C.LIVE_DOLLARS_PER_WORD,
      all(abs(ct - sized) < 1e-6 for _, _, _, ct in signed3.orders_sent), signed3.orders_sent)
rows3 = store.live_orders_for_day(DATE3)
check("live rows tagged mode=live, dry_run=False, status=resting",
      all(r["mode"] == "live" and not r["dry_run"] and r["status"] == "resting" for r in rows3), rows3)
check("post_only is on (book was empty, so take-if-cheap never overrides it)",
      all(r["post_only"] for r in rows3), rows3)

print("6) a real rejection from Kalshi is recorded and never silently retried as a new order")
DATE4 = "2026-09-29"
EVENT4 = EVENT.replace("26SEP26", "26SEP29")
MARKETS4 = [m("OIL", "Oil / Gas", 20)]


class FakePublic4(FakePublic):
    def get_events(self, series, status="open"):
        return [{"event_ticker": EVENT4}]

    def get_markets(self, event_ticker):
        return [dict(x) for x in MARKETS4]


pub4 = FakePublic4()
signed4 = FakeSignedClient()
signed4.reject_next = True
runner5 = live.LiveRunner(client=signed4, public=pub4)
runner5.place_all(EVENT4, DATE4)
rows4 = store.live_orders_for_day(DATE4)
check("rejected order recorded with status=rejected and a reason",
      len(rows4) == 1 and rows4[0]["status"] == "rejected" and rows4[0].get("reject_reason"), rows4)
runner5b = live.LiveRunner(client=signed4, public=pub4)
runner5b.place_all(EVENT4, DATE4)   # same day: claim_live_run refuses a second run anyway
check("re-firing after a rejection sends no new real order", len(signed4.orders_sent) == 0)

print("7) cancel_all: dry_run never calls the real cancel endpoint; live mode does and verifies")
n = runner4.cancel_all(DATE3, reason="test")
check("live cancel_all called batch_cancel for both resting orders", len(signed3.cancelled) == 2,
      signed3.cancelled)
check("live cancel_all verified nothing left resting", n["verified"] is True, n)
check("cancelled orders are marked cancelled in the DB",
      all(r["status"] == "cancelled" for r in store.live_orders_for_day(DATE3)))

C.LIVE_DRY_RUN = True
signed_dry = FakeSignedClient()
runner_dry_cancel = live.LiveRunner(client=signed_dry, public=pub)
n2 = runner_dry_cancel.cancel_all(DATE, reason="test dry")
check("dry_run cancel_all never touches the real (signed) client",
      len(signed_dry.cancelled) == 0 and len(signed_dry.orders_sent) == 0)

print("8) safety caps: LIVE_MAX_MARKETS_PER_DAY trims the order list before anything is sent")
C.LIVE_DRY_RUN = False   # step 7 left this on True to test the dry_run cancel path; back to live for this check
DATE5 = "2026-09-30"
EVENT5 = EVENT.replace("26SEP26", "26SEP30")
MARKETS5 = [m("W%d" % i, "Word %d" % i, 20) for i in range(10)]


class FakePublic5(FakePublic):
    def get_events(self, series, status="open"):
        return [{"event_ticker": EVENT5}]

    def get_markets(self, event_ticker):
        return [dict(x) for x in MARKETS5]


old_cap = C.LIVE_MAX_MARKETS_PER_DAY
C.LIVE_MAX_MARKETS_PER_DAY = 3
pub5 = FakePublic5()
signed5 = FakeSignedClient()
runner6 = live.LiveRunner(client=signed5, public=pub5)
runner6.place_all(EVENT5, DATE5)
check("LIVE_MAX_MARKETS_PER_DAY=3 caps real orders sent to 3, not 10",
      len(signed5.orders_sent) == 3, signed5.orders_sent)
C.LIVE_MAX_MARKETS_PER_DAY = old_cap

print("9) safe defaults: a fresh config import defaults to DRY_RUN=true, SMOKE=false")
check("LIVE_DRY_RUN defaults to True in config.py (no env/secret set)",
      "LIVE_DRY_RUN = _flag(\"LIVE_DRY_RUN\", True)" in open(os.path.join(ROOT, "nolive", "config.py")).read())
check("LIVE_SMOKE defaults to False in config.py",
      "LIVE_SMOKE = _flag(\"LIVE_SMOKE\", False)" in open(os.path.join(ROOT, "nolive", "config.py")).read())

print("10) the in-app backup cancel fires AFTER Kalshi's own server-side expiry, never at/before it")
d = "2026-10-05"
gap = (clock.live_app_cancel_at(d) - clock.live_cancel_at(d)).total_seconds()
check("live_app_cancel_at is strictly after live_cancel_at", gap > 0, gap)
check("the gap matches LIVE_APP_CANCEL_BUFFER_SECONDS", gap == C.LIVE_APP_CANCEL_BUFFER_SECONDS, gap)

print("11) ticker-based guard: a differently-ID'd row for the same ticker still blocks a new order")
C.LIVE_DRY_RUN = False
DATE6 = "2026-10-10"
EVENT6 = EVENT.replace("26SEP26", "10OCT26")
market6 = m("OIL", "Oil / Gas", 20)
market6["ticker"] = EVENT6 + "-OIL"
signed6 = FakeSignedClient()
pub6 = FakePublic()
runner7 = live.LiveRunner(client=signed6, public=pub6)
# Simulate an order row already on file for this exact ticker, but under a DIFFERENT client_order_id
# than the one client_order_id(DATE6, ticker) would compute right now -- e.g. as if the ID scheme or
# a config value changed mid-day. The fast ID-based check alone would miss this.
store.record_live_order(
    client_order_id="some-other-coid-for-the-same-ticker", event_date=DATE6, event_ticker=EVENT6,
    market_ticker=market6["ticker"], word="Oil / Gas", no_price_cents=80, yes_price_cents=20,
    contracts=5, dollars=5.0, collateral=4.0, placed_at=datetime.now(timezone.utc),
    mode="live", dry_run=False, post_only=True, took_at_open=False,
    expiration_epoch=None, status="resting",
)
outcome, label = runner7._place_one(market6, "Oil / Gas", EVENT6, DATE6, None)
check("a same-ticker row under a different client_order_id is treated as already placed",
      outcome == "exists", (outcome, label))
check("no real order was sent to Kalshi for the already-covered ticker",
      len(signed6.orders_sent) == 0, signed6.orders_sent)

print()
print("%d checks, %d failed" % (CHECKS[0], len(FAILS)))
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL GOOD")
