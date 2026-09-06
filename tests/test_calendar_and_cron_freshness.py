"""🔴 Three defects found in the 2026-09-06 architecture review, pinned here.

1. **Only 3 of 19 modes knew about market holidays.** `run_morning` still calls
   `save_picks()` for crypto on a holiday, so `load_picks()` is non-empty and
   `eod_summary`/`confirmation` send a full stock wrap-up about prices that
   never moved. Thanksgiving and Christmas are both weekdays.
2. **`friday_wrap` had no weekday guard at all** — it relied on its cron
   (`0 23 * * 5`) firing only on Fridays, and a manual cron-job.org TEST RUN
   walked straight past that and sent a "Friday evening wrap-up" on a SATURDAY
   (observed 2026-09-06).
3. **Freshness monitoring covered 2 of 19 modes.** Every mode stamps
   `cron_last_<mode>`; the canary read `delivery.morning` and `cron_last_weekly`
   and nothing else. That blind spot is why `vix_check` and `digest` stayed
   broken for WEEKS behind green workflows.

🔑 The shared lesson: a SCHEDULE IS NOT A GUARD. It is a convention that every
manual trigger, relay and backup path ignores.
"""
import datetime as dt
import importlib
import importlib.util
import sys

import pytest
import pytz

ET = pytz.timezone("America/New_York")


def _et(y, m, d, hh=12, mm=0):
    return ET.localize(dt.datetime(y, m, d, hh, mm))


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.delenv("FORCE_CALENDAR_RUN", raising=False)
    if "agent" in sys.modules:
        del sys.modules["agent"]
    a = importlib.import_module("agent")
    monkeypatch.setattr(a, "MOCK_DATA", False)
    return a


@pytest.fixture
def canary():
    sp = importlib.util.spec_from_file_location("canary_mod", "scripts/canary.py")
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    return mod


# ══════════════════════════════════════════════════════════════════════════════
# 1. The trading-calendar gate
# ══════════════════════════════════════════════════════════════════════════════

# 2026-11-26 is Thanksgiving (a THURSDAY — the whole point), 2026-11-27 a normal
# trading Friday, 2026-11-28 a Saturday.
THANKSGIVING = _et(2026, 11, 26, 16, 30)
NORMAL_THU   = _et(2026, 11, 19, 16, 30)
SATURDAY     = _et(2026, 11, 28, 16, 30)


class TestSessionBoundModesStopOnAClosedMarket:
    @pytest.mark.parametrize("mode", sorted({
        "premarket", "confirmation", "midday_check",
        "close_check", "eod_summary", "vix_check"}))
    def test_blocked_on_a_weekday_market_holiday(self, agent, mode):
        assert agent.is_market_holiday(THANKSGIVING.date()), "fixture must be a real holiday"
        assert THANKSGIVING.weekday() < 5, "must be a WEEKDAY holiday or it proves nothing"
        assert agent._calendar_block_reason(mode, THANKSGIVING)

    @pytest.mark.parametrize("mode", sorted({
        "premarket", "confirmation", "midday_check",
        "close_check", "eod_summary", "vix_check"}))
    def test_runs_normally_on_a_trading_day(self, agent, mode):
        assert agent._calendar_block_reason(mode, NORMAL_THU) == ""

    def test_blocked_at_the_weekend_too(self, agent):
        """None of these has a weekend cron, so this costs nothing scheduled —
        it exists because a cron-job.org TEST RUN is a real production run."""
        assert agent._calendar_block_reason("eod_summary", SATURDAY) == "weekend"

    def test_the_reason_names_the_holiday(self, agent):
        assert "thanksgiving" in agent._calendar_block_reason(
            "eod_summary", THANKSGIVING).lower()


class TestTheGateIsNarrowOnPurpose:
    """⚠️ Crypto trades 24/7 and news/macro/earnings are real on a holiday.
    Widening SESSION_BOUND_MODES silently turns features OFF, so the modes that
    must survive a closed market are pinned by name."""

    @pytest.mark.parametrize("mode", [
        "morning",        # handles the holiday itself and still does crypto picks
        "price_alerts",   # crypto trailing stops
        "news_check",     # news happens on holidays
        "digest",         # positions include crypto
        "macro_alert",    # tomorrow's macro events still matter
        "pre_earnings",   # earnings after the holiday are real
        "watchdog", "prescreener", "weekly", "week_ahead",
        "monthly_commentary", "tax_harvest", "friday_wrap",
    ])
    def test_non_session_modes_are_never_calendar_blocked(self, agent, mode):
        assert agent._calendar_block_reason(mode, THANKSGIVING) == ""
        assert agent._calendar_block_reason(mode, SATURDAY) == ""

    def test_every_gated_mode_is_a_real_mode(self, agent):
        from run_modes import VALID_MODES
        assert agent.SESSION_BOUND_MODES <= VALID_MODES

    def test_the_gate_is_a_minority_of_modes(self, agent):
        from run_modes import VALID_MODES
        assert len(agent.SESSION_BOUND_MODES) < len(VALID_MODES) / 2


