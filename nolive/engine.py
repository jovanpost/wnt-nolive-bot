"""The trading rules, as plain functions (no database, no network, easy to test).

THE ORDER: SELL YES at LIMIT cents (55c). Same thing as BUY NO at 100 - LIMIT (45c).
  - If YES buyers are ALREADY bidding LIMIT or more when we send it, we match them right away.
    We are the TAKER: we sell at THEIR price (better than our limit) and pay the taker fee.
  - Whatever is left RESTS. It fills when a YES buyer pays MORE than our limit later.
    We are the MAKER: we sell at OUR limit and pay no fee (on this series).
Only what fills counts. Money is settled from Kalshi's official yes/no result.
"""
from __future__ import annotations

from . import fees

EPS = 1e-9


def contracts_for(dollars: float, limit_yes_cents: float) -> float:
    """Dollars of collateral per word -> contracts. Collateral per contract = 100 - limit (cents)."""
    per = (100.0 - limit_yes_cents) / 100.0
    return round(float(dollars) / per, 2)


# ---------------------------------------------------------------- who qualifies
def qualify(last, bid, ask, status, skip_at: float) -> dict:
    """The rule: YES price below `skip_at` (1c to 97c). Words at 98c+ are already said.

    YES price = last trade price. If there has been no trade, fall back to the best YES bid, then the ask.
    A best YES bid at `skip_at` or more also disqualifies (the book says the word is said).
    """
    out = {"qualified": False, "reason": "", "price": None, "basis": "none"}
    st = str(status or "").lower()
    if st and st not in ("active", "open"):
        out["reason"] = "market not active (%s)" % st
        return out
    if last is not None and last > 0:
        out["price"], out["basis"] = last, "last"
    elif bid is not None and bid > 0:
        out["price"], out["basis"] = bid, "bid"
    elif ask is not None and ask > 0:
        out["price"], out["basis"] = ask, "ask"
    if out["price"] is None:
        out["reason"] = "no price"
        return out
    if out["price"] >= skip_at - EPS:
        out["reason"] = "YES %gc >= %gc" % (out["price"], skip_at)
        return out
    if bid is not None and bid >= skip_at - EPS:
        out["reason"] = "YES bid %gc >= %gc" % (bid, skip_at)
        return out
    out["qualified"] = True
    return out


# ---------------------------------------------------------------- the book
def book_summary(book: dict, limit_yes: float) -> dict:
    yes = book.get("yes") or []
    no = book.get("no") or []
    return {
        "best_yes_bid": yes[-1][0] if yes else None,
        "best_no_bid": no[-1][0] if no else None,
        "yes_size_total": sum(c for _, c in yes),
        "no_size_total": sum(c for _, c in no),
        # YES bids we would match on arrival (sell YES at limit meets bids >= limit)
        "yes_size_at_limit": sum(c for p, c in yes if p >= limit_yes - EPS),
        # NO bids at >= 100 - limit are YES asks at <= limit: they sit ahead of our resting ask
        "queue_ahead": sum(c for p, c in no if p >= (100.0 - limit_yes) - EPS),
    }


def taker_fills(book: dict, limit_yes: float, want: float, multiplier: float = 1.0) -> list:
    """Match YES bids at or above the limit, best (highest) bid first. Each price level = one fill."""
    fills = []
    remaining = float(want)
    for price, size in sorted(book.get("yes") or [], key=lambda x: -x[0]):
        if price < limit_yes - EPS or remaining <= EPS:
            break
        take = min(remaining, float(size))
        if take <= EPS:
            continue
        take = round(take, 4)
        fills.append({
            "kind": "taker", "source": "book", "price_cents": float(price), "contracts": take,
            "fee_cents": fees.taker_fee_cents(take, price, multiplier),
            "ref": "book@%g" % price,
        })
        remaining -= take
    return fills


def maker_fills(trades: list, after_ts, until_ts, limit_yes: float, remaining: float, maker_rate: float = 0.0) -> list:
    """Resting fills from the public trade tape.

    A trade counts only if it happened after we placed the order (and before the last cancel), a YES buyer
    was the taker, and the price was ABOVE our limit (so the buyer had to go through our level first).
    We take min(what is left, the trade size) at OUR limit price.
    """
    fills = []
    left = float(remaining)
    for t in sorted(trades, key=lambda x: x["ts"]):
        if left <= EPS:
            break
        if t["ts"] <= after_ts or t["ts"] > until_ts:
            continue
        if t.get("taker_side") != "yes":
            continue
        if t["yes_cents"] <= limit_yes + EPS:
            continue
        take = round(min(left, float(t["count"])), 4)
        if take <= EPS:
            continue
        fills.append({
            "kind": "maker", "source": "trade", "ts": t["ts"], "price_cents": float(limit_yes), "contracts": take,
            "fee_cents": fees.maker_fee_cents(take, limit_yes, maker_rate),
            "ref": "trade:%s" % t["id"],
        })
        left -= take
    return fills


# ---------------------------------------------------------------- one cancel time = a cut-off on the fills
def aggregate(fills: list, cancel_at) -> dict:
    """Totals for one version (cancel time): only fills at or before its cancel time count."""
    tk = mk = 0.0
    tf = mf = 0.0
    proceeds = risk = 0.0
    for f in fills:
        if f["ts"] > cancel_at:
            continue
        n, p = float(f["contracts"]), float(f["price_cents"])
        if f["kind"] == "taker":
            tk += n
            tf += float(f["fee_cents"])
        else:
            mk += n
            mf += float(f["fee_cents"])
        proceeds += n * p
        risk += n * (100.0 - p)
    return {
        "taker_contracts": round(tk, 4), "maker_contracts": round(mk, 4),
        "filled_contracts": round(tk + mk, 4),
        "taker_fee_cents": round(tf, 4), "maker_fee_cents": round(mf, 4),
        "proceeds_cents": round(proceeds, 4), "risk_cents": round(risk, 4),
    }


def status_for(agg: dict, intended: float, cancel_at, now) -> str:
    filled = agg["filled_contracts"]
    open_ = now <= cancel_at
    if filled >= intended - 1e-6:
        return "filled"
    if open_:
        return "partial · resting" if filled > 0 else "resting"
    return "partial · cancelled" if filled > 0 else "unfilled"


def settle(fills: list, cancel_at, result: str) -> dict:
    """Money in cents. We SOLD YES at price p: word not said (result 'no') -> we keep p;
    word said (result 'yes') -> we owe $1 and lose (100 - p). Fees always come off."""
    if result == "void":
        return {"taker_pnl_cents": 0.0, "maker_pnl_cents": 0.0, "pnl_cents": 0.0}
    parts = {"taker": 0.0, "maker": 0.0}
    for f in fills:
        if f["ts"] > cancel_at:
            continue
        n, p = float(f["contracts"]), float(f["price_cents"])
        gross = n * p if result == "no" else -n * (100.0 - p)
        parts[f["kind"]] += gross - float(f["fee_cents"])
    return {
        "taker_pnl_cents": round(parts["taker"], 4),
        "maker_pnl_cents": round(parts["maker"], 4),
        "pnl_cents": round(parts["taker"] + parts["maker"], 4),
    }
