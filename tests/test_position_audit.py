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

    def test_two_positions_same_ticker_same_day_get_different_ids(self):
        """🔴 The bot scales into a ticker twice in one day routinely — it did
        exactly that with AMBA on 2026-08-03. Hashing only
        (check, account, ticker, date) collided, so resolving one finding in the
        admin worklist silently resolved the other."""
        f = audit_account("u1", _log(open_=[
            {"ticker": "AMBA", "entry_price": 82.675, "target_price": 78.54,
             "opened_date": "2026-08-03"},
            {"ticker": "AMBA", "entry_price": 82.21, "target_price": 79.74,
             "opened_date": "2026-08-03"}]))
        assert len(f) == 2
        assert f[0]["id"] != f[1]["id"], "two distinct positions collapsed to one finding"

    def test_id_changes_when_the_level_is_corrected(self):
        """A corrected position is a DIFFERENT finding — the old disposition
        must not carry over and hide the new state."""
        bad = audit_account("u1", _log(open_=[
            {"ticker": "A", "entry_price": 10.0, "target_price": 9.0, "opened_date": "d"}]))
        worse = audit_account("u1", _log(open_=[
            {"ticker": "A", "entry_price": 10.0, "target_price": 8.0, "opened_date": "d"}]))
        assert bad[0]["id"] != worse[0]["id"]

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


class TestExitReasonIsRecorded:
    """🔴 close_trade used to hardcode outcome='manual' for EVERY caller,
    including the synthetic bot — which knows perfectly well whether it sold at
    target or at stop. The reason was thrown away, so stop-vs-target could not
    be measured, and that is the one number separating "the pick was wrong" from
    "the stop was too tight" — problems with opposite fixes."""

    def _wire(self, monkeypatch, log):
        import trade_logger, config_manager
        state = {"log": log}
        def _mut(chat_id, mutator):
            new, res = mutator(state["log"])
            if new is not config_manager.NO_WRITE:
                state["log"] = new
            return res
        monkeypatch.setattr(trade_logger, "mutate_user_trade_log", _mut)
        return state

    def test_the_bot_can_record_a_target_exit(self, monkeypatch):
        import trade_logger
        st = self._wire(monkeypatch, {"open": [{"ticker": "A", "entry_price": 10.0}],
                                      "closed": [], "watchlist": []})
        trade_logger.close_trade("A", "u1", exit_price=12.0, outcome="target")
        assert st["log"]["closed"][0]["outcome"] == "target"

    def test_the_bot_can_record_a_stop_exit(self, monkeypatch):
        import trade_logger
        st = self._wire(monkeypatch, {"open": [{"ticker": "A", "entry_price": 10.0}],
                                      "closed": [], "watchlist": []})
        trade_logger.close_trade("A", "u1", exit_price=9.0, outcome="stop")
        assert st["log"]["closed"][0]["outcome"] == "stop"

    def test_a_human_sell_still_defaults_to_manual(self, monkeypatch):
        """The Mini App sell flow genuinely IS a human decision — it must keep
        the default rather than be mislabelled as a target/stop hit."""
        import trade_logger
        st = self._wire(monkeypatch, {"open": [{"ticker": "A", "entry_price": 10.0}],
                                      "closed": [], "watchlist": []})
        trade_logger.close_trade("A", "u1", exit_price=11.0)
        assert st["log"]["closed"][0]["outcome"] == "manual"

    def test_junk_outcomes_fall_back_to_manual(self, monkeypatch):
        """A free-text outcome would poison every aggregate that groups by it."""
        import trade_logger
        st = self._wire(monkeypatch, {"open": [{"ticker": "A", "entry_price": 10.0}],
                                      "closed": [], "watchlist": []})
        trade_logger.close_trade("A", "u1", exit_price=11.0, outcome="<script>")
        assert st["log"]["closed"][0]["outcome"] == "manual"

    def test_the_synthetic_bot_actually_passes_a_reason(self):
        """Guard the wiring, not just the capability — the capability existing
        while the caller ignores it is exactly the bug being fixed."""
        import os
        src = open(os.path.join(ROOT, "scripts", "synthetic_user.py")).read()
        assert 'outcome="target"' in src and 'outcome="stop"' in src
