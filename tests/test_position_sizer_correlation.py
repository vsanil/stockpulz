"""_correlation_warnings — 73 lines, zero coverage.

It tells a user two picks move together and to consider skipping one. Both
failure directions cost something: a false warning talks them out of a good
pick, and silence leaves them doubled up on one bet without knowing it.
"""
import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import position_sizer as ps


def _picks(*tickers, cat="short_term"):
    return {cat: [{"ticker": t} for t in tickers]}


def _frame(series: dict, n=30):
    """Build the (Close, TICKER) MultiIndex frame yf.download returns."""
    return pd.DataFrame({("Close", t): v for t, v in series.items()},
                        index=pd.date_range("2026-01-01", periods=n))


@pytest.fixture
def yf(monkeypatch):
    box = {"frame": None, "asked": None}

    def _dl(tickers, **k):
        box["asked"] = tickers
        return box["frame"]
    import yfinance
    monkeypatch.setattr(yfinance, "download", _dl)
    return box


def _from_returns(rets, start=100.0):
    p = [start]
    for r in rets:
        p.append(p[-1] * (1 + r))
    return p


# 🔴 Correlation is computed on daily RETURNS, not on prices. Two linear ramps
# in opposite directions have returns that BOTH decay monotonically (1/100 >
# 1/101 > …), so they correlate at +0.99 — the opposite of what the price
# picture suggests. Genuine anti-correlation needs mirrored daily MOVES.
_UP_A = [100 + i for i in range(30)]
_UP_B = [50 + i * 0.5 for i in range(30)]
_MIRROR_UP = _from_returns([(0.02 if i % 2 else -0.01) for i in range(29)])
_MIRROR_DN = _from_returns([(-0.02 if i % 2 else 0.01) for i in range(29)])


class TestWhenItWarns:
    def test_two_tightly_correlated_picks_are_flagged(self, yf):
        yf["frame"] = _frame({"AAPL": _UP_A, "MSFT": _UP_B})
        w = ps._correlation_warnings(_picks("AAPL", "MSFT"))
        assert len(w) == 1 and "AAPL" in w[0] and "MSFT" in w[0]
        assert "High correlation" in w[0]

    def test_inversely_moving_picks_are_not_flagged(self, yf):
        """Negative correlation is diversification, not overlap.

        The fixture uses MIRRORED DAILY MOVES (r = -1.0), not two opposite
        price ramps — those have returns correlating at +0.99 and this test
        failed against them, correctly. Price direction is not return
        correlation.
        """
        yf["frame"] = _frame({"AAPL": _MIRROR_UP, "GLD": _MIRROR_DN})
        assert ps._correlation_warnings(_picks("AAPL", "GLD")) == []

    def test_the_threshold_is_the_module_constant(self, yf):
        assert 0 < ps._HIGH_CORR_THRESHOLD <= 1.0
        yf["frame"] = _frame({"AAPL": _UP_A, "MSFT": _UP_B})
        assert ps._correlation_warnings(_picks("AAPL", "MSFT")), \
            "a perfectly correlated pair must clear any sane threshold"

    def test_each_pair_is_reported_once_and_never_against_itself(self, yf):
        yf["frame"] = _frame({"A": _UP_A, "B": _UP_B, "C": [10 + i for i in range(30)]})
        w = ps._correlation_warnings(_picks("A", "B", "C"))
        assert len(w) == 3, f"3 tickers = 3 unique pairs, got {len(w)}"
        assert not any(f"{t} ↔ {t}" in x for x in w for t in "ABC")

    def test_long_term_picks_are_included_too(self, yf):
        yf["frame"] = _frame({"AAPL": _UP_A, "MSFT": _UP_B})
        assert ps._correlation_warnings(
            {"short_term": [{"ticker": "AAPL"}],
             "long_term": [{"ticker": "MSFT"}]})


class TestWhenItStaysSilent:
    def test_a_single_pick_cannot_be_correlated_with_anything(self, yf):
        assert ps._correlation_warnings(_picks("AAPL")) == []
        assert yf["asked"] is None, "it fetched prices for a single ticker"

    def test_no_picks_at_all_is_silent(self, yf):
        assert ps._correlation_warnings({}) == []

    def test_a_ticker_with_too_little_history_is_dropped(self, yf):
        """< 15 points is not enough to claim a correlation."""
        short = [100 + i for i in range(10)] + [None] * 20
        yf["frame"] = _frame({"AAPL": _UP_A, "NEW": short})
        assert ps._correlation_warnings(_picks("AAPL", "NEW")) == []

    def test_a_thin_frame_is_silent_via_the_column_filter(self, yf):
        """This is caught by dropna(thresh=15), NOT by the `len(log_rets) < 10`
        line below it.

        🔴 That second guard is UNREACHABLE. A column must have >= 15 non-NaN
        points to survive the filter, and pandas' pct_change() pads interior
        NaNs by default, so anything that survives yields >= 14 return rows —
        never fewer than 10. Verified by construction: a 30-row frame with a
        column valid only on even indices still produces 29 return rows.

        Documented rather than contorted into a passing test. It is harmless
        defensive code, but nobody should believe it is covered.
        """
        yf["frame"] = _frame({"A": _UP_A[:8], "B": _UP_B[:8]}, n=8)
        assert ps._correlation_warnings(_picks("A", "B")) == []

    def test_the_min_rows_guard_is_unreachable_given_the_column_filter(self):
        """If the thresh ever drops below ~11, the guard becomes live again and
        this test should be replaced with a real one."""
        import inspect
        src = inspect.getsource(ps._correlation_warnings)
        assert "thresh=15" in src and "len(log_rets) < 10" in src, \
            "the two guards moved — re-derive whether the second is reachable"

    def test_a_fetch_failure_degrades_to_silence_and_never_raises(self, monkeypatch):
        """Sizing must not break because a correlation check could not run."""
        import yfinance
        monkeypatch.setattr(yfinance, "download",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429")))
        assert ps._correlation_warnings(_picks("AAPL", "MSFT")) == []

    def test_an_empty_frame_is_silent(self, yf):
        yf["frame"] = pd.DataFrame()
        assert ps._correlation_warnings(_picks("AAPL", "MSFT")) == []

    def test_a_pick_with_no_ticker_is_ignored(self, yf):
        assert ps._correlation_warnings(
            {"short_term": [{"ticker": ""}, {"entry_price": 100}]}) == []
