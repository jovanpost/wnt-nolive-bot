"""WNT post-cold-open bot: the scoreboard for every cancel-time version, plus the live-engine status.

No order buttons here. The live engine (real money) runs its own background thread, controlled only
by Streamlit Secrets and Telegram (/live_status /live_pause /live_resume /live_cancel_now). P&L shows
only after Kalshi publishes the official result.
"""
from __future__ import annotations

import logging
import threading
from datetime import timedelta

import pandas as pd
import streamlit as st

import mdkit
from nolive import analytics, clock, config as C, live, notify, pipeline, store

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")

st.set_page_config(page_title="WNT Post-Cold-Open", page_icon="🕔", layout="wide")


@st.cache_resource
def boot():
    store.init_db()
    pipeline.register_commands()
    live.register_commands()
    notify.start_listener()
    threading.Thread(target=pipeline.run_forever, name="nolive-loop", daemon=True).start()
    if live.enabled():
        threading.Thread(target=live.run_forever, name="nolive-live-loop", daemon=True).start()
    return {"started_at": clock.now_ct().isoformat(), "live_enabled": live.enabled()}


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

st.title("🕔 WNT Post-Cold-Open")
_sz = C.sized_contracts()
st.caption(
    "%s · paper: $%g/word -> %.2f contracts%s, %d cancel-time versions · live: %s"
    % (C.VERSION, C.PAPER_DOLLARS, _sz["contracts"], " ⚠️ capped" if _sz["capped"] else "", len(C.VARIANTS),
       ("REAL MONEY $%g/word" % C.LIVE_DOLLARS_PER_WORD) if (live.enabled() and C.live_mode() == "live")
       else C.live_mode() if live.enabled() else "off (no key)")
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

tab_ledger, tab_score, tab_tonight, tab_nights, tab_price, tab_data, tab_live, tab_log = st.tabs(
    ["Ledger (day / week)", "Scoreboard", "Tonight", "Nights", "By price", "Fills & data", "Live", "Log"]
)
_NOW_TXT = clock.fmt(clock.now_utc())


def _page(title):
    mdkit.page(title, C.VERSION, _NOW_TXT)


# ------------------------------------------------------------------ ledger
def _names() -> dict:
    """market_ticker -> the word as Kalshi shows it."""
    out = {}
    for o in orders:
        if o.get("word"):
            out[o["market_ticker"]] = o["word"]
    try:
        for o in store.all_live_orders():
            if o.get("word"):
                out[o["market_ticker"]] = o["word"]
    except Exception:
        pass
    return out


NAMES = _names()


def _nm(ticker):
    return NAMES.get(ticker) or ticker


def _said(result):
    return {"yes": "SAID (YES)", "no": "not said (NO)", "void": "void"}.get(result or "", "waiting for result")


with tab_ledger:
    _page("Ledger")
    live_all = [o for o in store.all_live_orders() if not o.get("dry_run")]
    fills_all = store.all_live_fills()
    dates = [o["event_date"] for o in live_all] + [o["event_date"] for o in orders]
    per = mdkit.period_picker(dates, "ledger", today)
    st.caption("Showing: %s. All times are Central." % per["label"])

    st.subheader("Live (real money)")
    lv = [o for o in live_all if per["match"](o["event_date"]) and o.get("mode") in ("live", "smoke")]
    fills_by_order = {}
    for f in fills_all:
        fills_by_order.setdefault(str(f.get("order_id")), []).append(f)
    if not lv:
        st.info("No real nolive orders in this period.")
    else:
        rows_l = []
        for o in sorted(lv, key=lambda x: (x["event_date"], str(x.get("placed_at")))):
            fl = fills_by_order.get(str(o.get("order_id")), [])
            filled = float(o.get("filled_contracts") or 0)
            avg = o.get("avg_fill_price_cents")
            cost = filled * float(avg or 0) / 100.0
            fees = float(o.get("fees_cents") or 0) / 100.0
            n_t = sum(1 for f in fl if f.get("is_taker"))
            rows_l.append({
                "date": o["event_date"], "word": o.get("word") or o["market_ticker"],
                "mode": o.get("mode"),
                "placed": mdkit.ct_time(o.get("placed_at"), C.CT),
                "order": "buy NO at %d¢ or less x %.2f" % (o["no_price_cents"], float(o["contracts"] or 0)),
                "filled": round(filled, 2),
                "first fill": mdkit.ct_time(o.get("first_fill_at"), C.CT),
                "fills": ("%d (%d taker, %d maker)" % (len(fl), n_t, len(fl) - n_t)) if fl else "0",
                "avg NO price ¢": None if avg is None else round(float(avg), 2),
                "cost $": round(cost, 2), "fees $": round(fees, 2),
                "ended": o.get("status"),
                "result": _said(o.get("result")) if filled > 0 else "not filled",
                "paid out $": (round(filled, 2) if o.get("result") == "no" else 0.0) if o.get("result") else None,
                "P&L $": mdkit.money(o.get("realized_pnl_cents")),
                "return %": (round(100.0 * float(o["realized_pnl_cents"]) / 100.0 / cost, 1)
                             if o.get("realized_pnl_cents") is not None and cost > 0 else None),
            })
        done = [r for r in rows_l if r["P&L $"] is not None]
        spent = sum(r["cost $"] for r in rows_l)
        net = sum(float(o["realized_pnl_cents"]) for o in lv if o.get("realized_pnl_cents") is not None) / 100.0
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Orders", len(rows_l))
        m2.metric("Filled", sum(1 for r in rows_l if r["filled"] > 0))
        m3.metric("Won / settled", "%d / %d" % (sum(1 for r in done if r["result"].startswith("not said")), len(done)))
        m4.metric("Money in fills", "$%.2f" % spent)
        m5.metric("Net P&L (after fees)", "%+.2f" % net)
        st.dataframe(pd.DataFrame(rows_l), hide_index=True, width="stretch")
        fl_rows = []
        for o in lv:
            for f in fills_by_order.get(str(o.get("order_id")), []):
                fl_rows.append({
                    "time": mdkit.ct_time(f.get("created_at"), C.CT), "date": f["event_date"],
                    "word": o.get("word") or f["market_ticker"], "contracts": round(float(f["contracts"]), 2),
                    "NO price ¢": f["price_cents"], "type": "taker (instant)" if f.get("is_taker") else "maker (rested)",
                    "fee $": round(float(f.get("fee_cents") or 0) / 100.0, 2),
                    "cost $": round(float(f["contracts"]) * float(f["price_cents"]) / 100.0, 2),
                })
        st.subheader("Every real fill")
        st.dataframe(pd.DataFrame(sorted(fl_rows, key=lambda r: r["time"])), hide_index=True, width="stretch")

    st.subheader("Paper (all cancel versions)")
    pp = [o for o in orders if per["match"](o["event_date"])]
    if not pp:
        st.info("No paper orders in this period.")
    else:
        rows_p = []
        for o in sorted(pp, key=lambda x: (x["event_date"], x.get("word") or "", x["cancel_ct"])):
            rows_p.append({
                "date": o["event_date"], "word": o.get("word") or o["market_ticker"],
                "cancel at": o["cancel_ct"], "placed": mdkit.ct_time(o.get("placed_at"), C.CT),
                "YES price at fire ¢": o.get("yes_price_at_place"),
                "wanted": round(float(o["contracts"] or 0), 2),
                "taker ct": round(float(o.get("taker_contracts") or 0), 2),
                "maker ct": round(float(o.get("maker_contracts") or 0), 2),
                "fees $": round((float(o.get("taker_fee_cents") or 0) + float(o.get("maker_fee_cents") or 0)) / 100.0, 2),
                "money used $": mdkit.money(o.get("risk_cents")),
                "result": _said(o.get("result")) if float(o.get("filled_contracts") or 0) > 0 else "not filled",
                "P&L $": mdkit.money(o.get("pnl_cents")),
            })
        st.dataframe(pd.DataFrame(rows_p), hide_index=True, width="stretch")

# ------------------------------------------------------------------ scoreboard
with tab_score:
    _page("Scoreboard")
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
        avg_words = (sum(r["words_ordered"] for r in rows) / len(C.VARIANTS)) / best["nights"] if best["nights"] else 0
        if avg_words and avg_words < 3:
            st.info(
                "Averaging about %.1f qualifying words a night. The 30c cap is a much tighter filter than the old "
                "rule, so there are fewer trades to learn from per night -- expect it to take more nights, not fewer, "
                "before the ranking above means much." % avg_words
            )
        capped_n = sum(1 for o in orders if o.get("size_capped"))
        if orders:
            st.caption("%d of %d orders so far hit the %g-contract cap." % (capped_n, len(orders), C.MAX_CONTRACTS_PER_WORD)
                       if capped_n else "No orders have hit the %g-contract cap yet (current sizing is $%g -> %.2f contracts)."
                       % (C.MAX_CONTRACTS_PER_WORD, C.PAPER_DOLLARS, C.sized_contracts()["contracts"]))
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
            "- **Taker**: matched at once when the order went in, at the other side's price; pays Kalshi's taker fee.\n"
            "- **Maker**: rested and filled later.\n"
            "- **Cancel**: each version is the same order, cancelled at a different time.\n"
            "- **Return %** = profit / money used on what actually filled. **NO won %** = words not said, of the words that filled.\n"
            "- Money uses Kalshi's official yes/no result, so a night's numbers appear after settlement."
        )

