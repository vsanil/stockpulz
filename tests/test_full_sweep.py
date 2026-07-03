"""Smoke tests for the full-sweep exerciser helpers (scripts/full_sweep.py).

Only the pure helpers are unit-tested here (no webhook import / no network) —
the endpoint/command/callback driving is validated by running the sweep itself
on GitHub Actions against live data.
"""
import os
import datetime as dt
import importlib.util

import requests

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "full_sweep.py")
_spec = importlib.util.spec_from_file_location("full_sweep", _PATH)
fs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fs)


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
