"""Smoke tests for the full-sweep exerciser helpers (scripts/full_sweep.py).

Only the pure helpers are unit-tested here (no webhook import / no network) —
the endpoint/command/callback driving is validated by running the sweep itself
on GitHub Actions against live data.
"""
import os
import datetime as dt
import importlib.util

import pytest
import requests

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "full_sweep.py")
_spec = importlib.util.spec_from_file_location("full_sweep", _PATH)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)


@pytest.fixture(autouse=True)
def _undo_the_sweeps_mutes(monkeypatch):
    """`fs.main()` switches traffic counting and activity recording OFF for the
    life of the process — correct for the real script, which exits afterwards,
    and poison for a test suite, where the next test would inherit a silenced
    recorder (and a test that then saved "the original" would save the no-op).
    Registering them with monkeypatch first puts the real ones back."""
    import bot_commands
    import traffic_tracker
    monkeypatch.setattr(bot_commands, "_record_activity", bot_commands._record_activity)
    monkeypatch.setattr(traffic_tracker, "record", traffic_tracker.record)
    monkeypatch.setattr(traffic_tracker, "flush", traffic_tracker.flush)
    yield


class TestFullSweepHelpers:
    def test_pos_fin(self):
        assert fs._pos(1.5) and fs._fin(0.0)
        assert not fs._pos(0) and not fs._pos(None) and not fs._pos(float("nan"))
        assert not fs._fin(float("inf")) and not fs._fin("x")

    def test_is_tg(self):
        assert fs._is_tg("https://api.telegram.org/bot123/sendMessage")
        assert fs._is_tg("https://api.telegram.org/bot123/sendPhoto")
        # read-only getMe passes through (needed for bot-username resolution)
        assert not fs._is_tg("https://api.telegram.org/bot123/getMe")
        assert not fs._is_tg("https://api.github.com/gists/x")
        assert not fs._is_tg(None)

    def test_recent_weekday(self):
        d = dt.date.fromisoformat(fs._recent_weekday())
        assert d.weekday() < 5              # never a weekend
        assert d < dt.date.today()          # always in the past


class TestTelegramMuter:
    def test_mute_intercepts_telegram_only_and_restores(self):
        orig_post = requests.post
        fs._mute_telegram()
        try:
            # a Telegram call is intercepted with a fake 200 — no network
            r = requests.post("https://api.telegram.org/bot1/sendMessage", json={})
            assert r.status_code == 200 and r.json().get("ok") is True
            assert requests.post is not orig_post   # patched
        finally:
            fs._unmute_telegram()
        assert requests.post is orig_post           # restored


class TestTheSweepIsNotCountedAsTraffic:
    """Measured 2026-11-14: 42 sweep runs x 33 requests = 1,386 of the month's
    1,389 "interactions" on the admin traffic card, every one flagged a cold
    start because each run is a fresh process. The card is the evidence for the
    paid-plan decision, so a monitor must never write to it."""

    def _drive_main(self, monkeypatch):
        """Run the REAL main() with the four sweeps replaced by one request
        through the real app. Returns how often the traffic counter was hit."""
        import sys
        import traffic_tracker

        seen = {"record": 0, "flush": 0, "requests": 0}

        def _spy(name):
            def spy(*a, **k):
                seen[name] += 1
                return False
            return spy

        # monkeypatch owns the originals: main() replaces these for the life of
        # the process, and the rest of the suite needs the real ones back.
        monkeypatch.setattr(traffic_tracker, "record", _spy("record"))
        monkeypatch.setattr(traffic_tracker, "flush", _spy("flush"))

        def _one_request(client, admin):
            client.get("/api/miniapp/picks", query_string={"chat_id": admin})
            seen["requests"] += 1

        monkeypatch.setattr(fs, "sweep_get_endpoints", _one_request)
        monkeypatch.setattr(fs, "sweep_bot_commands", lambda admin: None)
        monkeypatch.setattr(fs, "sweep_callbacks", lambda admin: None)
        monkeypatch.setattr(fs, "sweep_llm", lambda client, admin: None)
        monkeypatch.setattr(sys, "argv", ["full_sweep.py", "--dry-run"])
        fs.RESULTS.clear()
        fs.main()
        fs.RESULTS.clear()
        return seen

    def test_a_sweep_request_never_reaches_the_traffic_counter(self, monkeypatch):
        seen = self._drive_main(monkeypatch)
        assert seen["requests"] == 1, "the sweep made no request — nothing was tested"
        assert seen["record"] == 0, "the sweep was counted as user traffic"
        assert seen["flush"] == 0

    def test_the_same_request_IS_counted_without_the_mute(self, monkeypatch):
        """The control. If this path were skipped by the counter anyway, the
        test above would pass with the mute deleted and prove nothing."""
        monkeypatch.setattr(fs, "_mute_traffic", lambda: None)
        seen = self._drive_main(monkeypatch)
        assert seen["requests"] == 1
        assert seen["record"] >= 1

    def test_the_mute_silences_both_the_count_and_the_write(self, monkeypatch):
        import traffic_tracker
        real_record, real_flush = traffic_tracker.record, traffic_tracker.flush
        monkeypatch.setattr(traffic_tracker, "record", real_record)
        monkeypatch.setattr(traffic_tracker, "flush", real_flush)
        before = traffic_tracker.pending_snapshot()

        fs._mute_traffic()

        assert traffic_tracker.record is not real_record
        assert traffic_tracker.flush is not real_flush
        assert traffic_tracker.record("123", cold=True) is False
        assert traffic_tracker.flush() is False
        assert traffic_tracker.pending_snapshot() == before, "a muted hit was still counted"


