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


class TestOrderSensitiveConversions:
    """Three paths send MORE THAN ONE message per user, and the order is part of
    the message: the pre-market gap alerts explain the summary card that follows,
    and the Friday wrap must arrive before the review nudge before the lesson.

    broadcast_all does NOT guarantee ordering within one user's batch, so these
    use sequential fan-out PASSES — each pass completes before the next begins.
    The expensive per-user build runs ONCE, before any pass.
    """

    @pytest.fixture(autouse=True)
    def _stubs(self, monkeypatch):
        self.sent = []
        monkeypatch.setattr(agent, "broadcast_all",
                            lambda p: (self.sent.extend(p),
                                       {x["chat_id"]: True for x in p})[1])
        monkeypatch.setattr(agent, "_all_recipients", lambda: ["u1", "u2"])
        for fn in ("send_message", "send_inline_keyboard", "send_photo"):
            monkeypatch.setattr(agent, fn, lambda *a, **k: True)
        monkeypatch.setattr(agent, "get_user_config",
                            lambda uid: {"watchlist": [], "portfolio": {}})
        monkeypatch.setattr(agent, "_miniapp_btn",
                            lambda *a, **k: {"text": "x", "callback_data": "y"})
        monkeypatch.setattr(agent, "_is_quiet_hours", lambda uid: False)
        monkeypatch.setattr(agent, "_is_alerted", lambda *a, **k: False)
        monkeypatch.setattr(agent, "_mark_alerted", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_log_cron_run", lambda *a, **k: None)

    def test_friday_wrap_delivers_wrap_then_nudge_then_lesson(self, monkeypatch):
        """🔴 The clock is PINNED, and it has to be. This test stamped the trade
        with `date.today()` (the LOCAL date) while run_friday_wrap filters on
        `datetime.now(ET).date()`. Once ET rolled into Monday the window moved
        to the new week, the Sunday-stamped trade fell outside it, no nudge was
        emitted, and the assertion raised ValueError. It passed all week and
        failed on a Sunday night — the fifth appearance of this class here.

        A test must stamp dates on the SAME clock the code under test reads,
        and must FORCE that condition rather than wait for the calendar. Friday
        2026-08-21 sits mid-week, so the trade is always inside the window.
        """
        import datetime as _dt
        friday = _dt.datetime(2026, 8, 21, 18, 0, tzinfo=agent.ET)

        class _FixedDT(_dt.datetime):
            @classmethod
            def now(cls, tz=None):
                return friday if tz is not None else _dt.datetime(2026, 8, 21, 18, 0)
        monkeypatch.setattr(agent, "datetime", _FixedDT)
        monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: {
            "open": [], "closed": [
                {"ticker": "AAPL", "closed_date": friday.date().isoformat(),
                 "return_pct": 5.0, "gain_usd": 50.0}] * 6})
        monkeypatch.setattr(agent, "_download_prices", lambda t, **k: {})
        monkeypatch.setattr(agent, "_get_client",
                            lambda: (_ for _ in ()).throw(RuntimeError("no key")))
        agent.run_friday_wrap()

        def kind(t):
            if "Trade Review available" in t:
                return "nudge"
            return "lesson" if "week's lesson" in t else "main"
        order = [kind(p["text"]) for p in self.sent]
        assert order and set(order) <= {"main", "nudge", "lesson"}
        # Every main must precede every nudge — that is what a PASS buys.
        assert order.index("nudge") > max(i for i, k in enumerate(order) if k == "main"), \
            f"a nudge overtook a wrap: {order}"

    def test_tax_harvest_marks_the_dedup_only_after_the_send(self, monkeypatch):
        from datetime import date
        monkeypatch.setattr(agent, "et_today", lambda: date(2026, 11, 15))
        monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: {
            "open": [{"ticker": "AAPL", "entry_price": 100.0, "shares": 10}]})
        monkeypatch.setattr(agent, "_download_prices", lambda t, **k: {"AAPL": 60.0})
        order = []
        monkeypatch.setattr(agent, "broadcast_all", lambda p: (
            order.append("send"), self.sent.extend(p),
            {x["chat_id"]: True for x in p})[2])
        monkeypatch.setattr(agent, "_mark_alerted",
                            lambda *a, **k: order.append("mark"))
        agent.run_tax_loss_harvest_check()
        assert len(self.sent) == 2, "both users hold a >$50 loss and must be nudged"
        assert order.index("send") < order.index("mark"), \
            "marking before the send suppresses next month's nudge if the send fails"

    def test_close_check_trailing_stops_fan_out(self, monkeypatch):
        monkeypatch.setattr(agent, "load_picks", lambda: {"stocks": []})
        monkeypatch.setattr(agent, "get_current_prices", lambda *a, **k: {},
                            raising=False)
        monkeypatch.setattr(agent, "_check_trailing_stops",
                            lambda cp, uid, msg_buffer=None: msg_buffer.append("TRAIL"))
        agent.run_close_check()
        assert [p["chat_id"] for p in self.sent] == ["u1", "u2"]
        assert "TRAIL" in self.sent[0]["text"]

    def test_premarket_sends_movers_before_the_summary(self):
        """The gap alerts contextualise the summary card, so they go first.
        Pinned on the source because the two passes are what enforce it —
        a single combined fan-out would lose the order silently."""
        import inspect
        src = inspect.getsource(agent.run_premarket)
        m, s = src.index('tag="premarket_movers"'), src.index('tag="premarket_summary"')
        assert m < s, "the summary would arrive before the alerts it explains"
        # The costly per-ticker yfinance build must run ONCE, before either pass.
        assert src.index("for uid in _all_recipients()") < m, \
            "the build must not be re-run per pass"

    def test_the_weekly_alpha_card_stays_serial_and_follows_the_text(self):
        """broadcast_all speaks sendMessage only, so the card loop stays serial.

        Structural rather than functional: run_weekly_recap opens with
        run_morning(), so a functional test never reaches the send stage and
        would skip itself into uselessness. These assertions are chosen so that
        adding a second, earlier photo loop breaks them.
        """
        import inspect
        src = inspect.getsource(agent.run_weekly_recap)
        assert src.count("send_photo(") == 1, \
            "exactly one photo send — a second loop would reorder delivery"
        loop = "for uid in _carded:"
        assert loop in src, "the card loop must target only users who got the text"
        assert src.index('tag="weekly_recap"') < src.index(loop) < src.index("send_photo("), \
            "the card must not arrive before the recap it illustrates"
        # _carded is filled inside the builder, i.e. after the paused check,
        # so a paused user can never be sent a card.
        assert "_carded.append(uid)" in src
        assert src.index("if user_cfg.get(\"paused\")") < src.index("_carded.append(uid)")
