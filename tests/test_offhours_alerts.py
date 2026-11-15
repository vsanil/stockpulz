"""Price alerts outside the stock session: crypto only, but actually checked.

Found 2026-11-14. `check_all_alerts` was called by exactly three jobs
(confirmation, close_check, price_alerts), all Monday-Friday inside the trading
day. Crypto trades around the clock and picks go out on Saturdays, so a weekend
crypto pick carried a stop alert that nothing looked at until Monday morning.

The price_alerts trigger now also fires hourly through evenings, nights and
weekends. Outside the stock session the run restricts itself to crypto:
  * the watchlist "moving X% today" check keys its dedup on the DATE, so on a
    Saturday it would re-announce Friday's stock move as today's;
  * a stock alert priced off a stale close is noise at best.

Every test PINS the clock. `run_price_alerts` used to have no clock dependence;
a test that inherits the wall clock would now pass or fail by the hour the suite
happened to run — the clock class this repo has paid for six times.
"""
import os
import sys
from datetime import datetime

import pytest
import pytz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent  # noqa: E402
import price_alert_manager as pam  # noqa: E402

ET = pytz.timezone("America/New_York")


def _et(y, m, d, hh, mm=0):
    return ET.localize(datetime(y, m, d, hh, mm))


FRI_OPEN   = _et(2026, 11, 13, 10, 0)     # a normal trading morning
FRI_NIGHT  = _et(2026, 11, 13, 21, 15)
SATURDAY   = _et(2026, 11, 14, 11, 15)
THANKSGIVING = _et(2026, 11, 26, 11, 0)   # a weekday US market holiday


# ── When is the stock session "open" for alerting? ───────────────────────────

class TestStockSessionWindow:
    @pytest.mark.parametrize("when, expected", [
        (_et(2026, 11, 13, 8, 29), False),    # one minute early
        (_et(2026, 11, 13, 8, 30), True),     # pre-market quotes are live
        (FRI_OPEN, True),
        (_et(2026, 11, 13, 16, 30), True),    # the run that sees the closing print
        (_et(2026, 11, 13, 16, 59), True),
        (_et(2026, 11, 13, 17, 0), False),    # the boundary is exclusive
        (FRI_NIGHT, False),
        (_et(2026, 11, 13, 3, 15), False),    # small hours of a trading day
        (SATURDAY, False),
        (_et(2026, 11, 15, 11, 15), False),   # Sunday
        (THANKSGIVING, False),
    ])
    def test_the_window(self, monkeypatch, when, expected):
        monkeypatch.delenv("FORCE_CALENDAR_RUN", raising=False)
        assert agent._stock_session_open(when) is expected

    def test_the_override_forces_a_full_run(self, monkeypatch):
        monkeypatch.setenv("FORCE_CALENDAR_RUN", "1")
        assert agent._stock_session_open(SATURDAY) is True


class TestCryptoTickerScope:
    @pytest.mark.parametrize("t", ["BTC", "btc", "BTC-USD", " eth "])
    def test_coins_are_in_scope(self, t):
        assert agent._is_crypto_ticker(t) is True

    @pytest.mark.parametrize("t", ["AAPL", "GLD", "SPY", "", None, "USD"])
    def test_everything_else_is_not(self, t):
        assert agent._is_crypto_ticker(t) is False

    def test_it_is_the_one_canonical_set(self):
        """A coin added to price_checker must be alertable off-hours with no
        second list to update — eight divergent lists is how HYPE once worked
        in one feature and broke in another."""
        from price_checker import CRYPTO_SYMBOLS
        assert CRYPTO_SYMBOLS, "the canonical set is empty — nothing was tested"
        assert all(agent._is_crypto_ticker(s) for s in CRYPTO_SYMBOLS)


# ── The alert store: a filtered check must not touch what it skipped ─────────

CHAT = "7770001"


class TestFilteredAlertCheck:
    def _seed(self):
        # conftest prices: AAPL 189.50, BTC 68,000 — both alerts are already hit.
        pam.add_alert(CHAT, "AAPL", 180.0, direction="above")
        pam.add_alert(CHAT, "BTC", 60000.0, direction="above")

    def test_only_the_accepted_tickers_fire(self):
        self._seed()
        fired = pam.check_alerts(CHAT, ticker_filter=agent._is_crypto_ticker)
        assert len(fired) == 1 and "BTC" in fired[0]

    def test_a_skipped_alert_stays_armed(self):
        """The dangerous failure: a stock alert silently DELETED by a weekend
        run. It must still be there, untouched, on Monday."""
        self._seed()
        pam.check_alerts(CHAT, ticker_filter=agent._is_crypto_ticker)
        left = pam._load_alerts().get(CHAT) or []
        assert [a["ticker"] for a in left] == ["AAPL"]

    def test_the_skipped_alert_fires_on_the_next_full_check(self):
        self._seed()
        pam.check_alerts(CHAT, ticker_filter=agent._is_crypto_ticker)
        fired = pam.check_alerts(CHAT)
        assert len(fired) == 1 and "AAPL" in fired[0]

    def test_no_filter_means_everything_as_before(self):
        self._seed()
        assert len(pam.check_alerts(CHAT)) == 2

    def test_check_all_alerts_passes_the_filter_through(self):
        self._seed()
        assert pam.check_all_alerts(ticker_filter=agent._is_crypto_ticker) == 1
        assert [a["ticker"] for a in pam._load_alerts().get(CHAT) or []] == ["AAPL"]


