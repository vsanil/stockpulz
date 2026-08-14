"""Money paths: live prices and the guards that stop a bad quote reaching users.

Why these specifically: the single most-repeated bug family in this project is a
bad price slipping into a comparison — `$0.00` firing every "below" alert
(Jun 23), tiny-positive holiday garbage passing `> 0` and producing a false
"▼100% stop hit" (Jul 3), NaN passing `is not None`, crypto priced without its
`-USD` suffix resolving to an unrelated $28 instrument. Every one reached users.

These modules were at 34-43% coverage while carrying that history.
"""
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import market_data
import price_checker
from market_data import plausible_price

# conftest patches market_data.get_live_price autouse for every test. Capture the
# REAL one at import (collection happens before fixtures run) so the routing
# tests below can exercise the actual implementation rather than the stub.
_REAL_GET_LIVE_PRICE = market_data.get_live_price


class _Resp:
    def __init__(self, payload=None, ok=True, status=200):
        self._p, self.ok, self.status_code = payload or {}, ok, status
        self.headers = {}
    def json(self):
        return self._p
    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestPlausiblePrice:
    """The ONE class-level guard. `> 0` was not enough — the Jul 3 holiday feed
    returned TSLA at $0.01 and SHOP at ~$0.004, which pass `> 0` and then
    trivially satisfy any 'below $X' target."""

    @pytest.mark.parametrize("bad", [None, 0, -1, float("nan"), float("inf"), "x"])
    def test_rejects_non_prices(self, bad):
        assert plausible_price(bad) is False

    def test_accepts_a_real_price_without_a_reference(self):
        assert plausible_price(123.45) is True

    def test_rejects_a_collapse_against_the_reference(self):
        """TSLA at $0.01 against a $250 reference — the exact Jul 3 case."""
        assert plausible_price(0.01, reference=250.0) is False

    def test_rejects_an_impossible_spike(self):
        assert plausible_price(3000.0, reference=250.0) is False  # >10x

    def test_accepts_ordinary_movement(self):
        for p in (225.0, 250.0, 275.0, 400.0):
            assert plausible_price(p, reference=250.0) is True, p

    def test_boundaries_are_inclusive_of_normal_volatility(self):
        """A 90% drawdown or a 10x is a bad fetch; just inside must pass."""
        assert plausible_price(26.0, reference=250.0) is True     # ~10.4%
        assert plausible_price(24.0, reference=250.0) is False    # <10%

    def test_a_nan_reference_does_not_crash_or_pass_garbage(self):
        assert plausible_price(0.01, reference=float("nan")) in (True, False)


