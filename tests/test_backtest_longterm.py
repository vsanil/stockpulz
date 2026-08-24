"""Guards for the long-term backtest.

Its whole value is that the numbers are honest, so the tests are about the
disciplines rather than the arithmetic: no look-ahead, no forked rules, no
reachable holdout, and an honest verdict at small n.
"""
import ast
import pathlib

import pytest

SRC = pathlib.Path("scripts/backtest_longterm.py")


def _mod():
    import importlib.util
    spec = importlib.util.spec_from_file_location("lt_t", str(SRC.resolve()))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class TestRulesAreInheritedNotForked:
    """`backtest_compare` has the same guard, for the same reason: a forked
    scoring rule lets one harness silently use different rules than another."""

    def test_bars_and_wilson_come_from_the_short_term_harness(self):
        src = SRC.read_text()
        assert "from backtest_walkforward import" in src
        for name in ("MIN_BARS", "load_bars", "wilson"):
            assert name in src.split("from backtest_walkforward import")[1][:120]

    def test_it_does_not_redefine_them(self):
        tree = ast.parse(SRC.read_text())
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert not defined & {"load_bars", "wilson", "score_forward"}, \
            "a re-implemented rule drifts from the harness it claims to match"

    def test_the_real_rubric_is_called(self):
        """Re-implementing _long_term_score here would let the backtest and
        production disagree without anything failing."""
        assert "screener._long_term_score(" in SRC.read_text()


class TestNoLookAhead:
    def test_the_200ma_leg_is_fed_a_point_in_time_slice(self):
        """🔴 The subtlest hole. _long_term_score fetches its own bars, so
        without patching that fetch the leg reads TODAY's price — look-ahead in
        the one leg that looks like price data and therefore feels safe."""
        src = SRC.read_text()
        assert "screener._alpaca_single_bars = lambda" in src
        i = src.index("screener._alpaca_single_bars = lambda")
        assert "hist.iloc[max(0, i - 400):i + 1]" in src[:i], \
            "the patched fetch must return a slice ending at the decision bar"

    def test_the_real_fetch_is_restored(self):
        """A leaked patch would silently affect every later caller in-process."""
        src = SRC.read_text()
        assert "finally:" in src and "screener._alpaca_single_bars = real_bars" in src

    def test_fundamentals_are_asked_for_as_of_the_decision_bar(self):
        src = SRC.read_text()
        assert 'date = str(hist.index[i].date())' in src
        assert "fundamentals_as_of(ticker, date" in src

    def test_the_forward_window_starts_AFTER_the_decision_bar(self):
        m = _mod()
        import pandas as pd
        idx = pd.date_range("2024-01-01", periods=400, freq="D")
        hist = pd.DataFrame({"Close": [100.0] * 200 + [110.0] * 200}, index=idx)
        out = m.forward(hist, 199, None)
        assert out["ret_pct"] > 0, "entry must be the decision close, exit later"


class TestTheHoldoutIsUnreachable:
    """Comparing candidates IS tuning, and the holdout is spent the first time
    it is looked at. It belongs to the short-term harness."""

    def test_there_is_no_holdout_flag(self):
        # Anchor on the argparse CALL, not the bare string: the module docstring
        # explains that there is deliberately no --holdout here, so a prose scan
        # flags itself. Fifth time that trap has appeared in this repo.
        assert 'add_argument("--holdout"' not in SRC.read_text()

    def test_the_holdout_log_is_never_imported(self):
        assert "HOLDOUT_LOG" not in SRC.read_text()


class TestHonestVerdict:
    def test_it_refuses_to_conclude_below_the_gate(self):
        src = SRC.read_text()
        assert "MIN_N = 30" in src
        assert "NOT CONCLUSIVE" in src

    def test_the_gate_matches_the_live_evaluator(self):
        import sys
        sys.path.insert(0, "scripts")
        import evaluate_picks as ep
        assert _mod().MIN_N == ep._MIN_N, \
            "if 30 is the bar for the report the owner reads, it is the bar here"

    def test_the_confidence_interval_is_not_double_scaled(self):
        """🔴 It printed CI[4530.0-6540.0]. wilson() already returns percentages,
        and multiplying again garbles the ONE number that decides whether to
        believe the result."""
        src = SRC.read_text()
        assert "CI[{lo:.1f}-{hi:.1f}]" in src
        assert "lo*100" not in src and "lo * 100" not in src

    def test_a_missing_benchmark_is_announced_not_silently_dropped(self):
        """Without SPY the win rate is absolute, so it cannot separate a good
        rubric from a rising market. Saying nothing would overstate it."""
        assert "no SPY benchmark" in SRC.read_text()

    def test_a_baseline_bucket_exists(self):
        """A win rate alone is unreadable — it might just be the market."""
        assert '"baseline"' in SRC.read_text()

    def test_the_caveats_are_printed_not_just_documented(self):
        src = SRC.read_text()
        tail = src[src.index("def main("):]
        for phrase in ("ANNUAL", "survivorship", "no stops"):
            assert phrase in tail, f"{phrase!r} must travel with the numbers"

    def test_the_horizon_is_long_enough_to_mean_something(self):
        """A multi-year thesis judged at 30 days measures noise."""
        assert _mod().LT_HORIZON_DAYS >= 90


class TestSectorMapping:
    def test_sic_maps_to_the_names_the_rubric_uses(self):
        import sys
        sys.path.insert(0, ".")
        import screener
        m = _mod()
        for _lo, _hi, name in m._SIC:
            assert name in screener.SECTOR_MEDIAN_PE, \
                f"{name} is not a key the P/E leg can look up"

    @pytest.mark.parametrize("sic,expect", [
        (3571, "Technology"), (2834, "Health Care"), (1311, "Energy"),
        (6022, "Financials"), (4911, "Utilities"), (6798, "Real Estate"),
    ])
    def test_known_codes(self, sic, expect):
        assert _mod()._sector(sic) == expect

    def test_an_unknown_code_is_Unknown_not_a_guess(self):
        m = _mod()
        assert m._sector(None) == "Unknown" and m._sector(9999) == "Unknown"
