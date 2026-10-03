#!/usr/bin/env python3
"""Offline test: the live engine runs TWO days in a row in ONE process (no restart in between).

Why it exists: on Oct 2, 2026 six unfilled live orders stayed 'resting' in the ledger overnight.
The day's "already cancelled" flag was only cleared after a restart, so on the second day the
in-app backup cancel never ran. Kalshi's own expiry still removed the orders (no money at risk),
but the bot's safety check and its ledger were silent. A server worker never restarts, so there it
would have happened every day.

No network, no Supabase, no Telegram, no real Kalshi key.

    python3 scripts/offline_two_days_test.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.pop("DATABASE_URL", None)
os.environ["SQLITE_PATH"] = tempfile.mktemp(suffix=".db")

from nolive import clock, config as C, lease, live, nofade, notify, store  # noqa: E402

FAILS = []
CHECKS = [0]


def check(name, cond, detail=""):
    CHECKS[0] += 1
    if not cond:
        FAILS.append(name)
        print("  FAIL  %s  %s" % (name, detail))


store.init_db()

SENT = []
notify.send = lambda text, **k: SENT.append(text)
live.time.sleep = lambda s: None
nofade.event_ticker = lambda d: None            # no no-fade table here: the live engine asks "Kalshi"
lease.running = lambda: True

NOW = [None]
clock.now_utc = lambda: NOW[0].astimezone(timezone.utc)
clock.now_ct = lambda: NOW[0]
clock.today_ct = lambda: NOW[0].strftime("%Y-%m-%d")


def at(day: str, hhmmss: str) -> None:
    NOW[0] = clock.at(day, hhmmss)


def event_for(day: str) -> str:
    return "KXWORLDNEWSMENTION-" + datetime.strptime(day, "%Y-%m-%d").strftime("%y%b%d").upper()


class Public:
    def get_events(self, series, status="open"):
        return [{"event_ticker": event_for(clock.today_ct())}]

    def get_markets(self, event_ticker):
        return [{"ticker": event_ticker + "-" + tk, "title": w, "yes_sub_title": w, "status": "active",
                 "last_price_dollars": "0.2000"} for tk, w in (("OIL", "Oil / Gas"), ("ICE", "ICE"), ("SNAP", "SNAP"))]

    def get_orderbook(self, ticker, depth=10):
        return {"orderbook": {"yes": [], "no": []}}


class Signed:
    """Fake signed Kalshi client. Orders rest until cancelled; `expire()` plays Kalshi's own expiry."""
    def __init__(self):
        self.sent, self.cancelled, self.resting, self.fills = [], [], [], []
        self.n = 0
        self.authenticated = True
        self.down = False
        self.fill_reads = 0

    def get_balance(self):
        return {"balance": 100000}

    def create_no_order(self, ticker, no_price_cents, count, client_order_id, post_only=True, expiration_epoch=None):
        self.n += 1
        oid = "order-%d" % self.n
        self.sent.append((client_order_id, ticker, oid))
        self.resting.append({"ticker": ticker, "order_id": oid})
        return {"order_id": oid, "client_order_id": client_order_id, "fill_count": 0, "remaining_count": count,
                "avg_fill_price_cents": None, "fee_cents": None, "raw": {}}

    def get_resting_orders(self, series_prefix=None):
        if self.down:
            raise RuntimeError("kalshi is down")
        return list(self.resting)

    def batch_cancel(self, order_ids):
        for oid in order_ids:
            self.cancelled.append(oid)
            self.resting = [o for o in self.resting if o["order_id"] != oid]
        return len(order_ids), []

    def get_fills(self, ticker=None, limit=200):
        if self.down:
            raise RuntimeError("kalshi is down")
        self.fill_reads += 1
        return list(self.fills)

    def expire(self):
        self.resting = []


C.LIVE_DRY_RUN = False
C.LIVE_SMOKE = False
C.LIVE_DOLLARS_PER_WORD = 10.0
C.LIVE_CANCEL_CT = "17:45"
C.LIVE_APP_CANCEL_BUFFER_SECONDS = 60
C.LIVE_USE_SERVER_SIDE_EXPIRY = True

signed, pub = Signed(), Public()
runner = live.LiveRunner(client=signed, public=pub)
D1, D2, D3 = "2026-11-02", "2026-11-03", "2026-11-04"   # Mon, Tue, Wed


def statuses(day: str) -> list:
    return sorted(r["status"] for r in store.live_orders_for_day(day))


def cancel_messages() -> int:
    return len([t for t in SENT if "All orders cancelled" in t])


