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

    def test_yfinance_usd_suffix_for_known_crypto(self):
        """Known crypto like BNB tries BNB-USD via yfinance."""
        from paper_trader import _live_price

        call_args = []
        mock_fi = MagicMock()
        mock_fi.fast_info.last_price = 738.0

        def _ticker(sym):
            call_args.append(sym)
            return mock_fi

        with patch("yfinance.Ticker", side_effect=_ticker):
            price = _live_price("BNB")

        assert price == 738.0
        assert "BNB" in call_args or "BNB-USD" in call_args