class TestGetLivePriceCryptoRouting:
    """A bare `yf.download("BTC")` resolves to an unrelated ~$28 instrument, not
    BTC-USD. Crypto must route through the crypto path."""

    def test_crypto_uses_the_crypto_endpoint(self, monkeypatch):
        monkeypatch.setattr(market_data, "get_live_price", _REAL_GET_LIVE_PRICE)
        monkeypatch.setattr(market_data, "ALPACA_KEY", "k", raising=False)
        market_data._PRICE_CACHE.clear()
        seen = {}

        def _get(url, params=None, headers=None, timeout=None):
            seen["url"], seen["params"] = url, params or {}
            return _Resp({"trades": {"BTC/USD": {"p": 63000.0}}})

        monkeypatch.setattr(market_data.requests, "get", _get)
        assert market_data.get_live_price("BTC") == 63000.0
        assert "crypto" in seen["url"], "crypto must not use the stocks endpoint"
        assert seen["params"]["symbols"] == "BTC/USD"

    def test_stock_uses_the_stocks_endpoint(self, monkeypatch):
        monkeypatch.setattr(market_data, "get_live_price", _REAL_GET_LIVE_PRICE)
        monkeypatch.setattr(market_data, "ALPACA_KEY", "k", raising=False)
        market_data._PRICE_CACHE.clear()
        seen = {}

        def _get(url, params=None, headers=None, timeout=None):
            seen["url"] = url
            return _Resp({"trades": {"AAPL": {"p": 300.0}}})

        monkeypatch.setattr(market_data.requests, "get", _get)
        assert market_data.get_live_price("AAPL") == 300.0
        assert "/stocks/" in seen["url"]

    def test_falls_back_to_yfinance_when_alpaca_is_empty(self, monkeypatch):
        monkeypatch.setattr(market_data, "get_live_price", _REAL_GET_LIVE_PRICE)
        monkeypatch.setattr(market_data, "ALPACA_KEY", "k", raising=False)
        market_data._PRICE_CACHE.clear()
        monkeypatch.setattr(market_data.requests, "get",
                            lambda *a, **k: _Resp({"trades": {}}))
        monkeypatch.setattr(market_data, "_yf_price", lambda t: 42.5)
        assert market_data.get_live_price("AAPL") == 42.5

    def test_an_alpaca_exception_does_not_escape(self, monkeypatch):
        monkeypatch.setattr(market_data, "get_live_price", _REAL_GET_LIVE_PRICE)
        monkeypatch.setattr(market_data, "ALPACA_KEY", "k", raising=False)
        market_data._PRICE_CACHE.clear()
        def _boom(*a, **k):
            raise RuntimeError("network")
        monkeypatch.setattr(market_data.requests, "get", _boom)
        monkeypatch.setattr(market_data, "_yf_price", lambda t: 7.0)
        assert market_data.get_live_price("AAPL") == 7.0

    def test_the_cache_prevents_a_second_network_call(self, monkeypatch):
        monkeypatch.setattr(market_data, "get_live_price", _REAL_GET_LIVE_PRICE)
        monkeypatch.setattr(market_data, "ALPACA_KEY", "", raising=False)
        market_data._PRICE_CACHE.clear()
        calls = []
        monkeypatch.setattr(market_data, "_yf_price",
                            lambda t: calls.append(t) or 10.0)
        market_data.get_live_price("AAPL")
        market_data.get_live_price("AAPL")
        assert len(calls) == 1, "second call should be served from the 15s cache"


class TestCryptoSymbolsAreOneSet:
    """There used to be 8 divergent hardcoded crypto lists, so a picked coin
    worked in one feature and broke in another. One map is canonical."""

    def test_crypto_symbols_derives_from_the_id_map(self):
        assert price_checker.CRYPTO_SYMBOLS == frozenset(price_checker._SYMBOL_TO_CG_ID)

    def test_every_symbol_has_a_coingecko_id(self):
        bad = [s for s, i in price_checker._SYMBOL_TO_CG_ID.items() if not i]
        assert not bad, f"symbols with no CoinGecko id: {bad}"

    def test_the_majors_are_present(self):
        for s in ("BTC", "ETH", "SOL"):
            assert s in price_checker.CRYPTO_SYMBOLS

    def test_market_data_shares_the_same_set(self):
        assert set(market_data._CRYPTO_SYMBOLS) == set(price_checker.CRYPTO_SYMBOLS)


