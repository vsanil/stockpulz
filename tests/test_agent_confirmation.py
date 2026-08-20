"""run_confirmation — the 10:30 AM check, 177 lines with no behaviour tests.

The load-bearing rule here is IDEMPOTENCY. Render retries failed cron runs, and
delivery is not idempotent, so without the per-day guard every retry re-sends
the confirmation to every user. Everything else in this file guards a path that
must NOT reach users: no picks, a failed price fetch, or an opted-out user.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent


@pytest.fixture
def wired(monkeypatch):
    box = {"sent": [], "saved": [], "admin": []}
    monkeypatch.setattr(agent, "_all_recipients", lambda: ["u1", "u2"])
    monkeypatch.setattr(agent, "broadcast_all",
                        lambda p: (box["sent"].extend(p),
                                   {x["chat_id"]: True for x in p})[1])
    monkeypatch.setattr(agent, "send_message", lambda *a, **k: True)
    monkeypatch.setattr(agent, "send_inline_keyboard", lambda *a, **k: True)
    monkeypatch.setattr(agent, "_alert",
                        lambda msg, **k: box["admin"].append(msg))
    monkeypatch.setattr(agent, "save_picks", lambda p: box["saved"].append(dict(p)))
    monkeypatch.setattr(agent, "get_user_config", lambda uid: {})
    monkeypatch.setattr(agent, "load_user_trade_log",
                        lambda uid: {"open": [], "closed": [], "watchlist": []})
    monkeypatch.setattr(agent, "get_current_prices", lambda picks: {"AAPL": 110.0})
    monkeypatch.setattr(agent, "_check_trailing_stops",
                        lambda *a, **k: None)
    monkeypatch.setattr(agent, "_is_quiet_hours", lambda uid: False)
    monkeypatch.setattr(agent, "_log_cron_run", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_miniapp_btn", lambda *a, **k: {})
    monkeypatch.setattr(agent, "_miniapp_url_btn", lambda *a, **k: {})
    monkeypatch.setattr(agent, "format_confirmation_message",
                        lambda *a, **k: "CONFIRMATION", raising=False)
    monkeypatch.setattr(agent, "DRY_RUN", False)
    return box


PICKS = {"stocks": {"short_term": [
    {"ticker": "AAPL", "entry_price": 100.0, "target_price": 110.0}]}}


def _picks(monkeypatch, extra=None):
    p = {**PICKS, **(extra or {})}
    monkeypatch.setattr(agent, "load_picks", lambda: p)
    return p


class TestIdempotency:
    def test_it_sends_once_and_stamps_the_date(self, wired, monkeypatch):
        p = _picks(monkeypatch)
        agent.run_confirmation()
        assert wired["sent"], "nothing was delivered"
        assert p["_confirmation_sent_date"] == agent.et_today().isoformat()
        assert wired["saved"], "the sent-flag was never persisted"

    def test_a_retry_the_same_day_sends_NOTHING(self, wired, monkeypatch):
        """🔴 Render retries failed cron runs. Delivery is not idempotent, so
        without this guard every retry re-sends to every user."""
        _picks(monkeypatch,
               {"_confirmation_sent_date": agent.et_today().isoformat()})
        agent.run_confirmation()
        assert wired["sent"] == [], "a retry re-sent the confirmation"

    def test_yesterdays_stamp_does_not_block_today(self, wired, monkeypatch):
        from datetime import timedelta
        y = (agent.et_today() - timedelta(days=1)).isoformat()
        _picks(monkeypatch, {"_confirmation_sent_date": y})
        agent.run_confirmation()
        assert wired["sent"], "yesterday's stamp suppressed today's send"

    def test_the_stamp_is_read_on_the_ET_clock(self, monkeypatch, wired):
        """A UTC date rolls over at 7-8 PM ET, which is the documented cause of
        evening alerts double-firing. Writer and reader must share one clock."""
        import inspect
        src = inspect.getsource(agent.run_confirmation)
        assert "et_today()" in src and "date.today()" not in src

    def test_a_dry_run_never_stamps(self, wired, monkeypatch):
        monkeypatch.setattr(agent, "DRY_RUN", True)
        p = _picks(monkeypatch)
        agent.run_confirmation()
        assert "_confirmation_sent_date" not in p
        assert wired["saved"] == []


class TestPathsThatMustNotReachUsers:
    def test_no_picks_means_no_send(self, wired, monkeypatch):
        monkeypatch.setattr(agent, "load_picks", lambda: {})
        agent.run_confirmation()
        assert wired["sent"] == []

    def test_a_price_fetch_failure_alerts_the_ADMIN_and_sends_nobody_else(
            self, wired, monkeypatch):
        """A confirmation built on no prices would compare picks against
        nothing — better to tell the owner and stay silent."""
        _picks(monkeypatch)
        monkeypatch.setattr(agent, "get_current_prices",
                            lambda p: (_ for _ in ()).throw(RuntimeError("429")))
        agent.run_confirmation()
        assert wired["sent"] == []
        assert wired["admin"], "the owner was not told the price fetch failed"

    @pytest.mark.parametrize("flag", ["paused", "skip_confirmation"])
    def test_opted_out_users_are_skipped(self, wired, monkeypatch, flag):
        _picks(monkeypatch)
        monkeypatch.setattr(agent, "get_user_config", lambda uid: {flag: True})
        agent.run_confirmation()
        assert not any(p.get("text") == "CONFIRMATION" for p in wired["sent"])

    def test_one_users_broken_config_does_not_stop_the_others(
            self, wired, monkeypatch):
        _picks(monkeypatch)

        def cfg(uid):
            if uid == "u1":
                raise RuntimeError("corrupt")
            return {}
        monkeypatch.setattr(agent, "get_user_config", cfg)
        agent.run_confirmation()
        assert any(p["chat_id"] == "u2" for p in wired["sent"])
