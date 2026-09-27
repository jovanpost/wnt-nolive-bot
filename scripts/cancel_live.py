#!/usr/bin/env python3
"""Cancel every resting LIVE order. Backup for the in-app cancel, run by GitHub Actions.
Mirrors wnt-nofade-bot's scripts/cancel_all.py.
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from nolive import clock, config as C, live, notify, store  # noqa: E402
from nolive.kalshi import KalshiClient  # noqa: E402


def wait_until(hhmm: str, max_wait_seconds: int = 2700) -> None:
    import time
    target = clock.at(clock.today_ct(), hhmm)
    remaining = (target - clock.now_ct()).total_seconds()
    if remaining <= 0:
        print("%s CT has already passed; cancelling immediately." % hhmm)
        return
    if remaining > max_wait_seconds:
        print("%.0fs until %s CT is longer than the %ds cap. Exiting without cancelling."
              % (remaining, hhmm, max_wait_seconds))
        sys.exit(0)
    print("Sleeping %.0fs until %s CT..." % (remaining, hhmm))
    if remaining > 30:
        time.sleep(remaining - 20)
    while (target - clock.now_ct()).total_seconds() > 0:
        time.sleep(0.5)
    print("Now %s. Cancelling." % clock.fmt(clock.now_ct()))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait-until", metavar="HH:MM")
    parser.add_argument("--series", default=C.SERIES)
    parser.add_argument("--dry", action="store_true")
    args = parser.parse_args()

    if args.wait_until:
        wait_until(args.wait_until)

    client = KalshiClient()
    if not client.authenticated:
        print("No private key loaded -- cannot cancel. This is a hard failure.")
        notify.send("🚨 [LIVE] Backup cancel could not run: no Kalshi key in the GitHub Actions "
                    "environment. Check your resting orders by hand.")
        return 1

    resting = client.get_resting_orders(series_prefix=args.series)
    print("%d resting order(s) in %s" % (len(resting), args.series))
    for order in resting:
        print("  %s  id=%s  remaining=%s" % (order.get("ticker"), order.get("order_id"),
                                              order.get("remaining_count")))

    if not resting:
        print("Nothing to cancel.")
        notify.send("✅ [LIVE] Backup cancel ran: nothing was resting.", quiet=True)
        return 0
    if args.dry:
        print("--dry set, stopping here.")
        return 0

    ids = [o["order_id"] for o in resting if o.get("order_id")]
    cancelled, failed = client.batch_cancel(ids)
    print("cancelled=%d failed=%d" % (cancelled, len(failed)))

    import time
    time.sleep(2)
    leftover = client.get_resting_orders(series_prefix=args.series)
    verified = len(leftover) == 0

    try:
        store.init_db()
        store.mark_all_live_resting_cancelled(clock.today_ct())
        store.log_activity("live_backup_cancel", "cancelled=%d remaining=%d verified=%s"
                           % (cancelled, len(leftover), verified))
    except Exception as exc:
        print("(could not write to the database: %s)" % exc)

    if verified:
        notify.send("🛡 [LIVE] Backup cancel fired\nCancelled %d resting order(s) at %s.\nNothing left resting."
                    % (cancelled, clock.fmt(clock.now_ct())))
        print("VERIFIED: nothing resting.")
        return 0

    notify.send("🚨🚨 [LIVE] BACKUP CANCEL INCOMPLETE\n%d order(s) STILL RESTING at %s.\n"
                "Open Kalshi and cancel them by hand immediately." % (len(leftover), clock.fmt(clock.now_ct())))
    print("FAILED: %d still resting." % len(leftover))
    return 1


if __name__ == "__main__":
    sys.exit(main())
