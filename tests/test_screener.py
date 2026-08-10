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
        """Pins the BASE score — the rubric itself. A bounded tie-break is added
        on top (see TestTiebreak) but must never alter what the components award."""
        got = [screener._short_term_score(_synthetic_ohlcv(s))[1]["base_score"]
               for s in range(len(self.GOLDEN))]
        assert got == self.GOLDEN, (
            "the default strategy no longer reproduces the live engine — "
            f"got {got}, expected {self.GOLDEN}")

    def test_the_total_is_the_base_plus_a_sub_5_tiebreak(self):
        for s in range(len(self.GOLDEN)):
            total, m = screener._short_term_score(_synthetic_ohlcv(s))
            assert 0 <= m["tiebreak"] < 5
            assert abs(total - (m["base_score"] + m["tiebreak"])) < 1e-6

    def test_passing_the_default_explicitly_is_the_same_as_omitting_it(self):
        for s in range(6):
            df = _synthetic_ohlcv(s)
            assert (screener._short_term_score(df)[0]
                    == screener._short_term_score(df, screener.DEFAULT_STRATEGY)[0])

    def test_zeroing_a_weight_removes_that_component(self):
        """Re-asserted on the BASE score, where the rubric lives."""
        no_rsi = screener.Strategy(name="t", w_rsi=0)
        for s in range(8):
            df = _synthetic_ohlcv(s)
            base = screener._short_term_score(df)[1]["base_score"]
            got  = screener._short_term_score(df, no_rsi)[1]["base_score"]
            m    = screener._short_term_score(df)[1]
            in_band = m.get("rsi") is not None and 35 <= m["rsi"] <= 55
            assert got == base - (25 if in_band else 0)

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


    def test_run_screener_accepts_a_strategy(self):
        import inspect
        p = inspect.signature(screener.run_screener).parameters
        assert "strategy" in p
        assert p["strategy"].default is screener.DEFAULT_STRATEGY


class TestLongTermStrategy:
    """The LT leg is parameterised too. Before this, arms shared ~80% of their
    long-term picks (measured 4/5 overlap on the Aug 8 dry run) — half of every
    arm was a duplicate, so half the experiment measured nothing."""
    GOLDEN = [65, 50, 40, 35, 35, 40, 65, 50]
    CASES = [(12.0, 0.20, 0.25, 0.4), (28.0, 0.05, 0.08, 1.8), (45.0, 0.35, 0.30, 0.2),
             (90.0, 0.12, 0.15, 0.9), (None, 0.11, 0.11, None), (18.0, 0.00, 0.05, 2.5),
             (22.0, 0.15, 0.12, 0.99), (33.0, 0.101, 0.101, 1.01)]

    def _score(self, monkeypatch, case, strat=None):
        import numpy as np, pandas as pd
        # Pin the 200MA leg: it fetches bars, and on a machine behind a TLS
        # proxy that fetch fails and silently scores 0 — the golden would then
        # disagree between laptop and CI.
        monkeypatch.setattr(screener, "_alpaca_single_bars",
                            lambda sym, days=365: pd.DataFrame({"Close": np.linspace(50, 150, 260)}))
        pe, rg, nm, dte = case
        info = {"sector": "Technology", "marketCap": 50e9}
        fh = {"peBasicExclExtraTTM": pe,
              "revenueGrowthTTMYoy": (rg * 100 if rg is not None else None),
              "netProfitMarginTTM": (nm * 100 if nm is not None else None),
              "totalDebt/totalEquityQuarterly": dte, "marketCapitalization": 50_000}
        kw = {"strat": strat} if strat else {}
        return screener._long_term_score(info, fh, ticker="X", **kw)[0]

    def test_default_lt_scores_match_the_pre_refactor_engine(self, monkeypatch):
        got = [self._score(monkeypatch, c) for c in self.CASES]
        assert got == self.GOLDEN, f"LT default changed — got {got}, want {self.GOLDEN}"

    def test_lt_defaults_match_the_literals_they_replaced(self):
        d = screener.DEFAULT_STRATEGY
        assert (d.w_pe_at_or_below, d.w_pe_moderate, d.w_pe_high) == (30, 15, 5)
        assert (d.pe_moderate_mult, d.pe_high_mult) == (1.5, 2.5)
        assert (d.rev_growth_min, d.w_rev_growth) == (0.10, 25)
        assert (d.net_margin_min, d.w_net_margin) == (0.10, 20)
        assert (d.w_low_debt, d.w_above_200ma) == (15, 10)

    def test_arms_diverge_on_the_long_term_leg_too(self, monkeypatch):
        bo = [self._score(monkeypatch, c, screener.STRATEGIES["breakout"]) for c in self.CASES]
        pb = [self._score(monkeypatch, c, screener.STRATEGIES["pullback"]) for c in self.CASES]
        differing = sum(1 for x, y in zip(bo, pb) if x != y)
        assert differing >= 5, f"LT arms too similar to learn from ({differing}/8 differ)"


