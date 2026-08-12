"""Walk-forward harness — the maths that decides whether an "edge" is real.

A backtest is the easiest thing in this repo to fool yourself with, so the two
things worth pinning are the forward-scoring rule (must be conservative and
free of look-ahead) and the confidence interval (must be wide when n is small).
"""
import importlib.util
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_spec = importlib.util.spec_from_file_location(
    "bwf", os.path.join(ROOT, "scripts", "backtest_walkforward.py"))
bwf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bwf)


def _bars(rows):
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D")
    return pd.DataFrame(rows, index=idx)


class TestForwardScoring:
    def test_stop_is_checked_BEFORE_target_on_the_same_bar(self):
        """The conservative assumption. A bar that touches both must count as a
        loss — otherwise every ambiguous day flatters the engine, and ambiguous
        days are exactly the volatile ones."""
        rows = [{"Close": 100, "High": 100, "Low": 100}]
        rows += [{"Close": 100, "High": 130, "Low": 80}]      # touches both
        rows += [{"Close": 100, "High": 100, "Low": 100}] * 6
        r = bwf.score_forward(_bars(rows), 0, atr_pct=4.0)
        assert r["outcome"] == "stop", "a both-touched bar must resolve as a loss"

    def test_target_hit(self):
        rows = [{"Close": 100, "High": 100, "Low": 100}]
        rows += [{"Close": 118, "High": 118, "Low": 99}]
        rows += [{"Close": 118, "High": 118, "Low": 118}] * 6
        assert bwf.score_forward(_bars(rows), 0, atr_pct=4.0)["outcome"] == "target"

    def test_neither_hit_marks_to_market(self):
        rows = [{"Close": 100, "High": 100, "Low": 100}]
        rows += [{"Close": 103, "High": 104, "Low": 99}] * 8
        r = bwf.score_forward(_bars(rows), 0, atr_pct=4.0)
        assert r["outcome"] == "expired"
        assert r["ret"] == pytest.approx(3.0, abs=0.01)

    def test_only_FUTURE_bars_are_used(self):
        """Look-ahead is the way a backtest lies. A crash BEFORE the entry bar
        must not count against the trade."""
        rows = [{"Close": 100, "High": 100, "Low": 1}]        # crash, but it is the past
        rows += [{"Close": 100, "High": 100, "Low": 100}]
        rows += [{"Close": 104, "High": 104, "Low": 103}] * 7
        r = bwf.score_forward(_bars(rows), 1, atr_pct=4.0)
        assert r["outcome"] != "stop"

    def test_too_little_forward_data_returns_none(self):
        rows = [{"Close": 100, "High": 100, "Low": 100}] * 3
        assert bwf.score_forward(_bars(rows), 0, atr_pct=4.0) is None

    def test_stop_width_is_clamped(self):
        """A 0.1% ATR must not produce a stop so tight everything loses, and a
        60% ATR must not produce one so wide nothing does."""
        rows = [{"Close": 100, "High": 100, "Low": 100}]
        rows += [{"Close": 100, "High": 100, "Low": 96}]      # -4%
        rows += [{"Close": 100, "High": 100, "Low": 100}] * 6
        assert bwf.score_forward(_bars(rows), 0, atr_pct=0.01)["outcome"] == "stop"   # floor 3%
        assert bwf.score_forward(_bars(rows), 0, atr_pct=99.0)["outcome"] == "expired"  # cap 20%


class TestWilson:
    def test_small_samples_are_honestly_wide(self):
        lo, hi = bwf.wilson(4, 7)
        assert lo < 30 and hi > 80, f"n=7 must be near-useless, got [{lo}-{hi}]"

    def test_large_samples_tighten(self):
        lo, hi = bwf.wilson(550, 1000)
        assert hi - lo < 8, f"n=1000 should be tight, got [{lo}-{hi}]"

    def test_zero_n_is_safe(self):
        assert bwf.wilson(0, 0) == (0.0, 0.0)


class TestHoldoutDiscipline:
    def test_holdout_is_withheld_unless_asked(self):
        """The whole value of the harness is that the holdout stays unseen.
        Assert the flag exists and defaults off, rather than trusting prose."""
        import inspect
        src = inspect.getsource(bwf.main)
        assert '"--holdout"' in src and 'action="store_true"' in src
        assert "if a.holdout:" in src
        assert "holdout withheld" in src

    def test_viewing_the_holdout_is_recorded(self):
        import inspect
        src = inspect.getsource(bwf.main)
        # NB: assert on a token that survives line-wrapping — the full phrase
        # "no longer out-of-sample" is split across two source lines.
        assert "HOLDOUT_LOG" in src
        assert "out-of-sample" in src and "HOLDOUT VIEWED" in src
