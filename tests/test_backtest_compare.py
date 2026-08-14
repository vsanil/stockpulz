"""The proposed-change harness: does a candidate strategy beat what we ship?

These tests care most about the ways this could produce a CONFIDENT WRONG
ANSWER, because its whole purpose is to gate changes to what real users are
recommended. A harness that says "adopt" on noise is worse than no harness.
"""
import importlib.util
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

_PATH = os.path.join(ROOT, "scripts", "backtest_compare.py")
_spec = importlib.util.spec_from_file_location("backtest_compare", _PATH)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


def _rows(wins: int, losses: int):
    return ([{"ticker": f"W{i}", "outcome": "target", "ret": 10.3} for i in range(wins)]
            + [{"ticker": f"L{i}", "outcome": "stop", "ret": -5.5} for i in range(losses)])


class TestDisagreementOnly:
    """🔴 The load-bearing design decision.

    Two strategies over the same dates mostly pick the same names. Those shared
    picks have IDENTICAL outcomes by construction, so counting them drags both
    win rates together and manufactures "no difference" regardless of how far
    apart the strategies actually are.
    """

    def _bars(self):
        idx = pd.date_range("2024-01-01", periods=120, freq="D")
        out = {}
        for t, up in (("WIN1", True), ("WIN2", True), ("LOSE1", False), ("LOSE2", False),
                      ("SAME1", True), ("SAME2", True), ("SAME3", True)):
            close = [100.0] * 60 + ([100 + i * 2 for i in range(60)] if up
                                    else [100 - i * 2 for i in range(60)])
            out[t] = pd.DataFrame(
                {"Open": close, "High": [c * 1.02 for c in close],
                 "Low": [c * 0.98 for c in close], "Close": close,
                 "Volume": [1_000_000] * 120}, index=idx)
        return out

    def test_shared_picks_are_excluded_from_the_verdict(self):
        bars = self._bars()
        shared = [("SAME1", 60, 3.0), ("SAME2", 60, 3.0), ("SAME3", 60, 3.0)]
        a = {"2024-03-01": shared + [("WIN1", 60, 3.0), ("WIN2", 60, 3.0)]}
        b = {"2024-03-01": shared + [("LOSE1", 60, 3.0), ("LOSE2", 60, 3.0)]}

        h = bc.head_to_head(bars, a, b)
        assert h["shared"] == 3
        assert h["a"]["n"] == 2 and h["b"]["n"] == 2, \
            "shared picks leaked into the head-to-head sample"
        assert h["a"]["win_pct"] == 100.0 and h["b"]["win_pct"] == 0.0, \
            "the difference was diluted by picks both strategies made"

    def test_total_agreement_reports_no_evidence_rather_than_zero_edge(self):
        """If A and B pick identically there is no information either way. The
        harness must say so, not print a confident 0.0 edge."""
        bars = self._bars()
        same = {"2024-03-01": [("SAME1", 60, 3.0), ("SAME2", 60, 3.0)]}
        h = bc.head_to_head(bars, same, dict(same))
        assert h["a"]["n"] == 0 and h["b"]["n"] == 0
        assert h["overlap_pct"] == 100.0
        v, why = bc.verdict(h, "cand", "default")
        assert v == "NOT CONCLUSIVE" and "agree" in why


class TestVerdictIsHardToPass:
    def test_a_small_sample_is_never_conclusive(self):
        """Matches evaluate_picks._MIN_N — the bar for changing what users see
        is the same as the bar for calling a result conclusive."""
        h = {"overlap_pct": 20.0, "shared": 5,
             "a": _s(20, 29), "b": _s(5, 29),
             "edge_pts": 51.0, "edge_lo": 30.0, "edge_hi": 70.0}
        v, _ = bc.verdict(h, "cand", "default")
        assert v == "NOT CONCLUSIVE", "a 29-pick sample must not adopt a change"

    def test_an_interval_spanning_zero_is_not_a_win(self):
        h = {"overlap_pct": 20.0, "shared": 5,
             "a": _s(31, 60), "b": _s(29, 60),
             "edge_pts": 3.3, "edge_lo": -14.0, "edge_hi": 20.0}
        v, why = bc.verdict(h, "cand", "default")
        assert v == "NO DETECTABLE DIFFERENCE"
        assert "Not evidence of equality" in why, \
            "failure to detect must not be reported as proof of no difference"

    def test_a_real_separation_is_reported_but_still_gated_on_a_live_arm(self):
        h = {"overlap_pct": 20.0, "shared": 5,
             "a": _s(50, 60), "b": _s(20, 60),
             "edge_pts": 50.0, "edge_lo": 33.0, "edge_hi": 65.0}
        v, why = bc.verdict(h, "cand", "default")
        assert v == "CAND WINS"
        assert "A/B arm" in why and "survivorship" in why.lower(), \
            "a backtest win must never read as permission to ship"


def _s(wins, n):
    p, lo, hi = bc._wilson(wins, n)
    return {"n": n, "wins": wins, "win_pct": p * 100,
            "lo": lo * 100, "hi": hi * 100, "avg": 0.0}


