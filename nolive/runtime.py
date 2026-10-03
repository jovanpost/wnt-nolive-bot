"""Start the bot's background work in exactly ONE place: Streamlit Cloud or the server.

Both entry points call start_workers():
  streamlit_app.py  -> start_workers(where="streamlit")            never blocks the page
  worker.py         -> start_workers(where="vps", block=True)      headless, run by systemd

RUN_WORKERS = "false" (a setting, default true) makes a place dashboard-only: the tables are
checked, nothing else starts. The worker lease (nolive/lease.py) makes sure only one place runs
the loops even when both have RUN_WORKERS on: the second one waits.
"""
from __future__ import annotations

import logging
import os
import threading

from . import config as C, lease, live, notify, pipeline, store

log = logging.getLogger("nolive.runtime")

NAME = "wnt-nolive-bot"
WAIT_S = 10                      # how often a waiting place looks at the lease again

_lock = threading.Lock()
_started = False
_keeper: lease.Keeper | None = None
_exit_on_lost = False
INFO = {"where": "", "host": "", "run_workers": True, "loops": []}


def host(where: str = "") -> str:
    """Who we are in the lease: the KLSH_HOST setting, else the entry point's own name."""
    return (C._secret("KLSH_HOST", "") or where or "unknown").strip()


def run_workers() -> bool:
    return C._flag("RUN_WORKERS", True)


def _tag_status_commands() -> None:
    """Every /..._status reply ends with the place it came from."""
    for name, fn in list(notify._handlers.items()):
        if name.endswith("status") and not getattr(fn, "_tagged", False):
            def tagged(args, _fn=fn):
                return "%s\nhost: %s · %s" % (_fn(args), INFO["host"], C.VERSION)
            tagged._tagged = True
            notify._handlers[name] = tagged


def _start_loops() -> None:
    notify.start_listener()
    threading.Thread(target=pipeline.run_forever, name="nolive-loop", daemon=True).start()
    INFO["loops"] = ["nolive-loop"]
    if live.enabled():
        threading.Thread(target=live.run_forever, name="nolive-live-loop", daemon=True).start()
        INFO["loops"].append("nolive-live-loop")
    msg = "🔑 %s workers started on %s" % (C.VERSION, INFO["host"])
    log.info(msg)
    store.log_activity("workers_start", "host=%s loops=%s" % (INFO["host"], ",".join(INFO["loops"])))
    notify.send(msg, quiet=True)


def _on_lost(other: str) -> None:
    msg = "⚠️ %s on %s LOST the worker lease to %s. Workers stopped here." % (C.VERSION, INFO["host"], other)
    log.error(msg)
    try:
        store.log_activity("workers_lost", "host=%s now=%s" % (INFO["host"], other))
        notify.send(msg)
    finally:
        if _exit_on_lost:
            os._exit(3)          # systemd starts it again; it then waits for the lease


def start_workers(where: str, block: bool = False, exit_on_lost: bool = False) -> dict:
    """Idempotent. Returns INFO. With block=True it returns once the loops are running."""
    global _started, _keeper, _exit_on_lost
    with _lock:
        if _started:
            return INFO
        _started = True
        _exit_on_lost = exit_on_lost
        INFO.update(where=where, host=host(where), run_workers=run_workers())
        store.init_db()
        if not INFO["run_workers"]:
            lease.GATE.set("dashboard", name=NAME, holder=INFO["host"])
            log.info("dashboard only (RUN_WORKERS=false): no listener, no loops")
            return INFO
        pipeline.register_commands()
        live.register_commands()
        _tag_status_commands()
        _keeper = lease.Keeper(store.engine(), NAME, INFO["host"], on_lost=_on_lost)
        lease.GATE.set("waiting", name=NAME, holder=INFO["host"], other="?")

    def go() -> None:
        if _keeper.wait(every_s=WAIT_S, say=log.info):
            _start_loops()

    if block:
        go()
    else:
        threading.Thread(target=go, name="nolive-lease-wait", daemon=True).start()
    return INFO


def interrupt() -> None:
    """Safe inside a signal handler: only wakes a worker that is still waiting for the lease."""
    if _keeper is not None:
        _keeper._stop.set()


def stop() -> None:
    """worker.py on SIGTERM: give the lease back so the other place can start at once."""
    if _keeper is not None:
        _keeper.stop(give_up=True)


def banner() -> tuple:
    """(level, text) for the top of the dashboard. level: info | warning | error | ok."""
    g = lease.GATE
    held_by = ""
    if g.mode in ("dashboard", "waiting", "lost"):
        try:
            cur = lease.current(store.engine(), NAME)
            held_by = "%s, last heartbeat %.0fs ago" % (cur["holder"], cur["age_s"]) if cur else "nobody"
        except Exception:
            held_by = "unknown (the lease table could not be read)"
    if g.mode == "dashboard":
        return ("info", "Dashboard only: the workers do not run here. Worker lease: %s." % held_by)
    if g.mode == "waiting":
        return ("warning", "Workers are WAITING here: the worker lease is held by %s." % held_by)
    if g.mode == "lost":
        return ("error", "Workers STOPPED here: the worker lease was taken by %s. Reboot the app to try again." % held_by)
    if g.mode == "held" and not lease.may_trade():
        return ("error", "Lease not renewed for over %ds (database unreachable?). No new orders until it renews." % lease.SAFE_S)
    return ("ok", "Workers run here (%s)." % INFO["host"])


def reset_for_tests() -> None:
    global _started, _keeper, _exit_on_lost
    if _keeper is not None:
        _keeper.stop(give_up=False)
    _started, _keeper, _exit_on_lost = False, None, False
    INFO.update(where="", host="", run_workers=True, loops=[])
    lease.GATE.reset()