# ------------------------------------------------------------------ tonight
with tab_tonight:
    _page("Tonight")
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
    _page("Nights")
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
    _page("By price")
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
    _page("Fills and data")
    cnt = store.counts()
    st.write("Rows saved: **%d** trades · **%d** book pictures · **%d** fills · **%d** orders" % (
        cnt["nolive_trades"], cnt["nolive_depth"], cnt["nolive_fills"], cnt["nolive_orders"]))
    st.caption("Kept for replaying other ideas: a book picture of EVERY word every %ds from %s CT to the fire (no-fade stops at ~5:28), "
        "every trade from %s CT, and after the fire a book every %ds plus every trade on EVERY word until the last cancel time." % (C.PRE_BOOK_SECONDS, C.RECORD_FROM_CT, C.RECORD_FROM_CT, C.BOOK_SNAPSHOT_SECONDS))
    fl = store.recent_fills(100)
    if fl:
        fdf = pd.DataFrame(fl)[["ts", "event_date", "market_ticker", "kind", "contracts", "price_cents", "fee_cents", "source"]]
        fdf.insert(2, "word", fdf["market_ticker"].map(_nm))
        fdf["ts"] = fdf["ts"].map(lambda v: mdkit.ct_time(v, C.CT))
        fdf = fdf.drop(columns=["market_ticker"])
        st.subheader("Latest simulated fills")
        st.dataframe(fdf, hide_index=True, width="stretch")
    if today_run:
        tick = st.selectbox("Book history for a word (tonight)", [m["market_ticker"] for m in store.markets_for_run(today_run["id"]) if m["qualified"]] or [""],
                            format_func=lambda t: _nm(t) if t else "")
        if tick:
            dp = store.depth_for_market(today, tick, 80)
            if dp:
                st.dataframe(pd.DataFrame(dp), hide_index=True, width="stretch")

