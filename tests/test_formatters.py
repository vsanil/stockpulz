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

    def test_st_lt_duplicate_collapsed(self):
        """A ticker in BOTH short + long term shows once (ST card + note), not a
        duplicate LT card."""
        result = format_daily_message(_picks(st_tickers=("VERX",), lt_tickers=("VERX",)), _cfg())
        assert result.count("also a long-term hold") == 1   # noted once in the ST card
        assert "shown above" in result                       # LT section cross-refs, no dup card
        assert "VERX" in result

    def test_distinct_st_lt_not_collapsed(self):
        result = format_daily_message(_picks(st_tickers=("AAPL",), lt_tickers=("NVDA",)), _cfg())
        assert "also a long-term hold" not in result
        assert "AAPL" in result and "NVDA" in result

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

    def _recent_stats(self, avg_return, median_return=None, wins=4, losses=0):
        total = {
            "wins": wins, "losses": losses,
            "win_rate": round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0,
            "avg_return": avg_return,
        }
        if median_return is not None:
            total["median_return"] = median_return
        return {"days": 30, "total": total, "spy_return": 5.0}

    def test_perf_bar_shows_median_label_when_field_present(self):
        stats = self._recent_stats(avg_return=441.4, median_return=3.2)
        result = format_daily_message(_picks(), _cfg(), recent_stats=stats)
        assert "median" in result
        assert "3.2" in result

    def test_perf_bar_hides_skewed_avg_when_median_present(self):
        stats = self._recent_stats(avg_return=441.4, median_return=3.2)
        result = format_daily_message(_picks(), _cfg(), recent_stats=stats)
        assert "441" not in result

    def test_perf_bar_falls_back_to_avg_when_median_absent(self):
        stats = self._recent_stats(avg_return=5.1)
        result = format_daily_message(_picks(), _cfg(), recent_stats=stats)
        assert "avg" in result
        assert "5.1" in result

    def test_pick_with_theme_does_not_raise(self):
        """Regression: _theme_tag used _re from _clean_thesis scope → NameError.
        Any pick with a 'theme' field must not crash format_daily_message."""
        def _stock_with_theme(ticker):
            return {"ticker": ticker, "entry_price": 100.0, "target_price": 115.0,
                    "stop_loss": 90.0, "conviction": 3, "thesis": "AI growth story.",
                    "catalyst": "Earnings beat.", "timeframe": "ST",
                    "sector": "Technology", "theme": "AI Infrastructure"}
        picks = {
            "stocks": {
                "short_term": [_stock_with_theme("NVDA")],
                "long_term":  [_stock_with_theme("MSFT")],
            },
            "crypto":      {"short_term": [], "long_term": []},
            "etfs":        {"short_term": [], "long_term": []},
            "commodities": {"short_term": [], "long_term": []},
            "options_plays": [],
        }
        # Must not raise NameError: name '_re' is not defined
        result = format_daily_message(picks, _cfg())
        assert isinstance(result, str)
        assert "NVDA" in result


class TestPicksKeyboardCollapsed:
    """The morning keyboard collapsed (Jul 2026) from per-pick Log/Alert rows to
    a single 'Today's Picks' button (opens the picks tab) + Paper Trade."""

    def _picks(self):
        return {"stocks": {"short_term": [{"ticker": "AAPL", "entry_price": 100,
                                           "stop_loss": 90, "target_price": 120}],
                           "long_term": [{"ticker": "MSFT", "entry_price": 300}]},
                "crypto": {"short_term": [{"symbol": "BTC", "entry_price": 50000}]}}

    def test_two_buttons_picks_and_paper(self, monkeypatch):
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
        from formatters import build_picks_keyboard
        kb = build_picks_keyboard(self._picks(), {})
        assert len(kb) == 2 and all(len(row) == 1 for row in kb)
        texts = [row[0]["text"] for row in kb]
        assert any("Today's Picks" in t for t in texts)
        assert any("Paper Trade" in t for t in texts)
        assert "tab=picks" in kb[0][0]["web_app"]["url"]

    def test_no_per_pick_log_or_alert_buttons(self, monkeypatch):
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://x.onrender.com")
        from formatters import build_picks_keyboard
        flat = [b["text"] for row in build_picks_keyboard(self._picks(), {}) for b in row]
        # no per-ticker rows (old 'Log AAPL' / 'Alert at $100')
        assert not any(sym in t for t in flat for sym in ("AAPL", "MSFT", "BTC"))
        assert not any("Alert at" in t for t in flat)

    def test_empty_when_no_miniapp_url(self, monkeypatch):
        monkeypatch.delenv("RENDER_EXTERNAL_URL", raising=False)
        monkeypatch.delenv("APP_URL", raising=False)
        from formatters import build_picks_keyboard
        assert build_picks_keyboard(self._picks(), {}) == []


