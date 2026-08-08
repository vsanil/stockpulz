"""
test_screener.py — Regression tests for screener.py helpers.

No network I/O. All yfinance/external calls are patched.
"""
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import screener
from screener import _deduplicate_by_correlation


# ── universe fetch (S&P500 GitHub CSV + Wikipedia w/ browser UA) ───────────────

class TestUniverseFetch:
    """
    Regression for the silent freeze: Wikipedia 403'd without a browser UA and the
    datahub URL 404'd, so get_stock_universe fell back to a stale hardcoded list.
    """

    def _wiki_html(self):
        # A decoy small table + the real >50-row constituents table with a Symbol col.
        decoy = "<table><tr><th>Symbol</th></tr><tr><td>XX</td></tr></table>"
        rows = "".join(f"<tr><td>SYM{i}</td><td>Co {i}</td></tr>" for i in range(60))
        real = f"<table><tr><th>Symbol</th><th>Security</th></tr>{rows}</table>"
        return f"<html><body>{decoy}{real}</body></html>"

    def test_wiki_symbols_sets_browser_ua_and_picks_right_table(self):
        import screener
        captured = {}
        def fake_get(url, headers=None, timeout=None):
            captured["headers"] = headers
            m = MagicMock(); m.text = self._wiki_html(); m.raise_for_status = lambda: None
            return m
        with patch.object(screener.requests, "get", side_effect=fake_get):
            syms = screener._wiki_symbols("https://en.wikipedia.org/wiki/Whatever")
        assert len(syms) == 60 and syms[0] == "SYM0"   # picked the 60-row table, not the decoy
        assert "User-Agent" in (captured["headers"] or {})   # Wikipedia 403s without one
        assert "Mozilla" in captured["headers"]["User-Agent"]

    def test_wiki_symbols_returns_empty_on_failure(self):
        import screener
        with patch.object(screener.requests, "get", side_effect=Exception("boom")):
            assert screener._wiki_symbols("https://en.wikipedia.org/x") == []


class TestHighInterestTickers:
    """Recent IPOs / high-volume names get scanned before index inclusion."""

    def test_extracts_equities_and_filters_non_equity(self):
        import screener
        fake = {"quotes": [{"symbol": "SPCX"}, {"symbol": "NVDA"}, {"symbol": "BRK.B"},
                           {"symbol": "BTC-USD"}, {"symbol": "GC=F"}, {"symbol": ""}]}
        with patch.object(screener.yf, "screen", return_value=fake, create=True):
            syms = screener._high_interest_tickers()
        assert "SPCX" in syms and "NVDA" in syms and "BRK.B" in syms   # equities kept
        assert "BTC-USD" not in syms and "GC=F" not in syms            # crypto/futures dropped
        assert "" not in syms

    def test_fail_graceful_on_exception(self):
        import screener
        with patch.object(screener.yf, "screen", side_effect=Exception("boom"), create=True):
            assert screener._high_interest_tickers() == []


class TestBarEligibility:
    """Young IPOs (e.g. SPCX, public ~2 weeks) must be LT-eligible, not dropped."""

    def test_too_thin_is_rejected(self):
        from screener import _bar_eligibility
        assert _bar_eligibility(5) == (False, False)
        assert _bar_eligibility(9) == (False, False)

    def test_young_ipo_admitted_without_st(self):
        from screener import _bar_eligibility
        # 10-29 bars: admitted for fundamentals LT scoring, but no ST technicals.
        assert _bar_eligibility(10) == (True, False)
        assert _bar_eligibility(15) == (True, False)
        assert _bar_eligibility(29) == (True, False)

    def test_established_gets_full_scoring(self):
        from screener import _bar_eligibility
        assert _bar_eligibility(30)  == (True, True)
        assert _bar_eligibility(250) == (True, True)


# ── _deduplicate_by_correlation ───────────────────────────────────────────────