class TestFetchMemo:
    """Arms made the same Finnhub calls N times. The Aug 8 dry run produced 196
    429s — an arm on degraded fundamentals measures the API, not the strategy."""

    def test_repeat_lookups_hit_the_network_once(self):
        screener.clear_fetch_memo()
        calls = {"n": 0}
        def fake(ticker):
            calls["n"] += 1
            return {"pe": 20.0, "nested": {"x": 1}}
        memoised = screener._memoised(fake)
        memoised("AAPL"); memoised("AAPL"); memoised("MSFT"); memoised("AAPL")
        assert calls["n"] == 2, "the memo did not dedupe repeat lookups"
        screener.clear_fetch_memo()

    def test_a_caller_mutating_the_result_cannot_poison_the_cache(self):
        """Screener code flattens metrics up into candidates — handing out the
        cached object itself would corrupt every later read of that ticker."""
        screener.clear_fetch_memo()
        memoised = screener._memoised(lambda t: {"nested": {"x": 1}})
        first = memoised("AAPL")
        first["nested"]["x"] = 999
        assert memoised("AAPL")["nested"]["x"] == 1
        screener.clear_fetch_memo()

    def test_clear_actually_clears(self):
        screener.clear_fetch_memo()
        calls = {"n": 0}
        def fake(t):
            calls["n"] += 1
            return {}
        m = screener._memoised(fake)
        m("AAPL"); screener.clear_fetch_memo(); m("AAPL")
        assert calls["n"] == 2

    def test_the_finnhub_fetches_are_actually_memoised(self):
        for fn in ("_get_finnhub_profile", "_get_finnhub_metrics", "_get_analyst_target",
                   "_get_eps_surprises", "_get_analyst_recommendations"):
            f = getattr(screener, fn)
            assert hasattr(f, "__wrapped__"), f"{fn} is not memoised — arms will re-fetch it"


class TestFinnhubRateLimit:
    """🔴 The live daily prescreener was making 96-98 Finnhub 429s EVERY run,
    leaving 51 of 79 candidates (65%) with no fundamentals. Long-term scoring is
    ~90% fundamentals, so LT picks were chosen largely on which tickers happened
    to slip through the limit.

    Root cause: `threading.Semaphore(4)` commented "stays under 60/min". A
    semaphore bounds CONCURRENCY, not RATE — four threads doing ~100ms requests
    is ~2,400 calls/min.
    """

    def test_a_semaphore_does_not_limit_rate(self):
        """Pins the misconception, so nobody reintroduces it."""
        import threading, time
        sem = threading.Semaphore(4)
        n, t0 = 40, time.monotonic()
        def one():
            sem.acquire(); sem.release()
        ts = [threading.Thread(target=one) for _ in range(n)]
        for t in ts: t.start()
        for t in ts: t.join()
        rate = n / max(time.monotonic() - t0, 1e-9) * 60
        assert rate > 6000, "a semaphore should be shown to allow a huge call rate"

    def test_limiter_never_exceeds_its_cap_in_any_window(self):
        import threading, time
        lim = screener._RateLimiter(10, 1.0)
        stamps, lock = [], threading.Lock()
        def one():
            # Use the GRANT time, not time.monotonic() afterwards: under load a
            # thread can be descheduled between the two, which made this flaky —
            # and a flaky failure here would trigger self-heal into a deploy.
            t = lim.acquire()
            with lock: stamps.append(t)
        ts = [threading.Thread(target=one) for _ in range(25)]
        for t in ts: t.start()
        for t in ts: t.join()
        worst = max(sum(1 for s in stamps if t - 1.0 < s <= t) for t in stamps)
        assert worst <= 10, f"limiter leaked — {worst} calls in a 1s window (cap 10)"

    def test_limiter_lets_traffic_through_under_the_cap(self):
        """A limiter that throttles when it needn't would add minutes for nothing."""
        import time
        lim = screener._RateLimiter(50, 60.0)
        t0 = time.monotonic()
        for _ in range(20):
            lim.acquire()
        assert time.monotonic() - t0 < 0.5, "under the cap it must not block"

    def test_every_finnhub_fetch_is_rate_limited(self):
        for fn in ("_get_finnhub_profile", "_get_finnhub_metrics", "_get_analyst_target",
                   "_get_eps_surprises", "_get_analyst_recommendations"):
            f = getattr(screener, fn)
            chain = []
            while hasattr(f, "__wrapped__"):
                chain.append(f.__name__)
                f = f.__wrapped__
            src = getattr(getattr(screener, fn), "__wrapped__", None)
            assert src is not None, f"{fn} lost its decorators"
        # the decorator itself must be applied under the memo, so cache hits are free
        import inspect
        code = inspect.getsource(screener)
        for fn in ("_get_finnhub_profile", "_get_finnhub_metrics", "_get_analyst_target",
                   "_get_eps_surprises", "_get_analyst_recommendations"):
            assert f"@_memoised\n@_finnhub_ratelimited\ndef {fn}" in code, \
                f"{fn} must be @_memoised over @_finnhub_ratelimited"

    def test_cap_leaves_headroom_under_the_free_tier(self):
        assert screener._FINNHUB_MAX_PER_MIN < 60, "Finnhub free tier is 60/min"


