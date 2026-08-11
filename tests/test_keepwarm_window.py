"""The in-process keep-warm thread — the one that actually decides the bill.

It ran unconditionally 24/7 until 2026-08-11 while claiming "zero extra cost",
and because it pings from inside the process at a cadence nothing throttles, it
silently overrode every external schedule: narrowing the cron-job.org job and
keepwarm.yml changed nothing while this thread was alive.
"""
import os
import sys
import datetime as dt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
import pytz

import webhook


def _et(hour):
    return dt.datetime(2026, 8, 11, hour, 30,
                       tzinfo=pytz.timezone("America/New_York"))


class TestKeepWarmWindow:
    def test_inside_the_window_pings(self):
        for h in sorted(webhook._KEEPWARM_HOURS_ET):
            assert webhook._in_keepwarm_window(_et(h)) is True, f"{h}:30 ET should ping"

    def test_outside_the_window_does_not(self):
        outside = set(range(24)) - webhook._KEEPWARM_HOURS_ET
        assert outside, "a 24-hour window is the bug this guards against"
        for h in sorted(outside):
            assert webhook._in_keepwarm_window(_et(h)) is False, f"{h}:30 ET must NOT ping"

    def test_the_documented_symptom_is_now_false(self):
        """0.15s at 01:42 ET is what exposed it — the service was warm hours
        outside any window it was supposed to be asleep in."""
        assert webhook._in_keepwarm_window(_et(1)) is False

    def test_the_window_covers_the_morning_relay_with_buffer(self):
        """/trigger/morning fires at 7 AM ET and delivery is NOT idempotent — a
        relay landing on a cold server means no picks that day."""
        assert webhook._in_keepwarm_window(_et(7)) is True
        assert webhook._in_keepwarm_window(_et(6)) is True, "need an hour of buffer before 7 AM"

    def test_a_clock_failure_keeps_the_service_warm(self, monkeypatch):
        """Fail OPEN. A broken clock costing a few instance-hours is recoverable;
        a bot that silently stops answering is not."""
        monkeypatch.setitem(sys.modules, "pytz", None)
        assert webhook._in_keepwarm_window() is True

    def test_window_is_configurable_without_a_deploy(self):
        assert webhook._KEEPWARM_HOURS_ET == {6, 7, 8, 9, 10, 11, 12, 13}, \
            "default must match the live cron-job.org window (6 AM-2 PM ET)"
