"""Env / Streamlit secrets. Every knob of the post-cold-open paper bot lives here.

PAPER ONLY. This repo has no Kalshi key, no signing code and no place-order call.
It can only READ public Kalshi data and write nolive_* tables.
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

try:
    import streamlit as st
except Exception:  # local scripts, no streamlit
    st = None


def _secret(name: str, default: str = "") -> str:
    if st is not None:
        try:
            if name in st.secrets:
                val = st.secrets[name]
                if val is not None and str(val) != "":
                    return str(val)
        except Exception:
            pass
    val = os.environ.get(name)
    return default if val is None else str(val)


def _flag(name: str, default: bool = False) -> bool:
    raw = _secret(name, "true" if default else "false").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _num(name: str, default: float) -> float:
    raw = _secret(name, str(default)).strip()
    try:
        return float(raw)
    except ValueError:
        return default


VERSION = "wnt-nolive-v2.0.0"    # bump this every release; it shows on the dashboard and in Telegram
CT = ZoneInfo("America/Chicago")

SERIES = _secret("SERIES", "KXWORLDNEWSMENTION")

# ---- the rules (frozen: change them in Streamlit Secrets, not in code) ----
# v2: backtested on 40 nights of Kalshi's own public trade history (independent of our data). The old
# 1c-97c / sell-55c rule only made money on the words that were already cheap when it fired; everything
# priced higher lost consistently across most nights. A top-of-scale version (buy YES on likely words)
# was tested two ways and found no edge either way. This version narrows to the zone that showed one.
FIRE_AT_CT = _secret("FIRE_AT_CT", "17:32:30")            # when the paper orders go in
FIRE_GRACE_S = int(_num("FIRE_GRACE_S", 120))             # if the app wakes late, still fire up to this many seconds late
LIMIT_YES_CENTS = int(_num("LIMIT_YES_CENTS", 30))        # Sell YES at 30c  (= Buy NO at 70c)
QUALIFY_MAX_YES_CENTS = _num("QUALIFY_MAX_YES_CENTS", 30) # a word only qualifies if YES is AT OR BELOW this
EXCLUDE_COUNTING_WORDS = _flag("EXCLUDE_COUNTING_WORDS", True)  # "3+ times" words behave differently; skip them entirely
PAPER_DOLLARS = _num("PAPER_DOLLARS", 3.0)                # dollars of collateral requested per word, BEFORE the cap below
MAX_CONTRACTS_PER_WORD = _num("MAX_CONTRACTS_PER_WORD", 50)   # hard cap regardless of what the dollar formula asks for
CANCEL_TIMES_CT = [x.strip() for x in _secret("CANCEL_TIMES_CT", "17:35,17:40,17:45,17:50,17:55").split(",") if x.strip()]

# ---- the recording window BEFORE the fire (no-fade's recorder stops at ~5:28 PM, so we cover 5:28 -> fire) ----
RECORD_FROM_CT = _secret("RECORD_FROM_CT", "17:28:00")
PRE_BOOK_SECONDS = int(_num("PRE_BOOK_SECONDS", 30))      # order-book picture per word while waiting for the fire

# ---- timing of the background loop ----
POLL_SECONDS = int(_num("POLL_SECONDS", 10))              # how often we read new trades while orders rest
BOOK_SNAPSHOT_SECONDS = int(_num("BOOK_SNAPSHOT_SECONDS", 60))   # order-book picture per word
TRACK_AFTER_LAST_CANCEL_S = int(_num("TRACK_AFTER_LAST_CANCEL_S", 20))
SETTLE_AFTER_CT = _secret("SETTLE_AFTER_CT", "18:05")
SETTLE_RETRY_S = int(_num("SETTLE_RETRY_S", 300))

# ---- Kalshi (public read-only endpoints, no key) ----
KALSHI_BASE = _secret("KALSHI_BASE", "https://external-api.kalshi.com")
API_ROOT = "/trade-api/v2"
USER_AGENT = "wnt-nolive-bot/" + VERSION

# ---- storage / alerts ----
DATABASE_URL = _secret("DATABASE_URL", "")
SQLITE_PATH = _secret("SQLITE_PATH", "nolive_bot.db")
TELEGRAM_TOKEN = _secret("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = _secret("TELEGRAM_CHAT_ID", "")
TELEGRAM_COMMANDS = _flag("TELEGRAM_COMMANDS", False)
STREAMLIT_APP_URL = _secret("STREAMLIT_APP_URL", "https://wnt-nolive-bot.streamlit.app")


def _label(hhmm: str) -> str:
    parts = hhmm.split(":")
    hh, mm = int(parts[0]), int(parts[1])
    suffix = "PM" if hh >= 12 else "AM"
    h12 = hh % 12 or 12
    return "%d:%02d %s" % (h12, mm, suffix)


def variant_id(hhmm: str) -> str:
    """'17:35' -> 'c1735'"""
    parts = hhmm.split(":")
    return "c%02d%02d" % (int(parts[0]), int(parts[1]))


VARIANTS = tuple(
    {"id": variant_id(c), "cancel_ct": c, "label": _label(c)} for c in CANCEL_TIMES_CT
)


def order_no_price_cents() -> int:
    """Selling YES at L is the same as buying NO at 100 - L."""
    return 100 - LIMIT_YES_CENTS


def sized_contracts() -> dict:
    from . import engine
    return engine.order_size(PAPER_DOLLARS, LIMIT_YES_CENTS, MAX_CONTRACTS_PER_WORD)


def summary() -> str:
    cancels = ", ".join(v["label"] for v in VARIANTS)
    sz = sized_contracts()
    return (
        "%s | PAPER ONLY | %s\n"
        "fires %s CT | qualifies: YES price at or below %gc (counting words excluded) | order: SELL YES at %dc (= BUY NO at %dc)\n"
        "$%g per word -> %.2f contracts%s, cap %g | cancel variants: %s\n"
        "records books every %ds + the trade tape from %s CT (fills the gap after no-fade stops)\n"
        "instant match against the real book = taker fee | resting fills = maker (no fee on this series)"
    ) % (VERSION, SERIES, FIRE_AT_CT, QUALIFY_MAX_YES_CENTS, LIMIT_YES_CENTS,
         order_no_price_cents(), PAPER_DOLLARS, sz["contracts"], " (CAPPED)" if sz["capped"] else "",
         MAX_CONTRACTS_PER_WORD, cancels, PRE_BOOK_SECONDS, RECORD_FROM_CT)
