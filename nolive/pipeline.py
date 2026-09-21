"""The nightly job. One Runner lives in the Streamlit background thread and calls tick() about twice a second
near 5:32:30 PM CT and every 20 seconds the rest of the day.

Night timeline (all Central):
  5:31   prepare: find tonight's event (no-fade's `days` first, Kalshi second) and read the series fee type
  5:32:30 FIRE: read every word's price + order book, pick the ones under 98c, send the paper orders
          (instant matches against the book = taker fills with fee; the rest rests)
  5:32:30 -> 5:55 poll the public trade tape every ~10s; a YES buyer paying MORE than our limit fills us (maker)
          also record an order-book picture each minute (no-fade stops at 5:28)
  5:55:20 close: final read of the tape, orders finalised per cancel time
  6:05+   settle: official yes/no results (no-fade's results first, Kalshi for the rest), money, Telegram
"""
from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from . import analytics, clock, config as C, engine, nofade, notify, settle, store
from .kalshi import KalshiPublic, count_needed, market_prices, word_from_market

log = logging.getLogger("nolive.pipeline")

STATE = {"ticks": 0, "last_tick": None, "last_error": None, "last_status": "", "started": None}


class Runner:
    def __init__(self, client=None, now_fn=None, sleep_fn=None, notifier=None):
        self.client = client or KalshiPublic()
        self._now_fn = now_fn or clock.now_utc
        self._sleep = sleep_fn or time.sleep
        self.notify = notifier or notify
        self.prep: dict = {}            # date -> {"event_ticker": ..., "fee": {...}}
        self.pre: dict = {}             # date -> pre-fire recorder state
        self.days: dict = {}            # date -> in-memory state of a fired night
        self.checked_fire: set = set()  # dates where we already looked for a run in the DB
        self.handled: set = set()       # dates where the fire step is finished (fired, missed, or no event)
        self.last_poll: dict = {}
        self.last_settle_try = None
        self._paused = (False, None)    # (value, checked_at)
        self.lock = threading.RLock()

    # ------------------------------------------------------------ small helpers
    def now(self) -> datetime:
        return self._now_fn()

    def paused(self) -> bool:
        val, at = self._paused
        now = self.now()
        if at is None or (now - at).total_seconds() > 30:
            try:
                val = bool(store.get_state("paused", False))
            except Exception:
                val = False
            self._paused = (val, now)
        return val

    def set_paused(self, value: bool) -> None:
        store.set_state("paused", bool(value))
        self._paused = (bool(value), self.now())

    # ------------------------------------------------------------ the tick
    def tick(self) -> str:
        with self.lock:
            now = self.now()
            today = now.astimezone(C.CT).strftime("%Y-%m-%d")
            STATE["ticks"] += 1
            STATE["last_tick"] = now
            if self.paused():
                STATE["last_status"] = "paused"
                return "paused"
            parts = []
            for step in (self._maybe_record_pre, self._maybe_prepare, self._maybe_fire, self._maybe_poll, self._maybe_settle):
                try:
                    out = step(today, now)
                    if out:
                        parts.append(out)
                except Exception as exc:
                    log.exception("%s failed", step.__name__)
                    STATE["last_error"] = "%s: %s" % (step.__name__, exc)
                    store.log_activity("error", "%s: %s" % (step.__name__, exc))
            STATE["last_status"] = " | ".join(parts)
            return STATE["last_status"]

    # ------------------------------------------------------------ 0) record the gap: RECORD_FROM (5:28) -> fire (5:32:30)
    def _maybe_record_pre(self, today: str, now: datetime):
        """No-fade's recorder stops at ~5:28. From then until we fire, save an order-book picture of EVERY word
        every PRE_BOOK_SECONDS. (The trade tape for the same minutes is pulled in one go right after the fire.)"""
        if today in self.handled or today in self.days:
            return ""
        if now < clock.record_from(today) or now >= clock.fire_at(today):
            return ""
        pre = self.pre.get(today)
        if pre is None:
            pre = self.pre[today] = {"ticker": None, "markets": [], "last_book": None, "next_lookup": now}
        if not pre["markets"]:
            if now < pre["next_lookup"]:
                return ""
            pre["next_lookup"] = now + timedelta(seconds=60)
            try:
                ticker = self._find_event_ticker(today)
                if not ticker:
                    return "pre: no event yet"
                pre["ticker"] = ticker
                pre["markets"] = [m["ticker"] for m in self.client.get_markets(ticker)]
            except Exception as exc:
                log.warning("pre-fire lookup failed: %s", exc)
                return ""
        if pre["last_book"] is not None and (now - pre["last_book"]).total_seconds() < C.PRE_BOOK_SECONDS:
            return ""
        pre["last_book"] = now
        limit = float(C.LIMIT_YES_CENTS)

        def one(t):
            try:
                return t, self.client.get_orderbook(t, depth=15), self.now()
            except Exception:
                return t, None, None

        saved = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            for t, book, ts in pool.map(one, list(pre["markets"])):
                if book is None:
                    continue
                s = engine.book_summary(book, limit)
                store.insert_depth({
                    "run_id": None, "event_date": today, "market_ticker": t, "ts": ts, "kind": "pre",
                    "best_yes_bid": s["best_yes_bid"], "best_no_bid": s["best_no_bid"],
                    "yes_size_total": s["yes_size_total"], "no_size_total": s["no_size_total"],
                    "yes_size_at_limit": s["yes_size_at_limit"],
                    "book_yes": _dumps(book["yes"]), "book_no": _dumps(book["no"]),
                })
                saved += 1
        return "pre-fire books: %d" % saved

    def _backfill_pre_trades(self, today: str, cutoffs: dict) -> int:
        """Save every trade from RECORD_FROM up to the moment each word's paper order went in.
        Trades are public history, so one read per word after the fire is enough. Covers ALL words."""
        start = clock.record_from(today).astimezone(timezone.utc)

        def read(t):
            try:
                return t, self.client.get_trades(t, min_ts=int(start.timestamp()) - 1, max_pages=5)
            except Exception as exc:
                log.warning("pre-fire trades %s failed: %s", t, exc)
                return t, []

        saved = 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            for t, trades in pool.map(read, list(cutoffs)):
                rows = [{"trade_id": x["id"], "event_date": today, "market_ticker": t, "ts": x["ts"],
                         "yes_price_cents": x["yes_cents"], "contracts": x["count"], "taker_side": x["taker_side"]}
                        for x in trades if start <= x["ts"] <= cutoffs[t]]
                store.insert_trades(rows)
                saved += len(rows)
        store.log_activity("pre_trades", "%s: saved %d trades from %s CT to the fire" % (today, saved, C.RECORD_FROM_CT))
        return saved

    # ------------------------------------------------------------ 1) prepare
    def _find_event_ticker(self, today: str):
        ticker = nofade.event_ticker(today)
        if ticker:
            return ticker
        for ev in self.client.get_events(C.SERIES, status="open"):
            t = str(ev.get("event_ticker") or "")
            if self._ticker_date(t) == today:
                return t
        return None

    @staticmethod
    def _ticker_date(event_ticker: str):
        try:
            return datetime.strptime("20" + event_ticker.split("-")[1], "%Y%b%d").strftime("%Y-%m-%d")
        except Exception:
            return None

    def _fee_info(self) -> dict:
        info = {"fee_type": "", "fee_multiplier": 1.0, "maker_rate": 0.0}
        try:
            s = self.client.get_series(C.SERIES)
            info["fee_type"] = str(s.get("fee_type") or "")
            try:
                info["fee_multiplier"] = float(s.get("fee_multiplier") or 1.0)
            except (TypeError, ValueError):
                info["fee_multiplier"] = 1.0
            if "maker" in info["fee_type"].lower():
                try:
                    mm = float(s.get("maker_fee_multiplier")) if s.get("maker_fee_multiplier") is not None else info["fee_multiplier"]
                except (TypeError, ValueError):
                    mm = info["fee_multiplier"]
                info["maker_rate"] = 0.0175 * mm
        except Exception as exc:
            log.warning("could not read series fee info: %s", exc)
            info["fee_type"] = "unknown (assumed: no maker fee)"
        return info

    def _maybe_prepare(self, today: str, now: datetime):
        if today in self.prep or now < clock.prepare_at(today) or now > clock.fire_at(today) + timedelta(seconds=C.FIRE_GRACE_S):
            return ""
        prep = {"event_ticker": None, "fee": self._fee_info()}
        try:
            prep["event_ticker"] = (self.pre.get(today) or {}).get("ticker") or self._find_event_ticker(today)
        except Exception as exc:
            log.warning("event lookup failed: %s", exc)
            prep["error"] = str(exc)
        self.prep[today] = prep
        return "prepared %s" % (prep["event_ticker"] or "no event yet")

    # ------------------------------------------------------------ 2) fire
    def _maybe_fire(self, today: str, now: datetime):
        T = clock.fire_at(today)
        if now < T - timedelta(seconds=2) or today in self.handled:
            return ""
        if today not in self.checked_fire:
            # first look tonight: has any instance (this one before a restart, or another) already made a run?
            self.checked_fire.add(today)
            run = store.get_run(today)
            if run:
                self.handled.add(today)
                self._load_day(run)
                return "run exists (%s)" % run["status"]
        if now > T + timedelta(seconds=C.FIRE_GRACE_S):
            return self._record_missed(today, now)
        wait = (T - now).total_seconds()
        if 0 < wait <= 2.0:
            self._sleep(wait)
            now = self.now()
        if now < T:
            return ""
        return self.fire(today, now)

    def _record_missed(self, today: str, now: datetime):
        """The app was not awake in the fire window. Never fire late: note it and move on."""
        self.handled.add(today)
        fee = {"fee_type": "", "fee_multiplier": 1.0, "maker_rate": 0.0}
        try:
            ticker = self._find_event_ticker(today)
        except Exception:
            ticker = None
        if not ticker:
            store.claim_run(self._run_row(today, "", "no_event", None, fee, note="no event found"))
            return "no event"
        run, created = store.claim_run(self._run_row(today, ticker, "missed", None, fee,
                                                     note="app was not running in the fire window"))
        if created:
            store.log_activity("missed", "%s: missed the fire window" % today)
            self.notify.send("⚠️ WNT post-cold-open: the app was not awake at %s CT on %s. No paper orders tonight." % (C.FIRE_AT_CT, today))
        return "missed"

    def _run_row(self, today, ticker, status, fired_at, fee, note=""):
        T = clock.fire_at(today)
        late = max(0.0, (fired_at - T).total_seconds()) if fired_at else None
        return {
            "event_date": today, "event_ticker": ticker, "status": status, "scheduled_at": T,
            "fired_at": fired_at, "late_seconds": late, "limit_yes_cents": C.LIMIT_YES_CENTS,
            "dollars": C.PAPER_DOLLARS, "skip_at_or_above": C.SKIP_YES_AT_OR_ABOVE,
            "cancel_times": ",".join(C.CANCEL_TIMES_CT), "fee_type": fee.get("fee_type"),
            "fee_multiplier": fee.get("fee_multiplier"), "maker_rate": fee.get("maker_rate"),
            "version": C.VERSION, "notes": note,
        }

    def fire(self, today: str, now: datetime) -> str:
        prep = self.prep.get(today)
        if prep is None:
            self._maybe_prepare(today, now)
            prep = self.prep.get(today) or {"event_ticker": None, "fee": self._fee_info()}
        ticker = prep.get("event_ticker")
        if not ticker:
            try:
                ticker = self._find_event_ticker(today)
            except Exception as exc:
                log.warning("event lookup at fire failed: %s", exc)
        fee = prep.get("fee") or self._fee_info()
        if not ticker:
            self.handled.add(today)
            run, created = store.claim_run(self._run_row(today, "", "no_event", now, fee, note="no event found"))
            if created:
                store.log_activity("no_event", "%s: no WNT event found at fire time" % today)
                if now.astimezone(C.CT).weekday() < 5:     # weekends have no broadcast: stay quiet
                    self.notify.send("ℹ️ WNT post-cold-open %s: no event found at %s CT. Nothing to trade." % (today, C.FIRE_AT_CT))
            return "no event"

        markets = self.client.get_markets(ticker)      # if this fails we retry next tick (still inside the grace window)
        fired_at = self.now()
        run, created = store.claim_run(self._run_row(today, ticker, "fired", fired_at, fee))
        self.handled.add(today)
        if not created:
            self._load_day(run)
            return "another instance already fired"

        limit = float(C.LIMIT_YES_CENTS)
        intended = engine.contracts_for(C.PAPER_DOLLARS, limit)
        rows = []
        for m in markets:
            last, bid, ask = market_prices(m)
            q = engine.qualify(last, bid, ask, m.get("status"), C.SKIP_YES_AT_OR_ABOVE)
            word = word_from_market(m)
            rows.append({"m": m, "last": last, "bid": bid, "ask": ask, "q": q, "word": word})
        qualified = [r for r in rows if r["q"]["qualified"]]

        def book_for(r):
            for attempt in range(2):
                try:
                    return r, self.client.get_orderbook(r["m"]["ticker"], depth=15), self.now(), None
                except Exception as exc:
                    err = str(exc)
            return r, {"yes": [], "no": []}, self.now(), err

        with ThreadPoolExecutor(max_workers=6) as pool:
            books = list(pool.map(book_for, qualified))

        day = {"run": run, "date": today, "markets": {}, "seen": set(), "closed": False,
               "maker_rate": float(fee.get("maker_rate") or 0.0), "fee_mult": float(fee.get("fee_multiplier") or 1.0)}
        instant_words = 0
        instant_ct = 0.0
        book_errors = 0
        by_ticker = dict((r["m"]["ticker"], (r, book, ts, err)) for r, book, ts, err in books)
        for r in rows:
            m = r["m"]
            t = m["ticker"]
            pre = nofade.pre_book(t, today, clock.fire_at(today)) if r["q"]["qualified"] else None
            base = {
                "run_id": run["id"], "event_date": today, "market_ticker": t, "word": r["word"],
                "title": m.get("title"), "is_counting": count_needed(r["word"]) >= 2,
                "count_needed": count_needed(r["word"]), "market_status": m.get("status"),
                "last_price_cents": r["last"], "yes_bid_cents": r["bid"], "yes_ask_cents": r["ask"],
                "price_basis": r["q"]["basis"], "yes_price_cents": r["q"]["price"],
                "qualified": bool(r["q"]["qualified"]), "skip_reason": r["q"]["reason"] or None,
                "book_yes": None, "book_no": None, "yes_size_at_limit": None, "queue_ahead": None,
                "pre_ts": (pre or {}).get("ts"), "pre_yes_bid_cents": (pre or {}).get("best_yes_bid"),
                "pre_no_bid_cents": (pre or {}).get("best_no_bid"),
            }
            if not r["q"]["qualified"]:
                store.insert_market(base)
                # no order, but keep recording its book and trades after the fire (for testing other ideas later)
                day["markets"][t] = {"ticker": t, "word": r["word"], "is_counting": base["is_counting"],
                                     "yes_price": r["q"]["price"], "placed_at": fired_at, "intended": 0.0, "fills": [],
                                     "cursor": fired_at, "last_book_at": fired_at - timedelta(seconds=C.BOOK_SNAPSHOT_SECONDS),
                                     "written": {}, "orders": {}}
                continue
            _, book, book_ts, err = by_ticker[t]
            if err:
                book_errors += 1
            summ = engine.book_summary(book, limit)
            base.update({"book_yes": _dumps(book["yes"]), "book_no": _dumps(book["no"]),
                         "yes_size_at_limit": summ["yes_size_at_limit"], "queue_ahead": summ["queue_ahead"]})
            store.insert_market(base)
            store.insert_depth({
                "run_id": run["id"], "event_date": today, "market_ticker": t, "ts": book_ts, "kind": "fire",
                "best_yes_bid": summ["best_yes_bid"], "best_no_bid": summ["best_no_bid"],
                "yes_size_total": summ["yes_size_total"], "no_size_total": summ["no_size_total"],
                "yes_size_at_limit": summ["yes_size_at_limit"],
                "book_yes": _dumps(book["yes"]), "book_no": _dumps(book["no"]),
            })
            mk = {"ticker": t, "word": r["word"], "is_counting": base["is_counting"], "yes_price": r["q"]["price"],
                  "placed_at": book_ts, "intended": intended, "fills": [], "cursor": book_ts,
                  "last_book_at": book_ts, "written": {}, "orders": {}}
            for v in C.VARIANTS:
                ca = clock.cancel_at(today, v["cancel_ct"])
                order = {"run_id": run["id"], "event_date": today, "event_ticker": ticker, "market_ticker": t,
                         "word": r["word"], "variant_id": v["id"], "cancel_ct": v["cancel_ct"], "cancel_at": ca,
                         "limit_yes_cents": C.LIMIT_YES_CENTS, "contracts": intended, "dollars": C.PAPER_DOLLARS,
                         "placed_at": book_ts, "yes_price_at_place": r["q"]["price"],
                         "is_counting": base["is_counting"], "status": "resting"}
                store.insert_order(order)
                mk["orders"][v["id"]] = order
            tk = engine.taker_fills(book, limit, intended, day["fee_mult"])
            for f in tk:
                fill = dict(f, ts=book_ts)
                if store.insert_fill({"run_id": run["id"], "event_date": today, "market_ticker": t, **fill}):
                    mk["fills"].append(fill)
            if tk:
                instant_words += 1
                instant_ct += sum(f["contracts"] for f in tk)
            day["markets"][t] = mk
        self._attach_order_ids(day)
        store.attach_depth_to_run(today, run["id"])
        store.update_run(run["id"], status="polling", markets_seen=len(rows), qualified=len(qualified),
                         skipped=len(rows) - len(qualified),
                         notes=("late %.1fs; " % (run.get("late_seconds") or 0)) + ("%d book errors" % book_errors if book_errors else ""))
        self.days[today] = day
        self.checked_fire.add(today)
        self._refresh_orders(day, self.now())
        store.log_activity("fire", "%s: %d words, %d qualified, %d instant" % (today, len(rows), len(qualified), instant_words))
        skipped = [r for r in rows if not r["q"]["qualified"]]
        self.notify.send(
            "🔔 WNT post-cold-open %s\nfired %s CT (%.1fs after %s)\n%d words | %d qualify (YES under %gc) | %d skipped\n"
            "SELL YES %dc on each, $%g per word (%.2f contracts)\ninstant (taker) fills: %d words, %.1f contracts\nresting on the rest until %s"
            % (today, clock.fmt(fired_at), (run.get("late_seconds") or 0), C.FIRE_AT_CT, len(rows), len(qualified),
               C.SKIP_YES_AT_OR_ABOVE, len(skipped), C.LIMIT_YES_CENTS, C.PAPER_DOLLARS, intended,
               instant_words, instant_ct, ", ".join(v["label"].replace(" PM", "") for v in C.VARIANTS)))
        try:      # after the Telegram message so nothing waits on it: save the 5:28 -> fire tape for every word
            cutoffs = dict((r["m"]["ticker"], day["markets"][r["m"]["ticker"]]["placed_at"] if r["m"]["ticker"] in day["markets"] else fired_at)
                           for r in rows)
            self._backfill_pre_trades(today, cutoffs)
        except Exception as exc:
            log.exception("pre-fire trade backfill failed")
            store.log_activity("error", "pre-fire trade backfill: %s" % exc)
        return "fired: %d qualified, %d instant" % (len(qualified), instant_words)

    def _attach_order_ids(self, day: dict) -> None:
        by = {}
        for o in store.orders_for_run(day["run"]["id"]):
            by[(o["market_ticker"], o["variant_id"])] = o
        for t, mk in day["markets"].items():
            for vid in list(mk["orders"].keys()):
                row = by.get((t, vid))
                if row:
                    mk["orders"][vid] = row

    # ------------------------------------------------------------ rebuild a night after a restart
    def _load_day(self, run: dict):
        today = run["event_date"]
        if today in self.days or run.get("status") in ("no_event", "missed"):
            return
        markets = [m for m in store.markets_for_run(run["id"]) if m.get("qualified")]
        orders = store.orders_for_run(run["id"])
        fills = store.fills_for_run(run["id"])
        day = {"run": run, "date": today, "markets": {}, "seen": store.trade_ids(today),
               "closed": run.get("status") in ("closed", "settled"),
               "maker_rate": float(run.get("maker_rate") or 0.0), "fee_mult": float(run.get("fee_multiplier") or 1.0)}
        for m in markets:
            t = m["market_ticker"]
            mine = [o for o in orders if o["market_ticker"] == t]
            if not mine:
                continue
            placed = mine[0]["placed_at"]
            day["markets"][t] = {
                "ticker": t, "word": m.get("word"), "is_counting": bool(m.get("is_counting")),
                "yes_price": m.get("yes_price_cents"), "placed_at": placed, "intended": float(mine[0]["contracts"]),
                "fills": [f for f in fills if f["market_ticker"] == t], "cursor": placed,
                "last_book_at": placed, "written": {}, "orders": dict((o["variant_id"], o) for o in mine),
            }
        fired_at = run.get("fired_at") or clock.now_utc()
        for m in store.markets_for_run(run["id"]):
            if m.get("qualified") or m["market_ticker"] in day["markets"]:
                continue
            day["markets"][m["market_ticker"]] = {
                "ticker": m["market_ticker"], "word": m.get("word"), "is_counting": bool(m.get("is_counting")),
                "yes_price": m.get("yes_price_cents"), "placed_at": fired_at, "intended": 0.0, "fills": [],
                "cursor": fired_at, "last_book_at": fired_at, "written": {}, "orders": {}}
        self.days[today] = day
        store.log_activity("resume", "%s: rebuilt %d words from the database" % (today, len(day["markets"])))

    # ------------------------------------------------------------ 3) poll the tape
    def _maybe_poll(self, today: str, now: datetime):
        out = []
        for date, day in list(self.days.items()):
            if day["closed"]:
                continue
            last_cancel = clock.last_cancel_at(date)
            end = last_cancel + timedelta(seconds=C.TRACK_AFTER_LAST_CANCEL_S)
            if now <= end:
                lp = self.last_poll.get(date)
                if lp is None or (now - lp).total_seconds() >= C.POLL_SECONDS:
                    self.last_poll[date] = now
                    out.append(self._poll(day, now, backfill=(lp is None and now > clock.fire_at(date) + timedelta(seconds=C.POLL_SECONDS * 2))))
            else:
                out.append(self._close(day, now))
        return " ".join(x for x in out if x)

    def _poll(self, day: dict, now: datetime, backfill: bool = False) -> str:
        date = day["date"]
        last_cancel = clock.last_cancel_at(date)

        def read(mk):
            min_ts = int((mk["placed_at"] if backfill else mk["cursor"]).timestamp()) - 2
            try:
                return mk, self.client.get_trades(mk["ticker"], min_ts=min_ts, max_pages=5 if backfill else 3), None
            except Exception as exc:
                return mk, [], str(exc)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(read, list(day["markets"].values())))
        new_fills = errors = new_trades = 0
        for mk, trades, err in results:
            if err:
                errors += 1
                continue
            fresh = [t for t in trades if t["id"] not in day["seen"] and t["ts"] > mk["placed_at"]]
            if not fresh:
                continue
            store.insert_trades([{"trade_id": t["id"], "event_date": date, "market_ticker": mk["ticker"], "ts": t["ts"],
                                  "yes_price_cents": t["yes_cents"], "contracts": t["count"], "taker_side": t["taker_side"]}
                                 for t in fresh])
            for t in fresh:
                day["seen"].add(t["id"])
            new_trades += len(fresh)
            mk["cursor"] = max(mk["cursor"], max(t["ts"] for t in fresh))
            remaining = mk["intended"] - sum(float(f["contracts"]) for f in mk["fills"])
            if remaining > 1e-6:
                for f in engine.maker_fills(fresh, mk["placed_at"], last_cancel, float(C.LIMIT_YES_CENTS), remaining, day["maker_rate"]):
                    if store.insert_fill({"run_id": day["run"]["id"], "event_date": date, "market_ticker": mk["ticker"], **f}):
                        mk["fills"].append(f)
                        new_fills += 1
        self._snapshot_books(day, now, last_cancel)
        self._refresh_orders(day, now)
        return "polled %d words: %d trades, %d fills%s" % (len(results), new_trades, new_fills, (", %d read errors" % errors) if errors else "")

    def _snapshot_books(self, day: dict, now: datetime, last_cancel: datetime) -> None:
        if now > last_cancel:
            return
        due = [mk for mk in day["markets"].values()
               if (now - mk["last_book_at"]).total_seconds() >= C.BOOK_SNAPSHOT_SECONDS]
        if not due:
            return
        limit = float(C.LIMIT_YES_CENTS)

        def one(mk):
            try:
                return mk, self.client.get_orderbook(mk["ticker"], depth=15)
            except Exception:
                return mk, None

        with ThreadPoolExecutor(max_workers=4) as pool:
            for mk, book in pool.map(one, due):
                mk["last_book_at"] = now
                if book is None:
                    continue
                s = engine.book_summary(book, limit)
                store.insert_depth({
                    "run_id": day["run"]["id"], "event_date": day["date"], "market_ticker": mk["ticker"], "ts": now,
                    "kind": "poll", "best_yes_bid": s["best_yes_bid"], "best_no_bid": s["best_no_bid"],
                    "yes_size_total": s["yes_size_total"], "no_size_total": s["no_size_total"],
                    "yes_size_at_limit": s["yes_size_at_limit"],
                    "book_yes": _dumps(book["yes"]), "book_no": _dumps(book["no"]),
                })

    def _refresh_orders(self, day: dict, now: datetime) -> None:
        for mk in day["markets"].values():
            for vid, order in mk["orders"].items():
                agg = engine.aggregate(mk["fills"], order["cancel_at"])
                status = engine.status_for(agg, float(order["contracts"]), order["cancel_at"], now)
                sig = (status, agg["filled_contracts"], agg["taker_contracts"], agg["taker_fee_cents"], agg["maker_fee_cents"])
                if mk["written"].get(vid) != sig and order.get("id") is not None:
                    store.update_order(order["id"], status=status, **agg)
                    mk["written"][vid] = sig

    def _close(self, day: dict, now: datetime) -> str:
        date = day["date"]
        self._poll(day, now, backfill=True)      # one last full read of the tape from the moment we placed the orders
        self._refresh_orders(day, now)
        day["closed"] = True
        store.update_run(day["run"]["id"], status="closed")
        orders = store.orders_for_run(day["run"]["id"])
        lines = ["🕔 WNT post-cold-open %s: all orders now cancelled (waiting for Kalshi results)" % date,
                 "cancel   words filled   taker/maker contracts"]
        for v in C.VARIANTS:
            mine = [o for o in orders if o["variant_id"] == v["id"]]
            filled = [o for o in mine if float(o["filled_contracts"] or 0) > 0]
            lines.append("%-8s %2d/%-3d          %5.1f / %-5.1f" % (
                v["label"].replace(" PM", ""), len(filled), len(mine),
                sum(float(o["taker_contracts"] or 0) for o in mine), sum(float(o["maker_contracts"] or 0) for o in mine)))
        self.notify.send("\n".join(lines))
        store.log_activity("close", "%s closed" % date)
        return "closed %s" % date

    # ------------------------------------------------------------ 4) settle
    def _maybe_settle(self, today: str, now: datetime):
        if self.last_settle_try is not None and (now - self.last_settle_try).total_seconds() < C.SETTLE_RETRY_S:
            return ""
        self.last_settle_try = now             # one look per interval, even when nothing is due
        runs = store.unsettled_runs()
        due = [r for r in runs
               if now >= clock.settle_start(r["event_date"])
               and now - clock.settle_start(r["event_date"]) < timedelta(days=10)]
        if not due:
            return ""
        out = []
        for run in due:
            if run["event_date"] not in self.days:
                self._load_day(run)
            day = self.days.get(run["event_date"])
            if day is not None and not day["closed"] and now > clock.last_cancel_at(run["event_date"]):
                self._close(day, now)          # the app was down at close time: read the tape now and finish
            res = settle.settle_run(run, self.client, now)
            out.append("%s: settled %d orders, %d words pending" % (run["event_date"], res["settled_orders"], res["pending_words"]))
            if res["done"] and not store.get_state("summary_sent:%s" % run["event_date"]):
                store.set_state("summary_sent:%s" % run["event_date"], True)
                allo = store.all_orders()
                self.notify.send(analytics.night_summary_text(run["event_date"], allo, allo))
        return " ".join(out)

    # ------------------------------------------------------------ status text (Telegram / dashboard)
    def status_text(self) -> str:
        today = clock.today_ct()
        run = store.get_run(today)
        lines = [C.VERSION + (" · PAUSED" if self.paused() else " · running")]
        lines.append("last tick: %s" % (clock.fmt(STATE["last_tick"]) if STATE["last_tick"] else "never"))
        if run:
            lines.append("today %s: %s · %s words · %s qualified" % (today, run["status"], run["markets_seen"], run["qualified"]))
        else:
            lines.append("today %s: not fired yet (fires %s CT)" % (today, C.FIRE_AT_CT))
        if STATE["last_error"]:
            lines.append("last error: %s" % STATE["last_error"])
        return "\n".join(lines)


def _dumps(levels) -> str:
    import json
    return json.dumps([[float(p), float(c)] for p, c in levels])


# ---------------------------------------------------------------- Streamlit / script entry points
_runner: Runner | None = None


def runner() -> Runner:
    global _runner
    if _runner is None:
        _runner = Runner()
    return _runner


def register_commands() -> None:
    r = runner()
    notify.register("nolive_status", lambda a: r.status_text())
    notify.register("nolive_pause", lambda a: (r.set_paused(True), "⏸ paused: no new paper orders until /nolive_resume")[1])
    notify.register("nolive_resume", lambda a: (r.set_paused(False), "▶️ resumed")[1])
    notify.register("nolive_help", lambda a: "/nolive_status /nolive_pause /nolive_resume")


def run_forever() -> None:
    STATE["started"] = clock.now_utc()
    r = runner()
    while True:
        try:
            r.tick()
        except Exception as exc:
            STATE["last_error"] = str(exc)
            log.exception("tick")
        time.sleep(clock.loop_interval(clock.now_utc()))
