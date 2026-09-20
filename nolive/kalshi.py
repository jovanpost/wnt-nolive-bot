"""Kalshi PUBLIC read-only client. No key, no signing, no order calls.

Prices come back in DOLLARS ("0.5500") on the current API and in whole cents on the old one.
Everything here is converted to CENTS as floats (55.0), so half-cent prices are not rounded away.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any

import requests

from . import config as C

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
