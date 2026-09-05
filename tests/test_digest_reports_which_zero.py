"""🔴 `run_digest` printed "sent to 0 user(s)" for FOUR different situations:
nobody had open positions, everyone was paused, the handler crashed, or the
handler returned nothing. Three of those are fine and one is an outage, and the
log could not tell them apart.

That is not hypothetical. MEASURED 2026-09-05: `/digest` was raising
UnboundLocalError inside `cmd_market._cmd_market` (a function-local import
shadowing a module-level name). `run_digest` caught it by design — users must
never see "Something went wrong" from a background job — printed the suppressed
notice, reported "sent to 0 user(s)" and exited 0. The workflow was green and
every user got nothing, for weeks.

🔑 A zero is not a result until you can say WHICH zero it is. These tests pin
that the outcomes stay distinguishable, so the next silent breakage announces
itself instead of looking like a quiet day.
"""
import importlib
import sys
import types

import pytest


@pytest.fixture
def agent(monkeypatch):
    """Import agent with its heavy/networked imports neutralised."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    if "agent" in sys.modules:
        del sys.modules["agent"]
    return importlib.import_module("agent")


def _wire(agent, monkeypatch, users, *, open_map, reply_map, cfg_map=None):
    """Drive run_digest's dependencies. They are imported FUNCTION-LOCALLY
    inside run_digest, so patch the SOURCE modules, not `agent` — the same
    scope trap that once let a 'patched' canary test write to the live gist."""
    import bot_commands
    import config_manager
    import telegram_api

    monkeypatch.setattr(config_manager, "get_allowed_users", lambda: list(users))
    monkeypatch.setattr(config_manager, "get_user_config",
                        lambda uid: (cfg_map or {}).get(uid, {}))
    monkeypatch.setattr(config_manager, "load_user_trade_log",
                        lambda uid: {"open": ["X"] if open_map.get(uid) else []})

    def _exec(cmd, original=None, chat_id=None):
        r = reply_map.get(chat_id)
        if isinstance(r, Exception):
            raise r
        return r
    monkeypatch.setattr(bot_commands, "_parse_and_execute", _exec)

    sent = []
    monkeypatch.setattr(telegram_api, "send_message",
                        lambda text, chat_id=None, **k: sent.append(chat_id) or True)
    monkeypatch.setattr(agent, "_log_cron_run", lambda mode: None)
    return sent


def _run(agent, capsys):
    agent.run_digest()
    return capsys.readouterr().out


class TestTheFourZerosAreDistinguishable:
    def test_no_open_positions_says_so(self, agent, monkeypatch, capsys):
        _wire(agent, monkeypatch, ["a", "b"],
              open_map={"a": False, "b": False}, reply_map={})
        out = _run(agent, capsys)
        assert "2 no open positions" in out
        assert "DIGEST IS BROKEN" not in out, "a quiet day must not read as an outage"

    def test_a_crashing_handler_is_called_out_not_hidden_in_a_zero(
            self, agent, monkeypatch, capsys):
        """The exact 2026-09-05 production shape."""
        _wire(agent, monkeypatch, ["a"], open_map={"a": True},
              reply_map={"a": "Something went wrong, please try again."})
        out = _run(agent, capsys)
        assert "1 handler errored" in out
        assert "DIGEST IS BROKEN" in out, \
            "a suppressed handler error must be stated, not inferred from 'sent to 0'"

    def test_paused_users_are_counted_separately(self, agent, monkeypatch, capsys):
        _wire(agent, monkeypatch, ["a"], open_map={"a": True}, reply_map={},
              cfg_map={"a": {"skip_digest": True}})
        out = _run(agent, capsys)
        assert "1 paused/opted out" in out
        assert "DIGEST IS BROKEN" not in out

    def test_an_empty_reply_is_a_problem_not_a_success(self, agent, monkeypatch, capsys):
        _wire(agent, monkeypatch, ["a"], open_map={"a": True}, reply_map={"a": None})
        out = _run(agent, capsys)
        assert "1 empty reply" in out and "DIGEST IS BROKEN" in out

    def test_an_exception_is_counted(self, agent, monkeypatch, capsys):
        _wire(agent, monkeypatch, ["a"], open_map={"a": True},
              reply_map={"a": RuntimeError("boom")})
        out = _run(agent, capsys)
        assert "1 exception" in out and "DIGEST IS BROKEN" in out

    def test_the_happy_path_still_sends_and_stays_quiet(self, agent, monkeypatch, capsys):
        sent = _wire(agent, monkeypatch, ["a"], open_map={"a": True},
                     reply_map={"a": "your digest"})
        out = _run(agent, capsys)
        assert sent == ["a"]
        assert "sent to 1 of 1" in out
        assert "DIGEST IS BROKEN" not in out


class TestOneUserFailingDoesNotAbortTheRest:
    """Pre-existing contract, kept explicit: a background job must not let one
    user's failure deny everyone else."""

    def test_a_crash_for_one_still_delivers_to_the_others(
            self, agent, monkeypatch, capsys):
        sent = _wire(agent, monkeypatch, ["bad", "good"],
                     open_map={"bad": True, "good": True},
                     reply_map={"bad": RuntimeError("boom"), "good": "ok"})
        out = _run(agent, capsys)
        assert sent == ["good"]
        assert "sent to 1 of 2" in out and "1 exception" in out