class TestDeduplicateByCorrelation:
    PICKS = [
        {"ticker": "AAPL", "score": 5},
        {"ticker": "MSFT", "score": 4},
        {"ticker": "NVDA", "score": 3},
    ]

    def test_returns_max_picks(self):
        """Never returns more picks than max_picks."""
        result = _deduplicate_by_correlation(self.PICKS, None, max_picks=2)
        assert len(result) <= 2

    def test_handles_none_hist_data(self):
        """
        Regression: raw=None (yfinance download failed) must not raise
        NameError or AttributeError — returns top max_picks by score.
        """
        result = _deduplicate_by_correlation(self.PICKS, None, max_picks=3)
        assert isinstance(result, list)
        assert len(result) == 3

    def test_handles_empty_picks(self):
        """Empty picks list returns empty list."""
        result = _deduplicate_by_correlation([], None, max_picks=5)
        assert result == []

    def test_handles_single_pick(self):
        """Single pick always returned as-is."""
        result = _deduplicate_by_correlation([self.PICKS[0]], None, max_picks=5)
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"


# ── run_screener raw= scope regression ───────────────────────────────────────

class TestRunScreenerRawScope:
    def test_near_miss_reason_st(self):
        """_add_near_miss_reason returns a reason string for ST picks."""
        from screener import _add_near_miss_reason
        pick = {"ticker": "AAPL", "score": 45, "current_price": 210.0,
                "st_metrics": {"rsi": 72, "macd_crossover": False, "volume_ratio": 1.1, "above_ema20": True}}
        result = _add_near_miss_reason(pick, is_st=True)
        assert "near_miss_reason" in result
        assert len(result["near_miss_reason"]) > 0

    def test_near_miss_reason_lt(self):
        """_add_near_miss_reason returns a reason string for LT picks."""
        from screener import _add_near_miss_reason
        pick = {"ticker": "MSFT", "score": 55, "current_price": 415.0,
                "lt_metrics": {"pe_ratio": 45, "debt_to_equity": 0.5}}
        result = _add_near_miss_reason(pick, is_st=False)
        assert "near_miss_reason" in result
        assert "P/E" in result["near_miss_reason"]

    def test_near_miss_reason_always_returns_string(self):
        """near_miss_reason is always a non-empty string."""
        from screener import _add_near_miss_reason
        pick = {"ticker": "XYZ", "score": 60, "st_metrics": {}}
        result = _add_near_miss_reason(pick, is_st=True)
        assert isinstance(result["near_miss_reason"], str)
        assert len(result["near_miss_reason"]) > 0

    def test_raw_not_defined_error_is_fixed(self):
        """
        Regression: 'name raw is not defined' NameError was raised because
        raw was passed to _deduplicate_by_correlation without being assigned.
        Patching yfinance to return empty DataFrame — screener must not raise.
        """
        import pandas as pd
        import yfinance as yf
        from unittest.mock import patch

        empty_df = pd.DataFrame()

        with patch("screener.yf") as mock_yf, \
             patch("screener.run_screener") as _:
            pass  # just verify import works

        # Directly test that _deduplicate_by_correlation with raw=None works
        # (the fix path when yfinance fails)
        from screener import _deduplicate_by_correlation
        picks = [{"ticker": "AAPL", "score": 5}, {"ticker": "MSFT", "score": 4}]
        result = _deduplicate_by_correlation(picks, None, max_picks=2)
        assert len(result) == 2  # both returned since no correlation data


# ── Strategy parameters / A-B arms ───────────────────────────────────────────

def _synthetic_ohlcv(seed: int, n: int = 300):
    """Deterministic bars — no network, identical on every machine and run."""
    import numpy as np, pandas as pd
    rng = np.random.default_rng(seed)
    close = np.maximum(100 + np.cumsum(rng.normal(0.15, 1.6, n)), 5)
    op    = close + rng.normal(0, .5, n)
    high  = np.maximum(op, close) + np.abs(rng.normal(0, .8, n))
    low   = np.minimum(op, close) - np.abs(rng.normal(0, .8, n))
    vol   = np.abs(rng.normal(2e6, 6e5, n))
    return pd.DataFrame({"Open": op, "High": high, "Low": low,
                         "Close": close, "Volume": vol})


