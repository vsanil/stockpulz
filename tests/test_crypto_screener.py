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


class TestRsiDivergence:
    """🔴 OPEN DECISION, not a pinned behaviour.

    crypto_screener._simple_rsi divides the gain sum by the COUNT OF GAINS:

        avg_gain = statistics.mean(gains)          # len(gains)

    Every other RSI in this repo divides by the PERIOD, which is Wilder's
    standard definition:

        agent.py:3508        sum(gains[-period:]) / period
        agent.py:1114        delta.clip(lower=0).rolling(14).mean()
        ai_analyzer.py:463   delta.clip(lower=0).rolling(14).mean()

    On a downtrend containing one spike the two forms give 90.91 and 43.48. The
    value feeds `if rsi and 42 <= rsi <= 62` in _short_term_score, so the
    divergence changes which coins are picked.

    This test asserts the divergence EXISTS rather than that either value is
    right. Fixing it changes crypto pick output for real users, so it is the
    owner's call — and it must not be silently "cleaned up" by a future
    refactor without that decision being made.
    """

    @staticmethod
    def _wilder(prices, period=14):
        d = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
        g, l = [max(0.0, x) for x in d][-period:], [max(0.0, -x) for x in d][-period:]
        ag, al = sum(g) / period, sum(l) / period
        return 100.0 if al == 0 else round(100 - 100 / (1 + ag / al), 2)

    def test_the_divergence_is_real_and_large(self):
        spiky = [100, 110] + [109 - i for i in range(13)]
        ours, standard = cs._simple_rsi(spiky), self._wilder(spiky)
        assert abs(ours - standard) > 15, (
            "the two RSI forms have converged — if _simple_rsi was corrected to "
            "the standard divisor, delete this test and record the decision; "
            "crypto pick output changes with it"
        )

    def test_they_agree_on_monotonic_series(self):
        """Scope the claim honestly: the forms only diverge on mixed series."""
        up = [100 + i for i in range(20)]
        assert cs._simple_rsi(up) == self._wilder(up) == 100.0
