"""`synthetic.opened` must not fail before the bot has had its chance.

On 2026-09-16 this check FAILED on a perfectly healthy bot, every weekday, by
construction — and a canary failure summons self-heal, so it was also spending
Anthropic credits nightly on a non-bug.

The cause was an ordering assumption nobody had written down. The bot's OPEN
phase and the canary had both just been moved off GitHub's late scheduler to
cron-job.org, and the canary landed 30 minutes EARLIER than the bot (11:30 vs
12:00 UTC). Before that, both ran hours late on GitHub's queue and the canary
happened to land after the bot every time — the ordering held by accident, so
nothing recorded that it mattered.

The schedules were fixed too (canary 08:30 ET, bot 08:00 ET), but a schedule is
a convention: it is edited from a web console, by a person, with nothing in this
repo to stop them. These tests pin the BEHAVIOUR instead, so that moving either
job can no longer manufacture a false alarm.

⚠️ Honest limit, stated rather than papered over: a canary permanently moved
BEFORE the bot would report "not run yet" forever and quietly stop verifying
anything. The note names both times so that a human reading the line can see it
— but nobody reads passing lines, so this is a mitigation, not a guarantee.
Neither schedule lives in this repo, so no test here can assert their order.
"""
import datetime as os_free_datetime
import os
import sys

import pytest
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import canary  # noqa: E402

ET = pytz.timezone("America/New_York")


def _at(hour, minute, day=16):
    """A concrete ET wall clock. 2026-09-16 is a Wednesday."""
    return ET.localize(os_free_datetime.datetime(2026, 9, 16, hour, minute))


@pytest.fixture
def results(monkeypatch):
    out = []
    # Mirror the REAL contract exactly: `detail` on a pass, `fail_detail` on a
    # failure. A stub that collapses the two is more permissive than production
    # and certifies bugs — the trap that let the traffic tracker ship without
    # ever writing a row.
    monkeypatch.setattr(canary, "_check",
                        lambda name, ok, detail="", fail_detail="":
                        out.append((name, ok, detail if ok else (fail_detail or detail))))
    return out


def _stub_store(monkeypatch, *, real=(), paper=()):
    """Patch config_manager — check_synthetic_user imports it function-locally,
    the scope trap that once let a 'patched' test write to the live gist."""
    import config_manager as cm
    today = os_free_datetime.date(2026, 9, 16)
    monkeypatch.setattr(cm, "et_today", lambda: today)
    monkeypatch.setattr(cm, "load_picks",
                        lambda: {"_saved_date": today.isoformat(), "stocks": [{"ticker": "X"}]})
    monkeypatch.setattr(cm, "load_user_trade_log",
                        lambda uid: {"open": [{"ticker": t, "opened_date": today.isoformat()}
                                              for t in real]})
    monkeypatch.setattr(cm, "load_user_paper",
                        lambda uid: {"positions": [{"ticker": t, "bought_date": today.isoformat()}
                                                   for t in paper]})


class TestItWaitsForTheBot:
    def test_before_the_open_hour_it_reports_not_run_yet_instead_of_failing(
            self, monkeypatch, results):
        """THE REGRESSION. 07:30 ET is where the canary actually sat on 09-16."""
        _stub_store(monkeypatch)                       # bot has opened nothing
        monkeypatch.setattr(canary, "_now_et", lambda: _at(7, 30))
        canary.check_synthetic_user()
        name, ok, detail = results[0]
        assert ok is True, (
            "the canary failed on a bot that had not run yet — this is the "
            "2026-09-16 false alarm, which also summons self-heal"
        )
        assert "not run yet" in detail

    def test_the_note_names_both_clocks_so_a_permanent_inversion_is_visible(
            self, monkeypatch, results):
        _stub_store(monkeypatch)
        monkeypatch.setattr(canary, "_now_et", lambda: _at(7, 30))
        canary.check_synthetic_user()
        detail = results[0][2]
        assert "08:00 ET" in detail, "the bot's trigger time is not named"
        assert "07:30 ET" in detail, "the canary's own time is not named"

    def test_at_the_boundary_it_is_still_waiting(self, monkeypatch, results):
        """08:14 is inside the grace window; the bot may still be mid-run."""
        _stub_store(monkeypatch)
        monkeypatch.setattr(canary, "_now_et", lambda: _at(8, 14))
        canary.check_synthetic_user()
        assert results[0][1] is True and "not run yet" in results[0][2]


class TestItStillCatchesADeadBot:
    """The whole point of the check. Silencing it would be worse than the false
    alarm — the bot opened ZERO positions for two days in Aug 2026 across 30
    consecutive 'success' runs, and the only symptom was an ABSENCE."""

    def test_after_the_grace_window_an_empty_bot_FAILS(self, monkeypatch, results):
        _stub_store(monkeypatch)                       # nothing opened
        monkeypatch.setattr(canary, "_now_et", lambda: _at(8, 30))
        canary.check_synthetic_user()
        name, ok, detail = results[0]
        assert ok is False, "a genuinely blind bot was reported as healthy"
        assert "opened NOTHING" in detail

    def test_a_bot_that_opened_positions_passes(self, monkeypatch, results):
        _stub_store(monkeypatch, real=("WST", "RVTY"), paper=("KR",))
        monkeypatch.setattr(canary, "_now_et", lambda: _at(8, 30))
        canary.check_synthetic_user()
        assert results[0][1] is True
        assert "2 real + 1 paper" in results[0][2]

    def test_the_weekend_guard_still_short_circuits_first(self, monkeypatch, results):
        """Saturday 2026-09-19 — and at 07:00, so the new guard would also fire.
        The weekend reason must win, or the note misattributes the silence."""
        import config_manager as cm
        monkeypatch.setattr(cm, "et_today", lambda: os_free_datetime.date(2026, 9, 19))
        monkeypatch.setattr(canary, "_now_et",
                            lambda: ET.localize(os_free_datetime.datetime(2026, 9, 19, 7, 0)))
        canary.check_synthetic_user()
        assert results[0][1] is True and "weekend" in results[0][2]
