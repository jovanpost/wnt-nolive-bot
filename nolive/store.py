"""Database layer (Supabase Postgres, or a local SQLite file for testing).

WRITES only ever touch nolive_* tables.
The nofade reads live in nolive/nofade.py and are SELECT-only.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from . import clock, config as C

log = logging.getLogger("nolive.store")

_engine: Engine | None = None
_lock = threading.Lock()

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "sql" / "001_nolive_schema.sql"

_TS_KEYS = ("_at", "ts")


def using_postgres() -> bool:
    return bool(C.DATABASE_URL)


def engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine
    with _lock:
        if _engine is not None:
            return _engine
        if C.DATABASE_URL:
            url = C.DATABASE_URL
            if url.startswith("postgres://"):
                url = "postgresql+psycopg2://" + url[len("postgres://"):]
            elif url.startswith("postgresql://"):
                url = "postgresql+psycopg2://" + url[len("postgresql://"):]
            _engine = create_engine(url, pool_pre_ping=True, pool_recycle=280,
                                    pool_size=3, max_overflow=2, future=True)
        else:
            _engine = create_engine("sqlite:///%s" % C.SQLITE_PATH,
                                    connect_args={"check_same_thread": False}, future=True)
        return _engine


def reset_engine(new_engine: Engine | None = None) -> None:
    """Tests only."""
    global _engine
    _engine = new_engine


def _ts(dt):
    """Postgres takes datetimes. SQLite gets ISO text (no deprecated adapters)."""
    if dt is None:
        return None
    if using_postgres():
        return dt
    # SQLite compares text, so keep every stored time in UTC in one format
    return clock.parse_dt(dt).astimezone(timezone.utc).isoformat()


def _row(mapping) -> dict:
    out = dict(mapping)
    for k, v in list(out.items()):
        if isinstance(v, str) and (k.endswith("_at") or k.endswith("_ts") or k == "ts") and v:
            out[k] = clock.parse_dt(v)
    return out


def _split_statements(sql: str) -> list:
    statements, buf = [], []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            stmt = "\n".join(buf).strip().rstrip(";")
            if stmt:
                statements.append(stmt)
            buf = []
    return statements


def _for_sqlite(stmt: str) -> str:
    stmt = stmt.replace("bigserial primary key", "integer primary key autoincrement")
    stmt = stmt.replace("now()", "CURRENT_TIMESTAMP")
    return stmt


def init_db() -> None:
    statements = _split_statements(SCHEMA_FILE.read_text(encoding="utf-8"))
    for stmt in statements:
        if not using_postgres():
            stmt = _for_sqlite(stmt)
        try:
            with engine().begin() as conn:
                conn.execute(text(stmt))
        except Exception as exc:
            log.warning("init_db statement skipped: %s | %s", exc, stmt[:80])
    log.info("nolive tables ready (%s)", "postgres" if using_postgres() else "sqlite")


# ---------------------------------------------------------------- state / activity
def log_activity(kind: str, message: str) -> None:
    try:
        with engine().begin() as conn:
            conn.execute(text("insert into nolive_activity (kind, message, ts) values (:k, :m, :t)"),
                         {"k": kind, "m": message[:2000], "t": _ts(clock.now_utc())})
    except Exception as exc:
        log.warning("activity log failed: %s", exc)


def recent_activity(limit: int = 40) -> list:
    with engine().connect() as conn:
        rows = conn.execute(text("select ts, kind, message from nolive_activity order by id desc limit :n"),
                            {"n": limit}).mappings().all()
    return [_row(r) for r in rows]


def get_state(key: str, default: Any = None) -> Any:
    with engine().connect() as conn:
        row = conn.execute(text("select value from nolive_state where key = :k"), {"k": key}).mappings().first()
    if not row:
        return default
    val = row["value"]
    if isinstance(val, str):
        try:
            return json.loads(val)
        except ValueError:
            return val
    return val


def set_state(key: str, value: Any) -> None:
    with engine().begin() as conn:
        conn.execute(text("""
            insert into nolive_state (key, value, updated_at) values (:k, :v, :t)
            on conflict (key) do update set value = excluded.value, updated_at = excluded.updated_at
        """), {"k": key, "v": json.dumps(value), "t": _ts(clock.now_utc())})


# ---------------------------------------------------------------- runs
def get_run(event_date: str) -> dict | None:
    with engine().connect() as conn:
        row = conn.execute(text("select * from nolive_runs where event_date = :d"), {"d": event_date}).mappings().first()
    return _row(row) if row else None


def claim_run(row: dict) -> tuple:
    """Insert tonight's run. Returns (run, created). created=False means another instance already did."""
    params = dict(row)
    for k in ("scheduled_at", "fired_at"):
        params[k] = _ts(params.get(k))
    with engine().begin() as conn:
        res = conn.execute(text("""
            insert into nolive_runs (event_date, event_ticker, status, scheduled_at, fired_at, late_seconds,
                limit_yes_cents, dollars, skip_at_or_above, cancel_times, fee_type, fee_multiplier, maker_rate,
                version, notes)
            values (:event_date, :event_ticker, :status, :scheduled_at, :fired_at, :late_seconds,
                :limit_yes_cents, :dollars, :skip_at_or_above, :cancel_times, :fee_type, :fee_multiplier,
                :maker_rate, :version, :notes)
            on conflict (event_date) do nothing
        """), params)
        created = (res.rowcount or 0) > 0
    return get_run(row["event_date"]), created