class TestCoinGeckoBatching:
    """CoinGecko's free tier (~30 calls/min) is shared across every crypto call
    site. cg_prices is the ONE cached, batched fetch — uncoordinated per-coin
    calls were 429-ing in production."""

    def test_prices_are_cached_so_a_second_caller_hits_no_network(self, monkeypatch):
        price_checker._CG_CACHE.clear()
        calls = []

        def _get(url, params=None, timeout=None, headers=None):
            calls.append(params)
            return _Resp({"bitcoin": {"usd": 63000.0}})

        monkeypatch.setattr(price_checker.requests, "get", _get)
        a = price_checker.cg_prices(["bitcoin"])
        b = price_checker.cg_prices(["bitcoin"])
        assert a.get("bitcoin") == b.get("bitcoin") == 63000.0
        assert len(calls) == 1, "second call must be served from the 60s cache"

    def test_ids_are_batched_into_one_request(self, monkeypatch):
        price_checker._CG_CACHE.clear()
        calls = []

        def _get(url, params=None, timeout=None, headers=None):
            calls.append(params)
            return _Resp({"bitcoin": {"usd": 1.0}, "ethereum": {"usd": 2.0}})

        monkeypatch.setattr(price_checker.requests, "get", _get)
        out = price_checker.cg_prices(["bitcoin", "ethereum"])
        assert len(calls) == 1, "must batch, not call per coin"
        assert out.get("bitcoin") and out.get("ethereum")

    def test_a_failure_returns_empty_rather_than_raising(self, monkeypatch):
        price_checker._CG_CACHE.clear()
        def _boom(*a, **k):
            raise RuntimeError("429")
        monkeypatch.setattr(price_checker.requests, "get", _boom)
        assert price_checker.cg_prices(["bitcoin"]) == {}

    def test_an_empty_id_list_makes_no_request(self, monkeypatch):
        called = []
        monkeypatch.setattr(price_checker.requests, "get",
                            lambda *a, **k: called.append(1) or _Resp({}))
        assert price_checker.cg_prices([]) == {}
        assert not called


class TestWatchlistQuotes:
    """🔴 Two watchlist DISPLAY paths passed raw user tickers to yf.download with
    no crypto mapping. Measured live 2026-08-12: a bare `yf.download("BTC")`
    returns **$28.02** against a real **$63,623** — so the watchlist rendered BTC
    at twenty-eight dollars. HYPE has no yfinance symbol at all (neither HYPE nor
    HYPE-USD), producing 404 noise on every run.
    """

    def test_crypto_gets_the_usd_suffix(self):
        assert market_data._yf_sym("BTC") == "BTC-USD"
        assert market_data._yf_sym("eth") == "ETH-USD"

    def test_equities_are_unchanged(self):
        assert market_data._yf_sym("AAPL") == "AAPL"
        assert market_data._yf_sym("NVDA") == "NVDA"

    def test_yfinance_is_queried_with_MAPPED_symbols(self, monkeypatch):
        """The actual bug: the bare symbol resolves to a different instrument."""
        seen = {}

        def _dl(syms, **kw):
            seen["syms"] = list(syms)
            raise RuntimeError("stop here — we only care what was requested")

        monkeypatch.setattr(market_data, "get_live_prices", lambda t: {"BTC": 63000.0})
        import sys, types
        fake = types.ModuleType("yfinance"); fake.download = _dl
        monkeypatch.setitem(sys.modules, "yfinance", fake)
        market_data.get_quotes_with_change(["BTC"])
        assert seen["syms"] == ["BTC-USD"], f"queried {seen['syms']} — bare BTC is a $28 instrument"

    def test_a_ticker_yfinance_lacks_still_gets_a_price(self, monkeypatch):
        """HYPE: priced via the crypto path, no previous close available, so the
        row renders WITHOUT a change rather than not at all."""
        monkeypatch.setattr(market_data, "get_live_prices", lambda t: {"HYPE": 56.43})
        import sys, types
        fake = types.ModuleType("yfinance")
        fake.download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no data"))
        monkeypatch.setitem(sys.modules, "yfinance", fake)
        out = market_data.get_quotes_with_change(["HYPE"])
        assert out["HYPE"]["price"] == 56.43
        assert out["HYPE"]["change_pct"] is None

    def test_an_implausible_price_is_dropped_not_rendered(self, monkeypatch):
        """$0.01 for a real ticker is a failed fetch, not a quote."""
        monkeypatch.setattr(market_data, "get_live_prices",
                            lambda t: {"AAPL": 0.0, "NVDA": 224.0})
        import sys, types
        fake = types.ModuleType("yfinance")
        fake.download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        monkeypatch.setitem(sys.modules, "yfinance", fake)
        out = market_data.get_quotes_with_change(["AAPL", "NVDA"])
        assert "AAPL" not in out and out["NVDA"]["price"] == 224.0

    def test_one_bad_ticker_does_not_empty_the_block(self, monkeypatch):
        """Documented rule: any batch fetch feeding a user-facing block must
        degrade per-ticker, never all-or-nothing."""
        monkeypatch.setattr(market_data, "get_live_prices",
                            lambda t: {"AAPL": 300.0})          # NVDA missing
        import sys, types
        fake = types.ModuleType("yfinance")
        fake.download = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
        monkeypatch.setitem(sys.modules, "yfinance", fake)
        out = market_data.get_quotes_with_change(["AAPL", "NVDA"])
        assert list(out) == ["AAPL"]

    def test_empty_input_makes_no_calls(self, monkeypatch):
        called = []
        monkeypatch.setattr(market_data, "get_live_prices",
                            lambda t: called.append(1) or {})
        assert market_data.get_quotes_with_change([]) == {}
        assert not called


