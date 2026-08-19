"""Behaviour tests for agent.py's user-facing nudge helpers.

These compute money and risk figures users act on — concentration, near-stop,
allocation, staleness, RSI — and every one of them had ZERO coverage while
self_heal auto-merges an LLM's fix to main gated only on this suite.

Three of the four sit directly on documented bug classes:
  • _check_portfolio_health  — the plausible_price fallback (Jul 3 holiday feed)
  • _get_rsi                 — the crypto `-USD` mapping (bare BTC ≠ BTC-USD)
  • _check_stale_positions   — fire-once notify flags
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
def quiet(monkeypatch):
    """No dedup suppression, no storage writes."""
    fired = []
    monkeypatch.setattr(agent, "_is_alerted", lambda *a, **k: False)
    monkeypatch.setattr(agent, "_mark_alerted", lambda k, **kw: fired.append(k))
    monkeypatch.setattr(agent, "_persist_notify_flags", lambda *a, **k: None)
    return fired


def _pos(ticker, entry, shares=10, **extra):
    return {"ticker": ticker, "entry_price": entry, "shares": shares, **extra}


class TestPortfolioHealth:
    def _run(self, monkeypatch, trades, prices):
        monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: {"open": trades})
        return agent._check_portfolio_health("u1", prices)

    def test_concentration_fires_above_35_percent(self, monkeypatch, quiet):
        blocks = self._run(monkeypatch,
                           [_pos("AAPL", 100, 90), _pos("MSFT", 100, 10)],
                           {"AAPL": 100.0, "MSFT": 100.0})
        assert any("AAPL" in b and "90% of your portfolio" in b for b in blocks)
        assert not any("MSFT" in b for b in blocks)

    def test_a_balanced_book_raises_nothing(self, monkeypatch, quiet):
        blocks = self._run(monkeypatch,
                           [_pos("A", 100), _pos("B", 100), _pos("C", 100)],
                           {"A": 100.0, "B": 100.0, "C": 100.0})
        assert blocks == []

    def test_near_stop_uses_STOP_PROXIMITY_PCT(self, monkeypatch, quiet):
        # 2% above the stop — inside the 3% radius.
        blocks = self._run(monkeypatch, [_pos("AAPL", 100, stop_loss=98.0)],
                           {"AAPL": 100.0})
        assert any("within 3% of your stop" in b for b in blocks)
        assert agent.STOP_PROXIMITY_PCT == 3, \
            "the copy says 3% — change the constant and the message lies"

    def test_a_blown_through_stop_is_NOT_reported_as_near(self, monkeypatch, quiet):
        """pct_to_stop must be positive — at/below the stop is a louder state
        handled by _stop_badge / _check_hold_or_fold, not a 'near' nudge."""
        blocks = self._run(monkeypatch, [_pos("AAPL", 100, stop_loss=110.0)],
                           {"AAPL": 100.0})
        assert not any("within 3%" in b for b in blocks)

    def test_big_winner_trim_fires_above_12_percent(self, monkeypatch, quiet):
        blocks = self._run(monkeypatch, [_pos("AAPL", 100)], {"AAPL": 113.0})
        assert any("up +13.0%" in b for b in blocks)

    def test_an_IMPLAUSIBLE_price_falls_back_to_entry_and_fires_nothing(
            self, monkeypatch, quiet):
        """🔴 The Jul 3 holiday-feed class. A $0.01 quote on one leg collapses the
        portfolio total, which made the OTHER position read as ~100% of the book
        and fired a false concentration nudge plus a false stop. Falling back to
        entry keeps it at a 0% move, so nothing fires."""
        # THREE positions, so a healthy book is 33% each — under the 35% bar.
        # Drop one to $0.01 and, without the fallback, the total collapses and
        # the other two read as 50% each. With it they stay at 33% and are quiet.
        trades = [_pos("AAPL", 100), _pos("TSLA", 100), _pos("MSFT", 100)]
        blocks = self._run(monkeypatch, trades,
                           {"AAPL": 0.01, "TSLA": 100.0, "MSFT": 100.0})
        assert blocks == [], f"garbage price produced a nudge: {blocks}"

    def test_the_fallback_is_plausible_price_not_a_bare_positive_check(
            self, monkeypatch, quiet):
        """$0.01 is positive, so `> 0` would let it through. Pin the real guard —
        patched on market_data because the import is function-local."""
        seen = []
        real = market_data.plausible_price
        monkeypatch.setattr(market_data, "plausible_price",
                            lambda p, r=None, **k: (seen.append((p, r)), real(p, r))[1])
        self._run(monkeypatch, [_pos("AAPL", 100)], {"AAPL": 0.01})
        assert (0.01, 100) in seen or (0.01, 100.0) in seen, \
            "the live price was not validated against the entry reference"

    def test_no_positions_returns_empty(self, monkeypatch, quiet):
        assert self._run(monkeypatch, [], {}) == []


class TestCashPosition:
    def test_fires_above_85_percent_invested(self, quiet):
        note = agent._check_cash_position(
            "u1", {"total_portfolio_size": 1000},
            [_pos("AAPL", 100, 9)], {"AAPL": 100.0})
        assert note and "90% invested" in note

    def test_silent_at_or_below_85(self, quiet):
        assert agent._check_cash_position(
            "u1", {"total_portfolio_size": 1000},
            [_pos("AAPL", 100, 8)], {"AAPL": 100.0}) is None

    @pytest.mark.parametrize("settings", [
        {"total_portfolio_size": 1000},
        {"settings": {"total_portfolio_size": 1000}},
        {"portfolio": {"portfolio_size": 1000}},
    ])
    def test_all_three_capital_locations_are_honoured(self, settings, quiet):
        """Capital can live in onboarding, nested settings, or /settings. A user
        who set it in the third place used to get no warning at all."""
        assert agent._check_cash_position(
            "u1", settings, [_pos("AAPL", 100, 9)], {"AAPL": 100.0}) is not None

    def test_no_capital_configured_is_silent(self, quiet):
        assert agent._check_cash_position(
            "u1", {}, [_pos("AAPL", 100, 9)], {"AAPL": 100.0}) is None

    @pytest.mark.parametrize("bad", ["abc", None, 0, -100])
    def test_garbage_capital_never_raises(self, bad, quiet):
        assert agent._check_cash_position(
            "u1", {"total_portfolio_size": bad},
            [_pos("AAPL", 100, 9)], {"AAPL": 100.0}) is None

    def test_fires_once_per_week(self, monkeypatch):
        monkeypatch.setattr(agent, "_is_alerted", lambda *a, **k: True)
        assert agent._check_cash_position(
            "u1", {"total_portfolio_size": 1000},
            [_pos("AAPL", 100, 9)], {"AAPL": 100.0}) is None


class TestStalePositions:
    def _run(self, monkeypatch, trades, prices):
        monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: {"open": trades})
        return agent._check_stale_positions("u1", prices)

    def _old(self, days=30, **extra):
        from datetime import timedelta
        d = (agent.et_today() - timedelta(days=days)).isoformat()
        return _pos("AAPL", 100.0, opened_date=d, **extra)

    def test_old_and_going_nowhere_is_flagged(self, monkeypatch, quiet):
        blocks = self._run(monkeypatch, [self._old(30)], {"AAPL": 101.0})
        assert any("open 30 days" in b for b in blocks)

    def test_a_young_position_is_never_stale(self, monkeypatch, quiet):
        assert self._run(monkeypatch, [self._old(20)], {"AAPL": 101.0}) == []

    def test_progress_is_measured_against_the_TARGET_not_raw_gain(
            self, monkeypatch, quiet):
        """+5% looks small, but on a target only 10% away it is halfway there."""
        assert self._run(monkeypatch, [self._old(30, target_price=110.0)],
                         {"AAPL": 105.0}) == []

    def test_a_paused_position_is_left_alone(self, monkeypatch, quiet):
        assert self._run(monkeypatch, [self._old(30, _paused=True)],
                         {"AAPL": 101.0}) == []

    def test_it_fires_once_and_persists_the_flag(self, monkeypatch, quiet):
        saved = []
        monkeypatch.setattr(agent, "_persist_notify_flags",
                            lambda uid, tr, names: saved.append(names))
        trades = [self._old(30)]
        assert self._run(monkeypatch, trades, {"AAPL": 101.0})
        assert trades[0]["_notified_stale"] is True
        assert saved == [("_notified_stale",)], "the flag was never persisted"
        assert self._run(monkeypatch, trades, {"AAPL": 101.0}) == []

    def test_a_missing_or_zero_price_is_skipped_not_treated_as_a_crash(
            self, monkeypatch, quiet):
        for px in ({}, {"AAPL": 0}, {"AAPL": None}):
            assert self._run(monkeypatch, [self._old(30)], px) == []


class TestGetRsi:
    @pytest.fixture(autouse=True)
    def _clear(self):
        agent._rsi_cache.clear()
        yield
        agent._rsi_cache.clear()

    def _stub_yf(self, monkeypatch, closes):
        import pandas as pd
        import yfinance
        seen = []

        class _T:
            def __init__(self, sym):
                seen.append(sym)

            def history(self, **k):
                return pd.DataFrame({"Close": closes})

        monkeypatch.setattr(yfinance, "Ticker", _T)
        return seen

    def test_crypto_is_queried_as_BTC_USD_never_bare_BTC(self, monkeypatch):
        """🔴 A bare yfinance "BTC" resolves to an unrelated ~$28 instrument, not
        BTC-USD. That is measured, not hypothetical. Assert the EXACT symbol —
        an `or` accepting both is not a guard."""
        seen = self._stub_yf(monkeypatch, list(range(100, 130)))
        agent._get_rsi("BTC")
        assert seen == ["BTC-USD"], f"queried {seen}, which is a different asset"

    def test_an_equity_keeps_its_plain_symbol(self, monkeypatch):
        seen = self._stub_yf(monkeypatch, list(range(100, 130)))
        agent._get_rsi("AAPL")
        assert seen == ["AAPL"]

    def test_a_pure_uptrend_is_rsi_100(self, monkeypatch):
        self._stub_yf(monkeypatch, list(range(100, 130)))
        assert agent._get_rsi("AAPL") == 100.0

    def test_rsi_stays_in_range_on_mixed_data(self, monkeypatch):
        self._stub_yf(monkeypatch,
                      [100 + (7 * i % 11) - 5 for i in range(40)])
        assert 0 <= agent._get_rsi("AAPL") <= 100

    def test_too_little_history_returns_None(self, monkeypatch):
        self._stub_yf(monkeypatch, [100, 101, 102])
        assert agent._get_rsi("AAPL") is None

    def test_a_fetch_failure_returns_None_rather_than_raising(self, monkeypatch):
        import yfinance
        monkeypatch.setattr(yfinance, "Ticker",
                            lambda s: (_ for _ in ()).throw(RuntimeError("429")))
        assert agent._get_rsi("AAPL") is None

    def test_the_cache_spares_a_second_network_call(self, monkeypatch):
        seen = self._stub_yf(monkeypatch, list(range(100, 130)))
        agent._get_rsi("AAPL")
        agent._get_rsi("AAPL")
        assert len(seen) == 1, "multiple users on one ticker must pay one call"


class TestSellNudgesRejectGarbageQuotes:
    """🔴 The Jul 3 holiday-feed class, two sites the sweep missed.

    CLAUDE.md already says: *any code that compares a live price to a
    stop/target/entry, or leads a P&L number, must gate on plausible_price —
    never a bare `> 0` / `_is_pos`.* `_check_hold_or_fold` was fixed then; these
    two were not, and both TELL THE USER TO SELL.

    Measured before the fix:
      • _check_take_profit_nudge RAISED on NaN ("cannot convert float NaN to
        integer") and on inf — and because it raises mid-loop, ONE bad ticker
        killed the nudge for every remaining position of that user, with the
        dedup key already marked so it would not retry that day.
      • A 10x spike fired "Up 900.0% ... Unrealised gain ~$9,000" — a
        take-profit recommendation off a phantom gain.
      • _check_trailing_stops fired "crossed below your entry" on $0.01 and
        "up +900.0%" on a spike.
    """

    @pytest.fixture(autouse=True)
    def _wire(self, monkeypatch):
        self.sent = []
        # A stub must accept EVERYTHING production accepts — this one first
        # omitted the `buttons=` kwarg and raised TypeError into the caller,
        # which is the same class as the add_alert(kind=) stub bug.
        monkeypatch.setattr(agent, "send_inline_keyboard",
                            lambda *a, **k: self.sent.append(a[0] if a else k.get("text", "")))
        monkeypatch.setattr(agent, "send_message",
                            lambda *a, **k: self.sent.append(a[0] if a else ""))
        monkeypatch.setattr(agent, "_is_alerted", lambda *a, **k: False)
        monkeypatch.setattr(agent, "_mark_alerted", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_clear_alerted", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_miniapp_btn", lambda *a, **k: {})
        monkeypatch.setattr(agent, "_persist_notify_flags", lambda *a, **k: None)
        monkeypatch.setattr(agent, "_get_rsi", lambda t: None)
        self.pos = {"ticker": "AAPL", "entry_price": 100.0, "target_price": 110.0,
                    "stop_loss": 95.0, "shares": 10}
        monkeypatch.setattr(agent, "load_user_trade_log",
                            lambda uid: {"open": [dict(self.pos)]})

    BAD = [
        pytest.param(float("nan"), id="NaN"),
        pytest.param(float("inf"), id="inf"),
        pytest.param(0.01, id="holiday-$0.01"),
        # 15x. NOT 10x: plausible_price's documented window is
        # [ref*0.1, ref*10] INCLUSIVE, so exactly 1000 against a 100 entry is
        # accepted by design. Widening that is a change to a shared guard with
        # many callers, not a side effect of this fix.
        pytest.param(1500.0, id="15x-spike"),
    ]

    @pytest.mark.parametrize("px", BAD)
    def test_take_profit_is_silent_on_a_bad_quote(self, px):
        agent._check_take_profit_nudge("u1", {"AAPL": px})
        assert self.sent == [], f"a sell nudge fired off {px}: {self.sent}"

    @pytest.mark.parametrize("px", BAD)
    def test_trailing_stops_is_silent_on_a_bad_quote(self, px):
        buf = []
        agent._check_trailing_stops({"AAPL": px}, "u1", msg_buffer=buf)
        assert buf == [] and self.sent == [], f"a nudge fired off {px}: {buf or self.sent}"

    def test_a_legitimate_near_target_price_STILL_fires(self):
        """Raising a guard must not silence the feature it protects."""
        agent._check_take_profit_nudge("u1", {"AAPL": 109.0})
        assert any("near target" in m for m in self.sent)

    def test_a_legitimate_move_still_fires_a_trailing_nudge(self):
        buf = []
        agent._check_trailing_stops({"AAPL": 109.0}, "u1", msg_buffer=buf)
        assert buf or self.sent, "the trailing-stop nudge was silenced entirely"

    def test_ONE_bad_ticker_does_not_kill_the_users_other_positions(self, monkeypatch):
        """Degrade per-item, never all-or-nothing — the rule already applied to
        batch price fetches. A raise mid-loop dropped every later position."""
        monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: {"open": [
            {"ticker": "BAD", "entry_price": 100.0, "target_price": 110.0, "shares": 1},
            {"ticker": "GOOD", "entry_price": 100.0, "target_price": 110.0, "shares": 10},
        ]})
        agent._check_take_profit_nudge("u1", {"BAD": float("nan"), "GOOD": 109.0})
        assert any("GOOD" in m for m in self.sent), \
            "a bad quote on one ticker suppressed a valid nudge on another"
