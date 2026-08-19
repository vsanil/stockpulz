"""crypto_screener — the pick-generation path for crypto.

12% covered. This is the same class of risk as the commodities screener, which
was structurally dead for 10+ days producing ZERO candidates while every monitor
stayed green: a starved section is indistinguishable from a quiet market unless
the code says which.

NOTE: _simple_rsi's divisor divergence is deliberately NOT pinned here — see
TestRsiDivergence, which documents it as an open decision rather than blessing
current behaviour as correct.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import crypto_screener as cs


class _Resp:
    def __init__(self, payload, status=200, headers=None):
        self._p, self.status_code, self.headers = payload, status, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._p


@pytest.fixture(autouse=True)
def slept(monkeypatch):
    """Record sleeps instead of taking them — the durations are the only way to
    tell the 429 branch apart from the generic retry."""
    seen = []
    monkeypatch.setattr(cs.time, "sleep", lambda s: seen.append(s))
    return seen


class TestSimpleMa:
    def test_averages_the_last_period_only(self):
        assert cs._simple_ma([1, 2, 3, 100, 200], period=2) == 150.0

    def test_too_little_history_is_None_not_a_partial_average(self):
        """A partial average silently understates a moving average and would
        score the coin on a window that does not exist."""
        assert cs._simple_ma([1, 2], period=5) is None

    def test_empty_input_is_None(self):
        assert cs._simple_ma([], period=5) is None


class TestSimpleRsiEdges:
    """Edges that hold regardless of which divisor is used."""

    def test_too_little_history_returns_None(self):
        assert cs._simple_rsi([1, 2, 3], period=14) is None
        assert cs._simple_rsi([], period=14) is None

    def test_a_monotonic_uptrend_pins_at_100(self):
        assert cs._simple_rsi([100 + i for i in range(20)]) == 100.0

    def test_a_monotonic_downtrend_pins_at_0(self):
        assert cs._simple_rsi([100 - i for i in range(20)]) == 0.0

    def test_a_flat_series_never_divides_by_zero(self):
        assert cs._simple_rsi([100.0] * 20) is not None


class TestBulkFetch:
    def test_a_429_waits_the_Retry_After_the_server_asked_for(self, monkeypatch, slept):
        """Asserting only "it retried" cannot distinguish the 429 branch from
        the generic exception retry — deleting the branch still passes, because
        raise_for_status then throws and the generic path covers it. The
        honoured Retry-After is what pins the branch."""
        calls = []

        def get(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                return _Resp(None, 429, {"Retry-After": "17"})
            return _Resp([{"id": "bitcoin", "market_cap": 9e11}])
        monkeypatch.setattr(cs.requests, "get", get)
        assert cs._get_top_coins()[0]["id"] == "bitcoin"
        assert len(calls) == 2, "the 429 was not retried"
        assert 17 in slept, \
            "the server's Retry-After was ignored — hammering a rate limiter"

    def test_an_empty_response_is_retried_not_accepted_as_no_coins(self, monkeypatch):
        """🔴 The commodities lesson: an empty result must not be mistaken for
        'the market is quiet'. An empty bulk call is a FAILURE."""
        calls = []

        def get(*a, **k):
            calls.append(1)
            return _Resp([]) if len(calls) < 3 else _Resp([{"id": "eth"}])
        monkeypatch.setattr(cs.requests, "get", get)
        assert cs._get_top_coins() == [{"id": "eth"}]
        assert len(calls) == 3

    def test_persistent_failure_RAISES_rather_than_returning_empty(self, monkeypatch):
        """Loud and early beats a silent empty list that reads as 'no setups'."""
        monkeypatch.setattr(cs.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
        with pytest.raises(Exception):
            cs._get_top_coins()

    def test_sparkline_is_never_requested(self, monkeypatch):
        """It is a paid CoinGecko feature and requesting it triggers 429s on the
        free tier — the documented reason phase 2 exists."""
        seen = {}
        monkeypatch.setattr(cs.requests, "get",
                            lambda url, **k: (seen.update(k.get("params", {})),
                                              _Resp([{"id": "btc"}]))[1])
        cs._get_top_coins()
        assert seen.get("sparkline") is False


class TestPriceHistory:
    def test_prices_are_unwrapped_from_timestamp_pairs(self, monkeypatch):
        monkeypatch.setattr(cs.requests, "get", lambda *a, **k: _Resp(
            {"prices": [[1000, 10.0], [2000, 11.0]]}))
        assert cs._get_price_history("bitcoin") == [10.0, 11.0]

    def test_a_failure_returns_empty_and_does_not_raise(self, monkeypatch):
        """One bad coin must not abort the whole screen — degrade per item."""
        monkeypatch.setattr(cs.requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert cs._get_price_history("nosuchcoin") == []

    def test_a_429_is_retried_once(self, monkeypatch):
        calls = []

        def get(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                return _Resp(None, 429, {"Retry-After": "1"})
            return _Resp({"prices": [[1, 5.0]]})
        monkeypatch.setattr(cs.requests, "get", get)
        assert cs._get_price_history("btc") == [5.0]
        assert len(calls) == 2


class TestRsiMatchesTheStandard:
    """The divisor was corrected on 2026-08-19 (owner's call), replacing
    TestRsiDivergence which had documented it as an open question.

    It previously divided the gain sum by the COUNT OF GAINS; every other RSI in
    this repo divides by the PERIOD (Wilder's standard) — agent.py:3508,
    agent.py:1114, ai_analyzer.py:463. On a downtrend containing one spike the
    two forms gave 90.91 and 43.48, and the value feeds the 42-62 band in
    _short_term_score, so it changed which coins were picked.

    🔴 This is an ENGINE CHANGE: crypto numbers before and after are not
    directly comparable. It is dated in evaluate_picks.ENGINE_CHANGES so the
    report says so rather than averaging across it.
    """

    @staticmethod
    def _wilder(prices, period=14):
        d = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        g, l = [max(0.0, x) for x in d][-period:], [max(0.0, -x) for x in d][-period:]
        ag, al = sum(g) / period, sum(l) / period
        return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)

    @pytest.mark.parametrize("prices", [
        pytest.param([100, 110] + [109 - i for i in range(13)], id="spiky-downtrend"),
        pytest.param([100 + (3 if i % 2 else -3) for i in range(20)], id="choppy"),
        pytest.param([100 + i * (1 if i % 3 else -2) for i in range(25)], id="mixed"),
        pytest.param([100 + i for i in range(20)], id="uptrend"),
        pytest.param([100 - i for i in range(20)], id="downtrend"),
    ])
    def test_it_now_agrees_with_the_standard_definition(self, prices):
        assert cs._simple_rsi(prices) == pytest.approx(self._wilder(prices), abs=0.01)

    def test_the_specific_case_that_prompted_the_change(self):
        spiky = [100, 110] + [109 - i for i in range(13)]
        assert cs._simple_rsi(spiky) == pytest.approx(43.48, abs=0.01), \
            "the old count-of-gains divisor scored this 90.91 — strongly overbought"

    def test_a_period_with_no_gains_does_not_divide_by_zero(self):
        assert cs._simple_rsi([100 - i for i in range(20)]) == 0.0

    def test_the_change_is_dated_in_the_evaluator(self):
        """A change to pick logic resets what the numbers mean; the report has
        to say so rather than averaging two engines together."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ev", os.path.join(ROOT, "scripts", "evaluate_picks.py"))
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        assert any(d == "2026-08-19" and scope == "crypto"
                   for d, scope, _ in ev.ENGINE_CHANGES), \
            "the RSI correction is not logged as an engine change"
