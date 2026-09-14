#!/usr/bin/env python3
"""Flag cron-job.org schedules whose LOCAL weekday is not the UTC day they land on.

WHY THIS EXISTS (2026-09-14). `StockPulz-prescreener` was scheduled
`23:00 America/New_York, wdays Mon-Fri`. cron-job.org evaluates the weekday in
the job's OWN timezone, so 23:00 ET Mon-Fri lands at 03:00 UTC **Tue-Sat**:

    Sun 23:00 ET -> Mon 03:00 UTC    never fired (Sunday not in Mon-Fri)
    Fri 23:00 ET -> Sat 03:00 UTC    fired, and Saturday screens no stocks

So **Monday had no punctual prescreener, ever** — measured across 8 consecutive
Mondays, zero 03:00 dispatches. Monday's screener cache came only from GitHub's
own cron, which runs 1.6-6 h late; on 2026-07-27 and 2026-08-03 it landed at
10:49 against an 11:00 morning run, an **11-minute margin** on the highest-risk
path in the app. Nothing detected it, because every run reported success.

WHAT IT CHECKS, and why it is shaped this way. Encoding "what each job is for"
would need a hand-maintained map of 19 purposes that rots the first time a
schedule moves. Instead it checks a self-maintaining property: **a job whose
local weekday set differs from the UTC weekday set it lands on is a day-shift**,
which is either a bug or a deliberate choice. Deliberate ones go in
ACKNOWLEDGED with a real reason, the same reasoned-allowlist pattern used by
tests/test_canary_reads_the_live_store.py — an unexplained allowlist is how a
class re-opens quietly.

A UTC-scheduled job can never shift, so this is silent for most of the fleet.

Usage:  CRONJOB_API_KEY=... python3 scripts/audit_cron_schedules.py
Exits non-zero when an unacknowledged shift is found.
"""
import datetime as dt
import json
import os
import sys
import urllib.request

try:
    from zoneinfo import ZoneInfo
except ImportError:                                     # py<3.9 fallback
    import pytz

    def ZoneInfo(name):                                 # noqa: N802
        return pytz.timezone(name)

API = "https://api.cron-job.org/jobs"
DAY = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}

MIN_REASON = 120

# A deliberate day-shift, and WHY. Keep the reason long enough to be a reason.
ACKNOWLEDGED = {
    "StockPulz-prescreener": (
        "Deliberate: it runs at 23:00 ET the NIGHT BEFORE the market day it "
        "serves, so its local weekdays are Sun-Thu precisely so the dispatches "
        "land Mon-Fri UTC, matching the 11:00 UTC morning run that consumes the "
        "cache. Corrected 2026-09-14 from Mon-Fri ET, which landed Tue-Sat and "
        "left Monday with no punctual prescreener for at least 8 weeks. "
        "ET-anchored on purpose: 23:00 ET is 03:00 UTC under EDT and 04:00 "
        "under EST, always the next calendar day, so the mapping is DST-safe."
    ),
}


def utc_landing_days(timezone, hours, wdays, ref=None, span=14):
    """UTC weekday numbers a schedule actually fires on. Pure — unit-testable.

    Replays `span` days from `ref` rather than doing modular arithmetic, so DST
    transitions are handled by the tz database instead of by a clever offset.
    """
    ref = ref or dt.date(2026, 9, 14)
    tz = ZoneInfo(timezone)
    landed = set()
    for off in range(span):
        d = ref + dt.timedelta(days=off)
        if (d.weekday() + 1) % 7 not in wdays:
            continue
        for h in hours:
            naive = dt.datetime(d.year, d.month, d.day, h, 0)
            try:
                local = naive.replace(tzinfo=tz)
            except TypeError:                            # pytz
                local = tz.localize(naive)
            landed.add((local.astimezone(dt.timezone.utc).weekday() + 1) % 7)
    return landed


def shifts(schedule):
    """(local_days, utc_days) when a schedule lands on different UTC days."""
    tz = schedule.get("timezone")
    hours = schedule.get("hours") or []
    wdays = schedule.get("wdays") or []
    if not tz or hours == [-1] or wdays == [-1] or not wdays or not hours:
        return None                                      # every-hour/every-day
    utc = utc_landing_days(tz, hours, wdays)
    local = set(wdays)
    return (local, utc) if utc != local else None


def _fetch(key):
    req = urllib.request.Request(API, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(req, timeout=30) as f:
        return json.load(f)["jobs"]


def main():
    key = os.environ.get("CRONJOB_API_KEY", "").strip()
    if not key:
        print("CRONJOB_API_KEY not set — cannot audit.", file=sys.stderr)
        return 2
    problems = []
    print(f"{'job':<28}{'tz':<18}{'local days':<22}{'lands on (UTC)':<22}verdict")
    for job in sorted(_fetch(key), key=lambda j: j.get("title", "")):
        title = job.get("title", "")
        sh = shifts(job.get("schedule", {}))
        if not sh:
            continue
        local, utc = sh
        reason = ACKNOWLEDGED.get(title, "")
        if len(reason) >= MIN_REASON:
            verdict = "ok (acknowledged)"
        elif reason:
            verdict = "REASON TOO SHORT"
            problems.append(title)
        else:
            verdict = "UNACKNOWLEDGED SHIFT"
            problems.append(title)
        print(f"{title[:27]:<28}{job['schedule']['timezone']:<18}"
              f"{','.join(DAY[d] for d in sorted(local)):<22}"
              f"{','.join(DAY[d] for d in sorted(utc)):<22}{verdict}")
    if problems:
        print(f"\n{len(problems)} unacknowledged day-shift(s): "
              f"{', '.join(problems)}\nEither fix the wdays or add a reason to "
              f"ACKNOWLEDGED explaining why the shift is intended.")
        return 1
    print("\nNo unacknowledged day-shifts.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
