"""The in-process keep-warm thread — DISABLED, but deliberately not deleted.

History worth keeping: this ran unconditionally 24/7 while its comment claimed
"zero extra cost". Because it pings from inside the process at a cadence nothing
can throttle, it silently OVERRODE every external schedule — narrowing the
cron-job.org job and keepwarm.yml changed nothing at all while it was alive.

It is now off by default, because StockPulz runs on Render's Starter plan where
the service never idles. Kept behind KEEPALIVE_ENABLED because a downgrade to
Free brings the need straight back, and a cold first /start is what makes the
bot look broken to a new user.
"""
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest
import pytz

import webhook


def _et(hour):
    return dt.datetime(2026, 8, 11, hour, 30, tzinfo=pytz.timezone("America/New_York"))


class TestDisabledByDefault:
    def test_keepalive_is_off(self):
        """Starter never spins down, so a self-ping wakes an already-running
        service. Verified empirically on 2026-08-11: StockPulz stayed warm
        through 45 min of total silence while PiQValet and pricedrop (both Free,
        same account) went cold in 20 min."""
        assert webhook._KEEPALIVE_ENABLED is False

    def test_the_loop_returns_without_pinging(self, monkeypatch):
        """The real guarantee: not merely that a flag is False, but that no
        request is issued. A flag nothing reads would be the same bug in a new
        costume."""
        calls = []
        monkeypatch.setattr(webhook.requests, "get",
                            lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
                                AssertionError("keep-alive pinged while disabled")))
        monkeypatch.setattr(webhook.time, "sleep",
                            lambda *_: (_ for _ in ()).throw(
                                AssertionError("keep-alive entered its sleep loop while disabled")))
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
        webhook._keep_alive_loop()          # must return immediately
        assert calls == []

    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("true", True), ("YES", True), ("on", True),
        ("", False), ("0", False), ("false", False), ("no", False),
    ])
    def test_enable_flag_parsing(self, raw, expected):
        got = raw.strip().lower() in ("1", "true", "yes", "on")
        assert got is expected


class TestWindowMechanismStillWorks:
    """Exercised with an EXPLICIT window so these hold whatever the default is —
    this is the code path a downgrade to Free would depend on."""

    @pytest.fixture
    def narrow(self, monkeypatch):
        window = {6, 7, 8, 9, 10, 11, 12, 13}          # 6 AM - 2 PM ET
        monkeypatch.setattr(webhook, "_KEEPWARM_HOURS_ET", window)
        return window

    def test_inside_the_window_pings(self, narrow):
        for h in sorted(narrow):
            assert webhook._in_keepwarm_window(_et(h)) is True

    def test_outside_the_window_does_not(self, narrow):
        for h in sorted(set(range(24)) - narrow):
            assert webhook._in_keepwarm_window(_et(h)) is False, f"{h}:30 ET must NOT ping"

    def test_the_symptom_that_exposed_the_original_bug(self, narrow):
        """/health answered in 0.15s at 01:42 ET — warm hours outside any window
        it was supposedly asleep in."""
        assert webhook._in_keepwarm_window(_et(1)) is False

    def test_any_production_window_must_cover_the_morning_relay(self, narrow):
        """/trigger/morning fires at 7 AM ET and delivery is NOT idempotent — a
        relay landing on a cold server means no picks that day, with buffer."""
        assert webhook._in_keepwarm_window(_et(7)) is True
        assert webhook._in_keepwarm_window(_et(6)) is True

    def test_a_clock_failure_keeps_the_service_warm(self, monkeypatch, narrow):
        """Fail OPEN: a broken clock costing a few instance-hours is
        recoverable; a bot that silently stops answering is not."""
        monkeypatch.setitem(sys.modules, "pytz", None)
        assert webhook._in_keepwarm_window() is True

    def test_garbage_in_the_env_var_is_ignored_not_fatal(self):
        assert {int(h) for h in "6,,x,7, 8 ,".split(",") if h.strip().isdigit()} == {6, 7, 8}
