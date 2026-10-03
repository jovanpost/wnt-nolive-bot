"""Kalshi clients.

KalshiPublic: read-only, no key, no signing, no order calls. Everything the paper bot uses.
KalshiClient: signed requests + order placement, used ONLY by nolive/live.py (real money). Ported
from wnt-nofade-bot's wnt/kalshi.py -- same signing scheme, same order body, same idempotent
client_order_id contract, so a client_order_id Kalshi has already seen is refused as a duplicate.

Prices come back in DOLLARS ("0.5500") on the current API and in whole cents on the old one.
Everything here is converted to CENTS as floats (55.0), so half-cent prices are not rounded away.
"""
from __future__ import annotations

import base64
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from . import config as C, lease

log = logging.getLogger("nolive.kalshi")


class KalshiError(RuntimeError):
    def __init__(self, status: int, body: str, endpoint: str):
        self.status = status
        self.body = body
        self.endpoint = endpoint
        super().__init__("%s -> %s: %s" % (endpoint, status, body[:300]))


def _f(value: Any):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def dollars_to_cents(value: Any):
    v = _f(value)
    return None if v is None else round(v * 100.0, 4)


def pick_cents(obj: dict, dollars_key: str, cents_key: str):
    """Read a price from a dict: the *_dollars field if present, else the old cents field."""
    if obj.get(dollars_key) not in (None, ""):
        return dollars_to_cents(obj.get(dollars_key))
    v = _f(obj.get(cents_key))
    return v


def parse_ts(raw) -> datetime | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (int, float)):
        try:
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        except Exception:
            return None
    s = str(raw).strip().replace("Z", "+00:00")
    # Python 3.9 fromisoformat wants exactly 3 or 6 fraction digits: force 6.
    s = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], s, count=1)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def market_result(market: dict) -> str | None:
    """Official YES/NO from Kalshi only. Never inferred from a price."""
    for key in ("result", "settlement_result", "market_result"):
        raw = str(market.get(key) or "").strip().lower()
        if raw in ("yes", "no", "void"):
            return raw
    return None


def market_prices(market: dict) -> tuple:
    """(last_price, yes_bid, yes_ask) in cents (floats) or None."""
    last = pick_cents(market, "last_price_dollars", "last_price")
    bid = pick_cents(market, "yes_bid_dollars", "yes_bid")
    ask = pick_cents(market, "yes_ask_dollars", "yes_ask")
    return last, bid, ask


_COUNT_RE = re.compile(r"(\d+)\s*\+")


def word_from_market(market: dict) -> str:
    for key in ("yes_sub_title", "no_sub_title", "subtitle"):
        raw = market.get(key)
        if isinstance(raw, str) and raw.strip() and len(raw.strip()) < 100:
            return re.sub(r"\s+", " ", raw.strip())
    strike = market.get("custom_strike")
    if isinstance(strike, dict) and strike:
        val = list(strike.values())[0]
        if isinstance(val, str) and val.strip():
            return re.sub(r"\s+", " ", val.strip())
    title = (market.get("title") or "").strip()
    return title or str(market.get("ticker") or "unknown")


def count_needed(word: str) -> int:
    """'Iran (3+ times)' -> 3. A plain word -> 1."""
    m = _COUNT_RE.search(word or "")
    return int(m.group(1)) if m else 1


