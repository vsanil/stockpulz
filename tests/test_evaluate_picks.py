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
