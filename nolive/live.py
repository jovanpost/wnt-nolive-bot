"""The LIVE (real-money) engine. Separate from pipeline.py's paper engine on purpose:

- pipeline.py (paper): always runs, never touches Kalshi's order endpoints, 5 cancel-time variants,
  for research/comparison. This file cannot break that -- it does not import or call pipeline.py.
- live.py (this file): only runs real orders when you turn it on in Streamlit Secrets. One cancel
  time (config.LIVE_CANCEL_CT). Same qualify rule, same word list, as the paper engine.

Safety rules this file follows (same ones wnt-nofade-bot follows for its own live orders):
  - Never place the same order twice. Every order's client_order_id is built from
    (date, ticker, price, size) -- Kalshi itself refuses a repeat, and we also check our own
    nolive_live_orders table first. Retrying this code after a crash is always safe.
  - Every real order defaults to post_only (must rest, never take), except when the book is
    ALREADY past our price -- then take_if_cheap fires once, deliberately, instead of resting
    into a fill we'd have taken anyway.
  - The REAL safety net is server-side expiry: every order tells Kalshi itself, at the moment it
    is placed, to expire at LIVE_CANCEL_CT -- enforced by Kalshi's own matching engine, so it works
    even if this app is down. The in-app cancel_all() (run_forever()'s own tick loop, while the
    Streamlit app is up) is a second, belt-and-suspenders layer on top of that -- not the other way
    around -- and it deliberately fires LIVE_APP_CANCEL_BUFFER_SECONDS (default 60s) AFTER
    LIVE_CANCEL_CT, so it never races Kalshi's own expiry over the same instant. There is
    deliberately no GitHub Actions cancel workflow: it isn't reliable enough to depend on (same
    conclusion reached for wnt-nofade-bot), so it was left out rather than shipped as a false sense
    of security.
  - A hard per-day cap on markets and on total resting collateral, checked BEFORE any order goes out.
  - Three modes, same meaning as wnt-nofade-bot: dry_run (default, no API calls that place money),
    smoke (also sends one tiny real order per word, for testing), live (the real thing).
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from . import clock, config as C, engine, nofade, notify, store
from .kalshi import KalshiClient, KalshiError, KalshiPublic, live_book_metrics
from .kalshi import count_needed, market_prices, word_from_market

log = logging.getLogger("nolive.live")

STATE = {
    "running": False,
    "active_event": None,
    "active_date": None,
    "last_poll": None,
    "orders_today": 0,
    "fills_today": 0,
    "last_error": None,
    "cancelled_today": False,
}


def client_order_id(event_date: str, ticker: str) -> str:
    """Deterministic per (day, ticker, price, size): a retry never double-rests."""
    seed = "nolive-live|%s|%s|%d|%s|v1" % (event_date, ticker, C.live_no_price_cents(),
                                           C.live_sized_contracts()["contracts"])
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


def smoke_client_order_id(event_date: str, ticker: str) -> str:
    import hashlib
    digest = hashlib.md5(ticker.encode()).hexdigest()[:12]
    return ("nolive-smoke-%s-%s-%d-%d" % (event_date, digest, C.live_no_price_cents(),
                                          C.LIVE_SMOKE_CONTRACTS))[:64]


def _is_smoke_row(row: dict) -> bool:
    return row.get("mode") == "smoke" or str(row.get("client_order_id") or "").startswith("nolive-smoke-")


class LiveRunner:
    def __init__(self, client=None, public=None):
        self.client = client or KalshiClient()
        self.pub = public or KalshiPublic()

    # ---------------------------------------------------------------- discovery
    @staticmethod
    def _ticker_date(event_ticker: str):
        """'KXWORLDNEWSMENTION-26SEP18' -> '2026-09-18'. Same parser pipeline.py uses."""
        try:
            return datetime.strptime("20" + event_ticker.split("-")[1], "%Y%b%d").strftime("%Y-%m-%d")
        except Exception:
            return None

    def find_todays_event(self) -> tuple | None:
        today = clock.today_ct()
        if store.live_day_handled(today):
            return None
        ticker = nofade.event_ticker(today)   # no-fade already found it -> read it, don't ask Kalshi again
        if ticker:
            return ticker, today
        try:
            events = self.pub.get_events(C.SERIES, status="open")
        except Exception as exc:
            log.warning("live event lookup failed: %s", exc)
            STATE["last_error"] = str(exc)[:200]
            return None
        for event in events:
            t = str(event.get("event_ticker") or "")
            if self._ticker_date(t) == today:
                return t, today
        return None

    def active_markets(self, event_ticker: str) -> list:
        markets = self.pub.get_markets(event_ticker)
        return [m for m in markets if str(m.get("status") or "").lower() in ("active", "open")]

    def mode(self) -> str:
        return C.live_mode()

    # ---------------------------------------------------------------- placing
    def place_all(self, event_ticker: str, event_date: str) -> None:
        detected_at = datetime.now(timezone.utc)
        run, created = store.claim_live_run({
            "event_date": event_date, "event_ticker": event_ticker, "status": "fired",
            "mode": self.mode(), "detected_at": detected_at, "notes": "",
        })
        if not created:
            log.info("live: %s already handled, skipping", event_date)
            return

        if store.get_state("live_paused", False):
            notify.send("⏸ [LIVE] Event detected but LIVE is PAUSED. No real orders placed.")
            store.update_live_run(event_date, notes="skipped: paused")
            return

        markets = self.active_markets(event_ticker)
        if not markets:
            notify.send("⚠️ [LIVE] %s found but has no active markets." % event_ticker)
            store.update_live_run(event_date, markets_seen=0, notes="no active markets")
            return

        seen = len(markets)
        per_market = C.live_collateral_per_market()
        cap_by_count = markets[:C.LIVE_MAX_MARKETS_PER_DAY]
        max_by_money = int(C.LIVE_MAX_DAILY_COLLATERAL // per_market) if per_market > 0 else len(cap_by_count)
        markets = cap_by_count[:max_by_money]

        needed = len(markets) * per_market
        if not C.LIVE_DRY_RUN and not self._balance_ok(needed):
            store.update_live_run(event_date, markets_seen=seen, notes="insufficient balance")
            return

        expiry = clock.live_cancel_at(event_date).astimezone(timezone.utc)
        expiry_epoch = int(expiry.timestamp()) if C.LIVE_USE_SERVER_SIDE_EXPIRY else None

        placed = rejected = taken = 0
        words = []
        for m in markets:
            word = word_from_market(m)
            if count_needed(word) >= 2 and C.EXCLUDE_COUNTING_WORDS:
                continue  # same exclusion as the paper rule; no row at all for these
            last, bid, ask = market_prices(m)
            q = engine.qualify(last, bid, ask, m.get("status"), C.QUALIFY_MAX_YES_CENTS, is_counting=False)
            if not q["qualified"]:
                continue
            outcome, label = self._place_one(m, word, event_ticker, event_date, expiry_epoch)
            words.append(label)
            if outcome == "taken":
                taken += 1
                placed += 1
            elif outcome == "placed":
                placed += 1
            elif outcome != "exists":
                rejected += 1
            time.sleep(0.15)

        rows = store.live_orders_for_day(event_date)
        live_rows = [r for r in rows if not _is_smoke_row(r)]
        total_live = len([r for r in live_rows if r.get("status") != "rejected"])
        total_rejected = len([r for r in live_rows if r.get("status") == "rejected"])
        collateral = total_live * per_market

        store.update_live_run(event_date, markets_seen=seen, orders_placed=total_live,
                              orders_rejected=total_rejected, collateral=collateral)
        STATE.update(active_event=event_ticker, active_date=event_date, orders_today=total_live)

        header = ("🧪 [LIVE dry_run] orders simulated" if C.LIVE_DRY_RUN
                 else "🎯 [LIVE $%g] orders working" % C.LIVE_DOLLARS_PER_WORD)
        if C.LIVE_SMOKE:
            header += " + 🔥 smoke %d" % C.LIVE_SMOKE_CONTRACTS
        take_bit = (", %d bought immediately (book already past our price)" % taken) if taken else ""
        notify.send(
            "%s\n%s\n%d placed%s, %d rejected of %d attempted\n"
            "sell YES %dc (= buy NO %dc) x %.2f contracts, $%.2f resting\ncancel %s CT%s\n\n"
            "%s" % (header, event_ticker, placed, take_bit, rejected, len(markets),
                    C.LIMIT_YES_CENTS, C.live_no_price_cents(), C.live_sized_contracts()["contracts"],
                    collateral, C.LIVE_CANCEL_CT,
                    " (server-side expiry set)" if expiry_epoch else "",
                    "\n".join("• " + w for w in words[:25]))
        )
        store.log_activity("live_place_all", "%s: %d placed / %d rejected of %d attempted"
                           % (event_ticker, placed, rejected, len(markets)))

    def _place_one(self, market: dict, word: str, event_ticker: str, event_date: str,
                   expiry_epoch: int | None) -> tuple:
        ticker = market["ticker"]
        title = word[:120]
        coid = client_order_id(event_date, ticker)
        if store.live_order_exists(coid):
            return "exists", "%s (already placed)" % title
        for existing in store.live_orders_for_day(event_date):
            if existing.get("market_ticker") != ticker:
                continue
            if existing.get("status") in ("rejected",):
                continue
            if _is_smoke_row(existing):
                continue
            log.info("live row already exists for %s (%s), skipping", ticker, existing.get("status"))
            return "exists", "%s (already placed)" % title

        sized = C.live_sized_contracts()
        no_price = C.live_no_price_cents()

        take_now = False
        if C.LIVE_TAKE_IF_ALREADY_CHEAP:
            try:
                book = self.pub.get_orderbook(ticker, depth=10)
                metrics = live_book_metrics(book, no_price)
                take_now = float(metrics.get("yes_size_that_would_fill_us") or 0) > 0
            except Exception as exc:
                log.debug("take-if-cheap book %s failed: %s", ticker, exc)
        post_only = bool(C.LIVE_POST_ONLY) and not take_now

        row = {
            "client_order_id": coid, "event_date": event_date, "event_ticker": event_ticker,
            "market_ticker": ticker, "word": word, "no_price_cents": no_price,
            "yes_price_cents": 100 - no_price, "contracts": sized["contracts"],
            "dollars": C.LIVE_DOLLARS_PER_WORD, "collateral": C.live_collateral_per_market(),
            "placed_at": datetime.now(timezone.utc), "mode": self.mode(),
            "dry_run": bool(C.LIVE_DRY_RUN), "post_only": bool(post_only),
            "took_at_open": bool(take_now), "expiration_epoch": expiry_epoch, "status": "resting",
        }

        if C.LIVE_DRY_RUN:
            row["status"] = "dry_run"
            store.record_live_order(**row)
            smoke_note = self._place_smoke(market, word, event_ticker, event_date, expiry_epoch, take_now, post_only)
            if take_now:
                return "taken", "%s (would buy now)%s" % (title, smoke_note)
            return "placed", "%s%s" % (title, smoke_note)

        try:
            resp = self.client.create_no_order(
                ticker=ticker, no_price_cents=no_price, count=sized["contracts"],
                client_order_id=coid, post_only=post_only, expiration_epoch=expiry_epoch,
            )
        except KalshiError as exc:
            reason = "%s: %s" % (exc.status, exc.body[:300])
            row["status"] = "rejected"
            row["reject_reason"] = reason
            store.record_live_order(**row)
            log.warning("live order rejected for %s -- %s", ticker, reason)
            return "rejected", "%s (rejected)" % title

        row["order_id"] = resp.get("order_id")
        filled_now = bool(resp.get("fill_count"))
        if filled_now:
            row["status"] = "filled"
            row["filled_contracts"] = resp["fill_count"]
            row["first_fill_at"] = datetime.now(timezone.utc)
            row["avg_fill_price_cents"] = resp.get("avg_fill_price_cents")
        store.record_live_order(**row)
        if take_now or filled_now:
            return "taken", "%s (bought immediately)" % title
        return "placed", title

    def _place_smoke(self, market: dict, word: str, event_ticker: str, event_date: str,
                     expiry_epoch: int | None, take_now: bool, post_only: bool) -> str:
        if not C.LIVE_SMOKE:
            return ""
        ticker = market["ticker"]
        coid = smoke_client_order_id(event_date, ticker)
        if store.live_order_exists(coid):
            return " [smoke already sent]"
        no_price = C.live_no_price_cents()
        row = {
            "client_order_id": coid, "event_date": event_date, "event_ticker": event_ticker,
            "market_ticker": ticker, "word": word, "no_price_cents": no_price,
            "yes_price_cents": 100 - no_price, "contracts": float(C.LIVE_SMOKE_CONTRACTS),
            "dollars": C.LIVE_SMOKE_CONTRACTS * no_price / 100.0,
            "collateral": C.LIVE_SMOKE_CONTRACTS * no_price / 100.0,
            "placed_at": datetime.now(timezone.utc), "mode": "smoke", "dry_run": False,
            "post_only": bool(post_only), "took_at_open": bool(take_now),
            "expiration_epoch": expiry_epoch, "status": "resting",
        }
        try:
            resp = self.client.create_no_order(
                ticker=ticker, no_price_cents=no_price, count=int(C.LIVE_SMOKE_CONTRACTS),
                client_order_id=coid, post_only=post_only, expiration_epoch=expiry_epoch,
            )
        except KalshiError as exc:
            reason = "%s: %s" % (exc.status, exc.body[:300])
            row["status"] = "rejected"
            row["reject_reason"] = reason
            store.record_live_order(**row)
            notify.send("🔥 [LIVE smoke] rejected: %s\n%s" % (word, reason[:200]))
            return " [smoke REJECTED]"
        row["order_id"] = resp.get("order_id")
        filled_now = bool(resp.get("fill_count"))
        if filled_now:
            row["status"] = "filled"
            row["filled_contracts"] = resp["fill_count"]
            row["first_fill_at"] = datetime.now(timezone.utc)
            row["avg_fill_price_cents"] = resp.get("avg_fill_price_cents")
        store.record_live_order(**row)
        return " [smoke LIVE TAKEN]" if filled_now else " [smoke LIVE resting]"

    def _balance_ok(self, needed: float) -> bool:
        try:
            balance = self.client.get_balance()
        except Exception as exc:
            notify.send("⚠️ [LIVE] Could not read Kalshi balance: %s\nNo orders placed." % str(exc)[:200])
            return False
        cash = (balance.get("balance") or 0) / 100.0
        if cash < needed:
            notify.send("🛑 [LIVE] Not enough cash: need $%.2f resting, have $%.2f. No orders placed."
                        % (needed, cash))
            return False
        return True

    # ---------------------------------------------------------------- fills
    def poll_fills(self, event_date: str) -> None:
        if C.LIVE_DRY_RUN and not C.LIVE_SMOKE:
            return  # nothing real was sent; the paper c1755 variant is the fill proxy for dry_run
        try:
            recent = self.client.get_fills(limit=200)
        except Exception as exc:
            log.warning("live fill poll failed: %s", exc)
            return

        known = {o["market_ticker"]: o for o in store.live_orders_for_day(event_date)}
        for fill in recent:
            ticker = fill.get("ticker") or fill.get("market_ticker")
            if ticker not in known:
                continue
            from .kalshi import _to_cents, _to_count
            count = _to_count(fill.get("count_fp") or fill.get("count"))
            price = _to_cents(fill.get("no_price_dollars") or fill.get("no_price") or fill.get("price"))
            if price is None and fill.get("yes_price_dollars") is not None:
                yes_px = _to_cents(fill.get("yes_price_dollars"))
                if yes_px is not None:
                    price = max(1, 100 - yes_px)
            if price is None:
                price = C.live_no_price_cents()
            fee = _to_cents(fill.get("fee_cost") or fill.get("fee_paid")) or 0
            if count <= 0:
                continue
            fill_id = str(fill.get("trade_id") or fill.get("fill_id")
                         or "%s-%s-%s" % (ticker, fill.get("created_time"), count))[:64]
            is_new = store.record_live_fill(
                fill_id=fill_id, order_id=fill.get("order_id"), event_date=event_date,
                market_ticker=ticker, contracts=count, price_cents=price,
                is_taker=bool(fill.get("is_taker")), fee_cents=fee,
                created_at=clock.parse_dt(fill.get("created_time")) or clock.now_utc(),
                raw=str(fill)[:4000],
            )
            if not is_new:
                continue
            order = known[ticker]
            already = float(order.get("filled_contracts") or 0)
            total = already + count
            prev_px = float(order.get("avg_fill_price_cents") or price)
            avg_px = ((already * prev_px) + (count * price)) / total if total else price
            store.update_live_order(
                order["client_order_id"],
                status="filled" if total >= float(order.get("contracts") or 0) else "resting",
                filled_contracts=total,
                first_fill_at=order.get("first_fill_at") or clock.now_utc(),
                avg_fill_price_cents=avg_px, fees_cents=(order.get("fees_cents") or 0) + fee,
            )
            known[ticker]["filled_contracts"] = total
            STATE["fills_today"] += 1
            tag = "🔥 [LIVE smoke] " if _is_smoke_row(order) else "✅ [LIVE] "
            taker_flag = " ⚠️ TAKER FILL" if fill.get("is_taker") else ""
            notify.send("%sFilled: %s\n%g contracts NO @ %d¢%s"
                        % (tag, order.get("word") or ticker, count, price, taker_flag))

    # ---------------------------------------------------------------- cancel
    def cancel_all(self, event_date: str | None = None, reason: str = "scheduled") -> dict:
        event_date = event_date or clock.today_ct()
        summary = {"attempted": 0, "cancelled": 0, "remaining": 0, "verified": False}

        if C.LIVE_DRY_RUN and not C.LIVE_SMOKE:
            n = store.mark_all_live_resting_cancelled(event_date)
            summary.update(attempted=n, cancelled=n, verified=True)
            store.update_live_run(event_date, cancelled_at=datetime.now(timezone.utc), cancel_verified=True)
            STATE["cancelled_today"] = True
            self._cancel_report(event_date, summary, reason)
            return summary

        if C.LIVE_DRY_RUN and C.LIVE_SMOKE:
            store.mark_all_live_resting_cancelled(event_date)

        for attempt in range(3):
            try:
                resting = self.client.get_resting_orders(series_prefix=C.SERIES)
            except Exception as exc:
                log.error("live: could not list resting orders: %s", exc)
                notify.send("🚨 [LIVE] CANCEL PROBLEM: could not list resting orders: %s\nRetrying."
                            % str(exc)[:200])
                time.sleep(3)
                continue
            if not resting:
                summary["verified"] = True
                break
            ids = [o["order_id"] for o in resting if o.get("order_id")]
            summary["attempted"] += len(ids)
            ok, failed = self.client.batch_cancel(ids)
            summary["cancelled"] += ok
            time.sleep(2)

        try:
            leftover = self.client.get_resting_orders(series_prefix=C.SERIES)
            summary["remaining"] = len(leftover)
            summary["verified"] = len(leftover) == 0
        except Exception as exc:
            summary["verified"] = False
            log.error("live: cancel verification failed: %s", exc)

        store.mark_all_live_resting_cancelled(event_date)
        store.update_live_run(event_date, cancelled_at=datetime.now(timezone.utc),
                              cancel_verified=summary["verified"])
        STATE["cancelled_today"] = True
        self._cancel_report(event_date, summary, reason)
        return summary

    def _cancel_report(self, event_date: str, summary: dict, reason: str) -> None:
        if summary["verified"]:
            head = "🛑 [LIVE] All orders cancelled"
        else:
            head = "🚨🚨 [LIVE] CANCEL NOT VERIFIED -- %d STILL RESTING. Go to Kalshi and cancel by hand NOW." % summary["remaining"]
        notify.send("%s\nTrigger: %s at %s\nCancelled %d of %d attempted"
                    % (head, reason, clock.fmt(clock.now_utc()), summary["cancelled"], summary["attempted"]))
        store.log_activity("live_cancel_all", "%s: cancelled=%d remaining=%d verified=%s"
                           % (reason, summary["cancelled"], summary["remaining"], summary["verified"]))

    # ---------------------------------------------------------------- loop
    def stop(self) -> None:
        self._stop = True

    def run_forever(self) -> None:
        self._stop = False
        STATE["running"] = True
        notify.send("🤖 [LIVE] engine started\n%s" % C.live_summary())
        store.log_activity("live_start", C.live_summary())
        while not getattr(self, "_stop", False):
            try:
                self._tick()
            except Exception as exc:
                log.exception("live loop error")
                STATE["last_error"] = str(exc)[:300]
                store.log_activity("live_loop_error", str(exc)[:1000])
                notify.send("⚠️ [LIVE] engine error: %s" % str(exc)[:300])
                time.sleep(60)
        STATE["running"] = False

    def _tick(self) -> None:
        now = clock.now_ct()
        today = clock.today_ct()
        STATE["last_poll"] = now

        if STATE["active_date"] and STATE["active_date"] != today:
            STATE.update(active_event=None, active_date=None, orders_today=0,
                         fills_today=0, cancelled_today=False)

        # Kalshi's own server-side expiry (baked into each order) fires at LIVE_CANCEL_CT itself.
        # This in-app backup deliberately fires a bit LATER (live_app_cancel_at), so it never races
        # Kalshi's own engine over the same instant -- it's a check that the real thing worked, not
        # a second copy of it.
        app_deadline = clock.live_app_cancel_at(today)

        if not STATE["active_event"]:
            run = store.get_live_run(today)
            if run and (run.get("orders_placed") or 0) > 0 and not run.get("cancelled_at"):
                STATE.update(active_event=run.get("event_ticker"), active_date=today,
                             orders_today=run.get("orders_placed") or 0)
                store.log_activity("live_resume", "recovered %s after a restart" % today)

        if STATE["active_event"]:
            if now >= app_deadline:
                if STATE.get("cancelled_today"):
                    STATE.update(active_event=None, active_date=None)
                    time.sleep(C.LIVE_COLD_POLL_SECONDS)
                    return
                self.cancel_all(STATE["active_date"],
                                reason="in-app backup cancel (+%ds after %s CT server-expiry)"
                                       % (C.LIVE_APP_CANCEL_BUFFER_SECONDS, C.LIVE_CANCEL_CT))
                STATE.update(active_event=None, active_date=None)
                time.sleep(C.LIVE_COLD_POLL_SECONDS)
                return
            self.poll_fills(STATE["active_date"])
            time.sleep(min(C.LIVE_POLL_SECONDS, max(5, clock.loop_interval(now))))
            return

        found = self.find_todays_event()
        if found:
            event_ticker, event_date = found
            if now >= clock.live_cancel_at(event_date):
                store.claim_live_run({"event_date": event_date, "event_ticker": event_ticker,
                                      "status": "skipped", "mode": self.mode(),
                                      "detected_at": datetime.now(timezone.utc),
                                      "notes": "appeared after cancel time"})
                time.sleep(C.LIVE_COLD_POLL_SECONDS)
                return
            self.place_all(event_ticker, event_date)
            return

        time.sleep(C.LIVE_DETECT_POLL_SECONDS if (11 <= now.hour < 18) else C.LIVE_COLD_POLL_SECONDS)


_runner: LiveRunner | None = None
_lock = threading.Lock()


def enabled() -> bool:
    """Whether the live engine should run at all: only if a Kalshi key is configured. A missing
    key is not an error -- it just means this deploy has not been set up for real trading yet."""
    return bool(C.KALSHI_KEY_ID and (C.KALSHI_PRIVATE_KEY_PEM or C.KALSHI_PRIVATE_KEY_PATH))


def runner() -> LiveRunner:
    global _runner
    with _lock:
        if _runner is None:
            _runner = LiveRunner()
        return _runner


def status_text() -> str:
    if not enabled():
        return "[LIVE] disabled: no Kalshi key configured in Secrets."
    paused = "yes" if store.get_state("live_paused", False) else "no"
    return ("%s\npaused=%s | active_event=%s | orders_today=%s | fills_today=%s | last_error=%s"
           % (C.live_summary(), paused, STATE.get("active_event"), STATE.get("orders_today"),
              STATE.get("fills_today"), STATE.get("last_error")))


def register_commands() -> None:
    notify.register("live_status", lambda a: status_text())
    notify.register("live_pause", lambda a: (store.set_state("live_paused", True),
                                             "⏸ [LIVE] paused: no new real orders until /live_resume")[1])
    notify.register("live_resume", lambda a: (store.set_state("live_paused", False), "▶️ [LIVE] resumed")[1])
    notify.register("live_cancel_now", lambda a: str(runner().cancel_all(reason="manual /live_cancel_now")))
    notify.register("live_help", lambda a: "/live_status /live_pause /live_resume /live_cancel_now")


def run_forever() -> None:
    if not enabled():
        log.info("live engine disabled: no KALSHI_KEY_ID / private key configured")
        return
    runner().run_forever()
