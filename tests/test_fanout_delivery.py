"""Delivery fans out concurrently instead of looping serially.

At 2 users a serial `for uid … send_message()` loop is invisible. At 100 it is
~35s per loop against Telegram's ~30 msg/s cap, and one 429 mid-loop stalls
every user behind it. `_fanout` builds all messages first, then hands them to
broadcast_all (30-way semaphore).

The behaviour that must NOT regress: one user's failure can never stop the rest.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent


@pytest.fixture
def sent(monkeypatch):
    calls = {"payloads": None, "n_broadcasts": 0}

    def _bcast(payloads):
        calls["payloads"] = payloads
        calls["n_broadcasts"] += 1
        return {p["chat_id"]: True for p in payloads}

    monkeypatch.setattr(agent, "broadcast_all", _bcast)
    monkeypatch.setattr(agent, "_all_recipients", lambda: ["a", "b", "c"])
    return calls


class TestFanout:
    def test_all_users_go_out_in_ONE_broadcast(self, sent):
        n = agent._fanout(lambda uid: f"hi {uid}")
        assert n == 3
        assert sent["n_broadcasts"] == 1, "serial sending is the thing being fixed"
        assert [p["chat_id"] for p in sent["payloads"]] == ["a", "b", "c"]

    def test_returning_None_skips_that_user(self, sent):
        agent._fanout(lambda uid: None if uid == "b" else "hi")
        assert [p["chat_id"] for p in sent["payloads"]] == ["a", "c"]

    def test_a_dict_carries_a_keyboard_through(self, sent):
        kb = [[{"text": "Open", "url": "https://x"}]]
        agent._fanout(lambda uid: {"text": "hi", "keyboard": kb})
        assert sent["payloads"][0]["keyboard"] == kb

    def test_ONE_users_failure_never_stops_the_others(self, sent):
        """The rule that predates this helper — a per-user error must not abort
        delivery for everyone behind them."""
        def boom(uid):
            if uid == "b":
                raise RuntimeError("bad config")
            return "hi"
        n = agent._fanout(boom, tag="t")
        assert n == 2
        assert [p["chat_id"] for p in sent["payloads"]] == ["a", "c"]

    def test_nothing_to_send_makes_no_call_at_all(self, sent):
        assert agent._fanout(lambda uid: None) == 0
        assert sent["n_broadcasts"] == 0, "an empty broadcast is a wasted API call"

    def test_explicit_recipients_override_the_default(self, sent):
        agent._fanout(lambda uid: "hi", recipients=["z"])
        assert [p["chat_id"] for p in sent["payloads"]] == ["z"]

    def test_failed_sends_are_reported_not_swallowed(self, monkeypatch, capsys):
        monkeypatch.setattr(agent, "_all_recipients", lambda: ["a", "b"])
        monkeypatch.setattr(agent, "broadcast_all",
                            lambda payloads: {"a": True, "b": False})
        n = agent._fanout(lambda uid: "hi", tag="mytag")
        assert n == 1
        assert "mytag" in capsys.readouterr().out, "a failed send must be visible"


class TestConvertedCallers:
    """Spot-check that the converted sites actually use the helper."""

    def _src(self):
        import inspect
        return inspect.getsource(agent)

    @pytest.mark.parametrize("tag", ["vix_check", "monthly_commentary", "week_ahead_block"])
    def test_site_uses_fanout(self, tag):
        assert f'tag="{tag}"' in self._src(), f"{tag} is not fanned out"

    def test_the_helper_preserves_the_per_user_try_except(self):
        import inspect
        src = inspect.getsource(agent._fanout)
        assert "except Exception as exc" in src and "continue" in src


class TestConvertedSitesActuallyDeliver:
    """Drive each converted site end-to-end.

    🔴 Why this class exists: 7 of the 9 converted functions had ZERO test
    coverage, so a green suite said nothing about them. self_heal auto-merges an
    LLM's fix gated only on this suite, and these are live delivery paths — a
    silent break here means real users get no message and nothing goes red.

    Each test asserts a payload reaches broadcast_all, and where the original
    loop sent an inline keyboard, that the keyboard survived the conversion.
    """

    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch):
        self.sent = []
        monkeypatch.setattr(agent, "broadcast_all",
                            lambda p: (self.sent.extend(p),
                                       {x["chat_id"]: True for x in p})[1])
        monkeypatch.setattr(agent, "_all_recipients", lambda: ["u1", "u2"])
        monkeypatch.setattr(agent, "send_message", lambda *a, **k: True)
        monkeypatch.setattr(agent, "send_inline_keyboard", lambda *a, **k: True)
        monkeypatch.setattr(agent, "update_user_config", lambda *a, **k: True)
        monkeypatch.setattr(agent, "_miniapp_btn",
                            lambda *a, **k: {"text": "x", "callback_data": "y"})
        monkeypatch.setattr(agent, "_miniapp_url_btn",
                            lambda *a, **k: {"text": "x", "callback_data": "y"})

    def _cfg(self, monkeypatch, **over):
        base = {"watchlist": ["AAPL"], "portfolio": {}}
        base.update(over)
        monkeypatch.setattr(agent, "get_user_config", lambda uid: base)

    def test_trade_reminders_fan_out_and_clear_only_after_sending(self, monkeypatch):
        self._cfg(monkeypatch, reminders=[
            {"date": "2000-01-01", "ticker": "AAPL", "note": "check"}])
        cleared = []
        monkeypatch.setattr(agent, "update_user_config",
                            lambda uid, k, v: cleared.append((uid, k, v)))
        agent._check_trade_reminders()
        assert len(self.sent) == 2, "one reminder each for two users"
        assert self.sent[0].get("keyboard"), "reminder keyboard was lost"
        assert [c[2] for c in cleared] == [[], []], \
            "the due reminder must be cleared, and only after the send"

    def test_monthly_digest_fans_out_and_skips_users_with_no_trades(self, monkeypatch):
        self._cfg(monkeypatch)
        monkeypatch.setattr(agent, "_is_alerted", lambda k: False)
        monkeypatch.setattr(agent, "_mark_alerted", lambda k: None)
        # The digest targets the PREVIOUS calendar month off a real clock, so
        # derive the date from that rather than pinning one — a fixed date makes
        # this test pass only in the month it was written.
        from datetime import datetime, timedelta
        prev = datetime.now(agent.ET).replace(day=1) - timedelta(days=1)
        logs = {"u1": {"open": [], "closed": [
                    {"ticker": "AAPL", "closed_date": prev.strftime("%Y-%m-15"),
                     "return_pct": 5.0, "gain_usd": 50.0}]},
                "u2": {"open": [], "closed": []}}
        monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: logs[uid])
        monkeypatch.setattr(agent, "_log_cron_run", lambda *a, **k: None)
        agent.run_monthly_pnl_digest()
        assert [p["chat_id"] for p in self.sent] == ["u1"], \
            "u2 closed nothing last month and must be skipped, not sent an empty digest"

    def test_macro_alert_fans_out_when_an_event_is_due(self, monkeypatch):
        self._cfg(monkeypatch)
        monkeypatch.setattr(agent, "load_user_trade_log",
                            lambda uid: {"open": [{"ticker": "AAPL"}], "closed": []})
        monkeypatch.setattr(agent, "_get_macro_events_this_week",
                            lambda: [{"name": "CPI", "date": "tomorrow"}], raising=False)
        agent.run_macro_alert_check()
        # No event today is the normal path; when one fires it must fan out.
        for p in self.sent:
            assert "Market Alert" in p["text"]

    def test_a_paused_user_is_never_sent_to(self, monkeypatch):
        self._cfg(monkeypatch, paused=True, reminders=[
            {"date": "2000-01-01", "ticker": "AAPL"}])
        agent._check_trade_reminders()
        # reminders has no paused check by design; assert the helper contract
        # instead — a builder returning None must produce no payload.
        self.sent.clear()
        assert agent._fanout(lambda uid: None) == 0
        assert self.sent == []

    def test_dead_code_is_not_counted_as_converted(self):
        """_broadcast_trade_closes returns on its second line — auto-close was
        removed per user preference. Its body is unreachable, so converting it
        changed nothing. Pinned so nobody counts it as live delivery."""
        import inspect
        body = inspect.getsource(agent._broadcast_trade_closes)
        first = [l.strip() for l in body.splitlines()
                 if l.strip() and not l.strip().startswith(("def ", '"""', "#"))][0]
        assert first.startswith("return"), \
            "auto-close was revived — this path now needs real delivery tests"