# ------------------------------------------------------------------ log
with tab_live:
    _page("Live")
    key_ok = live.enabled()
    lc1, lc2, lc3 = st.columns(3)
    lc1.metric("Kalshi key detected", "yes" if key_ok else "NO -- live engine is OFF")
    if not key_ok:
        st.error(
            "No live engine is running. This means KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PEM "
            "(or KALSHI_PRIVATE_KEY_PATH) are not BOTH set and non-empty in Streamlit Secrets right "
            "now, or the app hasn't restarted since they were added. Check the exact secret names, "
            "then use \"Reboot app\" from the Streamlit Cloud menu (⋮) if you just added them."
        )
    else:
        mode = C.live_mode()
        mode_label = {"dry_run": "DRY RUN -- simulating only, no real orders",
                      "demo": "DEMO -- Kalshi's fake-money server",
                      "live": "LIVE -- REAL MONEY"}[mode]
        lc2.metric("Mode", mode_label)
        lc3.metric("Live engine thread", "running" if live.STATE.get("running") else "not started yet")
        if mode == "dry_run":
            st.warning(
                "LIVE_DRY_RUN is true (the default). No real orders will be sent until you set "
                "LIVE_DRY_RUN = false in Streamlit Secrets. This is the safe, expected state until "
                "you're ready."
            )
        elif mode == "live":
            st.success("Real orders ARE enabled.")
        st.code(C.live_summary())
        st.write(
            "active_event=%s · orders_today=%s · fills_today=%s · last_poll=%s · last_error=%s"
            % (live.STATE.get("active_event"), live.STATE.get("orders_today"),
               live.STATE.get("fills_today"), clock.fmt(live.STATE.get("last_poll")) if live.STATE.get("last_poll") else "-",
               live.STATE.get("last_error") or "-")
        )
        paused_live = False
        try:
            paused_live = bool(store.get_state("live_paused", False))
        except Exception:
            pass
        if paused_live:
            st.error("LIVE is PAUSED (via /live_pause). Send /live_resume in Telegram to resume.")

    st.subheader("Tonight's live orders")
    live_rows_tonight = store.live_orders_for_day(today)
    if live_rows_tonight:
        st.dataframe(pd.DataFrame([{
            "word": o.get("word") or o["market_ticker"], "mode": o.get("mode"), "status": o.get("status"),
            "placed": mdkit.ct_time(o.get("placed_at"), C.CT), "NO limit ¢": o["no_price_cents"],
            "wanted": round(float(o["contracts"] or 0), 2), "filled": round(float(o.get("filled_contracts") or 0), 2),
            "avg NO ¢": None if o.get("avg_fill_price_cents") is None else round(float(o["avg_fill_price_cents"]), 2),
            "fees $": round(float(o.get("fees_cents") or 0) / 100.0, 2),
            "result": o.get("result") or "", "P&L $": mdkit.money(o.get("realized_pnl_cents")),
            "reject": o.get("reject_reason") or "",
        } for o in live_rows_tonight]), hide_index=True, width="stretch")
    else:
        st.write("No live order rows for today yet.")

    st.subheader("Live P&L (settled, real money)")
    _settled = store.live_orders_settled()
    if _settled:
        _net = sum(float(o.get("realized_pnl_cents") or 0) for o in _settled) / 100.0
        _wins = sum(1 for o in _settled if o.get("result") == "no")
        _cost = sum(float(o.get("filled_contracts") or 0) * float(o.get("avg_fill_price_cents") or 0) for o in _settled) / 100.0
        l1, l2, l3, l4 = st.columns(4)
        l1.metric("Settled filled orders", len(_settled))
        l2.metric("Won (NO)", "%d / %d" % (_wins, len(_settled)))
        l3.metric("Net $ after fees", "%+.2f" % _net)
        l4.metric("Return on filled $", ("%+.1f%%" % (100.0 * _net / _cost)) if _cost else "n/a")
        by_night: dict = {}
        for o in _settled:
            by_night.setdefault(o["event_date"], 0.0)
            by_night[o["event_date"]] += float(o.get("realized_pnl_cents") or 0) / 100.0
        st.dataframe(pd.DataFrame([{"night": k, "net $": round(v, 2)} for k, v in sorted(by_night.items(), reverse=True)]),
                     hide_index=True, width="stretch")
    else:
        st.write("No settled live fills yet (results come after the show; checked every few minutes).")

    st.subheader("Recent live runs (one row per night)")
    live_runs = store.recent_live_runs(20)
    if live_runs:
        st.dataframe(pd.DataFrame(live_runs), hide_index=True, width="stretch")
    else:
        st.write("No live runs recorded yet.")

    st.subheader("Recent real fills")
    live_fills = store.recent_live_fills(50)
    if live_fills:
        st.dataframe(pd.DataFrame([{
            "time": mdkit.ct_time(f.get("created_at"), C.CT), "word": _nm(f["market_ticker"]),
            "contracts": round(float(f["contracts"]), 2), "NO price ¢": f["price_cents"],
            "type": "taker" if f.get("is_taker") else "maker", "fee $": round(float(f.get("fee_cents") or 0) / 100.0, 2),
        } for f in live_fills]), hide_index=True, width="stretch")
    else:
        st.write("No real fills recorded yet.")

with tab_log:
    _page("Log")
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

mdkit.done()
