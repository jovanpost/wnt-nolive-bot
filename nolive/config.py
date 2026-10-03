"""Env / Streamlit secrets. Every knob of the post-cold-open bot lives here.

The paper engine (5 cancel-time variants, PAPER_DOLLARS) is unchanged and always runs -- it never
sends a real order. The LIVE section below is a separate, optional real-money engine (nolive_live_*
tables, its own single cancel time) that is OFF (LIVE_DRY_RUN=true) until you turn it on yourself.
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


VERSION = "wnt-nolive-v3.1.1"    # bump this every release; it shows on the dashboard and in Telegram
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


# =====================================================================
# LIVE (real money). Uses the exact same rule as the paper bot above (QUALIFY_MAX_YES_CENTS,
# LIMIT_YES_CENTS, EXCLUDE_COUNTING_WORDS, MAX_CONTRACTS_PER_WORD) -- only the dollar size, the
# cancel time, and whether it is real orders differ. Off (dry_run) by default: nothing real
# happens until LIVE_DRY_RUN is set to false in Streamlit Secrets, on purpose, by you.
#
# Rollout, same as wnt-nofade-bot: LIVE_DRY_RUN=true first (simulated, safe) -> LIVE_SMOKE=true for
# one night (adds ONE real 1-contract order per word, tiny money, to prove the order code works) ->
# LIVE_DRY_RUN=false for the real thing.
# =====================================================================
LIVE_DRY_RUN = _flag("LIVE_DRY_RUN", True)              # true = no real orders sent, ever (default)
LIVE_USE_DEMO = _flag("LIVE_USE_DEMO", False)           # true = Kalshi's fake-money demo server
LIVE_SMOKE = _flag("LIVE_SMOKE", False)                 # true = ALSO send one tiny REAL order per word
LIVE_SMOKE_CONTRACTS = max(1, int(_num("LIVE_SMOKE_CONTRACTS", 1)))

LIVE_DOLLARS_PER_WORD = _num("LIVE_DOLLARS_PER_WORD", 5.0)   # <-- change the $ amount here, in Secrets
LIVE_CANCEL_CT = _secret("LIVE_CANCEL_CT", "17:55")          # single cancel time for live (no variants)
                                                              # -- baked into each order as server-side expiry
LIVE_APP_CANCEL_BUFFER_SECONDS = int(_num("LIVE_APP_CANCEL_BUFFER_SECONDS", 60))
# the in-app backup cancel_all() fires this many seconds AFTER LIVE_CANCEL_CT, on purpose -- so it
# never races Kalshi's own server-side expiry over the same instant. Kalshi's expiry is the one
# that actually matters; the in-app cancel is only a second, belt-and-suspenders check.
LIVE_MAX_MARKETS_PER_DAY = int(_num("LIVE_MAX_MARKETS_PER_DAY", 25))
LIVE_MAX_DAILY_COLLATERAL = _num("LIVE_MAX_DAILY_COLLATERAL", 150.00)
LIVE_POLL_SECONDS = int(_num("LIVE_POLL_SECONDS", 60))
LIVE_DETECT_POLL_SECONDS = int(_num("LIVE_DETECT_POLL_SECONDS", 20))
LIVE_COLD_POLL_SECONDS = int(_num("LIVE_COLD_POLL_SECONDS", 300))

LIVE_POST_ONLY = _flag("LIVE_POST_ONLY", True)                    # order must rest, never take -- except:
LIVE_TAKE_IF_ALREADY_CHEAP = _flag("LIVE_TAKE_IF_ALREADY_CHEAP", True)  # ...if the book is already past our price
LIVE_USE_SERVER_SIDE_EXPIRY = _flag("LIVE_USE_SERVER_SIDE_EXPIRY", True)  # Kalshi itself expires the order too
LIVE_ORDER_API = _secret("LIVE_ORDER_API", "v2")

LIVE_PROD_BASE = "https://external-api.kalshi.com"
LIVE_DEMO_BASE = "https://external-api.demo.kalshi.co"
LIVE_API_ROOT = "/trade-api/v2"
LIVE_BASE_URL = LIVE_DEMO_BASE if LIVE_USE_DEMO else LIVE_PROD_BASE

KALSHI_KEY_ID = _secret("KALSHI_KEY_ID", "")
KALSHI_PRIVATE_KEY_PEM = _secret("KALSHI_PRIVATE_KEY_PEM", "")
KALSHI_PRIVATE_KEY_PATH = _secret("KALSHI_PRIVATE_KEY_PATH", "")
LIVE_USER_AGENT = "wnt-nolive-bot-live/" + VERSION


def live_no_price_cents() -> int:
    """Same 30/70 split as the paper rule: sell YES at LIMIT_YES_CENTS = buy NO at 100 - LIMIT_YES_CENTS."""
    return 100 - LIMIT_YES_CENTS


def live_sized_contracts() -> dict:
    from . import engine
    return engine.order_size(LIVE_DOLLARS_PER_WORD, LIMIT_YES_CENTS, MAX_CONTRACTS_PER_WORD)


def live_collateral_per_market() -> float:
    sized = live_sized_contracts()
    return sized["contracts"] * live_no_price_cents() / 100.0


def live_effective_max_markets() -> int:
    per = live_collateral_per_market()
    by_money = int(LIVE_MAX_DAILY_COLLATERAL // per) if per > 0 else LIVE_MAX_MARKETS_PER_DAY
    return max(0, min(LIVE_MAX_MARKETS_PER_DAY, by_money))


def live_mode() -> str:
    if LIVE_DRY_RUN:
        return "dry_run"
    return "demo" if LIVE_USE_DEMO else "live"


def live_summary() -> str:
    where = "DEMO (fake money)" if LIVE_USE_DEMO else "PRODUCTION"
    mode = "DRY RUN (no real orders)" if LIVE_DRY_RUN else ("LIVE $%g/word" % LIVE_DOLLARS_PER_WORD)
    if LIVE_SMOKE:
        mode += " + SMOKE %d" % LIVE_SMOKE_CONTRACTS
    sized = live_sized_contracts()
    money_cap = live_effective_max_markets()
    markets_txt = "max %d markets" % LIVE_MAX_MARKETS_PER_DAY
    if money_cap < LIVE_MAX_MARKETS_PER_DAY:
        markets_txt += " (money cap allows only %d)" % money_cap
    return (
        "%s | %s | %s\n"
        "same rule as paper: YES at or below %gc (counting words excluded), sell YES %dc (= buy NO %dc) x %.2f contracts%s\n"
        "$%.2f/market, %s, max $%.2f resting\n"
        "cancel %s CT | post_only=%s | take_if_cheap=%s | server_expiry=%s | order_api=%s"
    ) % (VERSION, mode, where, QUALIFY_MAX_YES_CENTS, LIMIT_YES_CENTS, live_no_price_cents(),
         sized["contracts"], " (CAPPED)" if sized["capped"] else "",
         live_collateral_per_market(), markets_txt, LIVE_MAX_DAILY_COLLATERAL, LIVE_CANCEL_CT,
         LIVE_POST_ONLY, LIVE_TAKE_IF_ALREADY_CHEAP, LIVE_USE_SERVER_SIDE_EXPIRY, LIVE_ORDER_API)


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