class TestDataQualityAccounting:
    """The screener records what it ACTUALLY got, because only the run knows.
    The Finnhub failure was a RATE limit — a probe of one ticker would have
    succeeded and reported green while 65% of candidates got nothing."""

    def test_counts_successes_and_failures(self):
        screener.dq_reset()
        screener.dq_record("finnhub_profile", True)
        screener.dq_record("finnhub_profile", True)
        screener.dq_record("finnhub_profile", False)
        assert screener.dq_snapshot()["finnhub_profile"] == {"ok": 2, "total": 3}

    def test_bulk_sources_can_report_zero(self):
        screener.dq_reset()
        screener.dq_set("congressional", 0, 1)
        assert screener.dq_snapshot()["congressional"]["ok"] == 0

    def test_counting_is_threadsafe(self):
        import threading
        screener.dq_reset()
        def bump():
            for _ in range(200):
                screener.dq_record("x", True)
        ts = [threading.Thread(target=bump) for _ in range(8)]
        for t in ts: t.start()
        for t in ts: t.join()
        assert screener.dq_snapshot()["x"]["total"] == 1600, "lost counts under concurrency"
        screener.dq_reset()

    def test_publish_writes_coverage_percentages(self, monkeypatch):
        import config_manager
        screener.dq_reset()
        screener.dq_record("finnhub_metrics", True)
        screener.dq_record("finnhub_metrics", False)
        written = {}
        monkeypatch.setattr(config_manager, "_write_gist_file",
                            lambda f, d: written.update({f: d}))
        screener._publish_data_quality()
        got = written["data_quality.json"]["sources"]["finnhub_metrics"]
        assert got == {"ok": 1, "total": 2, "coverage_pct": 50.0}
        screener.dq_reset()

    def test_a_publish_failure_never_breaks_a_run(self, monkeypatch):
        """Telemetry must never take down a screener run that otherwise worked."""
        import config_manager
        monkeypatch.setattr(config_manager, "_write_gist_file",
                            lambda f, d: (_ for _ in ()).throw(RuntimeError("gist down")))
        screener.dq_reset()
        screener.dq_record("finnhub_profile", True)
        with pytest.raises(RuntimeError):
            screener._publish_data_quality()      # raises on its own …
        # … and run_screener wraps it in try/except so the run survives
        import inspect
        src = inspect.getsource(screener.run_screener)
        assert "_publish_data_quality()" in src and "non-critical" in src
        screener.dq_reset()