print("1) day one: fire, poll, cancel")
at(D1, "17:32:31")
runner._tick()
check("day 1: three real orders went out", len(signed.sent) == 3, signed.sent)
check("day 1: rows are resting", statuses(D1) == ["resting"] * 3, statuses(D1))
at(D1, "17:40:00")
runner._tick()
at(D1, "17:46:05")
runner._tick()
check("day 1: the in-app backup cancel ran", len(signed.cancelled) == 3, signed.cancelled)
check("day 1: rows are marked cancelled", statuses(D1) == ["cancelled"] * 3, statuses(D1))
check("day 1: one cancel message", cancel_messages() == 1, SENT[-1:])
run1 = store.get_live_run(D1)
check("day 1: the run row has cancelled_at", bool(run1 and run1.get("cancelled_at")))

print("2) day two, SAME process: one order fills, the others are cancelled by the in-app backup")
at(D1, "23:30:00")
runner._tick()
at(D2, "09:00:00")
runner._tick()
at(D2, "17:32:31")
runner._tick()
check("day 2: three more real orders went out", len(signed.sent) == 6, len(signed.sent))
rows2 = store.live_orders_for_day(D2)
first = rows2[0]
signed.fills = [{"ticker": first["market_ticker"], "order_id": first["order_id"], "count": first["contracts"],
                 "no_price": 70, "trade_id": "t-1", "created_time": clock.now_utc().isoformat(), "is_taker": False}]
signed.resting = [o for o in signed.resting if o["order_id"] != first["order_id"]]
at(D2, "17:41:00")
runner._tick()
check("day 2: the fill was recorded", statuses(D2) == ["filled", "resting", "resting"], statuses(D2))
before = cancel_messages()
second = rows2[1]                                  # this one fills in the last seconds before the expiry
signed.fills.append({"ticker": second["market_ticker"], "order_id": second["order_id"], "count": second["contracts"],
                     "no_price": 70, "trade_id": "t-2", "created_time": clock.now_utc().isoformat(), "is_taker": False})
signed.resting = [o for o in signed.resting if o["order_id"] != second["order_id"]]
at(D2, "17:46:05")
runner._tick()
check("day 2: the in-app backup cancel ran again (it was skipped before v3.1.1)",
      len(signed.cancelled) == 4, signed.cancelled)
check("day 2: no row is left 'resting', and the last-second fill was caught at the cancel",
      statuses(D2) == ["cancelled", "filled", "filled"], statuses(D2))
check("day 2: a cancel message was sent", cancel_messages() == before + 1, SENT[-1:])
check("day 2: both filled orders kept their fill", all(r["filled_contracts"] > 0 for r in store.live_orders_for_day(D2)
                                                       if r["status"] == "filled"))
check("day 2: the run row has cancelled_at", bool((store.get_live_run(D2) or {}).get("cancelled_at")))

print("3) old rows left 'resting' by an earlier day: Kalshi is asked first, then they are closed")
D0 = "2026-10-30"
expiry = clock.at(D0, "17:55").astimezone(timezone.utc)


def old_row(coid: str, tk: str, oid: str) -> None:
    store.record_live_order(
        client_order_id=coid, event_date=D0, event_ticker=event_for(D0), market_ticker=event_for(D0) + "-" + tk,
        word=tk, no_price_cents=70, yes_price_cents=30, contracts=14, dollars=10.0, collateral=9.8,
        placed_at=clock.at(D0, "17:32:31").astimezone(timezone.utc), mode="live", dry_run=False, post_only=True,
        took_at_open=False, expiration_epoch=int(expiry.timestamp()), status="resting", order_id=oid)


def old(coid: str) -> dict:
    return [r for r in store.live_orders_for_day(D0) if r["client_order_id"] == coid][0]


old_row("stale-plain", "OIL", "old-1")            # expired unfilled
old_row("stale-filled", "ICE", "old-2")           # filled while the process was down: the fill was never read
old_row("stale-live", "SNAP", "old-3")            # still resting on Kalshi (must never be closed blindly)
signed.fills = [{"ticker": event_for(D0) + "-ICE", "order_id": "old-2", "count": 14, "no_price": 70,
                 "trade_id": "t-old", "created_time": expiry.isoformat(), "is_taker": False}]
signed.resting = [{"ticker": event_for(D0) + "-SNAP", "order_id": "old-3"}]
sent_before, cancelled_before = len(signed.sent), len(signed.cancelled)