class KalshiPublic:
    def __init__(self, base_url: str | None = None):
        self.base = (base_url or C.KALSHI_BASE).rstrip("/") + C.API_ROOT
        self._local = threading.local()

    def _session(self) -> requests.Session:
        s = getattr(self._local, "session", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": C.USER_AGENT, "Accept": "application/json"})
            self._local.session = s
        return s

    def get(self, endpoint: str, params: dict | None = None, retries: int = 3, timeout: int = 15) -> dict:
        url = self.base + endpoint
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._session().get(url, params=params, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if attempt >= retries:
                    raise
                time.sleep(min(0.4 * (2 ** attempt), 4) + random.random() * 0.2)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = KalshiError(resp.status_code, resp.text, endpoint)
                if attempt >= retries:
                    raise last
                time.sleep(min(0.4 * (2 ** attempt), 4) + random.random() * 0.2)
                continue
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text, endpoint)
            if not resp.text:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}
        raise last or RuntimeError("unreachable")

    def paginate(self, endpoint: str, key: str, params: dict | None = None, max_pages: int = 10) -> list:
        out: list = []
        cursor = None
        for _ in range(max_pages):
            p = dict(params or {})
            if cursor:
                p["cursor"] = cursor
            data = self.get(endpoint, params=p)
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    # ---- events / markets / series ----
    def get_events(self, series_ticker: str, status: str = "open") -> list:
        return self.paginate("/events", "events", {"series_ticker": series_ticker, "status": status, "limit": 200})

    def get_markets(self, event_ticker: str) -> list:
        return self.paginate("/markets", "markets", {"event_ticker": event_ticker, "limit": 200})

    def get_market(self, ticker: str) -> dict:
        return (self.get("/markets/%s" % ticker) or {}).get("market", {}) or {}

    def get_series(self, series_ticker: str) -> dict:
        data = self.get("/series/%s" % series_ticker) or {}
        return data.get("series", data) or {}

    # ---- order book: {"yes": [(cents, count), ...], "no": [...]} sorted ascending, best bid LAST ----
    def get_orderbook(self, ticker: str, depth: int = 15) -> dict:
        raw = self.get("/markets/%s/orderbook" % ticker, params={"depth": depth}) or {}
        book = raw.get("orderbook_fp") or raw.get("orderbook") or {}
        out: dict = {"yes": [], "no": []}
        for side in ("yes", "no"):
            dollars = book.get(side + "_dollars")
            levels = dollars if dollars is not None else book.get(side)
            in_dollars = dollars is not None
            parsed = []
            for level in levels or []:
                try:
                    price = _f(level[0])
                    count = _f(level[1]) or 0.0
                except (IndexError, TypeError):
                    continue
                if price is None:
                    continue
                cents = round(price * 100.0, 4) if in_dollars else price
                parsed.append((cents, count))
            parsed.sort(key=lambda x: x[0])
            out[side] = parsed
        return out

    # ---- trades: newest first from Kalshi; returned OLDEST first ----
    def get_trades(self, ticker: str, min_ts: int | None = None, limit: int = 1000, max_pages: int = 3) -> list:
        params: dict = {"ticker": ticker, "limit": limit}
        if min_ts:
            params["min_ts"] = int(min_ts)
        raw = self.paginate("/markets/trades", "trades", params, max_pages=max_pages)
        out = []
        for t in raw:
            ts = parse_ts(t.get("created_time") or t.get("ts"))
            price = pick_cents(t, "yes_price_dollars", "yes_price")
            count = _f(t.get("count_fp"))
            if count is None:
                count = _f(t.get("count"))
            if ts is None or price is None or count is None:
                continue
            out.append({
                "id": str(t.get("trade_id") or "%s|%s|%s" % (ticker, t.get("created_time"), price)),
                "ts": ts, "yes_cents": price, "count": count,
                "taker_side": str(t.get("taker_side") or "").lower(),
            })
        out.sort(key=lambda x: x["ts"])
        return out