class TestEodPositionsSection:
    """EOD shows the user's held positions (P&L vs entry + stop-hit flag)."""

    def _picks_min(self):
        return {"stocks": {"short_term": [{"ticker": "AAA", "entry_price": 100,
                            "target_price": 115, "stop_loss": 90}], "long_term": []},
                "crypto": {"short_term": [], "long_term": []},
                "etfs": {"short_term": [], "long_term": []},
                "commodities": {"short_term": [], "long_term": []},
                "options_plays": [], "daily_summary": "x"}

    def test_positions_render_with_stop_hit(self):
        from formatters import format_eod_full_summary
        positions = [{"ticker": "BNB", "entry_price": 651, "stop_loss": 645},
                     {"ticker": "XEL", "entry_price": 80,  "stop_loss": 75}]
        prices = {"AAA": 110, "BNB": 560.5, "XEL": 81.9}
        msg = format_eod_full_summary(self._picks_min(), prices, positions)
        assert "Your other positions" in msg
        assert "BNB" in msg and "XEL" in msg
        assert "stop hit" in msg          # BNB is below its stop

    def test_no_positions_no_section(self):
        from formatters import format_eod_full_summary
        msg = format_eod_full_summary(self._picks_min(), {"AAA": 110}, [])
        assert "Your other positions" not in msg


class TestWeeklyRecapAlphaDot:
    """The Avg-line dot reflects BEATING THE BENCHMARK (alpha), not just a
    positive absolute return. A +1.7% avg that trails the S&P must read 🟡."""

    @staticmethod
    def _avg_line(recap):
        from formatters import format_weekly_recap_message
        msg = format_weekly_recap_message(recap, {})
        return next(l for l in msg.split("\n") if l.startswith("Avg:"))

    def _recap(self, avg, spy):
        return {"stocks": {"count": 5, "wins": 4, "avg_return": avg,
                           "best": ("OPEN", 6.1), "worst": ("AMBA", -8.7)},
                "crypto": None, "spy_return": spy, "pick_outcomes": []}

    def test_positive_but_trails_index_is_yellow(self):
        # Jul 4 real case: +1.7% avg vs S&P +2.2% → alpha -0.5% → in line → 🟡
        line = self._avg_line(self._recap(1.7, 2.2))
        assert "🟡" in line and "🟢" not in line and "🔴" not in line

    def test_beats_index_is_green(self):
        line = self._avg_line(self._recap(3.0, 2.2))      # alpha +0.8%
        assert "🟢" in line

    def test_trails_index_badly_is_red(self):
        line = self._avg_line(self._recap(-2.0, 2.2))     # alpha -4.2%
        assert "🔴" in line

    def test_no_benchmark_falls_back_to_absolute_return(self):
        # crypto section passes no spy → dot reflects absolute return direction
        r = {"stocks": None,
             "crypto": {"count": 2, "wins": 2, "avg_return": 4.0,
                        "best": ("BTC", 5.0), "worst": ("ETH", 3.0)},
             "spy_return": None, "pick_outcomes": []}
        from formatters import format_weekly_recap_message
        msg = format_weekly_recap_message(r, {"assets": ["crypto"], "show_crypto": True})
        crypto_avg = next(l for l in msg.split("\n") if l.startswith("Avg:"))
        assert "🟢" in crypto_avg          # +4% absolute, no benchmark → green


class TestEODImplausiblePriceGuard:
    """A garbage feed value (UNI $0.00 / TSLA $0.01 on a holiday) must render as
    'price unavailable' in the EOD card — never a real −100% ▼ / stop hit."""

    def _picks_min(self):
        return {"stocks": {"short_term": [{"ticker": "AAA", "entry_price": 100,
                            "target_price": 115, "stop_loss": 90}], "long_term": []},
                "crypto": {"short_term": [], "long_term": []},
                "etfs": {"short_term": [], "long_term": []},
                "commodities": {"short_term": [], "long_term": []},
                "options_plays": [], "daily_summary": "x"}

    def test_garbage_price_shows_unavailable_not_stop_hit(self):
        from formatters import format_eod_full_summary
        positions = [{"ticker": "TSLA", "entry_price": 354.0, "stop_loss": 340.0}]
        prices = {"AAA": 110, "TSLA": 0.01}         # TSLA = holiday garbage
        msg = format_eod_full_summary(self._picks_min(), prices, positions)
        assert "TSLA" in msg and "price unavailable" in msg
        assert "stop hit" not in msg                # must NOT read as a real stop
        assert "100.0%" not in msg                  # no false −100%

    def test_real_stop_hit_still_shows(self):
        from formatters import format_eod_full_summary
        positions = [{"ticker": "BNB", "entry_price": 651.0, "stop_loss": 645.0}]
        prices = {"AAA": 110, "BNB": 570.31}        # real −12.4%, plausible
        msg = format_eod_full_summary(self._picks_min(), prices, positions)
        assert "stop hit" in msg and "price unavailable" not in msg