# ── The job itself ───────────────────────────────────────────────────────────

@pytest.fixture
def job(monkeypatch):
    """run_price_alerts with every outbound edge stubbed. One user holding a
    stock and a coin, both 3.8% short of target, both on the watchlist and both
    moving 10%."""
    box = {"sent": [], "filter": "unset", "picks_checked": 0, "picks_loaded": 0}
    monkeypatch.delenv("FORCE_CALENDAR_RUN", raising=False)
    monkeypatch.setattr(agent, "_log_cron_run", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_all_recipients", lambda: ["u1"])
    monkeypatch.setattr(agent, "get_user_config", lambda uid: {})
    monkeypatch.setattr(agent, "_is_quiet_hours", lambda uid: False)
    monkeypatch.setattr(agent, "broadcast_all",
                        lambda p: (box["sent"].extend(p),
                                   {x["chat_id"]: True for x in p})[1])
    monkeypatch.setattr(agent, "_miniapp_btn", lambda *a, **k: {})
    monkeypatch.setattr(agent, "_miniapp_url_btn", lambda *a, **k: {})
    monkeypatch.setattr(agent, "cache_get", lambda *a, **k: None)
    monkeypatch.setattr(agent, "cache_set", lambda *a, **k: None)

    def _alerts(*a, **k):
        box["filter"] = k.get("ticker_filter")
        return 0
    monkeypatch.setattr(agent, "check_all_alerts", _alerts)

    monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: {
        "open": [{"ticker": "AAPL", "entry_price": 100.0, "target_price": 110.0},
                 {"ticker": "BTC", "entry_price": 100.0, "target_price": 110.0}],
        "watchlist": ["AAPL", "BTC"]})
    monkeypatch.setattr(agent, "_download_prices",
                        lambda tickers, **k: {t: 106.0 for t in tickers})
    monkeypatch.setattr(agent, "_watch_move_quote", lambda t: (110.0, 100.0))

    def _load_picks():
        box["picks_loaded"] += 1
        return {"stocks": {"short_term": [{"ticker": "AAPL", "stop_loss": 95}]}}
    monkeypatch.setattr(agent, "load_picks", _load_picks)
    monkeypatch.setattr(agent, "get_current_prices", lambda p: {"AAPL": 90.0})
    monkeypatch.setattr(agent, "_check_picks_stop_loss",
                        lambda *a, **k: box.__setitem__("picks_checked",
                                                        box["picks_checked"] + 1))
    return box


def _texts(box):
    return " | ".join(p["text"] for p in box["sent"])


class TestOffHoursRunIsCryptoOnly:
    @pytest.mark.parametrize("when", [SATURDAY, FRI_NIGHT, THANKSGIVING])
    def test_the_alert_check_is_scoped_to_crypto(self, job, when):
        agent.run_price_alerts(when)
        f = job["filter"]
        assert callable(f), "off-hours the alert check must be filtered"
        assert f("BTC") and not f("AAPL")

    def test_only_the_coin_gets_a_target_approach_nudge(self, job):
        agent.run_price_alerts(SATURDAY)
        body = _texts(job)
        assert "BTC</b> is <b>3.8%</b> from your target" in body
        assert "AAPL</b> is <b>3.8%</b> from your target" not in body

    def test_fridays_stock_move_is_not_reannounced_on_saturday(self, job):
        agent.run_price_alerts(SATURDAY)
        body = _texts(job)
        assert "BTC</b> is moving" in body
        assert "AAPL</b> is moving" not in body

    def test_the_equity_stop_check_does_not_run(self, job):
        agent.run_price_alerts(SATURDAY)
        assert job["picks_checked"] == 0 and job["picks_loaded"] == 0


class TestInSessionRunIsUnchanged:
    """Raising a guard must not silence the feature."""

    def test_the_alert_check_is_unfiltered(self, job):
        agent.run_price_alerts(FRI_OPEN)
        assert job["filter"] is None

    def test_stocks_and_coins_both_nudge_and_both_move(self, job):
        agent.run_price_alerts(FRI_OPEN)
        body = _texts(job)
        for t in ("AAPL", "BTC"):
            assert f"{t}</b> is <b>3.8%</b> from your target" in body
            assert f"{t}</b> is moving" in body

    def test_the_equity_stop_check_runs(self, job):
        agent.run_price_alerts(FRI_OPEN)
        assert job["picks_checked"] == 1


class TestTheTriggerCanReachItOffHours:
    def test_price_alerts_is_not_calendar_blocked_on_a_weekend(self, monkeypatch):
        """If this mode were ever added to SESSION_BOUND_MODES the weekend
        trigger would be dispatched and silently skipped."""
        monkeypatch.delenv("FORCE_CALENDAR_RUN", raising=False)
        assert agent._calendar_block_reason("price_alerts", SATURDAY) == ""
        assert agent._calendar_block_reason("price_alerts", THANKSGIVING) == ""
