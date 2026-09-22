"""Scoreboards from the nolive_* tables. Plain dicts in, plain dicts / DataFrames out."""
from __future__ import annotations

from . import config as C

# v2: qualifying is now capped at 30c, so the old 0-98c buckets would mostly sit empty. Finer resolution
# inside the zone the backtest actually found an edge in.
BUCKETS = [(0, 5), (5, 10), (10, 15), (15, 20), (20, 25), (25, 31)]


def _f(x, default=0.0):
    try:
        return float(x) if x is not None else default
    except (TypeError, ValueError):
        return default


def settled(orders: list) -> list:
    return [o for o in orders if o.get("pnl_cents") is not None]


def variant_rows(orders: list) -> list:
    """One row per cancel version, over every settled night."""
    rows = []
    for v in C.VARIANTS:
        mine = [o for o in orders if o["variant_id"] == v["id"]]
        done = settled(mine)
        pending_nights = len(set(o["event_date"] for o in mine if o.get("pnl_cents") is None))
        nights = {}
        for o in done:
            nights[o["event_date"]] = nights.get(o["event_date"], 0.0) + _f(o["pnl_cents"])
        filled = [o for o in done if _f(o["filled_contracts"]) > 0]
        wins = [o for o in filled if o.get("result") == "no"]
        risk = sum(_f(o["risk_cents"]) for o in done)
        net = sum(_f(o["pnl_cents"]) for o in done)
        night_vals = list(nights.values())
        rows.append({
            "variant_id": v["id"], "cancel": v["label"],
            "nights": len(nights), "pending_nights": pending_nights,
            "words_ordered": len(done), "words_filled": len(filled),
            "fill_rate": (100.0 * len(filled) / len(done)) if done else None,
            "taker_ct": sum(_f(o["taker_contracts"]) for o in done),
            "maker_ct": sum(_f(o["maker_contracts"]) for o in done),
            "fees": (sum(_f(o["taker_fee_cents"]) + _f(o["maker_fee_cents"]) for o in done)) / 100.0,
            "taker_pnl": sum(_f(o["taker_pnl_cents"]) for o in done) / 100.0,
            "maker_pnl": sum(_f(o["maker_pnl_cents"]) for o in done) / 100.0,
            "net": net / 100.0,
            "risk": risk / 100.0,
            "roi": (100.0 * net / risk) if risk > 0 else None,
            "win_rate": (100.0 * len(wins) / len(filled)) if filled else None,
            "per_night": (sum(night_vals) / len(night_vals) / 100.0) if night_vals else None,
            "best_night": (max(night_vals) / 100.0) if night_vals else None,
            "worst_night": (min(night_vals) / 100.0) if night_vals else None,
        })
    return rows



def word_lines(orders: list, event_date: str, variant_id: str):
    """Per-word settlement detail for one night and one cancel version, no-fade style.
    Returns (filled_orders, skipped_count, bullet_lines)."""
    mine = [o for o in orders if o["event_date"] == event_date and o["variant_id"] == variant_id and o.get("pnl_cents") is not None]
    filled = [o for o in mine if _f(o["filled_contracts"]) > 0]
    lines = []
    for o in sorted(filled, key=lambda x: (x.get("word") or x["market_ticker"])):
        n = _f(o["filled_contracts"])
        no_price = (_f(o["risk_cents"]) / n) if n else 0.0     # NO-equivalent price actually paid, in cents
        lines.append("• %s: %s %.2f@%.1f\u00a2 $%+.2f" % (
            o.get("word") or o["market_ticker"], (o.get("result") or "?").upper(), n, no_price, _f(o["pnl_cents"]) / 100.0))
    return filled, len(mine) - len(filled), lines

def best_of(rows: list) -> dict:
    live = [r for r in rows if r["nights"] > 0]
    if not live:
        return {}
    by_net = max(live, key=lambda r: r["net"])
    with_roi = [r for r in live if r["roi"] is not None]
    by_roi = max(with_roi, key=lambda r: r["roi"]) if with_roi else None
    return {"by_net": by_net, "by_roi": by_roi, "nights": max(r["nights"] for r in live)}


def night_rows(orders: list) -> list:
    """One row per night per version (settled only), for the cumulative chart."""
    out = {}
    for o in settled(orders):
        key = (o["event_date"], o["variant_id"])
        out[key] = out.get(key, 0.0) + _f(o["pnl_cents"]) / 100.0
    rows = []
    for (d, vid), val in sorted(out.items()):
        label = next((v["label"] for v in C.VARIANTS if v["id"] == vid), vid)
        rows.append({"event_date": d, "variant_id": vid, "cancel": label, "net": val})
    return rows