signed.down = True                                 # Kalshi cannot be read: nothing is closed, it tries again later
at(D3, "08:00:00")
runner._tick()
check("Kalshi down: no old row was closed", [old(c)["status"] for c in ("stale-plain", "stale-filled", "stale-live")] == ["resting"] * 3)
check("Kalshi down: a retry is scheduled", live.STATE["heal_day"] != D3 and live.STATE["heal_retry_at"] is not None)
signed.down = False
runner._tick()
check("no retry before the wait is over", old("stale-plain")["status"] == "resting")
live.STATE["heal_retry_at"] = 0.0                  # the wait is over
runner._tick()
check("the plain old row is now cancelled", old("stale-plain")["status"] == "cancelled", old("stale-plain")["status"])
check("its cancel time is the order's own expiry, not this morning",
      abs((clock.parse_dt(old("stale-plain")["cancelled_at"]) - expiry).total_seconds()) < 2, old("stale-plain")["cancelled_at"])
check("the fill that landed while the process was down was found first",
      old("stale-filled")["status"] == "filled" and old("stale-filled")["filled_contracts"] >= 14, old("stale-filled")["status"])
check("it will be settled (a fill and no result yet)",
      any(r["client_order_id"] == "stale-filled" for r in store.live_orders_to_settle()))
check("the order still resting on Kalshi was left alone", old("stale-live")["status"] == "resting", old("stale-live")["status"])
check("and reported loudly", any("STILL RESTING on Kalshi" in t for t in SENT), SENT[-2:])
check("day 2 rows are untouched by the heal", statuses(D2) == ["cancelled", "filled", "filled"], statuses(D2))
check("the heal sent no order and no cancel", len(signed.sent) == sent_before and len(signed.cancelled) == cancelled_before)
check("the heal ran once for the day", live.STATE["heal_day"] == D3)
signed.resting, signed.fills = [], []

print("3b) the heal never touches TODAY's rows, and never runs around the fire")
at(D3, "17:32:31")
runner._tick()
check("day 3: orders went out", len(signed.sent) == sent_before + 3, len(signed.sent))
live.STATE.update(heal_day=None, heal_retry_at=None)          # as if the day's heal had not happened yet
reads = signed.fill_reads
at(D3, "17:40:00")
runner._tick()
check("mid-window: today's rows stay resting", statuses(D3) == ["resting"] * 3, statuses(D3))
check("mid-window: the heal did not run (the fire and the fills come first)", live.STATE["heal_day"] is None)
check("mid-window: exactly one fill read (the normal poll)", signed.fill_reads == reads + 1, signed.fill_reads - reads)
check("heal marks nothing of today even when asked directly",
      store.mark_stale_live_resting_cancelled(D3, skip_order_ids={"old-3"}) == (0, 1)
      and statuses(D3) == ["resting"] * 3, statuses(D3))
at(D3, "17:46:05")
runner._tick()
check("day 3: cancelled at the deadline", statuses(D3) == ["cancelled"] * 3, statuses(D3))

print("3c) a manual cancel BEFORE the fire must not switch off that evening's backup cancel")
D4 = "2026-11-05"
at(D4, "15:00:00")
runner._tick()
runner.cancel_all(reason="manual /live_cancel_now")            # nothing is resting; only the flag is set
check("the manual cancel set the flag", live.STATE["cancelled_today"] is True)
at(D4, "17:32:31")
runner._tick()
check("day 4: orders still went out", statuses(D4) == ["resting"] * 3, statuses(D4))
before4 = len(signed.cancelled)
at(D4, "17:46:05")
runner._tick()
check("day 4: the backup cancel ran", len(signed.cancelled) == before4 + 3, len(signed.cancelled) - before4)
check("day 4: no row left resting", statuses(D4) == ["cancelled"] * 3, statuses(D4))

print("4) a restart still works (state comes back from the database)")
D5 = "2026-11-06"
live.STATE.update(active_event=None, active_date=None, orders_today=0, fills_today=0, cancelled_today=False,
                  day=None, heal_day=None, heal_retry_at=None)
runner_b = live.LiveRunner(client=signed, public=pub)
n_sent = len(signed.sent)
at(D5, "17:32:31")
runner_b._tick()
check("day 5: orders went out once", len(signed.sent) == n_sent + 3, len(signed.sent) - n_sent)
live.STATE.update(active_event=None, active_date=None, cancelled_today=False, day=None,
                  heal_day=None, heal_retry_at=None)                                    # as after a reboot mid-window
at(D5, "17:40:00")
runner_b._tick()
check("day 5: a reboot does not send the orders again", len(signed.sent) == n_sent + 3, len(signed.sent) - n_sent)
check("day 5: the reboot left today's rows resting", statuses(D5) == ["resting"] * 3, statuses(D5))
at(D5, "17:46:05")
runner_b._tick()
check("day 5: cancelled after the reboot", statuses(D5) == ["cancelled"] * 3, statuses(D5))

print()
print("%d checks, %d failed" % (CHECKS[0], len(FAILS)))
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print("ALL GOOD")
