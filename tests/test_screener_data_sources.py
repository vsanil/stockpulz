"""Screener external data sources — the inputs that fail SILENTLY.

All three bugs guarded here ran in production behind green monitors, because
each had a fallback or a swallowed None. The screener kept producing picks; it
just produced them from less information than anyone believed.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd
import pytest

import screener


class _Resp:
    def __init__(self, payload=None, status=200, text=""):
        self._p, self.status_code, self.text = payload, status, text
    def json(self):  return self._p
    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class TestAlpacaSymbolTranslation:
    """Alpaca spells class shares BRK.B; everything else here uses BRK-B. Alpaca
    400s the WHOLE multi-symbol request on one unknown symbol, so these two
    tickers discarded all 100 bars in their chunk on every run."""

    def test_hyphen_class_shares_become_dots(self):
        assert screener._alpaca_sym("BRK-B") == "BRK.B"
        assert screener._alpaca_sym("BF-B") == "BF.B"
        assert screener._alpaca_sym("AAPL") == "AAPL"

    def test_the_inverse_map_restores_our_spelling(self):
        """Not optional: responses come back keyed by Alpaca's spelling and every
        caller looks up ours, so without this the bars are silently dropped by
        `if ticker not in _raw: continue`."""
        syms, back = screener._alpaca_syms(["AAPL", "BRK-B"])
        assert syms == ["AAPL", "BRK.B"]
        assert back["BRK.B"] == "BRK-B"

    def test_bulk_bars_returns_our_spelling(self, monkeypatch):
        monkeypatch.setenv("ALPACA_KEY_ID", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        bar = {"t": "2026-08-10T00:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 10}
        sent = {}

        def _get(url, headers=None, params=None, timeout=None):
            sent["symbols"] = params["symbols"]
            return _Resp({"bars": {"BRK.B": [bar, bar]}, "next_page_token": None})

        monkeypatch.setattr(screener.requests, "get", _get)
        out = screener._alpaca_bulk_bars(["BRK-B"])
        assert "BRK.B" in sent["symbols"], "must SEND Alpaca's spelling"
        assert "BRK-B" in out, "must RETURN ours"
        assert "BRK.B" not in out


class TestAlpacaSingleBarsUsesTheWorkingEndpoint:
    """🔴 /v2/stocks/bars/{symbol} 404s for EVERY ticker, AAPL included, so this
    function returned None on every call it ever made. Both callers swallow None,
    so the SPY relative-strength baseline and the 200MA leg of the long-term
    score silently contributed nothing."""

    def test_it_does_not_use_the_path_style_endpoint(self, monkeypatch):
        monkeypatch.setenv("ALPACA_KEY_ID", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        seen = {}

        def _get(url, headers=None, params=None, timeout=None):
            seen["url"], seen["params"] = url, params or {}
            return _Resp({"bars": {}})

        monkeypatch.setattr(screener.requests, "get", _get)
        screener._alpaca_single_bars("SPY", days=65)
        assert seen["url"].rstrip("/").endswith("/v2/stocks/bars"), \
            "path-style /v2/stocks/bars/{symbol} 404s for every symbol"
        assert seen["params"].get("symbols") == "SPY"

    def test_it_reads_the_symbol_keyed_payload(self, monkeypatch):
        monkeypatch.setenv("ALPACA_KEY_ID", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        bars = [{"t": f"2026-08-{d:02d}T00:00:00Z", "o": 1, "h": 2, "l": 1,
                 "c": 2, "v": 10} for d in range(1, 6)]
        monkeypatch.setattr(screener.requests, "get",
                            lambda *a, **k: _Resp({"bars": {"SPY": bars}}))
        df = screener._alpaca_single_bars("SPY", days=65)
        assert df is not None and len(df) == 5
        assert list(df.columns) == ["Open", "High", "Low", "Close", "Volume"]

    def test_class_shares_work_here_too(self, monkeypatch):
        monkeypatch.setenv("ALPACA_KEY_ID", "k")
        monkeypatch.setenv("ALPACA_SECRET_KEY", "s")
        seen = {}

        def _get(url, headers=None, params=None, timeout=None):
            seen["symbols"] = (params or {}).get("symbols")
            return _Resp({"bars": {"BRK.B": [
                {"t": "2026-08-10T00:00:00Z", "o": 1, "h": 2, "l": 1, "c": 2, "v": 1}]}})

        monkeypatch.setattr(screener.requests, "get", _get)
        assert screener._alpaca_single_bars("BRK-B") is not None
        assert seen["symbols"] == "BRK.B"

    def test_missing_credentials_return_none_quietly(self, monkeypatch):
        monkeypatch.delenv("ALPACA_KEY_ID", raising=False)
        monkeypatch.delenv("ALPACA_SECRET_KEY", raising=False)
        assert screener._alpaca_single_bars("SPY") is None


_FAKE_SYMS = ['AAA', 'AAB', 'AAC', 'AAD', 'AAE', 'AAF', 'AAG', 'AAH', 'AAI', 'AAJ', 'AAK', 'AAL', 'AAM', 'AAN', 'AAO', 'AAP', 'AAQ', 'AAR', 'AAS', 'AAT', 'AAU', 'AAV', 'AAW', 'AAX', 'AAY', 'AAZ', 'ABA', 'ABB', 'ABC', 'ABD', 'ABE', 'ABF', 'ABG', 'ABH', 'ABI', 'ABJ', 'ABK', 'ABL', 'ABM', 'ABN', 'ABO', 'ABP', 'ABQ', 'ABR', 'ABS', 'ABT', 'ABU', 'ABV', 'ABW', 'ABX', 'ABY', 'ABZ', 'ACA', 'ACB', 'ACC', 'ACD', 'ACE', 'ACF', 'ACG', 'ACH']

_NDX_HTML = """
<table><tr><th>Year</th><th>Return</th></tr>
""" + "".join(f"<tr><td>{y}</td><td>1</td></tr>" for y in range(1990, 2026)) + """
</table>
<table><tr><th>No.</th><th>Symbol</th><th>Company Name</th></tr>
""" + "".join(f"<tr><td>{i}</td><td>{t}</td><td>Co {i}</td></tr>"
              for i, t in enumerate(_FAKE_SYMS)) + """