def bucket_rows(orders: list, variant_id: str, counting: str = "all") -> list:
    """P&L by the word's YES price when we fired. counting: all | normal | counting."""
    rows = []
    mine = [o for o in settled(orders) if o["variant_id"] == variant_id]
    if counting == "normal":
        mine = [o for o in mine if not o.get("is_counting")]
    elif counting == "counting":
        mine = [o for o in mine if o.get("is_counting")]
    for lo, hi in BUCKETS:
        grp = [o for o in mine if o.get("yes_price_at_place") is not None and lo <= _f(o["yes_price_at_place"]) < hi]
        filled = [o for o in grp if _f(o["filled_contracts"]) > 0]
        wins = [o for o in filled if o.get("result") == "no"]
        risk = sum(_f(o["risk_cents"]) for o in grp)
        net = sum(_f(o["pnl_cents"]) for o in grp)
        rows.append({
            "YES price at fire": "%d-%dc" % (lo, hi), "words": len(grp), "filled": len(filled),
            "taker ct": sum(_f(o["taker_contracts"]) for o in grp),
            "maker ct": sum(_f(o["maker_contracts"]) for o in grp),
            "taker $": sum(_f(o["taker_pnl_cents"]) for o in grp) / 100.0,
            "maker $": sum(_f(o["maker_pnl_cents"]) for o in grp) / 100.0,
            "net $": net / 100.0,
            "return %": (100.0 * net / risk) if risk > 0 else None,
            "NO won %": (100.0 * len(wins) / len(filled)) if filled else None,
        })
    return rows


def night_summary_text(event_date: str, orders: list, all_orders: list) -> str:
    """Telegram text for one settled night: per-version comparison, plus a no-fade-style per-word
    breakdown for whichever cancel time came out best tonight (open the dashboard for the other 4)."""
    tonight = [o for o in orders if o["event_date"] == event_date]
    lines = ["🌙 WNT post-cold-open · %s settled" % event_date]
    words = len(set(o["market_ticker"] for o in tonight))
    lines.append("%d words ordered (SELL YES %dc)" % (words, C.LIMIT_YES_CENTS))
    lines.append("")
    lines.append("cancel   filled  taker/maker ct   fees    P&L")
    for v in C.VARIANTS:
        mine = [o for o in tonight if o["variant_id"] == v["id"]]
        done = [o for o in mine if o.get("pnl_cents") is not None]
        filled = [o for o in done if _f(o["filled_contracts"]) > 0]
        lines.append("%-8s %2d/%-3d  %5.1f/%-5.1f   $%4.2f  %s" % (
            v["label"].replace(" PM", ""), len(filled), len(done),
            sum(_f(o["taker_contracts"]) for o in done), sum(_f(o["maker_contracts"]) for o in done),
            sum(_f(o["taker_fee_cents"]) + _f(o["maker_fee_cents"]) for o in done) / 100.0,
            "%+.2f" % (sum(_f(o["pnl_cents"]) for o in done) / 100.0)))
    tonight_rows = variant_rows(tonight)
    tonight_best = max((r for r in tonight_rows if r["nights"]), key=lambda r: r["net"], default=None)
    if tonight_best:
        filled, skipped, bullets = word_lines(tonight, event_date, tonight_best["variant_id"])
        lines.append("")
        lines.append("📜 Settlement update — best tonight: %s cancel" % tonight_best["cancel"])
        lines.append("%s: %s dollars (%s%% on $%.2f filled, %d name(s))" % (
            event_date, "%+.2f" % tonight_best["net"],
            ("%+.1f" % tonight_best["roi"]) if tonight_best["roi"] is not None else "n/a",
            tonight_best["risk"], len(filled)))
        lines.extend(bullets)
        if skipped:
            lines.append("(%d word(s) never filled, no cost)" % skipped)
        lines.append("(the other 4 cancel times are on the dashboard's Tonight tab)")

    rows = variant_rows(all_orders)
    best = best_of(rows)
    if best:
        lines.append("")
        lines.append("All settled nights so far (%d):" % best["nights"])
        for r in rows:
            if r["nights"]:
                lines.append("  %s  %+.2f$  (%s%%)" % (
                    r["cancel"].replace(" PM", ""), r["net"],
                    ("%+.0f" % r["roi"]) if r["roi"] is not None else "n/a"))
        lines.append("Best so far: %s by $%s" % (best["by_net"]["cancel"], "%+.2f" % best["by_net"]["net"]))
        if best["nights"] < 10:
            lines.append("(only %d nights: too few to call a winner)" % best["nights"])
    return "\n".join(lines)