# ==================================================================
# SIGNED client (LIVE money). Only nolive/live.py imports this.
# Reuses the same KalshiError class defined above (both clients raise it).
# ==================================================================
def _to_cents(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        d = Decimal(str(value))
    except Exception:
        return None
    if d != d.to_integral_value() or (0 < d < 1):
        return int((d * 100).to_integral_value())
    return int(d)


def _to_count(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        return float(Decimal(str(value)))
    except Exception:
        return 0.0


class KalshiClient:
    """Signed requests. Reads KALSHI_KEY_ID / KALSHI_PRIVATE_KEY_PEM (or _PATH) from config."""

    def __init__(self, key_id: str | None = None, private_key_pem: str | None = None,
                 private_key_path: str | None = None, base_url: str | None = None):
        self.key_id = key_id if key_id is not None else C.KALSHI_KEY_ID
        self.base_url = (base_url or C.LIVE_BASE_URL).rstrip("/")
        self._key = self._load_key(
            private_key_pem if private_key_pem is not None else C.KALSHI_PRIVATE_KEY_PEM,
            private_key_path if private_key_path is not None else C.KALSHI_PRIVATE_KEY_PATH,
        )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": C.LIVE_USER_AGENT})

    @staticmethod
    def _load_key(pem_text: str, pem_path: str):
        raw = None
        if pem_text and "BEGIN" in pem_text:
            raw = pem_text.replace("\\n", "\n").strip().encode()
        elif pem_path:
            try:
                with open(pem_path, "rb") as fh:
                    raw = fh.read()
            except OSError as exc:
                log.warning("could not read private key at %s: %s", pem_path, exc)
        if raw is None:
            return None
        try:
            return serialization.load_pem_private_key(raw, password=None)
        except Exception as exc:
            log.error("private key failed to parse: %s", exc)
            return None

    @property
    def authenticated(self) -> bool:
        return self._key is not None and bool(self.key_id)

    def _headers(self, method: str, sign_path: str) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if not self.authenticated:
            return headers
        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + sign_path.split("?")[0]).encode()
        signature = self._key.sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        headers.update({
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        })
        return headers

    def request(self, method: str, endpoint: str, params: dict | None = None, body: dict | None = None,
                auth: bool = True, retries: int = 4, timeout: int = 20) -> dict:
        sign_path = C.LIVE_API_ROOT + endpoint
        url = self.base_url + sign_path
        last: Exception | None = None
        for attempt in range(retries + 1):
            headers = self._headers(method, sign_path) if auth else {
                "Content-Type": "application/json", "User-Agent": C.LIVE_USER_AGENT}
            try:
                resp = self.session.request(method, url, params=params, json=body,
                                            headers=headers, timeout=timeout)
            except requests.RequestException as exc:
                last = exc
                if attempt >= retries:
                    raise
                time.sleep(min(2 ** attempt, 8) + random.random())
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = KalshiError(resp.status_code, resp.text, endpoint)
                if attempt >= retries:
                    raise last
                time.sleep(min(0.25 * (2 ** attempt), 5) + random.random() * 0.25)
                continue
            if resp.status_code >= 400:
                raise KalshiError(resp.status_code, resp.text, endpoint)
            if not resp.text:
                return {}
            try:
                return resp.json()
            except ValueError:
                return {"raw": resp.text}
        raise last or RuntimeError("unreachable")

    def paginate(self, endpoint: str, key: str, params: dict | None = None,
                 auth: bool = True, max_pages: int = 20) -> list:
        out: list = []
        cursor = None
        for _ in range(max_pages):
            page_params = dict(params or {})
            if cursor:
                page_params["cursor"] = cursor
            data = self.request("GET", endpoint, params=page_params, auth=auth)
            out.extend(data.get(key) or [])
            cursor = data.get("cursor")
            if not cursor:
                break
        return out

    def get_balance(self) -> dict:
        return self.request("GET", "/portfolio/balance")

    def get_resting_orders(self, series_prefix: str | None = None) -> list:
        orders = self.paginate("/portfolio/orders", "orders", {"status": "resting", "limit": 200})
        if series_prefix:
            orders = [o for o in orders if str(o.get("ticker", "")).startswith(series_prefix)]
        return orders

    def get_fills(self, ticker: str | None = None, limit: int = 200) -> list:
        params: dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        return self.paginate("/portfolio/fills", "fills", params)

    def create_no_order(self, ticker: str, no_price_cents: int, count: float, client_order_id: str,
                        post_only: bool = True, expiration_epoch: int | None = None) -> dict:
        """SELL YES at (100 - no_price_cents) = BUY NO at no_price_cents. Same body wnt-nofade-bot sends."""
        lease.require("send a real order")   # only the place that holds the worker lease may send
        yes_price = 100 - int(no_price_cents)
        body = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": "ask",
            "count": "%.2f" % float(count),
            "price": "%.4f" % (yes_price / 100),
            "time_in_force": "good_till_canceled",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": bool(post_only),
            "cancel_order_on_pause": True,
            "reduce_only": False,
        }
        if expiration_epoch:
            body["expiration_time"] = int(expiration_epoch)
        resp = self.request("POST", "/portfolio/events/orders", body=body)
        return {
            "order_id": resp.get("order_id"),
            "client_order_id": resp.get("client_order_id") or client_order_id,
            "fill_count": _to_count(resp.get("fill_count")),
            "remaining_count": _to_count(resp.get("remaining_count")),
            "avg_fill_price_cents": _to_cents(resp.get("average_fill_price")),
            "fee_cents": _to_cents(resp.get("average_fee_paid")),
            "raw": resp,
        }

    def cancel_order(self, order_id: str) -> bool:
        for endpoint in ("/portfolio/events/orders/%s" % order_id, "/portfolio/orders/%s" % order_id):
            try:
                self.request("DELETE", endpoint, retries=3)
                return True
            except KalshiError as exc:
                if exc.status == 404:
                    return True
                log.warning("cancel via %s failed: %s", endpoint, exc)
        return False

    def batch_cancel(self, order_ids: list) -> tuple:
        if not order_ids:
            return 0, []
        try:
            resp = self.request("DELETE", "/portfolio/events/orders/batched",
                                body={"orders": [{"order_id": oid} for oid in order_ids]}, retries=3)
            failed = []
            ok = 0
            for entry in resp.get("orders", []):
                if entry.get("error"):
                    failed.append(entry.get("order_id"))
                else:
                    ok += 1
            if ok or not failed:
                return ok, [f for f in failed if f]
        except KalshiError as exc:
            log.warning("batch cancel failed (%s), falling back to singles", exc)
        ok, failed = 0, []
        for oid in order_ids:
            if self.cancel_order(oid):
                ok += 1
            else:
                failed.append(oid)
            time.sleep(0.05)
        return ok, failed


def live_book_metrics(book: dict, our_no_cents: int) -> dict:
    """Same shape as engine.book_summary, plus the fields live.py's take-if-cheap check wants."""
    yes = book.get("yes") or []
    no = book.get("no") or []
    yes_trigger = 100 - our_no_cents
    return {
        "best_yes_bid": yes[-1][0] if yes else None,
        "best_no_bid": no[-1][0] if no else None,
        "yes_size_total": sum(c for _, c in yes),
        "no_size_total": sum(c for _, c in no),
        "no_size_ahead": sum(c for p, c in no if p > our_no_cents),
        "no_size_at_our_price": sum(c for p, c in no if p == our_no_cents),
        "yes_size_that_would_fill_us": sum(c for p, c in yes if p >= yes_trigger),
    }
