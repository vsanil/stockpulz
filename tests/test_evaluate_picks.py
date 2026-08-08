"""Tests for the pick-quality evaluator (scripts/evaluate_picks.py).

The whole point of this script is HONESTY about small samples — these tests lock
that in, because a silently-confident report on n=8 is how an engine gets tuned
into overfitting.
"""
import os
import importlib.util

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "evaluate_picks.py")
_spec = importlib.util.spec_from_file_location("evaluate_picks", _PATH)
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)


def _rows(n, win_every_other=True, alpha=1.0):
    return [{"date": "2026-06-01", "ticker": f"T{i}", "asset": "stock",
             "timeframe": "short_term", "entry": 100, "target": 110, "stop": 95,
             "conviction": 4, "outcome": "target", "exit": 110,
             "ret_pct": 5.0 if (i % 2 or not win_every_other) else -3.0,
             "spy_pct": 1.0, "alpha_pct": alpha if (i % 2) else -alpha}
            for i in range(n)]


class TestWilsonInterval:
    def test_small_n_interval_is_wide(self):
        lo, hi = ev._wilson(5, 7)
        assert hi - lo > 40, "a 7-trade sample must NOT look precise"

    def test_large_n_interval_narrows(self):
        lo, hi = ev._wilson(55, 100)
        assert hi - lo < 25
        assert lo < 55 < hi

    def test_zero_n_safe(self):
        assert ev._wilson(0, 0) == (0.0, 0.0)


class TestHonestyGate:
    def test_small_sample_refuses_to_conclude(self):
        r = ev.build_report(_rows(8))
        assert "NOT CONCLUSIVE" in r
        assert "Do not tune the engine on this" in r

    def test_large_sample_is_allowed_to_conclude(self):
        r = ev.build_report(_rows(40))
        assert "NOT CONCLUSIVE" not in r
        assert "Sample is meaningful" in r

    def test_no_matured_picks_says_so(self):
        r = ev.build_report([])
        assert "No picks have matured" in r


class TestBenchmarkVerdict:
    def test_zero_alpha_reads_in_line_not_trailing(self):
        rows = _rows(40, alpha=0.0)
        for x in rows:
            x["alpha_pct"] = 0.0
        r = ev.build_report(rows)
        assert "in line" in r
        assert "trailing by" not in r      # "+0.00% trailing" was a real bug

    def test_positive_alpha_reads_beating(self):
        rows = _rows(40)
        for x in rows:
            x["alpha_pct"] = 2.0
        assert "beating" in ev.build_report(rows)


class TestScoringGuards:
    def test_unmatured_pick_is_not_scored(self):
        import datetime as dt
        today = dt.date.today().isoformat()
        assert ev.score_pick({"date": today, "ticker": "AAPL", "asset": "stock",
                              "entry": 100, "target": 110, "stop": 95}) is None, \
            "counting unmatured picks biases the sample toward fast winners"


class TestScreenProvenanceInLedger:
    """Screener features are recorded at pick time so the report can say WHICH
    signal earned a win. They cannot be backfilled — screener_cache has a 1-day
    TTL and holds survivors only — so a silent break here loses data forever."""

    def _picks(self, screen=None):
        p = {"ticker": "AAA", "entry_price": 100.0, "target_price": 110.0,
             "stop_loss": 95.0, "conviction": 4}
        if screen is not None:
            p["_screen"] = screen
        return {"_saved_date": "2026-08-10",
                "stocks": {"short_term": [p], "long_term": []}}

    def test_screen_is_copied_onto_the_ledger_row(self, monkeypatch):
        scr = {"score": 95, "rsi": 67.8, "setup_type": "breakout"}
        monkeypatch.setattr(ev, "_gist_file", lambda name: self._picks(scr))
        led = {}
        assert ev.record_today(led) == 1
        assert led["picks"][0]["screen"] == scr

    def test_row_still_records_when_screen_is_absent(self, monkeypatch):
        """Older picks (and any pick whose candidate wasn't matched) must still
        be ledgered — instrumentation is additive, never a filter."""
        monkeypatch.setattr(ev, "_gist_file", lambda name: self._picks(None))
        led = {}
        assert ev.record_today(led) == 1
        row = led["picks"][0]
        assert row["ticker"] == "AAA" and "screen" not in row

    def test_score_band_buckets_and_tolerates_missing(self):
        assert ev._score_band({"screen": {"score": 96}}) == "95+"
        assert ev._score_band({"screen": {"score": 90}}) == "85-94"
        assert ev._score_band({"screen": {"score": 71}}) == "70-84"
        assert ev._score_band({"screen": {"score": 40}}) == "<70"
        assert ev._score_band({}) == "?"
        assert ev._score_band({"screen": {"score": None}}) == "?"

    def test_slices_are_tagged_low_n_and_never_read_as_findings(self):
        rows = _rows(8)
        for i, r in enumerate(rows):
            r["screen"] = {"score": 95, "setup_type": "breakout" if i % 2 else "pullback"}
        out = ev.build_report(rows)
        assert "By setup" in out and "By screener score" in out
        assert "low n" in out, "an 8-pick slice must be marked low n"
        assert "NOT CONCLUSIVE" in out

    def test_setup_slice_is_skipped_when_nothing_was_recorded(self):
        out = ev.build_report(_rows(6))          # no `screen` on any row
        assert "By setup" not in out, "an all-unknown slice must not be printed"

    def test_report_flags_how_many_rows_predate_the_instrumentation(self):
        rows = _rows(10)
        for r in rows[:4]:
            r["screen"] = {"score": 95, "setup_type": "breakout"}
        out = ev.build_report(rows)
        assert "4/10" in out, "must say how much of the sample carries features"