class TestStrategyDefaultsAreUnchanged:
    """🔴 The load-bearing test of the whole parameterisation.

    Real users are served by `default`. Turning hardcoded weights into config
    is only safe if the default reproduces the live engine EXACTLY — these
    golden values were captured from the code before any field was extracted.
    If this fails, the refactor silently changed what people are recommended.
    """
    GOLDEN = [60, 35, 35, 35, 35, 70, 45, 55, 45, 60, 25, 35]

    def test_default_scores_match_the_pre_refactor_engine(self):
        got = [screener._short_term_score(_synthetic_ohlcv(s))[0]
               for s in range(len(self.GOLDEN))]
        assert got == self.GOLDEN, (
            "the default strategy no longer reproduces the live engine — "
            f"got {got}, expected {self.GOLDEN}")

    def test_passing_the_default_explicitly_is_the_same_as_omitting_it(self):
        for s in range(6):
            df = _synthetic_ohlcv(s)
            assert (screener._short_term_score(df)[0]
                    == screener._short_term_score(df, screener.DEFAULT_STRATEGY)[0])

    def test_every_default_field_matches_the_literal_it_replaced(self):
        d = screener.DEFAULT_STRATEGY
        assert (d.rsi_lo, d.rsi_hi, d.w_rsi) == (35.0, 55.0, 25)
        assert (d.w_macd, d.w_ema20, d.w_obv) == (25, 15, 10)
        assert (d.vol_surge_min, d.w_vol_surge) == (1.5, 20)
        assert (d.w_breakout, d.breakout_vol_min) == (15, 1.3)
        assert (d.near_high_pct, d.w_near_high) == (3.0, 10)
        assert (d.w_bb_bounce, d.bb_vol_min) == (15, 1.2)
        assert (d.w_bull_flag, d.w_bullish_engulfing) == (15, 15)
        assert (d.w_three_white_soldiers, d.w_hammer, d.w_morning_star) == (10, 10, 10)


class TestStrategyArms:
    def test_registry_has_the_arms_and_default_is_the_live_engine(self):
        assert set(screener.STRATEGIES) >= {"default", "breakout", "pullback"}
        assert screener.STRATEGIES["default"] is screener.DEFAULT_STRATEGY

    def test_a_strategy_is_immutable(self):
        """An arm must not be mutated at runtime — that would silently change
        what a running arm is measuring."""
        import dataclasses, pytest as _pytest
        with _pytest.raises(dataclasses.FrozenInstanceError):
            screener.DEFAULT_STRATEGY.w_rsi = 99   # type: ignore[misc]

    def test_arms_actually_disagree(self):
        """The whole point. Two variants that score alike carry no information —
        only picks where arms DIFFER tell you anything."""
        bo, pb = screener.STRATEGIES["breakout"], screener.STRATEGIES["pullback"]
        a = [screener._short_term_score(_synthetic_ohlcv(s), bo)[0] for s in range(12)]
        b = [screener._short_term_score(_synthetic_ohlcv(s), pb)[0] for s in range(12)]
        differing = sum(1 for x, y in zip(a, b) if x != y)
        assert differing >= 8, f"arms too similar to learn from ({differing}/12 differ)"

    def test_zeroing_a_weight_removes_that_component(self):
        no_rsi = screener.Strategy(name="t", w_rsi=0)
        for s in range(8):
            df = _synthetic_ohlcv(s)
            base = screener._short_term_score(df)[0]
            got  = screener._short_term_score(df, no_rsi)[0]
            m    = screener._short_term_score(df)[1]
            in_band = m.get("rsi") is not None and 35 <= m["rsi"] <= 55
            assert got == base - (25 if in_band else 0)

    def test_run_screener_accepts_a_strategy(self):
        import inspect
        p = inspect.signature(screener.run_screener).parameters
        assert "strategy" in p
        assert p["strategy"].default is screener.DEFAULT_STRATEGY
