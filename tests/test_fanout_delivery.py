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
