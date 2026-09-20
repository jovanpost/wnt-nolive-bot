"""WNT post-cold-open PAPER bot: the scoreboard for every cancel-time version.

Paper only. No Kalshi key, no order buttons. P&L shows only after Kalshi publishes the official result.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

import pandas as pd
import streamlit as st

from nolive import analytics, clock, config as C, notify, pipeline, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

st.set_page_config(page_title="WNT Post-Cold-Open (paper)", page_icon="🕔", layout="wide")


@st.cache_resource
def boot():
    store.init_db()
    pipeline.register_commands()
    notify.start_listener()
    threading.Thread(target=pipeline.run_forever, name="nolive-loop", daemon=True).start()
    return {"started_at": clock.now_ct().isoformat()}


if st.query_params.get("ping") == "true":     # the keep-alive workflow hits this
    boot()
    st.write("alive")
    st.stop()

boot()


@st.cache_data(ttl=30, show_spinner=False)
def load_orders():
    return store.all_orders()


@st.cache_data(ttl=30, show_spinner=False)
def load_runs():
    return store.recent_runs()


@st.cache_data(ttl=30, show_spinner=False)
def load_markets():
    return store.all_markets_light()


def money(x):
    return "" if x is None else "%s$%.2f" % ("-" if x < 0 else "", abs(x))


def pct(x, digits=1):
    return "" if x is None else "%+.*f%%" % (digits, x)


def next_fire_label(today: str) -> str:
    now = clock.now_utc()
    T = clock.fire_at(today)
    if now < T:
        left = int((T - now).total_seconds())
        return "today in %dh %02dm" % (left // 3600, (left % 3600) // 60) if left >= 3600 else "today in %dm %02ds" % (left // 60, left % 60)
    d = clock.now_ct().date()
    for i in range(1, 8):
        nxt = d + timedelta(days=i)
        if nxt.weekday() < 5:
            return nxt.strftime("%a %b %-d") + ", " + C.FIRE_AT_CT
    return "-"


orders = load_orders()
runs = load_runs()
today = clock.today_ct()
today_run = next((r for r in runs if r["event_date"] == today), None)
settled_nights = len(set(o["event_date"] for o in orders if o.get("pnl_cents") is not None))

st.title("🕔 WNT Post-Cold-Open · paper bot")
st.caption(
    "%s · sells YES at %dc on every word under %gc at %s CT · %d cancel-time versions · "
    "PAPER ONLY (no key, no orders)" % (C.VERSION, C.LIMIT_YES_CENTS, C.SKIP_YES_AT_OR_ABOVE, C.FIRE_AT_CT, len(C.VARIANTS))
)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Today (CT)", today)
paused = False
try:
    paused = bool(store.get_state("paused", False))
except Exception:
    pass
last_tick = pipeline.STATE.get("last_tick")
c2.metric("Bot", "PAUSED" if paused else "running", clock.fmt(last_tick) if last_tick else "no tick yet")
c3.metric("Tonight", today_run["status"] if today_run else "not fired yet")
c4.metric("Next fire", next_fire_label(today))
c5.metric("Settled nights", settled_nights)

tab_score, tab_tonight, tab_nights, tab_price, tab_data, tab_log = st.tabs(
    ["Scoreboard", "Tonight", "Nights", "By price", "Fills & data", "Log"]
)

# ------------------------------------------------------------------ scoreboard
with tab_score:
    rows = analytics.variant_rows(orders)
    best = analytics.best_of(rows)
    if not best:
        st.info(
            "No settled nights yet. The first paper night fires at %s CT and its money appears after Kalshi publishes "
            "the official results (about %s CT or later)." % (C.FIRE_AT_CT, C.SETTLE_AFTER_CT)
        )
    else:
        bn, br = best["by_net"], best["by_roi"]
        st.success(
            "Best so far by dollars: **%s** (%s over %d nights).  Best by return on money used: **%s** (%s)."
            % (bn["cancel"], money(bn["net"]), bn["nights"], br["cancel"] if br else "-", pct(br["roi"]) if br else "-")
        )
        if best["nights"] < 10:
            st.warning(
                "Only %d settled night(s). With this few nights the ranking is mostly luck. Look for a version that stays "
                "ahead as nights pile up, not one good night." % best["nights"]
            )
        table = []
        for r in rows:
            star = ""
            if r["nights"] and r["variant_id"] == bn["variant_id"]:
                star += "⭐$ "
            if r["nights"] and br and r["variant_id"] == br["variant_id"]:
                star += "⭐%"
            table.append({
                "Cancel": r["cancel"], "Best": star.strip(), "Nights": r["nights"],
                "Words ordered": r["words_ordered"], "Words filled": r["words_filled"],
                "Fill %": None if r["fill_rate"] is None else round(r["fill_rate"], 0),
                "Taker ct": round(r["taker_ct"], 1), "Maker ct": round(r["maker_ct"], 1),
                "Fees $": round(r["fees"], 2), "Taker P&L $": round(r["taker_pnl"], 2),
                "Maker P&L $": round(r["maker_pnl"], 2), "Net P&L $": round(r["net"], 2),
                "Money used $": round(r["risk"], 2),
                "Return %": None if r["roi"] is None else round(r["roi"], 1),
                "NO won %": None if r["win_rate"] is None else round(r["win_rate"], 0),
                "$ / night": None if r["per_night"] is None else round(r["per_night"], 2),
                "Best night $": None if r["best_night"] is None else round(r["best_night"], 2),
                "Worst night $": None if r["worst_night"] is None else round(r["worst_night"], 2),
                "Waiting nights": r["pending_nights"],
            })
        st.dataframe(pd.DataFrame(table), hide_index=True, width="stretch")

        nr = analytics.night_rows(orders)
        if nr:
            piv = pd.DataFrame(nr).pivot(index="event_date", columns="cancel", values="net").fillna(0.0).sort_index()
            left, right = st.columns(2)
            with left:
                st.subheader("Running total by night ($)")
                st.line_chart(piv.cumsum())
            with right:
                st.subheader("Net P&L by version ($)")
                st.bar_chart(pd.DataFrame({"Net P&L $": [r["net"] for r in rows]}, index=[r["cancel"] for r in rows]))
    with st.expander("How to read this"):
        st.markdown(
            "- **Order**: SELL YES at %dc on every word whose YES price is under %gc at %s CT (same as BUY NO at %dc). $%g of collateral per word.\n"
            "- **Taker**: YES buyers were already bidding %dc or more, so the order matched at once at THEIR price and paid Kalshi's taker fee.\n"
            "- **Maker**: the rest rested. It fills only when a YES buyer pays MORE than %dc (a trade above our price). No fee on this series.\n"
            "- **Cancel**: each version is the same order, cancelled at a different time. Later cancel = more chances to fill, and more risk.\n"
            "- **Return %%** = profit / money used (the collateral on what actually filled). **NO won %%** = words that were not said, of the words that filled.\n"
            "- Money uses Kalshi's official yes/no result, so tonight's numbers appear after settlement."
            % (C.LIMIT_YES_CENTS, C.SKIP_YES_AT_OR_ABOVE, C.FIRE_AT_CT, 100 - C.LIMIT_YES_CENTS, C.PAPER_DOLLARS,
               C.LIMIT_YES_CENTS, C.LIMIT_YES_CENTS)
        )
    with st.expander("What the backtest said (for comparison)"):
        st.markdown(
            "From 50 past nights of public trades ($5 per word, normal words only, NO limit 45c = YES ask 55c, after fees). "
            "The 'instant' part is an ESTIMATE; this bot measures it for real.\n\n"
            "| cancel | resting words | instant words (estimate) | both |\n|---|---|---|---|\n"
            "| 5:35:30 | +$1.36 / night | +$1.17 | +$2.52 |\n| 5:45:00 | +$2.57 / night | +$1.44 | +$4.01 |\n\n"
            "The second half of those 50 nights earned about half of the first half, so expect less than these numbers."
        )

# ------------------------------------------------------------------ tonight
with tab_tonight:
    if not today_run:
        st.write("Not fired yet tonight. The orders go in at **%s CT**." % C.FIRE_AT_CT)
    else:
        st.write(
            "`%s` · status **%s** · fired %s (%.1fs after %s) · fee type `%s`"
            % (today_run.get("event_ticker"), today_run["status"], clock.fmt(today_run.get("fired_at")),
               today_run.get("late_seconds") or 0.0, C.FIRE_AT_CT, today_run.get("fee_type"))
        )
        mk = store.markets_for_run(today_run["id"])
        od = store.orders_for_run(today_run["id"])
        if mk:
            longest = C.VARIANTS[-1]["id"]
            by_t = dict((o["market_ticker"], o) for o in od if o["variant_id"] == longest)
            view = []
            for m in mk:
                o = by_t.get(m["market_ticker"], {})
                view.append({
                    "Word": m["word"], "YES price at fire": m.get("yes_price_cents"),
                    "Order?": "yes" if m["qualified"] else "no: %s" % m.get("skip_reason"),
                    "YES bids >= %dc" % C.LIMIT_YES_CENTS: m.get("yes_size_at_limit"),
                    "Taker ct": o.get("taker_contracts"),
                    "Maker ct (latest cancel)": o.get("maker_contracts"),
                    "Status (latest cancel)": o.get("status"),
                    "Result": m.get("result"),
                })
            st.dataframe(pd.DataFrame(view), hide_index=True, width="stretch")
        if od:
            vt = []
            for v in C.VARIANTS:
                mine = [o for o in od if o["variant_id"] == v["id"]]
                filled = [o for o in mine if float(o["filled_contracts"] or 0) > 0]
                done = [o for o in mine if o.get("pnl_cents") is not None]
                vt.append({
                    "Cancel": v["label"], "Words filled": "%d / %d" % (len(filled), len(mine)),
                    "Taker ct": round(sum(float(o["taker_contracts"] or 0) for o in mine), 1),
                    "Maker ct": round(sum(float(o["maker_contracts"] or 0) for o in mine), 1),
                    "Fees $": round(sum(float(o["taker_fee_cents"] or 0) + float(o["maker_fee_cents"] or 0) for o in mine) / 100.0, 2),
                    "P&L $": round(sum(float(o["pnl_cents"]) for o in done) / 100.0, 2) if len(done) == len(mine) else None,
                })
            st.subheader("Tonight by cancel time")
            st.dataframe(pd.DataFrame(vt), hide_index=True, width="stretch")

# ------------------------------------------------------------------ nights
with tab_nights:
    if not runs:
        st.write("No nights yet.")
    else:
        night_tbl = []
        by_night = {}
        for r in analytics.night_rows(orders):
            by_night.setdefault(r["event_date"], {})[r["cancel"]] = r["net"]
        for r in runs:
            row = {"Date": r["event_date"], "Status": r["status"], "Words": r["markets_seen"], "Qualified": r["qualified"],
                   "Skipped": r["skipped"], "Fired late (s)": None if r.get("late_seconds") is None else round(r["late_seconds"], 1)}
            for v in C.VARIANTS:
                val = by_night.get(r["event_date"], {}).get(v["label"])
                row[v["label"] + " $"] = None if val is None else round(val, 2)
            night_tbl.append(row)
        st.dataframe(pd.DataFrame(night_tbl), hide_index=True, width="stretch")

# ------------------------------------------------------------------ by price
with tab_price:
    st.write("Where the money comes from, by the word's YES price when we fired. Settled nights only.")
    pick = st.selectbox("Cancel version", [v["label"] for v in C.VARIANTS], index=len(C.VARIANTS) - 1)
    which = st.radio("Words", ["all", "normal", "counting"], horizontal=True,
                     help="counting = words like 'Iran (3+ times)' that need several mentions")
    vid = next(v["id"] for v in C.VARIANTS if v["label"] == pick)
    br = analytics.bucket_rows(orders, vid, which)
    if not any(b["words"] for b in br):
        st.info("Nothing settled yet.")
    else:
        bdf = pd.DataFrame(br)
        st.dataframe(bdf.round(2), hide_index=True, width="stretch")
        st.bar_chart(bdf.set_index("YES price at fire")[["taker $", "maker $"]])
        st.caption("The backtest found the money and the losses at different YES prices. This table shows whether real paper fills agree.")

# ------------------------------------------------------------------ fills & data
with tab_data:
    cnt = store.counts()
    st.write("Rows saved: **%d** trades · **%d** book pictures · **%d** fills · **%d** orders" % (
        cnt["nolive_trades"], cnt["nolive_depth"], cnt["nolive_fills"], cnt["nolive_orders"]))
    st.caption("Every trade after %s CT and a book picture every %ds are kept, so any other rule can be replayed later without waiting more nights." % (C.FIRE_AT_CT, C.BOOK_SNAPSHOT_SECONDS))
    fl = store.recent_fills(100)
    if fl:
        fdf = pd.DataFrame(fl)[["ts", "event_date", "market_ticker", "kind", "contracts", "price_cents", "fee_cents", "source"]]
        st.subheader("Latest simulated fills")
        st.dataframe(fdf, hide_index=True, width="stretch")
    if today_run:
        tick = st.selectbox("Book history for a word (tonight)", [m["market_ticker"] for m in store.markets_for_run(today_run["id"]) if m["qualified"]] or [""])
        if tick:
            dp = store.depth_for_market(today, tick, 80)
            if dp:
                st.dataframe(pd.DataFrame(dp), hide_index=True, width="stretch")

# ------------------------------------------------------------------ log
with tab_log:
    st.code(C.summary())
    st.write("ticks this session: %s · last status: %s" % (pipeline.STATE["ticks"], pipeline.STATE["last_status"] or "-"))
    if pipeline.STATE["last_error"]:
        st.error(pipeline.STATE["last_error"])
    act = store.recent_activity(40)
    if act:
        st.dataframe(pd.DataFrame(act), hide_index=True, width="stretch")
    if st.button("Refresh now"):
        st.cache_data.clear()
        st.rerun()
