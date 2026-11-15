"""Automatic pick alerts expire after a week (owner's call, 2026-11-15).

They were created every morning for every pick and never removed: 76 to 119
per user, the oldest from May, mostly for picks the user never bought. Any of
them could still fire a Telegram alert months later.

Kept: every MANUAL alert, any automatic alert on a ticker the user holds, and
anything that cannot be dated. `_history_*` keys are never touched.
"""
import ast
import os
from datetime import datetime, timedelta, timezone

import price_alert_manager as pam

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _a(ticker, days_old, auto=True, kind="stop", **kw):
    a = {"ticker": ticker, "direction": "below", "target": 10.0,
         "set_at": (NOW - timedelta(days=days_old)).isoformat(), "price_at_set": 11.0}
    if auto:
        a.update(auto=True, kind=kind)
    a.update(kw)
    return a


def _tickers(store, uid):
    return [a["ticker"] for a in store.load("price_alerts.json")[uid]]


class TestTheRule:
    def test_an_automatic_alert_older_than_a_week_is_stale(self):
        assert pam.is_stale_auto_alert(_a("OLD", 8), set(), NOW)

    def test_a_week_old_or_newer_is_kept(self):
        assert not pam.is_stale_auto_alert(_a("NEW", 6.9), set(), NOW)
        assert not pam.is_stale_auto_alert(_a("EDGE", 7), set(), NOW)

    def test_every_kind_expires_the_same_way(self):
        for kind in ("stop", "entry", "invalidation"):
            assert pam.is_stale_auto_alert(_a("X", 30, kind=kind), set(), NOW), kind

    def test_a_legacy_automatic_alert_without_a_kind_expires_too(self):
        a = _a("LEG", 120)
        a.pop("kind")
        assert pam.is_stale_auto_alert(a, set(), NOW)

    def test_a_manual_alert_never_expires(self):
        assert not pam.is_stale_auto_alert(_a("MINE", 200, auto=False), set(), NOW)

    def test_a_held_ticker_keeps_its_alert(self):
        assert not pam.is_stale_auto_alert(_a("HELD", 60), {"HELD"}, NOW)

    def test_a_naive_timestamp_is_read_as_utc(self):
        a = _a("NAIVE", 0)
        a["set_at"] = (NOW - timedelta(days=8)).replace(tzinfo=None).isoformat()
        assert pam.is_stale_auto_alert(a, set(), NOW)

    def test_a_naive_timestamp_inside_the_week_is_kept(self):
        """The boundary case: a naive stamp read as anything but UTC moves it."""
        a = _a("NAIVE", 0)
        a["set_at"] = (NOW - timedelta(days=6, hours=12)).replace(tzinfo=None).isoformat()
        assert not pam.is_stale_auto_alert(a, set(), NOW)

    def test_a_naive_timestamp_does_not_crash_the_comparison(self):
        a = _a("NAIVE", 0)
        a["set_at"] = "2026-05-22T05:19:42.686422"      # the real oldest alert's stamp
        assert pam.is_stale_auto_alert(a, set(), NOW) is True

    def test_an_undatable_alert_is_kept(self):
        for bad in (None, "", "yesterday", 12345):
            a = _a("X", 30)
            a["set_at"] = bad
            assert not pam.is_stale_auto_alert(a, set(), NOW), bad


class TestThePrune:
    def test_it_removes_only_stale_automatic_alerts(self, _gist_store):
        _gist_store.write("price_alerts.json", {
            "u1": [_a("OLD", 30), _a("NEW", 2), _a("MINE", 90, auto=False), _a("HELD", 40)],
            "u2": [_a("GONE", 10, kind="invalidation")],
            "_history_u1": [{"ticker": "OLD", "triggered_at": "2026-09-01"}],
        })
        n, users = pam.prune_stale_auto_alerts(lambda uid: {"HELD"} if uid == "u1" else set(),
                                               now=NOW)
        assert (n, users) == (2, 2)
        assert _tickers(_gist_store, "u1") == ["NEW", "MINE", "HELD"]
        assert _tickers(_gist_store, "u2") == []
        assert _gist_store.load("price_alerts.json")["_history_u1"], "history must be untouched"

    def test_unreadable_holdings_leave_that_user_alone(self, _gist_store):
        _gist_store.write("price_alerts.json", {"u1": [_a("OLD", 30)], "u2": [_a("OLD", 30)]})

        def _held(uid):
            if uid == "u1":
                raise RuntimeError("trade log unreadable")
            return set()
        assert pam.prune_stale_auto_alerts(_held, now=NOW) == (1, 1)
        assert _tickers(_gist_store, "u1") == ["OLD"]
        assert _tickers(_gist_store, "u2") == []

    def test_nothing_stale_writes_nothing(self, _gist_store, monkeypatch):
        _gist_store.write("price_alerts.json", {"u1": [_a("NEW", 1)]})
        writes = []
        real = _gist_store.write
        monkeypatch.setattr(_gist_store, "write", lambda f, d: (writes.append(f), real(f, d)))
        assert pam.prune_stale_auto_alerts(lambda uid: set(), now=NOW) == (0, 0)
        assert writes == []


class TestTheMorningRunPrunesBeforeSetting:
    def test_the_order(self):
        """Pruning after setting would delete nothing new, but pruning FIRST is
        what lets a ticker picked again today be re-armed at today's levels."""
        src = open(os.path.join(ROOT, "agent.py"), encoding="utf-8").read()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "run_morning")
        calls = [c.func.id for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id in ("_prune_stale_pick_alerts", "_auto_set_pick_alerts")]
        lines = {name: min(c.lineno for c in ast.walk(fn) if isinstance(c, ast.Call)
                           and isinstance(c.func, ast.Name) and c.func.id == name)
                 for name in set(calls)}
        assert set(lines) == {"_prune_stale_pick_alerts", "_auto_set_pick_alerts"}
        assert lines["_prune_stale_pick_alerts"] < lines["_auto_set_pick_alerts"]

    def test_the_morning_wrapper_never_raises(self, monkeypatch):
        import agent
        import price_alert_manager
        monkeypatch.setattr(price_alert_manager, "prune_stale_auto_alerts",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert agent._prune_stale_pick_alerts() == 0