class TestScreenProvenanceAttach:
    """ai_analyzer re-joins the screener features Claude's response drops."""

    def _mod(self):
        import importlib, sys, os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        return importlib.import_module("ai_analyzer")

    def test_setup_type_separates_breakout_from_pullback(self):
        a = self._mod()
        assert a._setup_type({"breakout_today": True, "rsi": 67.8}) == "breakout"
        assert a._setup_type({"breakout_today": False, "rsi": 45.0}) == "pullback"
        assert a._setup_type({"breakout_today": False, "rsi": 78.0}) == "other"
        assert a._setup_type({}) == "other"

    def test_attach_joins_by_ticker_and_symbol_and_compresses(self):
        a = self._mod()
        picks = {"stocks": {"short_term": [{"ticker": "AAA", "entry_price": 10}]},
                 "crypto": {"short_term": [{"symbol": "BTC", "entry_price": 1}]}}
        cands = [{"ticker": "AAA", "score": 95, "rsi": 67.83291, "breakout_today": True,
                  "market_cap": 64_000_000_000, "sector": "Health Care"},
                 {"symbol": "BTC", "score": 80, "rsi": 44.4444}]
        a._attach_screen_provenance(picks, cands)
        s = picks["stocks"]["short_term"][0]["_screen"]
        assert s["score"] == 95 and s["setup_type"] == "breakout"
        assert s["rsi"] == 67.83, "float must be rounded — the ledger keeps it forever"
        assert s["mcap_b"] == 64.0 and "market_cap" not in s
        assert picks["crypto"]["short_term"][0]["_screen"]["setup_type"] == "pullback"

    def test_attach_is_measurement_only_and_changes_no_pick(self):
        """The evaluator must never contaminate the engine: attaching provenance
        may ONLY add `_screen` — no pick added, dropped, reordered or edited."""
        import copy, json
        a = self._mod()
        picks = {"stocks": {"short_term": [
            {"ticker": "AAA", "entry_price": 10.0, "target_price": 12.0,
             "stop_loss": 9.0, "conviction": 5, "thesis": "x"}]}}
        before = copy.deepcopy(picks)
        a._attach_screen_provenance(picks, [{"ticker": "AAA", "score": 90, "rsi": 50}])
        after = picks["stocks"]["short_term"][0]
        assert set(after) - set(before["stocks"]["short_term"][0]) == {"_screen"}
        after_wo = {k: v for k, v in after.items() if k != "_screen"}
        assert after_wo == before["stocks"]["short_term"][0]

    def test_unmatched_pick_is_left_untouched(self):
        a = self._mod()
        picks = {"stocks": {"short_term": [{"ticker": "ZZZ", "entry_price": 1}]}}
        a._attach_screen_provenance(picks, [{"ticker": "AAA", "score": 90}])
        assert "_screen" not in picks["stocks"]["short_term"][0]

    def test_attach_tolerates_empty_and_malformed_input(self):
        a = self._mod()
        a._attach_screen_provenance({}, [])
        a._attach_screen_provenance({"stocks": {"short_term": None}}, None)
        a._attach_screen_provenance({"stocks": {}}, [{"no_ticker": 1}])