def update_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets, params = [], {"id": run_id}
    for i, (k, v) in enumerate(fields.items()):
        if k.endswith("_at"):
            v = _ts(v)
        sets.append("%s = :p%d" % (k, i))
        params["p%d" % i] = v
    with engine().begin() as conn:
        conn.execute(text("update nolive_runs set %s where id = :id" % ", ".join(sets)), params)


def recent_runs(limit: int = 60) -> list:
    with engine().connect() as conn:
        rows = conn.execute(text("select * from nolive_runs order by event_date desc limit :n"), {"n": limit}).mappings().all()
    return [_row(r) for r in rows]


def unsettled_runs() -> list:
    with engine().connect() as conn:
        rows = conn.execute(text("""
            select * from nolive_runs
            where status not in ('settled', 'missed', 'no_event') and qualified > 0
            order by event_date
        """)).mappings().all()
    return [_row(r) for r in rows]


# ---------------------------------------------------------------- markets
def insert_market(row: dict) -> None:
    params = dict(row)
    params["pre_ts"] = _ts(params.get("pre_ts"))
    with engine().begin() as conn:
        conn.execute(text("""
            insert into nolive_markets (run_id, event_date, market_ticker, word, title, is_counting, count_needed,
                market_status, last_price_cents, yes_bid_cents, yes_ask_cents, price_basis, yes_price_cents,
                qualified, skip_reason, book_yes, book_no, yes_size_at_limit, queue_ahead,
                pre_ts, pre_yes_bid_cents, pre_no_bid_cents)
            values (:run_id, :event_date, :market_ticker, :word, :title, :is_counting, :count_needed,
                :market_status, :last_price_cents, :yes_bid_cents, :yes_ask_cents, :price_basis, :yes_price_cents,
                :qualified, :skip_reason, :book_yes, :book_no, :yes_size_at_limit, :queue_ahead,
                :pre_ts, :pre_yes_bid_cents, :pre_no_bid_cents)
            on conflict (run_id, market_ticker) do nothing
        """), params)


_MARKET_LIGHT = """id, run_id, event_date, market_ticker, word, is_counting, count_needed, market_status,
    last_price_cents, yes_bid_cents, yes_ask_cents, price_basis, yes_price_cents, qualified, skip_reason,
    yes_size_at_limit, queue_ahead, pre_ts, pre_yes_bid_cents, pre_no_bid_cents, result, result_source, result_at"""


def markets_for_run(run_id: int, light: bool = True) -> list:
    cols = _MARKET_LIGHT if light else "*"
    with engine().connect() as conn:
        rows = conn.execute(text("select %s from nolive_markets where run_id = :r order by id" % cols),
                            {"r": run_id}).mappings().all()
    return [_row(r) for r in rows]


def all_markets_light() -> list:
    with engine().connect() as conn:
        rows = conn.execute(text("select %s from nolive_markets order by id" % _MARKET_LIGHT)).mappings().all()
    return [_row(r) for r in rows]


def set_market_result(run_id: int, market_ticker: str, result: str, source: str) -> None:
    with engine().begin() as conn:
        conn.execute(text("""
            update nolive_markets set result = :res, result_source = :src, result_at = :t
            where run_id = :r and market_ticker = :m
        """), {"res": result, "src": source, "t": _ts(clock.now_utc()), "r": run_id, "m": market_ticker})


# ---------------------------------------------------------------- orders
def insert_order(row: dict) -> None:
    params = dict(row)
    for k in ("cancel_at", "placed_at"):
        params[k] = _ts(params.get(k))
    with engine().begin() as conn:
        conn.execute(text("""
            insert into nolive_orders (run_id, event_date, event_ticker, market_ticker, word, variant_id, cancel_ct,
                cancel_at, limit_yes_cents, contracts, dollars, placed_at, yes_price_at_place, is_counting, status)
            values (:run_id, :event_date, :event_ticker, :market_ticker, :word, :variant_id, :cancel_ct,
                :cancel_at, :limit_yes_cents, :contracts, :dollars, :placed_at, :yes_price_at_place, :is_counting, :status)
            on conflict (event_date, market_ticker, variant_id) do nothing
        """), params)


