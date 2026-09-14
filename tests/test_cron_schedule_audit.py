"""The ET-weekday / UTC-day shift that hid Monday's missing prescreener.

`StockPulz-prescreener` ran at 23:00 America/New_York with wdays Mon-Fri.
cron-job.org evaluates the weekday in the JOB'S OWN timezone, so those five
local nights landed at 03:00 UTC on Tue-Sat. Sunday night was never in the set,
so **Monday never got a punctual prescreener** — zero 03:00 dispatches across 8
consecutive Mondays. Monday's cache came only from GitHub's own cron (1.6-6 h
late); twice it landed 11 minutes before the 11:00 morning run.

The failure was invisible: every run reported success, and the job's own
"lastExecution" was always consistent with its schedule. Only comparing the
LOCAL weekday set against the UTC set it lands on reveals it.

These tests pin the pure mapping. They need no network and no API key.
"""
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pytest

import audit_cron_schedules as aud

SUN, MON, TUE, WED, THU, FRI, SAT = range(7)
WEEKDAYS_UTC = {MON, TUE, WED, THU, FRI}


class TestTheHistoricalBug:
    def test_the_old_schedule_landed_tue_to_sat_and_missed_monday(self):
        """The exact defect, replayed."""
        landed = aud.utc_landing_days("America/New_York", [23],
                                      [MON, TUE, WED, THU, FRI])
        assert landed == {TUE, WED, THU, FRI, SAT}
        assert MON not in landed, "Monday was supposed to be the missing day"

    def test_the_corrected_schedule_lands_exactly_on_weekdays(self):
        landed = aud.utc_landing_days("America/New_York", [23],
                                      [SUN, MON, TUE, WED, THU])
        assert landed == WEEKDAYS_UTC

    def test_the_fix_is_dst_safe(self):
        """23:00 ET is 03:00 UTC under EDT and 04:00 under EST — always the
        NEXT calendar day, so the weekday mapping holds year-round. A fix that
        silently broke every November would be worse than the bug."""
        for ref in (dt.date(2026, 6, 15),      # deep EDT
                    dt.date(2026, 12, 7),      # deep EST
                    dt.date(2026, 10, 28),     # spans the EDT->EST change
                    dt.date(2027, 3, 8)):      # spans EST->EDT
            landed = aud.utc_landing_days("America/New_York", [23],
                                          [SUN, MON, TUE, WED, THU], ref=ref)
            assert landed == WEEKDAYS_UTC, f"broke around {ref}"


class TestShiftDetection:
    def test_a_utc_schedule_never_shifts(self):
        """Most of the fleet is UTC-scheduled and must stay silent, or the
        audit cries wolf on 17 healthy jobs."""
        assert aud.shifts({"timezone": "UTC", "hours": [20],
                           "wdays": [MON, TUE, WED, THU, FRI]}) is None

    def test_a_morning_et_job_does_not_shift(self):
        """07:00 ET is 11:00 UTC the SAME day — the morning trigger is ET
        anchored too, and it is correct. The audit must not flag it."""
        assert aud.shifts({"timezone": "America/New_York", "hours": [7],
                           "wdays": [MON, TUE, WED, THU, FRI]}) is None

    def test_the_late_night_et_job_is_detected(self):
        got = aud.shifts({"timezone": "America/New_York", "hours": [23],
                          "wdays": [MON, TUE, WED, THU, FRI]})
        assert got is not None, "the audit no longer detects the known bug"
        local, utc = got
        assert local == {MON, TUE, WED, THU, FRI} and utc == {TUE, WED, THU, FRI, SAT}

    def test_an_every_hour_schedule_is_not_flagged(self):
        assert aud.shifts({"timezone": "UTC", "hours": [-1], "wdays": [-1]}) is None
        assert aud.shifts({"timezone": "America/New_York", "hours": [23],
                           "wdays": []}) is None


class TestTheAllowlistIsReasoned:
    """An unexplained allowlist is how a class re-opens quietly."""

    def test_the_prescreener_shift_is_acknowledged(self):
        assert "StockPulz-prescreener" in aud.ACKNOWLEDGED

    def test_every_acknowledgement_carries_a_real_reason(self):
        for name, reason in aud.ACKNOWLEDGED.items():
            assert len(reason) >= aud.MIN_REASON, (
                f"{name}'s reason is {len(reason)} chars; an allowlist entry "
                f"without an argument is just a mute button"
            )

    def test_the_reason_names_what_makes_the_shift_correct(self):
        r = aud.ACKNOWLEDGED["StockPulz-prescreener"].lower()
        assert "night before" in r and "dst" in r, (
            "the reason must say WHY landing a day later is intended"
        )