class TestTheEscapeHatches:
    def test_force_calendar_run_overrides(self, agent, monkeypatch):
        monkeypatch.setenv("FORCE_CALENDAR_RUN", "1")
        assert agent._calendar_block_reason("eod_summary", THANKSGIVING) == ""

    def test_mock_data_is_never_blocked(self, agent, monkeypatch):
        """A mock run must not depend on the calendar, or the ~10 s smoke test
        stops working every weekend."""
        monkeypatch.setattr(agent, "MOCK_DATA", True)
        assert agent._calendar_block_reason("eod_summary", THANKSGIVING) == ""

    def test_force_morning_does_NOT_open_the_calendar_gate(self, agent, monkeypatch):
        """FORCE_MORNING means 'bypass the once-per-day duplicate guard'. Reusing
        it here would make one flag silently mean two different things."""
        monkeypatch.setenv("FORCE_MORNING", "1")
        assert agent._calendar_block_reason("eod_summary", THANKSGIVING)


# ══════════════════════════════════════════════════════════════════════════════
# 2. friday_wrap
# ══════════════════════════════════════════════════════════════════════════════

class TestFridayWrapChecksItsOwnDay:
    def _run_on(self, agent, monkeypatch, when):
        stamped, sent = [], []
        monkeypatch.setattr(agent, "_log_cron_run", lambda m: stamped.append(m))

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return when
        monkeypatch.setattr(agent, "datetime", _DT)
        monkeypatch.setattr(agent, "_all_recipients", lambda: sent.append("fanned") or [])
        agent.run_friday_wrap()
        return stamped, sent

    def test_a_saturday_trigger_sends_nothing(self, agent, monkeypatch):
        """The exact 2026-09-06 production shape: a TEST RUN on a Saturday."""
        stamped, sent = self._run_on(agent, monkeypatch, SATURDAY)
        assert sent == [], "a 'Friday evening wrap-up' must not go out on a Saturday"

    def test_friday_still_works(self, agent, monkeypatch):
        friday = _et(2026, 11, 27, 19, 30)
        assert friday.weekday() == 4
        _, sent = self._run_on(agent, monkeypatch, friday)
        assert sent == ["fanned"], "the guard must not break the job it protects"

    def test_the_stamp_is_written_BEFORE_the_guard(self, agent, monkeypatch):
        """🔑 `cron_last_friday_wrap` records DISPATCH, which is what the canary's
        cron.all_modes_firing reads. Guarding first would make a perfectly
        healthy trigger look dead every day that is not a Friday."""
        stamped, _ = self._run_on(agent, monkeypatch, SATURDAY)
        assert stamped == ["friday_wrap"]

    def test_force_calendar_run_overrides_it(self, agent, monkeypatch):
        monkeypatch.setenv("FORCE_CALENDAR_RUN", "1")
        _, sent = self._run_on(agent, monkeypatch, SATURDAY)
        assert sent == ["fanned"]


# ══════════════════════════════════════════════════════════════════════════════
# 3. The watchdog stamps itself
# ══════════════════════════════════════════════════════════════════════════════

class TestTheWatchdogIsItselfWatchable:
    """🔴 run_watchdog was the ONE mode that never wrote cron_last_* — so the job
    that watches the morning run was the only unwatchable job in the system."""

    def test_it_stamps_even_on_a_non_trading_day(self, agent, monkeypatch):
        stamped = []
        monkeypatch.setattr(agent, "_log_cron_run", lambda m: stamped.append(m))
        monkeypatch.setattr(agent, "is_market_holiday", lambda d: True)
        agent.run_watchdog()
        assert stamped == ["watchdog"], (
            "the stamp records DISPATCH; returning on a non-trading day is a "
            "legitimate outcome, not a missed run")


# ══════════════════════════════════════════════════════════════════════════════
# 4. The generic cron-freshness check
# ══════════════════════════════════════════════════════════════════════════════