def orders_for_run(run_id: int) -> list:
    with engine().connect() as conn:
        rows = conn.execute(text("select * from nolive_orders where run_id = :r order by id"), {"r": run_id}).mappings().all()
    return [_row(r) for r in rows]


def all_orders() -> list:
    with engine().connect() as conn:
        rows = conn.execute(text("select * from nolive_orders order by event_date, id")).mappings().all()
    return [_row(r) for r in rows]


def update_order(order_id: int, **fields: Any) -> None:
    if not fields:
        return
    sets, params = [], {"id": order_id}
    for i, (k, v) in enumerate(fields.items()):
        if k.endswith("_at"):
            v = _ts(v)
        sets.append("%s = :p%d" % (k, i))
        params["p%d" % i] = v
    with engine().begin() as conn:
        conn.execute(text("update nolive_orders set %s where id = :id" % ", ".join(sets)), params)


# ---------------------------------------------------------------- fills / trades / depth
def insert_fill(row: dict) -> bool:
    params = dict(row)
    params["ts"] = _ts(params["ts"])
    with engine().begin() as conn:
        res = conn.execute(text("""
            insert into nolive_fills (run_id, event_date, market_ticker, ts, kind, contracts, price_cents,
                fee_cents, source, ref)
            values (:run_id, :event_date, :market_ticker, :ts, :kind, :contracts, :price_cents,
                :fee_cents, :source, :ref)
            on conflict (event_date, market_ticker, ref) do nothing
        """), params)
        return (res.rowcount or 0) > 0


def fills_for_run(run_id: int) -> list:
    with engine().connect() as conn:
        rows = conn.execute(text("select * from nolive_fills where run_id = :r order by ts, id"), {"r": run_id}).mappings().all()
    return [_row(r) for r in rows]


def recent_fills(limit: int = 100) -> list:
    with engine().connect() as conn:
        rows = conn.execute(text("select * from nolive_fills order by id desc limit :n"), {"n": limit}).mappings().all()
    return [_row(r) for r in rows]


def insert_trades(rows: list) -> None:
    if not rows:
        return
    params = []
    for r in rows:
        p = dict(r)
        p["ts"] = _ts(p["ts"])
        params.append(p)
    with engine().begin() as conn:
        conn.execute(text("""
            insert into nolive_trades (trade_id, event_date, market_ticker, ts, yes_price_cents, contracts, taker_side)
            values (:trade_id, :event_date, :market_ticker, :ts, :yes_price_cents, :contracts, :taker_side)
            on conflict (trade_id) do nothing
        """), params)


def trade_ids(event_date: str) -> set:
    with engine().connect() as conn:
        rows = conn.execute(text("select trade_id from nolive_trades where event_date = :d"), {"d": event_date}).all()
    return set(r[0] for r in rows)


def insert_depth(row: dict) -> None:
    params = dict(row)
    params["ts"] = _ts(params["ts"])
    with engine().begin() as conn:
        conn.execute(text("""
            insert into nolive_depth (run_id, event_date, market_ticker, ts, kind, best_yes_bid, best_no_bid,
                yes_size_total, no_size_total, yes_size_at_limit, book_yes, book_no)
            values (:run_id, :event_date, :market_ticker, :ts, :kind, :best_yes_bid, :best_no_bid,
                :yes_size_total, :no_size_total, :yes_size_at_limit, :book_yes, :book_no)
        """), params)


def counts() -> dict:
    out = {}
    with engine().connect() as conn:
        for t in ("nolive_runs", "nolive_orders", "nolive_fills", "nolive_trades", "nolive_depth"):
            try:
                out[t] = int(conn.execute(text("select count(*) from %s" % t)).scalar() or 0)
            except Exception:
                out[t] = -1
    return out


def depth_for_market(event_date: str, market_ticker: str, limit: int = 60) -> list:
    with engine().connect() as conn:
        rows = conn.execute(text("""
            select ts, kind, best_yes_bid, best_no_bid, yes_size_total, no_size_total, yes_size_at_limit
            from nolive_depth where event_date = :d and market_ticker = :m order by ts limit :n
        """), {"d": event_date, "m": market_ticker, "n": limit}).mappings().all()
    return [_row(r) for r in rows]
