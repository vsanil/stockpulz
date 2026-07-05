"""
test_market_data.py — market_data helpers.

Covers the get_ohlcv TTL cache: repeated chart opens for the same (ticker, days)
must not re-hit the price API within the TTL.

conftest's autouse _mock_prices fixture replaces market_data.get_ohlcv with a
fake, so we capture the REAL function at import time (before any fixture runs)
and restore it for these tests.
"""

import market_data

_REAL_GET_OHLCV = market_data.get_ohlcv   # captured before autouse fixtures patch it


class TestOhlcvCache:
    def _use_real(self, monkeypatch):
        monkeypatch.setattr(market_data, "get_ohlcv", _REAL_GET_OHLCV)
        monkeypatch.setattr(market_data, "POLYGON_KEY", "")   # force the yf fallback path
        market_data._OHLCV_CACHE.clear()

    def test_repeated_call_served_from_cache(self, monkeypatch):
        self._use_real(monkeypatch)
        calls = {"n": 0}

        def _fake_yf(ticker, days):
            calls["n"] += 1
            return [{"time": "2026-06-01", "open": 1, "high": 1,
                     "low": 1, "close": 1, "volume": 1}]
        monkeypatch.setattr(market_data, "_yf_ohlcv", _fake_yf)

        a = market_data.get_ohlcv("TESTX", 30)
        b = market_data.get_ohlcv("TESTX", 30)
        assert a == b
        assert calls["n"] == 1, "second call within TTL should hit the cache"

        # A different period is a different cache key → another fetch.
        market_data.get_ohlcv("TESTX", 92)
        assert calls["n"] == 2

    def test_expired_entry_refetches(self, monkeypatch):
        self._use_real(monkeypatch)
        monkeypatch.setattr(market_data, "_OHLCV_TTL", 0)   # everything is immediately stale
        calls = {"n": 0}

        def _fake_yf(ticker, days):
            calls["n"] += 1
            return [{"time": "2026-06-01", "open": 1, "high": 1,
                     "low": 1, "close": 1, "volume": 1}]
        monkeypatch.setattr(market_data, "_yf_ohlcv", _fake_yf)

        market_data.get_ohlcv("TESTX", 30)
        market_data.get_ohlcv("TESTX", 30)
        assert calls["n"] == 2, "with TTL 0 each call should re-fetch"

    def test_none_result_not_cached(self, monkeypatch):
        self._use_real(monkeypatch)
        monkeypatch.setattr(market_data, "_yf_ohlcv", lambda t, d: None)
        assert market_data.get_ohlcv("NODATA", 30) is None
        assert ("NODATA", 30) not in market_data._OHLCV_CACHE


class TestPlausiblePrice:
    """market_data.plausible_price — the class-level guard against a feed that
    returns tiny garbage ($0.01) instead of 0/None on a market holiday."""

    def test_garbage_vs_reference_rejected(self):
        from market_data import plausible_price
        assert plausible_price(0.01, 354.05) is False     # TSLA holiday garbage
        assert plausible_price(0.004, 3.07) is False      # UNI
        assert plausible_price(4000.0, 354.0) is False    # 11x spike

    def test_real_prices_accepted(self):
        from market_data import plausible_price
        assert plausible_price(354.0, 360.0) is True
        assert plausible_price(570.31, 651.0) is True     # BNB real −12.4% stop
        assert plausible_price(50.0, 100.0) is True       # legit −50% still ok

    def test_nonpositive_and_nan_rejected(self):
        from market_data import plausible_price
        assert plausible_price(0.0, 100.0) is False
        assert plausible_price(-5.0, 100.0) is False
        assert plausible_price(float("nan"), 100.0) is False
        assert plausible_price(None, 100.0) is False

    def test_no_reference_only_requires_positive(self):
        from market_data import plausible_price
        assert plausible_price(5.0, None) is True
        assert plausible_price(0.0, None) is False
        assert plausible_price(0.01, 0) is True           # ref<=0 → positivity only