class TestEveryModeIsMonitored:
    def test_the_schedule_table_covers_exactly_the_valid_modes(self, canary):
        """🔑 The whole point. A mode added to run_modes.py without a cadence
        here would be dispatched and never monitored — which is the blind spot
        this closes, quietly reopened."""
        from run_modes import VALID_MODES
        assert set(canary._MODE_SCHEDULE) == set(VALID_MODES), (
            f"missing: {sorted(VALID_MODES - set(canary._MODE_SCHEDULE))}  "
            f"extra: {sorted(set(canary._MODE_SCHEDULE) - VALID_MODES)}")


class TestExpectedRunDates:
    def test_today_does_not_count_until_done_by_has_passed(self, canary):
        spec = canary._MODE_SCHEDULE["eod_summary"]          # done_by 16:45 ET
        morning = canary._expected_run_dates(spec, _et(2026, 11, 19, 7, 30), 1)
        evening = canary._expected_run_dates(spec, _et(2026, 11, 19, 18, 0), 1)
        assert morning[0] == dt.date(2026, 11, 18)
        assert evening[0] == dt.date(2026, 11, 19)

    def test_weekend_modes_look_back_a_whole_week(self, canary):
        got = canary._expected_run_dates(
            canary._MODE_SCHEDULE["week_ahead"], _et(2026, 11, 19, 7, 30), 2)
        assert got == [dt.date(2026, 11, 15), dt.date(2026, 11, 8)]
        assert all(d.weekday() == 6 for d in got)

    def test_a_seasonal_mode_yields_nothing_before_its_first_window(self, canary):
        """tax_harvest is Nov+Dec only and was wired up 2026-09-05, so in
        September there is genuinely nothing it could have missed. That is an
        empty set, not a pass-with-an-excuse."""
        assert canary._expected_run_dates(
            canary._MODE_SCHEDULE["tax_harvest"], _et(2026, 9, 20, 7, 30), 2) == []

    def test_since_stops_the_walk_from_inventing_pre_history(self, canary):
        got = canary._expected_run_dates(
            canary._MODE_SCHEDULE["monthly_commentary"], _et(2026, 10, 5, 7, 30), 2)
        assert got == [dt.date(2026, 10, 1)], \
            "2026-09-01 predates the trigger and must not count as a miss"


class TestToleranceIsExactlyOneMissedRun:
    """⚠️ Zero-tolerance was rejected: GitHub runs 1.6-6 h late, cron-job.org can
    503, and a market holiday legitimately skips the six session-bound modes.
    Two consecutive US market holidays do not exist, so the holiday case can
    never on its own reach the failing branch."""

    def _verdict(self, canary, monkeypatch, stamps, now):
        import config_manager
        monkeypatch.setattr(config_manager, "get_config", lambda: dict(stamps))
        monkeypatch.setattr(canary, "_MODE_SCHEDULE",
                            {"digest": canary._MODE_SCHEDULE["digest"]})

        class _DT(dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        monkeypatch.setattr(dt, "datetime", _DT)
        canary.RESULTS.clear()
        try:
            canary.check_cron_freshness()
        finally:
            monkeypatch.undo()
        return dict((n, ok) for n, ok, _ in canary.RESULTS)["cron.all_modes_firing"]

    NOW = _et(2026, 11, 19, 7, 30)      # Thursday morning; digest due 09:45

    def test_yesterdays_run_passes(self, canary, monkeypatch):
        assert self._verdict(canary, monkeypatch,
                             {"cron_last_digest": "2026-11-18T14:45:00+00:00"}, self.NOW)

    def test_one_missed_run_is_tolerated(self, canary, monkeypatch):
        assert self._verdict(canary, monkeypatch,
                             {"cron_last_digest": "2026-11-17T14:45:00+00:00"}, self.NOW)

    def test_two_missed_runs_FAIL(self, canary, monkeypatch):
        assert not self._verdict(canary, monkeypatch,
                                 {"cron_last_digest": "2026-11-16T14:45:00+00:00"}, self.NOW)

    def test_a_mode_that_never_ran_FAILS(self, canary, monkeypatch):
        assert not self._verdict(canary, monkeypatch, {}, self.NOW)

    def test_weeks_of_silence_FAILS(self, canary, monkeypatch):
        """The literal vix_check / digest outage: green workflows, no delivery."""
        assert not self._verdict(canary, monkeypatch,
                                 {"cron_last_digest": "2026-10-20T14:45:00+00:00"}, self.NOW)