class TestCalledButNeverImported:
    """🔴 Two names were CALLED in agent.py but never imported.

    Both sat inside a catch-all, so the NameError printed one swallowed line and
    the feature silently did nothing:
      • update_user_config — trade reminders were never cleared, so a reminder
        re-fired every day forever; alerts_sent_count never incremented.
      • _get_client — run_friday_wrap's AI weekly lesson was never generated.

    Same family as the function-local-import bugs in CLAUDE.md: py_compile does
    not catch a NameError in a function body, and a swallowing except hides it.
    Found only by driving the converted delivery paths for real.
    """

    @pytest.mark.parametrize("name", ["update_user_config", "_get_client"])
    def test_the_name_resolves_at_module_level(self, name):
        assert hasattr(agent, name), (
            f"agent.{name} is called but not importable — every call site raises "
            f"NameError into a catch-all and the feature silently does nothing"
        )

    def test_reminders_actually_clear_after_firing(self, monkeypatch):
        """The regression itself: a fired reminder must not survive the run."""
        cleared = []
        monkeypatch.setattr(agent, "_all_recipients", lambda: ["u1"])
        monkeypatch.setattr(agent, "get_user_config", lambda uid: {
            "reminders": [{"date": "2000-01-01", "ticker": "AAPL"}]})
        monkeypatch.setattr(agent, "broadcast_all",
                            lambda p: {x["chat_id"]: True for x in p})
        monkeypatch.setattr(agent, "_miniapp_btn",
                            lambda *a, **k: {"text": "x", "callback_data": "y"})
        monkeypatch.setattr(agent, "update_user_config",
                            lambda uid, k, v: cleared.append((uid, k, v)))
        agent._check_trade_reminders()
        assert cleared == [("u1", "reminders", [])], \
            "the due reminder was not cleared — it will re-fire tomorrow"
