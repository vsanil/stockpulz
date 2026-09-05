"""🔴 `yf.download` returns MULTI-LEVEL columns even for ONE ticker since
yfinance ~0.2.51, so the long-standing idiom

    float(yf.download("^VIX", period="1d")["Close"].iloc[-1])

raises "float() argument must be a string or a real number, not 'Series'".

MEASURED in production 2026-09-05: `vix_check` failed on exactly this on every
run, inside a bare `except Exception` that printed and returned — so the job
exited 0, the workflow was GREEN, and the VIX alert had silently never fired.

These tests build BOTH column layouts by hand rather than calling yfinance, so
they pin the shape contract without a network call and keep working when the
library changes again.
"""
import pandas as pd
import pytest

from yf_utils import close_series, latest_close


def _flat(values):
    """Old layout: single-level columns."""
    return pd.DataFrame({"Open": values, "Close": values},
                        index=pd.date_range("2026-09-01", periods=len(values)))


def _multi(values, ticker="^VIX"):
    """Current layout: MultiIndex (field, ticker) — even for one ticker."""
    idx = pd.date_range("2026-09-01", periods=len(values))
    cols = pd.MultiIndex.from_tuples([("Open", ticker), ("Close", ticker)])
    return pd.DataFrame({("Open", ticker): values, ("Close", ticker): values},
                        index=idx, columns=cols)


class TestTheExactProductionFailure:
    def test_multiindex_frame_yields_a_float_not_a_Series(self):
        """The regression. Pre-fix this path produced a Series and TypeError'd."""
        assert latest_close(_multi([17.5, 18.25])) == pytest.approx(18.25)

    def test_the_old_idiom_yields_a_Series_not_a_scalar(self):
        """Proves the bug is real rather than assumed.

        ⚠️ Asserts the SHAPE, not the exception. Whether `float(one_element_series)`
        raises TypeError or merely FutureWarning depends on the pandas version
        (2.3.3 warns; CI's newer pandas raises — which is why production saw a
        hard TypeError and a local run would not have). The shape is the
        version-independent fact, so that is what gets pinned."""
        raw = _multi([17.5, 18.25])
        assert not isinstance(raw["Close"].iloc[-1], float)
        assert getattr(raw["Close"], "ndim", 1) == 2   # DataFrame, not Series

    def test_flat_layout_still_works(self):
        """Older yfinance / other callers must not regress."""
        assert latest_close(_flat([10.0, 11.5])) == pytest.approx(11.5)


class TestShapeTolerance:
    @pytest.mark.parametrize("frame", [_multi([1.0, 2.0]), _flat([1.0, 2.0])])
    def test_accepts_the_whole_frame(self, frame):
        assert latest_close(frame) == pytest.approx(2.0)

    def test_accepts_an_already_extracted_series(self):
        assert latest_close(_flat([3.0, 4.0])["Close"]) == pytest.approx(4.0)

    def test_close_series_is_one_dimensional(self):
        s = close_series(_multi([5.0, 6.0]))
        assert getattr(s, "ndim", 1) == 1 and len(s) == 2


class TestFailsToNoneNeverToZero:
    """⚠️ A 0.0 here would flow into price comparisons. This repo has a
    documented history of a bogus $0.00 satisfying every 'below target' test
    and cascading into false alerts, so absence must read as None."""

    @pytest.mark.parametrize("bad", [None, pd.DataFrame()])
    def test_no_data_is_None(self, bad):
        assert latest_close(bad) is None

    def test_all_nan_is_None_not_nan(self):
        assert latest_close(_flat([float("nan"), float("nan")])) is None


class TestCallSitesUseTheHelper:
    """Pins the CLASS, not just the one line. This codebase declared the
    `price > 0` guard 'fully closed' and four more live sites surfaced weeks
    later; a call-site assertion is what would have caught that."""

    def test_no_single_ticker_download_bypasses_the_helper(self):
        """Flags a SINGLE-ticker download indexed by ["Close"] without the helper.

        🔑 Keyed on the ticker being a STRING LITERAL, which is what makes it a
        single-ticker call and therefore multi-level-column shaped. Multi-ticker
        sites pass a list or a variable (`yf.download(tickers, ...)`) and get a
        DataFrame by design — they are correct and must not be flagged.

        ⚠️ An earlier version of this test required `float(` in the SAME
        statement. Both real SPY sites coerce two lines later, so the test
        passed with the fix reverted — it looked like a guard and was not one.
        Mutation-checking is what exposed that; keep doing it here.

        ⚠️ Also does NOT flag `Ticker().history()["Close"]` — history() returns
        FLAT columns, so those four agent.py sites are genuine scalars.
        """
        import pathlib
        import re
        pat = re.compile(r'(?:yf|_yf|yfinance)\.download\(\s*["\'][^"\']+["\'][^\n]*?\["Close"\]')
        bad = []
        for f in ["agent.py", "cmd_helpers.py", "cmd_market.py"]:
            src = pathlib.Path(f).read_text()
            for m in pat.finditer(src):
                line = src[:m.start()].count("\n") + 1
                bad.append(f"{f}:{line} {m.group(0)[:90]}")
        assert not bad, ("single-ticker download must go through "
                         "yf_utils.close_series/latest_close:\n" + "\n".join(bad))
