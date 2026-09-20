"""US Central Time helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config as C


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_ct() -> datetime:
    return now_utc().astimezone(C.CT)


def today_ct() -> str:
    return now_ct().strftime("%Y-%m-%d")


def at(date_str: str, hhmm: str) -> datetime:
    """'2026-09-18', '17:32:30' -> aware datetime in Central time."""
    parts = [int(x) for x in hhmm.split(":")]
    hh, mm = parts[0], parts[1]
    ss = parts[2] if len(parts) > 2 else 0
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=C.CT)


def fire_at(date_str: str) -> datetime:
    return at(date_str, C.FIRE_AT_CT)


def cancel_at(date_str: str, cancel_ct: str) -> datetime:
    return at(date_str, cancel_ct)


def last_cancel_at(date_str: str) -> datetime:
    return max(cancel_at(date_str, v["cancel_ct"]) for v in C.VARIANTS)


def settle_start(date_str: str) -> datetime:
    return at(date_str, C.SETTLE_AFTER_CT)


def prepare_at(date_str: str) -> datetime:
    return fire_at(date_str) - timedelta(seconds=90)


def parse_dt(val):
    if val is None:
        return None
    if isinstance(val, datetime):
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)
    raw = str(val).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt(when, with_seconds: bool = True) -> str:
    if when is None:
        return "never"
    when = parse_dt(when)
    pattern = "%-I:%M:%S %p CT" if with_seconds else "%-I:%M %p CT"
    return when.astimezone(C.CT).strftime(pattern)


def loop_interval(now: datetime) -> float:
    """Fast while the action is happening, slow the rest of the day."""
    d = now.astimezone(C.CT).strftime("%Y-%m-%d")
    start = fire_at(d) - timedelta(seconds=150)
    end = last_cancel_at(d) + timedelta(seconds=C.TRACK_AFTER_LAST_CANCEL_S + 60)
    if start <= now <= end:
        return 0.5
    return 20.0