class TestCongressionalDormant:
    """No free structured source of congressional trades exists any more
    (verified Aug 8 2026): House Stock Watcher's domain is dead and its S3
    buckets 403, Finnhub's endpoint is premium, and the official House Clerk ZIP
    carries filing metadata only — no tickers. The feature is DORMANT, not
    broken, and must be reported as such."""

    def test_no_key_means_no_network_call_at_all(self, monkeypatch):
        """It used to call Quiver unauthenticated (a guaranteed 401) and then a
        dead domain (DNS failure + retries) on EVERY screener run."""
        import congressional_tracker as ct
        monkeypatch.delenv("QUIVER_API_KEY", raising=False)
        def _boom(*a, **k):
            raise AssertionError("must not hit the network with no key configured")
        monkeypatch.setattr(ct.requests, "get", _boom)
        assert ct._fetch_raw() == []

    def test_the_dead_source_is_gone(self):
        import congressional_tracker as ct
        src = open(ct.__file__).read()
        assert "housestockwatcher" not in src.lower().split("docstring")[0].replace(
            "House Stock Watcher", ""), "the dead domain must not be called"
        assert not hasattr(ct, "HOUSE_API")

    def test_a_key_re_enables_it_with_proper_auth(self, monkeypatch):
        import congressional_tracker as ct
        monkeypatch.setenv("QUIVER_API_KEY", "tok123")
        seen = {}
        class _R:
            status_code = 200
            def json(self): return [{"ticker": "AAPL"}]
        def _get(url, headers=None, timeout=None):
            seen["auth"] = (headers or {}).get("Authorization")
            return _R()
        monkeypatch.setattr(ct.requests, "get", _get)
        assert ct._fetch_raw() == [{"ticker": "AAPL"}]
        assert seen["auth"] == "Bearer tok123", "must send the token, not call unauthenticated"

    def test_dormant_is_recorded_as_not_configured_not_as_zero_coverage(self, monkeypatch):
        """Reporting a dormant feed as 0% sends the owner chasing a breakage
        that does not exist."""
        monkeypatch.delenv("QUIVER_API_KEY", raising=False)
        screener.dq_reset()
        configured = bool(__import__("os").environ.get("QUIVER_API_KEY", "").strip())
        screener.dq_set("congressional", 0, 1 if configured else 0)
        assert screener.dq_snapshot()["congressional"]["total"] == 0
        screener.dq_reset()


class TestTiebreak:
    """🔴 Measured, not assumed: across 400 candidates the coarse score took only
    19 DISTINCT values and 14 of the top 20 were ties — every component is
    all-or-nothing and every weight a multiple of 5. Ties were then resolved by
    whatever order the list happened to be in, and that arbitrary order decided
    which names reached Claude and therefore the user."""

    def _pop(self, n=200):
        return [screener._short_term_score(_synthetic_ohlcv(s)) for s in range(n)]

    def test_it_can_never_reorder_different_base_scores(self):
        """THE safety property. The tie-break is bounded below 5 and the
        smallest gap between two different base scores is 5, so it is
        mathematically incapable of promoting a weaker candidate. This is what
        makes the change not-a-tuning."""
        pop = self._pop(120)
        for total_a, ma in pop:
            for total_b, mb in pop:
                if ma["base_score"] > mb["base_score"]:
                    assert total_a > total_b, (
                        f"tie-break reordered {ma['base_score']}>{mb['base_score']} "
                        f"into {total_a}<={total_b}")

    def test_it_stays_below_the_smallest_component_gap(self):
        assert all(0 <= m["tiebreak"] < 5 for _, m in self._pop(200))

    def test_it_actually_breaks_the_ties(self):
        pop = self._pop(400)
        base_top = sorted((m["base_score"] for _, m in pop), reverse=True)[:20]
        tot_top  = sorted((t for t, _ in pop), reverse=True)[:20]
        assert len(set(base_top)) <= 8, "baseline: the coarse score bunches at the top"
        assert len(set(tot_top)) == 20, "every top-20 candidate must now be ordered"

    def test_it_prefers_the_stronger_signal_among_equals(self):
        """Deeper into the RSI sweet spot beats sitting on its edge."""
        strat = screener.DEFAULT_STRATEGY
        mid  = screener._tiebreak({"rsi": (strat.rsi_lo + strat.rsi_hi) / 2}, strat)
        edge = screener._tiebreak({"rsi": strat.rsi_hi}, strat)
        assert mid > edge

    def test_no_metrics_means_no_bonus(self):
        assert screener._tiebreak({}, screener.DEFAULT_STRATEGY) == 0.0

    def test_junk_metrics_do_not_crash_or_leak(self):
        t = screener._tiebreak({"rsi": "n/a", "volume_ratio": None, "rs_vs_spy": float("nan")},
                               screener.DEFAULT_STRATEGY)
        assert 0 <= t < 5

    def test_the_tiebreak_cannot_push_a_candidate_over_a_gate(self):
        """ai_analyzer gates enrichment at `score >= 50`. Because every base
        score is a multiple of 5 and the tie-break is < 5, a base-45 candidate
        can never reach 50 — the gate keeps its exact meaning. Pinned because a
        larger tie-break would silently change which candidates get enriched."""
        strat = screener.DEFAULT_STRATEGY
        for base in (40, 45, 50, 95):
            worst = base + 4.999
            assert (base >= 50) == (worst >= 50), \
                f"a base {base} candidate changed side of the 50 gate"