class TestTheSweepIsNotRecordedAsTheOwnerBeingActive:
    """Found 2026-11-15: the admin page said "1 active today" and showed the
    owner last seen at 00:01 UTC — that was the sweep. Each run drove 16 bot
    commands as the owner through the real handler, which stamps `last_seen`
    and appends to the activity log: ~18 writes to the live store per run from
    a monitor whose header says READ-ONLY."""

    class _Now:
        """threading.Thread stand-in that runs the target at once, so the
        test can assert without racing a daemon thread."""
        def __init__(self, target=None, daemon=None, **k):
            self._t = target

        def start(self):
            self._t()

    def _wire(self, monkeypatch):
        import bot_commands as bc
        import config_manager as cm
        seen = {"last_seen": 0, "logged": 0}
        monkeypatch.setattr(bc, "_record_activity", bc._record_activity)   # restored afterwards
        monkeypatch.setattr(bc.threading, "Thread", self._Now)
        monkeypatch.setattr(bc, "_parse_and_execute", lambda *a, **k: "")
        monkeypatch.setattr(bc, "update_user_config",
                            lambda cid, key, val: seen.__setitem__(
                                "last_seen", seen["last_seen"] + (key == "last_seen")))
        monkeypatch.setattr(cm, "log_user_event",
                            lambda *a, **k: seen.__setitem__("logged", seen["logged"] + 1))
        return bc, seen

    def test_a_real_persons_command_still_records_activity(self, monkeypatch):
        """Non-regression: the fix must not stop real users being recorded."""
        from tests.conftest import TEST_CHAT_ID
        bc, seen = self._wire(monkeypatch)
        bc.handle_incoming_command("/help", chat_id=TEST_CHAT_ID)
        assert seen == {"last_seen": 1, "logged": 1}

    def test_a_command_driven_by_the_sweep_records_nothing(self, monkeypatch):
        import sys
        bc, seen = self._wire(monkeypatch)
        ran = []

        def _commands(admin):
            bc.handle_incoming_command("/help", chat_id=admin)
            ran.append(admin)
        monkeypatch.setattr(fs, "sweep_get_endpoints", lambda client, admin: None)
        monkeypatch.setattr(fs, "sweep_bot_commands", _commands)
        monkeypatch.setattr(fs, "sweep_callbacks", lambda admin: None)
        monkeypatch.setattr(fs, "sweep_llm", lambda client, admin: None)
        monkeypatch.setattr(sys, "argv", ["full_sweep.py", "--dry-run"])
        import traffic_tracker
        monkeypatch.setattr(traffic_tracker, "record", traffic_tracker.record)
        monkeypatch.setattr(traffic_tracker, "flush", traffic_tracker.flush)
        fs.RESULTS.clear()
        fs.main()
        fs.RESULTS.clear()
        assert len(ran) == 1, "the sweep drove no command — nothing was tested"
        assert seen == {"last_seen": 0, "logged": 0}


class TestReport:
    def test_report_loud_on_fail(self):
        fs.RESULTS.clear()
        fs._check("GET /x", True, "200")
        fs._check("POST /y", False, "500 boom")
        rep = fs.build_report()
        assert "ACTION NEEDED" in rep
        assert "POST /y" in rep and "1/2 paths OK" in rep
        fs.RESULTS.clear()

    def test_report_quiet_on_green(self):
        fs.RESULTS.clear()
        fs._check("GET /x", True, "200")
        rep = fs.build_report()
        assert "ACTION NEEDED" not in rep and "1/1 paths OK" in rep
        fs.RESULTS.clear()