</table>
"""


class TestNasdaq100Source:
    """Wikipedia dropped the constituents table from the Nasdaq-100 article — 18
    tables remain, the largest 41 rows of annual returns — so the old scrape
    returned 0 and 'added 0 new tickers' read as benign. ARM, ASML, MELI, MSTR,
    PDD, SHOP and others were never screened."""

    def test_it_picks_the_table_by_column_content_not_index(self, monkeypatch):
        monkeypatch.setattr(screener.requests, "get",
                            lambda *a, **k: _Resp(status=200, text=_NDX_HTML))
        out = screener._nasdaq_100_symbols()
        assert len(out) == 60
        assert _FAKE_SYMS[0] in out
        assert not any(str(y) in out for y in range(1990, 2026)), \
            "the annual-returns table must not be mistaken for constituents"

    def test_a_dead_source_returns_empty_rather_than_raising(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("dns")
        monkeypatch.setattr(screener.requests, "get", _boom)
        assert screener._nasdaq_100_symbols() == set()

    def test_an_http_error_returns_empty(self, monkeypatch):
        monkeypatch.setattr(screener.requests, "get",
                            lambda *a, **k: _Resp(status=503, text=""))
        assert screener._nasdaq_100_symbols() == set()

    def test_a_page_without_a_constituents_table_yields_nothing(self, monkeypatch):
        """Exactly the live Wikipedia failure — no table over 50 rows carries a
        symbol column."""
        html = "<table><tr><th>Year</th><th>Close</th></tr><tr><td>2025</td><td>1</td></tr></table>"
        monkeypatch.setattr(screener.requests, "get",
                            lambda *a, **k: _Resp(status=200, text=html))
        assert screener._nasdaq_100_symbols() == set()

    def test_it_sends_a_browser_user_agent(self, monkeypatch):
        seen = {}

        def _get(url, headers=None, timeout=None):
            seen["h"] = headers or {}
            return _Resp(status=200, text=_NDX_HTML)

        monkeypatch.setattr(screener.requests, "get", _get)
        screener._nasdaq_100_symbols()
        assert "User-Agent" in seen["h"], "most sites 403 the default UA"