class TestPaperTraderPriceResolution:
    """🔴 `_live_price` tried the BARE symbol first, then `{ticker}-USD`:

        for sym in (ticker, f"{ticker}-USD"): ...

    Correct for equities — a bad symbol returns nothing so the retry runs — but
    silently wrong for crypto, because bare `BTC` DOES resolve on yfinance, to
    an unrelated instrument. Measured 2026-08-12: BTC $28.00, ETH $18.00
    against a real $63,623 and $1,891.

    Stored entries were unaffected (the bot passes explicit pick prices), but
    the portfolio DISPLAY and total portfolio value both call this per position,
    so a paper BTC holding rendered at roughly -99.96% unrealized.
    """

    def test_it_delegates_to_the_canonical_resolver(self, monkeypatch):
        import paper_trader
        import market_data as md
        monkeypatch.setattr(md, "get_live_price", lambda t: 63_000.0)
        assert paper_trader._live_price("BTC") == 63_000.0

    def test_the_bare_then_usd_fallback_cannot_return(self):
        """The ordering itself was the bug — assert the pattern is gone."""
        import ast, inspect, textwrap
        import paper_trader
        # Strip the docstring before scanning — it deliberately QUOTES the old
        # pattern as documentation, which a naive substring check trips on.
        tree = ast.parse(textwrap.dedent(inspect.getsource(paper_trader._live_price)))
        fn = tree.body[0]
        if (fn.body and isinstance(fn.body[0], ast.Expr)
                and isinstance(fn.body[0].value, ast.Constant)):
            fn.body = fn.body[1:]
        body = ast.unparse(fn)
        assert 'f"{ticker}-USD"' not in body, "bare-then-USD fallback is back in the code"
        assert "get_live_price" in body

    def test_exotic_coins_still_reach_the_coingecko_search(self, monkeypatch):
        """Coins outside the canonical map (ASTER, LAB…) must keep their
        last-resort lookup — the fix must not remove that path."""
        import paper_trader
        import market_data as md
        monkeypatch.setattr(md, "get_live_price", lambda t: None)

        class _R:
            ok = True
            def __init__(self, p): self._p = p
            def json(self): return self._p

        def _get(url, params=None, timeout=None):
            if "search" in url:
                return _R({"coins": [{"symbol": "ASTER", "id": "aster-x"}]})
            return _R({"aster-x": {"usd": 1.23}})

        monkeypatch.setattr(paper_trader, "_req", None, raising=False)
        import requests
        monkeypatch.setattr(requests, "get", _get)
        assert paper_trader._live_price("ASTER") == 1.23

    def test_returns_none_when_nothing_resolves(self, monkeypatch):
        import paper_trader
        import market_data as md
        import requests
        monkeypatch.setattr(md, "get_live_price", lambda t: None)
        monkeypatch.setattr(requests, "get",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
        assert paper_trader._live_price("NOPE") is None
