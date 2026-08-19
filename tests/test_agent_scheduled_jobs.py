"""Behaviour tests for agent.py's scheduled jobs.

These run unattended every day and deliver to every user, and all of them had
ZERO coverage while self_heal auto-merges an LLM's fix gated only on this suite.

What is pinned here is chosen from failures this project has actually had:
  • EOD position pricing must degrade PER TICKER — a batch fetch that threw on
    one rate-limited crypto ticker silently deleted the whole "your positions"
    block plus the portfolio P&L from every user's End-of-Day.
  • Every per-user broadcast body must be try/except wrapped — run_midday_check
    shipped without it, so one bad user aborted the loop for everyone after them.
  • Skip flags (paused / skip_eod / skip_confirmation) must actually skip.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent
import market_data


@pytest.fixture
def wired(monkeypatch):
    """Stub every outbound edge. Stubs take *a/**k deliberately — a stub
    narrower than production hides a signature change (the add_alert(kind=)
    and send_inline_keyboard(buttons=) lessons)."""
    box = {"sent": [], "users": ["u1", "u2"]}
    monkeypatch.setattr(agent, "_all_recipients", lambda: box["users"])
    monkeypatch.setattr(agent, "broadcast_all",
                        lambda p: (box["sent"].extend(p),
                                   {x["chat_id"]: True for x in p})[1])
    for fn in ("send_message", "send_inline_keyboard", "send_photo"):
        monkeypatch.setattr(agent, fn,
                            lambda *a, **k: box["sent"].append(
                                {"chat_id": k.get("chat_id"),
                                 "text": a[0] if a else ""}))
    monkeypatch.setattr(agent, "get_user_config", lambda uid: {})
    monkeypatch.setattr(agent, "load_user_trade_log",
                        lambda uid: {"open": [], "closed": [], "watchlist": []})
    monkeypatch.setattr(agent, "_is_quiet_hours", lambda uid: False)
    monkeypatch.setattr(agent, "_is_alerted", lambda *a, **k: False)
    monkeypatch.setattr(agent, "_mark_alerted", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_log_cron_run", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_miniapp_btn", lambda *a, **k: {})
    monkeypatch.setattr(agent, "_miniapp_url_btn", lambda *a, **k: {})
    monkeypatch.setattr(agent, "_persist_notify_flags", lambda *a, **k: None)
    return box


class TestEodSummary:
    def _positions(self, monkeypatch, positions, live):
        monkeypatch.setattr(agent, "load_user_trade_log",
                            lambda uid: {"open": positions, "closed": []})
        monkeypatch.setattr(agent, "load_picks", lambda: {"stocks": {}})
        monkeypatch.setattr(agent, "get_current_prices", lambda *a, **k: {},
                            raising=False)
        monkeypatch.setattr(market_data, "get_live_prices", lambda t: live)

    def test_one_unpriceable_ticker_does_not_delete_the_whole_block(
            self, wired, monkeypatch):
        """🔴 The live bug: _download_prices threw on a single rate-limited
        crypto ticker and the except dropped ALL positions, so the section
        filtered out entirely. Partial results must still render."""
        self._positions(monkeypatch,
                        [{"ticker": "AAPL", "entry_price": 100.0, "shares": 10},
                         {"ticker": "BTC", "entry_price": 60000.0, "shares": 1}],
                        {"AAPL": 110.0})           # BTC missing, as on a 429
        agent.run_eod_summary()
        body = " ".join(p["text"] for p in wired["sent"])
        assert "AAPL" in body, "a partial price fetch deleted the priced position too"

    def test_the_price_fetch_is_the_partial_tolerant_one(self, wired, monkeypatch):
        """Pin the helper, not just the outcome — _download_prices is
        all-or-nothing and must never come back here."""
        called = []
        self._positions(monkeypatch,
                        [{"ticker": "AAPL", "entry_price": 100.0, "shares": 10}],
                        {})
        monkeypatch.setattr(market_data, "get_live_prices",
                            lambda t: (called.append(t), {"AAPL": 110.0})[1])
        agent.run_eod_summary()
        assert called, "run_eod_summary did not use market_data.get_live_prices"

    @pytest.mark.parametrize("flag", ["paused", "skip_eod"])
    def test_the_skip_flags_are_honoured(self, wired, monkeypatch, flag):
        monkeypatch.setattr(agent, "get_user_config", lambda uid: {flag: True})
        self._positions(monkeypatch,
                        [{"ticker": "AAPL", "entry_price": 100.0, "shares": 10}],
                        {"AAPL": 110.0})
        agent.run_eod_summary()
        assert wired["sent"] == [], f"{flag} did not suppress the EOD"

    def test_one_users_failure_does_not_abort_the_others(self, wired, monkeypatch):
        def cfg(uid):
            if uid == "u1":
                raise RuntimeError("corrupt config")
            return {}
        monkeypatch.setattr(agent, "get_user_config", cfg)
        self._positions(monkeypatch,
                        [{"ticker": "AAPL", "entry_price": 100.0, "shares": 10}],
                        {"AAPL": 110.0})
        agent.run_eod_summary()
        assert [p["chat_id"] for p in wired["sent"]] == ["u2"]


class TestMiddayCheck:
    def test_one_bad_user_does_not_abort_the_loop(self, wired, monkeypatch):
        """🔴 run_midday_check shipped WITHOUT a per-user try/except, so one
        user's failure aborted delivery for everyone after them."""
        seen = []
        monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: {
            "open": [{"ticker": "AAPL", "entry_price": 100.0, "shares": 10}]})
        monkeypatch.setattr(agent, "get_current_prices",
                            lambda *a, **k: {"AAPL": 110.0}, raising=False)
        monkeypatch.setattr(agent, "_download_prices",
                            lambda *a, **k: {"AAPL": 110.0})

        def hold_or_fold(uid, prices):
            seen.append(uid)
            if uid == "u1":
                raise RuntimeError("boom")
        monkeypatch.setattr(agent, "_check_hold_or_fold", hold_or_fold)
        monkeypatch.setattr(agent, "_check_take_profit_nudge", lambda *a, **k: None)
        agent.run_midday_check()
        assert seen == ["u1", "u2"], f"the loop aborted after the bad user: {seen}"

    def test_quiet_hours_suppress_the_nudges(self, wired, monkeypatch):
        seen = []
        monkeypatch.setattr(agent, "_is_quiet_hours", lambda uid: True)
        monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: {
            "open": [{"ticker": "AAPL", "entry_price": 100.0, "shares": 10}]})
        monkeypatch.setattr(agent, "_download_prices",
                            lambda *a, **k: {"AAPL": 110.0})
        monkeypatch.setattr(agent, "get_current_prices",
                            lambda *a, **k: {"AAPL": 110.0}, raising=False)
        monkeypatch.setattr(agent, "_check_hold_or_fold",
                            lambda uid, p: seen.append(uid))
        agent.run_midday_check()
        assert seen == [], "quiet hours did not suppress the midday nudges"

    def test_no_positions_anywhere_makes_no_price_call(self, wired, monkeypatch):
        called = []
        monkeypatch.setattr(agent, "_download_prices",
                            lambda *a, **k: (called.append(1), {})[1])
        agent.run_midday_check()
        assert called == [], "priced an empty universe"


class TestNewsCheck:
    def test_a_paused_user_is_never_even_scanned(self, wired, monkeypatch):
        """Asserted on the trade-log READ, not on the absence of a message.

        The first version of this test asserted `sent == []`, which passed even
        with the paused check deleted — news fetching is stubbed, so nothing
        would have been sent either way. A test that cannot fail is not a test.
        """
        read = []
        monkeypatch.setattr(agent, "get_user_config",
                            lambda uid: {"paused": uid == "u1", "watchlist": []})
        monkeypatch.setattr(agent, "load_user_trade_log",
                            lambda uid: (read.append(uid), {"open": [], "watchlist": []})[1])
        agent.run_news_check()
        assert read == ["u2"], f"a paused user was still scanned: {read}"

    def test_no_tickers_means_yfinance_is_never_touched(self, wired, monkeypatch):
        """run_news_check fetches through yfinance directly — pin the real call."""
        import yfinance
        called = []
        monkeypatch.setattr(yfinance, "Ticker",
                            lambda *a, **k: called.append(a) or (_ for _ in ()).throw(
                                AssertionError("fetched news with no tickers")))
        agent.run_news_check()
        assert called == []

    def test_one_bad_user_does_not_abort_collection(self, wired, monkeypatch):
        seen = []

        def cfg(uid):
            seen.append(uid)
            if uid == "u1":
                raise RuntimeError("boom")
            return {"watchlist": []}
        monkeypatch.setattr(agent, "get_user_config", cfg)
        agent.run_news_check()
        assert seen == ["u1", "u2"], "collection aborted on the first bad user"


class TestPriceAlerts:
    def _wire(self, monkeypatch, positions, prices):
        monkeypatch.setattr(agent, "load_user_trade_log",
                            lambda uid: {"open": positions, "watchlist": []})
        monkeypatch.setattr(agent, "_download_prices", lambda *a, **k: prices)
        monkeypatch.setattr(agent, "check_alerts", lambda *a, **k: [], raising=False)

    def test_target_approach_fires_inside_5_percent(self, wired, monkeypatch):
        self._wire(monkeypatch,
                   [{"ticker": "AAPL", "entry_price": 100.0, "target_price": 110.0}],
                   {"AAPL": 106.0})               # 3.8% away
        agent.run_price_alerts()
        assert any("from your target" in p["text"] for p in wired["sent"])

    def test_it_is_silent_when_the_target_is_far_off(self, wired, monkeypatch):
        self._wire(monkeypatch,
                   [{"ticker": "AAPL", "entry_price": 100.0, "target_price": 110.0}],
                   {"AAPL": 101.0})               # 8.9% away
        agent.run_price_alerts()
        assert not any("from your target" in p["text"] for p in wired["sent"])

    def test_a_target_below_entry_is_ignored_not_celebrated(self, wired, monkeypatch):
        """An inverted level is a data-integrity problem (position_audit), not
        an alert — firing on it would announce a 'target' that cannot be won."""
        self._wire(monkeypatch,
                   [{"ticker": "AAPL", "entry_price": 100.0, "target_price": 90.0}],
                   {"AAPL": 89.0})
        agent.run_price_alerts()
        assert not any("from your target" in p["text"] for p in wired["sent"])