class TestStatistics:
    def test_wilson_is_honestly_wide_at_small_n(self):
        _p, lo, hi = bc._wilson(4, 7)
        assert (hi - lo) * 100 > 50, "a 7-sample interval must look useless"

    def test_wilson_tightens_with_n(self):
        _p, lo1, hi1 = bc._wilson(40, 70)
        _p, lo2, hi2 = bc._wilson(400, 700)
        assert (hi2 - lo2) < (hi1 - lo1)

    def test_identical_rates_give_an_interval_containing_zero(self):
        d, lo, hi = bc._diff_ci(30, 60, 30, 60)
        assert d == 0.0 and lo < 0 < hi

    def test_a_large_separation_excludes_zero(self):
        _d, lo, hi = bc._diff_ci(55, 60, 10, 60)
        assert lo > 0 and hi > 0

    def test_a_perfect_arm_does_not_collapse_the_interval(self):
        """🔴 Where Wald breaks and a backtest lands often.

        At p=1.0 Wald's variance term p(1-p) is ZERO, so a 12-for-12 arm gets a
        zero-width interval and the harness would report a decisive, adoptable
        win from twelve observations. Newcombe inherits Wilson's behaviour and
        stays honestly wide.
        """
        w1, n1, w2, n2 = 12, 12, 6, 12
        _d, lo, hi = bc._diff_ci(w1, n1, w2, n2)
        p1, p2 = w1 / n1, w2 / n2
        wald = 1.96 * ((p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2) ** 0.5)
        assert (hi - lo) > 2 * wald, "interval is no wider than the one Wald collapses"
        assert lo < 0.5, "a 12-sample perfect run must not certify a >50 pt edge"

    def test_near_even_rates_on_a_modest_sample_cannot_separate(self):
        _d, lo, hi = bc._diff_ci(20, 32, 14, 32)
        assert lo < 0 < hi, "this sample cannot separate them; it must not claim to"


class TestSharedRulesWithTheWalkForwardHarness:
    """One definition of the rules, so a comparison cannot silently score picks
    differently than the single-strategy report does."""

    def test_levels_are_the_measured_medians_not_config_defaults(self):
        import backtest_walkforward as wf
        assert (wf.TARGET_PCT, wf.STOP_PCT) == (10.3, 5.5)
        assert 1.8 < wf.TARGET_PCT / wf.STOP_PCT < 2.0, \
            "config defaults (3.8:1) mechanically manufacture stop-outs"

    def test_forward_scoring_is_reused_not_reimplemented(self):
        import inspect
        src = inspect.getsource(bc)
        assert "wf.score_forward" in src
        assert "def score_forward" not in src, "must not fork the scoring rule"

    def test_the_holdout_is_unreachable_from_this_script(self):
        """Comparing candidates IS tuning. The holdout is spent the first time it
        is looked at, so this script must not be able to reach it."""
        import inspect
        src = inspect.getsource(bc)
        assert "--holdout" not in src and "HOLDOUT_LOG" not in src
        assert "d <= split" in src, "must restrict itself to the TRAIN window"


class TestPicksByDate:
    """One scoring pass yields both the picks and the mid-ranked control —
    scoring the universe is the expensive half, and deriving them separately
    doubled the cost of every comparison for no benefit."""

    def _wire(self, monkeypatch):
        idx = pd.date_range("2024-01-01", periods=100, freq="D")
        frame = pd.DataFrame({"Open": [10.0] * 100, "High": [11.0] * 100,
                              "Low": [9.0] * 100, "Close": [10.0] * 100,
                              "Volume": [1000] * 100}, index=idx)
        bars = {f"T{i}": frame for i in range(10)}

        # Rank deterministically: T0 scores 10, T1 scores 9, … so the ordering
        # is known and the buckets can be asserted by name.
        order = {f"T{i}": 10.0 - i for i in range(10)}
        seq = iter(sorted(bars))

        def scorer(_h, _strat):
            return (order[next(seq)], {"atr_pct": 3.0})

        monkeypatch.setattr(bc.screener, "_short_term_score", scorer, raising=False)
        monkeypatch.setattr(bc.wf, "MIN_BARS", 10)
        return bars, idx

    def test_it_returns_top_ranked_picks_and_a_mid_ranked_control(self, monkeypatch):
        bars, idx = self._wire(monkeypatch)
        picks, mids = bc.picks_by_date(bars, [idx[-1]], top_n=3, strat=None)
        key = str(idx[-1].date())
        assert [t for t, _i, _a in picks[key]] == ["T0", "T1", "T2"]
        assert len(mids[key]) == 3
        assert not set(t for t, _i, _a in picks[key]) & set(t for t, _i, _a in mids[key]), \
            "the control must not overlap the picks it is a control for"


class TestUnknownStrategyIsRejected:
    def test_main_refuses_an_unknown_name(self, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["backtest_compare.py", "does_not_exist"])
        assert bc.main() == 2
        assert "unknown strategy" in capsys.readouterr().out
