"""Position integrity: arithmetic that must hold regardless of strategy.

Found by hand on live data first — two AMBA positions whose target sat BELOW
their entry (a long trade that cannot win), and a FICO fill carrying a stop
ABOVE its entry (born stopped-out). No win rate and no mocked test surfaces
that: it only appears when a real position is built by the real path.
"""
import os, sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from position_audit import audit_account, apply_dispositions, summarise


def _log(open_=(), closed=()):
    return {"open": list(open_), "closed": list(closed), "watchlist": []}


class TestIntegrityChecks:
    def test_target_below_entry_is_caught(self):
        """The real AMBA record: entry 82.675, target 78.54."""
        f = audit_account("u1", _log(open_=[
            {"ticker": "AMBA", "entry_price": 82.675, "target_price": 78.54, "stop_loss": 75.68}]))
        assert [x["check"] for x in f] == ["levels.target_below_entry"]
        assert f[0]["live"] and f[0]["severity"] == "high"

    def test_stop_above_entry_is_caught(self):
        """The real FICO record: filled 1177.74 carrying the pick's 1290 stop."""
        f = audit_account("u1", _log(open_=[
            {"ticker": "FICO", "entry_price": 1177.74, "stop_loss": 1290.0}]))
        assert [x["check"] for x in f] == ["levels.stop_above_entry"]

    def test_inverted_levels_are_caught(self):
        f = audit_account("u1", _log(open_=[
            {"ticker": "X", "entry_price": 100.0, "target_price": 90.0, "stop_loss": 95.0}]))
        assert {x["check"] for x in f} == {"levels.target_below_entry", "levels.target_below_stop"}

    def test_missing_entry_short_circuits(self):
        """Without an entry every %-based number is dead — report that and stop,
        rather than emitting noise about levels relative to nothing."""
        f = audit_account("u1", _log(open_=[
            {"ticker": "X", "entry_price": None, "target_price": 90.0, "stop_loss": 200.0}]))
        assert [x["check"] for x in f] == ["entry.missing"]

    def test_nan_entry_is_treated_as_missing(self):
        f = audit_account("u1", _log(open_=[{"ticker": "X", "entry_price": float("nan")}]))
        assert [x["check"] for x in f] == ["entry.missing"]

    def test_a_healthy_position_yields_nothing(self):
        f = audit_account("u1", _log(open_=[
            {"ticker": "OK", "entry_price": 100.0, "target_price": 115.0,
             "stop_loss": 92.0, "shares": 3}]))
        assert f == []

    def test_closed_trades_are_historical_not_live(self):
        f = audit_account("u1", _log(closed=[
            {"ticker": "AMBA", "entry_price": 82.675, "target_price": 78.54}]))
        assert f[0]["live"] is False and f[0]["severity"] == "info"

    def test_paper_positions_use_avg_price(self):
        f = audit_account("u1", _log(), paper={"positions": [
            {"ticker": "P", "avg_price": 50.0, "target_price": 45.0}]})
        assert [x["check"] for x in f] == ["levels.target_below_entry"]

    def test_ids_are_stable_across_runs(self):
        """Otherwise the same defect becomes a new row every day and the list
        stops being readable."""
        pos = [{"ticker": "A", "entry_price": 10.0, "target_price": 9.0, "opened_date": "2026-08-03"}]
        a = audit_account("u1", _log(open_=pos))
        b = audit_account("u1", _log(open_=pos))
        assert a[0]["id"] == b[0]["id"]

    def test_ids_differ_across_accounts(self):
        pos = [{"ticker": "A", "entry_price": 10.0, "target_price": 9.0}]
        assert (audit_account("u1", _log(open_=pos))[0]["id"]
                != audit_account("u2", _log(open_=pos))[0]["id"])


class TestDispositions:
    def _f(self, live=True):
        return audit_account("u1", _log(open_=[{"ticker": "A", "entry_price": 10.0,
                                                "target_price": 9.0}])
                             if live else _log(closed=[{"ticker": "A", "entry_price": 10.0,
                                                        "target_price": 9.0}]))

    def test_resolving_a_still_present_live_finding_reopens_it(self):
        """🔴 The mechanic that makes this useful: otherwise 'resolved' just
        means 'hidden' while the defect is still in production."""
        f = self._f(live=True)
        out = apply_dispositions(f, {f[0]["id"]: {"status": "resolved"}})
        assert out[0]["status"] == "reopened"
        assert "STILL PRESENT" in out[0]["detail"]

    def test_resolving_a_historical_finding_sticks(self):
        """A closed trade cannot be fixed retroactively — acknowledging is final."""
        f = self._f(live=False)
        out = apply_dispositions(f, {f[0]["id"]: {"status": "resolved"}})
        assert out[0]["status"] == "resolved"

    def test_ignored_is_respected_even_when_live(self):
        f = self._f(live=True)
        out = apply_dispositions(f, {f[0]["id"]: {"status": "ignored"}})
        assert out[0]["status"] == "ignored"

    def test_worst_first_ordering(self):
        findings = [
            {"id": "1", "check": "c", "ticker": "Z", "live": False, "status": "open", "detail": ""},
            {"id": "2", "check": "c", "ticker": "A", "live": True,  "status": "open", "detail": ""},
        ]
        out = apply_dispositions(findings, {})
        assert [f["ticker"] for f in out] == ["A", "Z"], "live problems must sort first"

    def test_summary_counts_only_actionable_live(self):
        findings = [
            {"id": "1", "check": "c", "ticker": "A", "live": True,  "status": "open", "detail": ""},
            {"id": "2", "check": "c", "ticker": "B", "live": False, "status": "open", "detail": ""},
            {"id": "3", "check": "c", "ticker": "C", "live": True,  "status": "resolved", "detail": ""},
        ]
        s = summarise(findings)
        assert s["total"] == 3 and s["live_open"] == 1
