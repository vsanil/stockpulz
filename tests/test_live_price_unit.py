"""
tests/test_live_price_unit.py — Unit tests for paper_trader._live_price.

Kept separate from test_paper_trader.py because that file has an autouse
fixture that replaces _live_price globally, which would prevent us from
testing the real implementation.
"""
from __future__ import annotations

import os
import sys
import pytest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Minimal env so imports don't explode
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "fake")
os.environ.setdefault("GIST_ID", "fake")
os.environ.setdefault("GH_GIST_TOKEN", "fake")
os.environ.setdefault("FLASK_SECRET_KEY", "fake")


def _cg_mock(search_result, price_result):
    """Return a requests.get side_effect that serves CoinGecko mocks."""
    mock_search = MagicMock()
    mock_search.ok = True
    mock_search.json.return_value = {"coins": search_result}

    mock_price = MagicMock()
    mock_price.ok = True
    mock_price.json.return_value = price_result

    def _get(url, **kwargs):
        if "search" in url:
            return mock_search
        if "simple/price" in url:
            return mock_price
        m = MagicMock(); m.ok = False; return m

    return _get


class TestLivePriceUnit:

    def test_yfinance_stock_returns_price(self):
        """yfinance path works for regular stocks."""
        from paper_trader import _live_price

        mock_fi = MagicMock()
        mock_fi.fast_info.last_price = 189.50

        with patch("yfinance.Ticker", return_value=mock_fi):
            price = _live_price("AAPL")

        assert price == 189.50

    def test_coingecko_fallback_for_exotic_crypto(self):
        """CoinGecko fallback fires when yfinance has no data."""
        from paper_trader import _live_price

        with patch("yfinance.Ticker", side_effect=Exception("no data")), \
             patch("requests.get", side_effect=_cg_mock(
                 [{"id": "hyperliquid", "symbol": "HYPE"}],
                 {"hyperliquid": {"usd": 72.5}}
             )):
            price = _live_price("HYPE")

        assert price == 72.5

    def test_coingecko_fallback_unknown_symbol_returns_none(self):
        """Returns None when neither yfinance nor CoinGecko finds the ticker."""
        from paper_trader import _live_price

        mock_empty = MagicMock()
        mock_empty.ok = True
        mock_empty.json.return_value = {"coins": []}  # no match

        with patch("yfinance.Ticker", side_effect=Exception("no data")), \
             patch("requests.get", return_value=mock_empty):
            price = _live_price("FAKEXYZ123")

        assert price is None

    def test_known_crypto_resolves_through_the_canonical_resolver(self):
        """🔴 This test used to assert `"BNB" in call_args or "BNB-USD" in
        call_args` — an `or` that passes whether the code queries the CORRECT
        symbol or the wrong one, so it could never have caught the bug it looked
        like it was guarding. Bare `BNB` resolves on yfinance to an unrelated
        instrument; measured 2026-08-12, bare BTC returned $28 against $63,623.

        _live_price now delegates to market_data.get_live_price, which routes
        crypto to the crypto endpoint rather than guessing at suffixes.
        """
        import market_data as md
        from paper_trader import _live_price

        with patch.object(md, "get_live_price", return_value=738.0) as m:
            price = _live_price("BNB")

        assert price == 738.0
        m.assert_called_once_with("BNB")

    def test_the_canonical_resolver_maps_crypto_for_yfinance(self):
        """And the resolver itself must never hand yfinance a bare crypto
        symbol — that is where the $28 came from."""
        import market_data as md
        assert md._yf_sym("BNB") == "BNB-USD"
        assert md._yf_sym("AAPL") == "AAPL"
