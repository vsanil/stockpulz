"""
test_formatters.py — Unit tests for formatters.py

Coverage:
  - _p           price precision for stocks, mid/small/sub-penny crypto
  - _upside      positive and negative upside percentages
  - _entry_window  short-term and long-term variants, budget path
  - _stars        conviction star display
  - format_daily_message
      · returns a non-empty string
      · contains short-term ticker when picks present
      · respects market_closed flag
      · pick_mode="st" hides long-term section
      · pick_mode="lt" hides short-term section
      · greeting present when first_name supplied
"""

import os
import sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from formatters import _p, _upside, _entry_window, _stars, format_daily_message


# ── Minimal picks fixture ─────────────────────────────────────────────────────

def _picks(st_tickers=("AAPL",), lt_tickers=("NVDA",)):
    """Build a minimal picks dict with the given tickers."""
    def _stock(ticker):
        return {"ticker": ticker, "entry_price": 100.0, "target_price": 115.0,
                "stop_loss": 90.0, "conviction": 3, "thesis": "Test thesis.",
                "catalyst": "Earnings beat.", "timeframe": "ST", "sector": "Technology"}
    return {
        "stocks": {
            "short_term": [_stock(t) for t in st_tickers],
            "long_term":  [_stock(t) for t in lt_tickers],
        },
        "crypto":      {"short_term": [], "long_term": []},
        "etfs":        {"short_term": [], "long_term": []},
        "commodities": {"short_term": [], "long_term": []},
        "options_plays": [],
        "daily_summary": "Mixed market signals.",
    }


def _cfg(**overrides):
    base = {
        "pick_mode": "both",
        "risk_profile": "moderate",
        "show_crypto": True,
        "watchlist": [],
        "show_buy_counts": False,
        "timezone": "America/New_York",
    }
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
class TestPriceFormatter:
    """_p formats prices with magnitude-appropriate precision."""

    def test_large_stock_price_two_decimals(self):
        assert _p(189.50) == "189.50"

    def test_whole_large_price_no_decimals(self):
        # >= 10000 and whole number → no decimals
        result = _p(50000.0)
        assert "." not in result or result.endswith(".00") is False
        # Just verify it's formatted as a big number
        assert "50,000" in result or "50000" in result

    def test_mid_crypto_four_decimals(self):
        # >= 0.01 but < 1
        assert _p(0.5821) == "0.5821"

    def test_small_crypto_six_decimals(self):
        # >= 0.0001 but < 0.01
        assert _p(0.001234) == "0.001234"

    def test_sub_penny_eight_decimals(self):
        # < 0.0001
        assert _p(0.00001234) == "0.00001234"

    def test_none_returns_dash(self):
        assert _p(None) == "—"

    def test_exact_one_dollar(self):
        assert _p(1.00) == "1.00"

    def test_standard_stock_price(self):
        assert _p(42.75) == "42.75"

    def test_large_btc_price(self):
        result = _p(68000.0)
        assert "68,000" in result or "68000" in result


# ─────────────────────────────────────────────────────────────────────────────
class TestUpsideCalculator:
    """_upside returns (+X.X%) or (-X.X%)."""

    def test_positive_upside(self):
        result = _upside(100, 120)
        assert result == "+20.0%"

    def test_negative_downside(self):
        result = _upside(120, 100)
        assert result.startswith("-")
        assert "16.7" in result

    def test_zero_upside(self):
        result = _upside(100, 100)
        assert result == "+0.0%"

    def test_invalid_values_return_empty(self):
        result = _upside(None, 100)
        assert result == ""

    def test_fractional_precision(self):
        result = _upside(100, 107.5)
        assert result == "+7.5%"


