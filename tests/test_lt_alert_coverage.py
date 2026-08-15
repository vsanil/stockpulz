"""Long-term picks reached real users with NO alerts at all (Aug 15).

Attribution work on 2026-08-14's picks:

    MRK  (stocks LT)  real users: no alert   synthetic bot: 128.81 / 165.00
    TLT  (ETF LT)     real users: no alert   synthetic bot: 78.16
    KMI  (LT + ST)    real users: 31.13      <- the SHORT-term stop, coincidence

The synthetic bot's alerts came from trade_logger backfilling a stop when it
LOGS a position (135.59 * 0.95 = 128.8105 exactly). Real users who simply read
the morning message log nothing, so they got nothing — the robot's activity was
masking a hole real users had.

Three reasonable rules combined into total silence:
  1. LT picks carry no stop_loss by design (don't stop out a multi-year thesis).
  2. The entry alert only arms at >=1% above entry (so it cannot insta-fire).
  3. That test runs ONCE, at publication, when price is by definition within
     2-3% of entry — the worst possible instant to apply it.

And a separate live bug found while fixing it: only one auto "below" alert could
exist per ticker, so the entry alert SILENTLY DESTROYED THE STOP on any pick
trading >=1% above entry.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import agent
import config_manager as cm
import price_alert_manager as pam


@pytest.fixture
def alerts(monkeypatch):
    """Drive the REAL add_alert against an in-memory store."""
    store: dict = {}

    def fake_mutate(_f, mutator, default=None):
        _new, res = mutator(store)
        return res

    monkeypatch.setattr(cm, "mutate_gist_file", fake_mutate)
    monkeypatch.setattr(agent, "add_alert", pam.add_alert)
    monkeypatch.setattr(agent, "load_user_trade_log", lambda uid: {"open": []})
    monkeypatch.setattr(cm, "load_weekly_picks", lambda: {})
    return store


def _run(monkeypatch, picks, prices):
    monkeypatch.setattr(pam, "_current_price", lambda t: prices.get(t, 100.0))
    monkeypatch.setattr(agent, "_download_prices", lambda tks, **kw: prices)
    agent._auto_set_pick_alerts(picks, ["999"])


def _got(store):
    return {(a["ticker"], a["target"], a.get("kind")) for a in store.get("999", [])}


def _picks(st=None, lt=None):
    return {"stocks": {"short_term": st or [], "long_term": lt or []},
            "crypto": {"short_term": [], "long_term": []},
            "etfs": {"short_term": [], "long_term": []},
            "commodities": {"short_term": [], "long_term": []}}


class TestEntryAlertNoLongerDestroysTheStop:
    """🔴 A live bug, not a hypothetical. agent sets the stop first and the
    entry alert second; both are "below" on the same ticker, and the auto-replace
    matched on ticker+direction alone — so the stop was silently dropped on
    exactly the picks that were running."""

    def test_both_survive(self, alerts, monkeypatch):
        _run(monkeypatch, _picks(st=[{"ticker": "ST", "entry_price": 100.0,
                                      "stop_loss": 95.0}]), {"ST": 110.0})
        got = _got(alerts)
        assert ("ST", 95.0, "stop") in got, "the STOP was destroyed by the entry alert"
        assert ("ST", 100.0, "entry") in got

    def test_a_repeat_run_does_not_duplicate(self, alerts, monkeypatch):
        p = _picks(st=[{"ticker": "ST", "entry_price": 100.0, "stop_loss": 95.0}])
        _run(monkeypatch, p, {"ST": 110.0})
        _run(monkeypatch, p, {"ST": 110.0})
        assert len(alerts["999"]) == 2, "same-purpose alerts must still collapse"

    def test_a_moved_stop_replaces_the_old_one(self, alerts, monkeypatch):
        _run(monkeypatch, _picks(st=[{"ticker": "ST", "entry_price": 100.0,
                                      "stop_loss": 95.0}]), {"ST": 110.0})
        _run(monkeypatch, _picks(st=[{"ticker": "ST", "entry_price": 100.0,
                                      "stop_loss": 97.0}]), {"ST": 110.0})
        stops = [a for a in alerts["999"] if a.get("kind") == "stop"]
        assert len(stops) == 1 and stops[0]["target"] == 97.0

    def test_a_legacy_alert_without_kind_is_treated_as_a_stop(self, alerts, monkeypatch):
        """Alerts written before `kind` existed were all stops. A new entry alert
        must not clobber one."""
        alerts["999"] = [{"ticker": "ST", "target": 95.0, "direction": "below",
                          "auto": True, "price_at_set": 100.0}]
        monkeypatch.setattr(pam, "_current_price", lambda t: 110.0)
        pam.add_alert("999", "ST", 100.0, direction="below", auto=True, kind="entry")
        assert any(a["target"] == 95.0 for a in alerts["999"]), \
            "a legacy (kind-less) stop was clobbered by an entry alert"


class TestLongTermPicksAreAlerted:
    def test_the_MRK_case_now_produces_an_alert(self, alerts, monkeypatch):
        """The exact live failure: an LT pick trading at entry, no stop."""
        _run(monkeypatch, _picks(lt=[{"ticker": "MRK", "entry_price": 135.59}]),
             {"MRK": 135.55})
        got = _got(alerts)
        assert got, "a long-term pick reached a user with NO alert of any kind"
        assert ("MRK", round(135.59 * 0.85, 2), "invalidation") in got

    def test_the_level_is_far_wider_than_any_stop(self):
        """It must never read as a stop-loss — a long-term thesis should survive
        an ordinary drawdown."""
        assert agent.LT_INVALIDATION_PCT >= 12.0

    def test_a_ticker_that_is_both_ST_and_LT_keeps_its_real_stop(self, alerts, monkeypatch):
        """KMI on 2026-08-14. The tight stop is the actionable level; adding a
        redundant -15% line would be noise, and (before `kind`) would have
        destroyed the stop outright."""
        _run(monkeypatch, _picks(st=[{"ticker": "KMI", "entry_price": 32.09,
                                      "stop_loss": 31.13}],
                                 lt=[{"ticker": "KMI", "entry_price": 32.09}]),
             {"KMI": 32.08})
        got = _got(alerts)
        assert ("KMI", 31.13, "stop") in got
        assert not [g for g in got if g[2] == "invalidation"]

    def test_a_pick_with_no_entry_price_invents_nothing(self, alerts, monkeypatch):
        _run(monkeypatch, _picks(lt=[{"ticker": "X", "entry_price": None}]), {"X": 50.0})
        assert not _got(alerts)


class TestReArming:
    """The entry test runs once, at publication, when price is ~entry. Without a
    retry a long-term pick can never get its buy-zone alert."""

    def _weekly(self, monkeypatch, data):
        monkeypatch.setattr(cm, "load_weekly_picks", lambda: data)

    def test_an_older_pick_arms_once_price_rises(self, alerts, monkeypatch):
        self._weekly(monkeypatch, {"2026-08-10": {
            "stocks": {"long_term": [{"ticker": "OLD", "entry_price": 100.0}]}}})
        monkeypatch.setattr(agent, "et_today",
                            lambda: __import__("datetime").date(2026, 8, 12))
        _run(monkeypatch, _picks(), {"OLD": 112.0})
        assert ("OLD", 100.0, "entry") in _got(alerts)

    def test_it_does_not_arm_while_price_is_still_at_entry(self, alerts, monkeypatch):
        self._weekly(monkeypatch, {"2026-08-10": {
            "stocks": {"long_term": [{"ticker": "OLD", "entry_price": 100.0}]}}})
        monkeypatch.setattr(agent, "et_today",
                            lambda: __import__("datetime").date(2026, 8, 12))
        _run(monkeypatch, _picks(), {"OLD": 100.2})
        assert not [g for g in _got(alerts) if g[2] == "entry"], \
            "arming at entry is the insta-fire the 1% rule exists to prevent"

    def test_a_stale_pick_is_dropped(self, alerts, monkeypatch):
        """Past one trading week the entry level is no longer the same trade."""
        self._weekly(monkeypatch, {"2026-07-01": {
            "stocks": {"long_term": [{"ticker": "STALE", "entry_price": 100.0}]}}})
        monkeypatch.setattr(agent, "et_today",
                            lambda: __import__("datetime").date(2026, 8, 12))
        _run(monkeypatch, _picks(), {"STALE": 130.0})
        assert not _got(alerts)

    def test_todays_pick_wins_over_the_older_one(self, alerts, monkeypatch):
        """A fresher entry supersedes — never alert on a stale level for a
        ticker that was re-picked today."""
        self._weekly(monkeypatch, {"2026-08-10": {
            "stocks": {"long_term": [{"ticker": "DUP", "entry_price": 100.0}]}}})
        monkeypatch.setattr(agent, "et_today",
                            lambda: __import__("datetime").date(2026, 8, 12))
        _run(monkeypatch, _picks(lt=[{"ticker": "DUP", "entry_price": 120.0}]),
             {"DUP": 140.0})
        entries = [g for g in _got(alerts) if g[2] == "entry"]
        assert entries == [("DUP", 120.0, "entry")]

    def test_a_broken_weekly_store_never_breaks_the_morning_run(self, monkeypatch):
        def boom():
            raise RuntimeError("gist down")
        monkeypatch.setattr(cm, "load_weekly_picks", boom)
        assert agent._lt_rearm_picks(_picks()) == []