class TestEmptySectionsCollapse:
    """A section with nothing to say used to get its own header line. On a thin
    day that was five lines announcing absence wrapped around one real pick."""

    def _empty(self):
        p = _picks(st_tickers=(), lt_tickers=())
        p["daily_summary"] = "Quiet session."
        return p

    def test_all_empty_sections_become_one_line(self):
        msg = format_daily_message(self._empty(), _cfg())
        assert msg.count("📭") == 1, "the quiet summary must be a single line"
        for gone in ("no qualifying setup today", "no stock cleared the quality bar",
                     "no alignment with current regime", "no underlying stock setups"):
            assert gone not in msg, f"per-section empty header still rendered: {gone}"
        # every silent section is still ACCOUNTED for, just on one line
        for label in ("Stocks ST", "Stocks LT", "ETFs", "Commodities"):
            assert label in msg

    def test_a_section_with_picks_still_renders_in_full(self):
        msg = format_daily_message(_picks(st_tickers=("AAPL",),
                                                     lt_tickers=()), _cfg())
        assert "STOCKS — SHORT TERM" in msg and "AAPL" in msg
        assert "📈 Stocks ST" not in msg, "a section WITH picks must not be called quiet"
        assert "🏦 Stocks LT" in msg, "the empty LT section should be collapsed"

    def test_near_misses_keep_their_own_section(self):
        """Near-misses are real content — only true silence collapses."""
        p = self._empty()
        p["stocks"]["near_misses"] = {"short_term": [
            {"ticker": "AMD", "current_price": 100.0, "score": 72,
             "near_miss_reason": "RSI 72 — overbought"}], "long_term": []}
        msg = format_daily_message(p, _cfg())
        assert "STOCKS — SHORT TERM" in msg and "AMD" in msg
        assert "📈 Stocks ST" not in msg, "a section showing near-misses is not quiet"
        assert "🏦 Stocks LT" in msg

    def test_sections_hidden_by_preference_are_not_listed_as_quiet(self):
        """If a user turned crypto off, it isn't 'quiet' — it isn't theirs."""
        msg = format_daily_message(self._empty(), _cfg(show_crypto=False))
        assert "🪙 Crypto" not in msg

    def test_market_closed_says_so_once(self):
        msg = format_daily_message(self._empty(), _cfg(), market_closed=True)
        assert msg.count("📭") == 1
        assert "market closed" in msg

    def test_thin_day_is_materially_shorter(self):
        msg = format_daily_message(self._empty(), _cfg())
        assert len(msg.splitlines()) < 20, "a no-setup day should be a short message"


class TestDailyMessageUsesPlainEnglish:
    """The model is asked for `plain_english` ("one sentence, max 20 words, for a
    complete beginner, no jargon") on every pick. It was generated and stored on
    every pick and then NEVER rendered — the raw `thesis` went out instead,
    leaking internal metric names to users every morning."""

    def _p(self, **extra):
        p = _picks(st_tickers=("NWSA",), lt_tickers=())
        p["stocks"]["short_term"][0].update(extra)
        return p

    def test_plain_english_is_preferred_over_thesis(self):
        msg = format_daily_message(self._p(
            thesis="Breakout with three_white_soldiers, volume_ratio 2.02, P/C=0.06",
            plain_english="News Corp owns the Wall Street Journal — buying on a volume breakout"),
            _cfg())
        assert "News Corp owns the Wall Street Journal" in msg
        assert "three_white_soldiers" not in msg, "internal metric name leaked to users"
        assert "volume_ratio" not in msg and "P/C=" not in msg

    def test_falls_back_to_thesis_when_plain_english_is_absent(self):
        """Crypto/ETF picks and anything cached from before the field existed."""
        msg = format_daily_message(self._p(thesis="Solid breakout setup."), _cfg())
        assert "Solid breakout setup" in msg   # _clean_thesis strips the full stop

    def test_edge_is_not_repeated_in_the_daily_message(self):
        """`edge` restated the thesis with the same numbers (3/3 on a real
        message). It still renders in the mini-app pick sheet."""
        msg = format_daily_message(self._p(
            plain_english="A simple summary.",
            edge="Highest volume breakout in the candidate set"), _cfg())
        assert "Edge:" not in msg
        assert "Highest volume breakout" not in msg

    def test_the_catalyst_still_shows(self):
        msg = format_daily_message(self._p(plain_english="A simple summary.",
                                           catalyst="Q4 earnings beat"), _cfg())
        assert "Q4 earnings beat" in msg and "🎯" in msg

    def test_levels_and_sizing_are_untouched(self):
        """The two lines that carry the actual decision must not regress."""
        msg = format_daily_message(self._p(plain_english="A simple summary."), _cfg())
        assert "NWSA" in msg and "$100" in msg and "$115" in msg and "$90" in msg