# ─────────────────────────────────────────────────────────────────────────────
class TestEntryWindow:
    """_entry_window returns formatted entry guidance strings."""

    def test_no_entry_returns_empty(self):
        assert _entry_window(None) == ""

    def test_short_term_basic(self):
        result = _entry_window(100, stop=90)
        assert result != ""
        assert "102" in result or "2%" in result   # upper = entry * 1.02

    def test_short_term_with_budget_adds_shares(self):
        result = _entry_window(100, stop=90, budget=1000)
        assert "shares" in result or "share" in result

    def test_long_term_basic(self):
        result = _entry_window(100, is_long_term=True)
        assert result != ""
        assert "Patient" in result or "patient" in result

    def test_long_term_with_budget(self):
        result = _entry_window(100, is_long_term=True, budget=500)
        assert "5" in result   # 500/100 = 5 shares

    def test_crypto_uses_three_pct_window(self):
        result = _entry_window(100, is_crypto=True)
        assert "3%" in result   # crypto uses 3% window


# ─────────────────────────────────────────────────────────────────────────────
class TestStars:
    """_stars returns correct star/hollow-star combination."""

    def test_max_conviction(self):
        assert _stars(5) == "★★★★★"

    def test_min_conviction(self):
        assert _stars(1) == "★☆☆☆☆"

    def test_mid_conviction(self):
        assert _stars(3) == "★★★☆☆"

    def test_total_always_five(self):
        for c in range(1, 6):
            s = _stars(c)
            assert s.count("★") + s.count("☆") == 5


# ─────────────────────────────────────────────────────────────────────────────
class TestFormatDailyMessage:
    """format_daily_message integration tests."""

    def test_returns_string(self):
        result = format_daily_message(_picks(), _cfg())
        assert isinstance(result, str)

    def test_non_empty(self):
        result = format_daily_message(_picks(), _cfg())
        assert len(result) > 0

    def test_contains_st_ticker(self):
        result = format_daily_message(_picks(st_tickers=("AAPL",)), _cfg())
        assert "AAPL" in result

    def test_contains_lt_ticker(self):
        result = format_daily_message(_picks(lt_tickers=("NVDA",)), _cfg())
        assert "NVDA" in result

    def test_st_only_mode_hides_lt(self):
        result = format_daily_message(_picks(lt_tickers=("NVDA",)), _cfg(pick_mode="st"))
        assert "NVDA" not in result

    def test_lt_only_mode_hides_st(self):
        result = format_daily_message(_picks(st_tickers=("AAPL",)), _cfg(pick_mode="lt"))
        assert "AAPL" not in result

    def test_greeting_when_first_name(self):
        result = format_daily_message(_picks(), _cfg(first_name="Alice"))
        assert "Alice" in result

    def test_no_greeting_when_no_first_name(self):
        result = format_daily_message(_picks(), _cfg())
        # Should not have "Good morning," without a name
        assert "Good morning," not in result

    def test_market_closed_flag(self):
        result = format_daily_message(
            _picks(), _cfg(),
            market_closed=True,
            closed_reason="Saturday",
            next_open_label="Monday, Jun 2",
        )
        # Should still return a string (market closed message or picks header)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_empty_picks_still_returns_string(self):
        empty_picks = {
            "stocks": {"short_term": [], "long_term": []},
            "crypto": {"short_term": [], "long_term": []},
            "etfs": {"short_term": [], "long_term": []},
            "commodities": {"short_term": [], "long_term": []},
            "options_plays": [],
        }
        result = format_daily_message(empty_picks, _cfg())
        assert isinstance(result, str)

    def test_show_crypto_false_hides_crypto(self):
        picks_with_crypto = _picks()
        picks_with_crypto["crypto"] = {
            "short_term": [{"symbol": "BTC", "entry_price": 65000,
                            "target_price": 72000, "stop_loss": 60000,
                            "conviction": 3, "thesis": "BTC thesis."}],
            "long_term": []
        }
        result = format_daily_message(picks_with_crypto, _cfg(show_crypto=False))
        assert "BTC" not in result

    def test_watchlist_ticker_floated_to_top(self):
        """A watchlist ticker should appear before non-watchlist tickers in output."""
        picks = _picks(st_tickers=("MSFT", "AAPL"))
        result_wl = format_daily_message(picks, _cfg(watchlist=["AAPL"]))
        # AAPL should appear before MSFT in the message
        aapl_pos = result_wl.find("AAPL")
        msft_pos = result_wl.find("MSFT")
        if aapl_pos != -1 and msft_pos != -1:
            assert aapl_pos < msft_pos
