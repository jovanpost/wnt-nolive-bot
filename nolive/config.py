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


VERSION = "wnt-nolive-v1.0"
CT = ZoneInfo("America/Chicago")

SERIES = _secret("SERIES", "KXWORLDNEWSMENTION")

# ---- the rules (frozen: change them in Streamlit Secrets, not in code) ----
FIRE_AT_CT = _secret("FIRE_AT_CT", "17:32:30")            # when the paper orders go in
FIRE_GRACE_S = int(_num("FIRE_GRACE_S", 120))             # if the app wakes late, still fire up to this many seconds late
LIMIT_YES_CENTS = int(_num("LIMIT_YES_CENTS", 55))        # Sell YES at 55c  (= Buy NO at 45c)
SKIP_YES_AT_OR_ABOVE = _num("SKIP_YES_AT_OR_ABOVE", 98)   # words at 98c+ on the YES side do not qualify
PAPER_DOLLARS = _num("PAPER_DOLLARS", 5.0)                # dollars of collateral per word at the limit price
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


def summary() -> str:
    cancels = ", ".join(v["label"] for v in VARIANTS)
    return (
        "%s | PAPER ONLY | %s\n"
        "fires %s CT | qualifies: YES price below %gc | order: SELL YES at %dc (= BUY NO at %dc)\n"
        "$%g per word | cancel variants: %s\n"
        "records books every %ds + the trade tape from %s CT (fills the gap after no-fade stops)\n"
        "instant match against the real book = taker fee | resting fills = maker (no fee on this series)"
    ) % (VERSION, SERIES, FIRE_AT_CT, SKIP_YES_AT_OR_ABOVE, LIMIT_YES_CENTS,
         order_no_price_cents(), PAPER_DOLLARS, cancels, PRE_BOOK_SECONDS, RECORD_FROM_CT)
