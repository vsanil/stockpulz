"""
test_ai_analyzer.py — Unit tests for pure helper functions in ai_analyzer.py.

Network calls (yfinance, Anthropic API, Finnhub) are fully mocked.
Tests cover:
  - _strip_fences
  - _validate_and_clean_picks
  - _backfill_allocations
  - _call_claude (system= parameter fix + use_caching flag)
  - _build_commodity_candidates (bulk yf.download path)
"""

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Patch heavy optional imports before loading the module
for _mod in ("requests", "finnhub", "anthropic"):
    sys.modules.setdefault(_mod, MagicMock())

# Patch config_manager signal cache to avoid Gist I/O
with patch("config_manager.load_signal_cache", return_value={}), \
     patch("config_manager.save_signal_cache"), \
     patch("config_manager.get_cached_signal", return_value=None), \
     patch("config_manager.set_cached_signal"):
    import ai_analyzer as aa
    from ai_analyzer import (
        _strip_fences,
        _validate_and_clean_picks,
        _backfill_allocations,
        _call_claude,
        _build_commodity_candidates,
        SYSTEM_PROMPT,
        STRICT_RETRY_SYSTEM,
        _KNOWN_CRYPTO,
    )


# ── _strip_fences ─────────────────────────────────────────────────────────────

class TestStripFences:
    def test_plain_json_unchanged(self):
        s = '{"a": 1}'
        assert _strip_fences(s) == s

    def test_strips_json_fence(self):
        s = "```json\n{\"a\": 1}\n```"
        result = _strip_fences(s)
        assert result.strip() == '{"a": 1}'

    def test_strips_plain_fence(self):
        s = "```\n{\"b\": 2}\n```"
        result = _strip_fences(s)
        assert "{" in result

    def test_empty_string_unchanged(self):
        assert _strip_fences("") == ""


# ── _validate_and_clean_picks ─────────────────────────────────────────────────

def _make_raw_picks(st_tickers=None, lt_tickers=None, crypto_symbols=None):
    return {
        "daily_summary": "test",
        "stocks": {
            "short_term": [{"ticker": t, "conviction": 4} for t in (st_tickers or [])],
            "long_term":  [{"ticker": t, "conviction": 3} for t in (lt_tickers or [])],
        },
        "crypto": {
            "short_term": [{"symbol": s} for s in (crypto_symbols or [])],
            "long_term":  [],
        },
    }


class TestValidateAndCleanPicks:
    def test_known_candidates_pass_fast_path(self):
        picks = _make_raw_picks(st_tickers=["AAPL", "NVDA"])
        result = _validate_and_clean_picks(picks, {"AAPL", "NVDA"})
        assert len(result["stocks"]["short_term"]) == 2

    def test_unknown_ticker_dropped_after_live_check(self):
        """Ticker not in candidates set AND yfinance returns no price → drop."""
        picks = _make_raw_picks(st_tickers=["FAKE123"])
        with patch.object(aa, "_is_valid_ticker", return_value=False):
            result = _validate_and_clean_picks(picks, set())
        assert result["stocks"]["short_term"] == []

    def test_unknown_ticker_kept_if_yfinance_validates(self):
        """Ticker not in candidates but yfinance confirms it → keep."""
        picks = _make_raw_picks(st_tickers=["LEGIT"])
        with patch.object(aa, "_is_valid_ticker", return_value=True):
            result = _validate_and_clean_picks(picks, set())
        assert len(result["stocks"]["short_term"]) == 1

    def test_known_crypto_symbol_kept(self):
        picks = _make_raw_picks(crypto_symbols=["BTC"])
        result = _validate_and_clean_picks(picks, set())
        assert len(result["crypto"]["short_term"]) == 1

    def test_unknown_crypto_dropped(self):
        picks = _make_raw_picks(crypto_symbols=["FAKECOIN"])
        result = _validate_and_clean_picks(picks, set())
        assert result["crypto"]["short_term"] == []

    def test_stop_loss_fallback_applied_to_st_missing_stop(self):
        picks = {
            "stocks": {
                "short_term": [{"ticker": "AAPL", "entry_price": 200.0}],
                "long_term":  [],
            }
        }
        result = _validate_and_clean_picks(picks, {"AAPL"})
        st = result["stocks"]["short_term"][0]
        assert st["stop_loss"] == pytest.approx(200.0 * 0.95, rel=1e-3)

    def test_stop_loss_fallback_not_applied_when_present(self):
        picks = {
            "stocks": {
                "short_term": [{"ticker": "AAPL", "entry_price": 200.0, "stop_loss": 190.0}],
                "long_term":  [],
            }
        }
        result = _validate_and_clean_picks(picks, {"AAPL"})
        assert result["stocks"]["short_term"][0]["stop_loss"] == 190.0

    def test_options_play_dropped_for_invalid_ticker(self):
        picks = {
            "stocks": {"short_term": [], "long_term": []},
            "options_plays": [{"ticker": "FAKE"}],
        }
        with patch.object(aa, "_is_valid_ticker", return_value=False):
            result = _validate_and_clean_picks(picks, set())
        assert result.get("options_plays", []) == []


# ── _backfill_allocations ─────────────────────────────────────────────────────

class TestBackfillAllocations:
    def _picks_with_tickers(self, st_tickers, lt_tickers=None):
        return {
            "stocks": {
                "short_term": [{"ticker": t} for t in st_tickers],
                "long_term":  [{"ticker": t} for t in (lt_tickers or [])],
            },
            "etfs":        {"short_term": [], "long_term": []},
            "commodities": {"short_term": [], "long_term": []},
            "crypto":      {"short_term": [], "long_term": []},
        }

    def test_equal_split_across_picks(self):
        picks = self._picks_with_tickers(["A", "B", "C"])
        config = {"stock_budget": 3_000.0}
        _backfill_allocations(picks, config)
        for p in picks["stocks"]["short_term"]:
            assert p["allocation"] == pytest.approx(1_000.0)

    def test_crypto_budget_split(self):
        picks = {
            "stocks": {"short_term": [], "long_term": []},
            "etfs":   {"short_term": [], "long_term": []},
            "commodities": {"short_term": [], "long_term": []},
            "crypto": {"short_term": [{"symbol": "BTC"}, {"symbol": "ETH"}],
                       "long_term":  []},
        }
        _backfill_allocations(picks, {"crypto_budget": 1_000.0})
        for p in picks["crypto"]["short_term"]:
            assert p["allocation"] == pytest.approx(500.0)

    def test_no_budget_no_crash(self):
        picks = self._picks_with_tickers(["A"])
        _backfill_allocations(picks, {})   # no stock_budget key

    def test_zero_picks_no_crash(self):
        picks = self._picks_with_tickers([])
        _backfill_allocations(picks, {"stock_budget": 5_000.0})


# ── _call_claude ──────────────────────────────────────────────────────────────

class TestCallClaude:
    """Verify that _call_claude uses system= parameter correctly."""

    def _mock_response(self, text: str) -> MagicMock:
        msg = MagicMock()
        msg.content = [MagicMock(text=text)]
        return msg

    def test_uses_system_param_not_user_concat(self):
        """system text must go to the system= kwarg, NOT be concatenated into user."""
        captured = {}

        def _fake_create(**kwargs):
            captured.update(kwargs)
            return self._mock_response('{"a": 1}')

        client_mock = MagicMock()
        client_mock.messages.create.side_effect = _fake_create

        with patch("ai_analyzer._get_client", return_value=client_mock):
            _call_claude("MY_SYSTEM", "MY_USER")

        assert "system" in captured
        # The system param should contain "MY_SYSTEM", NOT appear in user message
        messages = captured["messages"]
        assert messages[0]["role"] == "user"
        assert "MY_SYSTEM" not in messages[0]["content"]

    def test_system_param_is_string_without_caching(self):
        """Without caching, system should be passed as a plain string."""
        captured = {}

        def _fake_create(**kwargs):
            captured.update(kwargs)
            return self._mock_response('{"a": 1}')

        client_mock = MagicMock()
        client_mock.messages.create.side_effect = _fake_create

        with patch("ai_analyzer._get_client", return_value=client_mock):
            _call_claude("SYS", "USER", use_caching=False)

        assert isinstance(captured["system"], str)

    def test_system_param_is_list_with_caching(self):
        """With caching, system should be a list[dict] with cache_control."""
        captured = {}

        def _fake_create(**kwargs):
            captured.update(kwargs)
            return self._mock_response('{"a": 1}')

        client_mock = MagicMock()
        client_mock.messages.create.side_effect = _fake_create

        with patch("ai_analyzer._get_client", return_value=client_mock):
            _call_claude("SYS", "USER", use_caching=True)

        sys_param = captured["system"]
        assert isinstance(sys_param, list)
        assert sys_param[0]["type"] == "text"
        assert sys_param[0]["text"] == "SYS"
        assert sys_param[0]["cache_control"] == {"type": "ephemeral"}

    def test_returns_parsed_json(self):
        payload = {"stocks": {"short_term": []}}
        client_mock = MagicMock()
        client_mock.messages.create.return_value = self._mock_response(
            json.dumps(payload)
        )
        with patch("ai_analyzer._get_client", return_value=client_mock):
            result = _call_claude("SYS", "USER")
        assert result == payload

    def test_strips_fence_before_parsing(self):
        payload = {"x": 1}
        fenced = f"```json\n{json.dumps(payload)}\n```"
        client_mock = MagicMock()
        client_mock.messages.create.return_value = self._mock_response(fenced)
        with patch("ai_analyzer._get_client", return_value=client_mock):
            result = _call_claude("SYS", "USER")
        assert result == payload


# ── _build_commodity_candidates (bulk download path) ─────────────────────────

class TestBuildCommodityCandidates:
    """Smoke test — checks the bulk download path doesn't crash and parses output."""

    def _make_bulk_df(self, tickers):
        """Build a fake multi-ticker yf.download() response (MultiIndex DataFrame)."""
        import pandas as pd
        import numpy as np

        n_rows = 65  # enough for 22-day checks
        idx    = pd.date_range("2024-01-01", periods=n_rows, freq="B")
        base   = np.linspace(100, 110, n_rows)

        cols = pd.MultiIndex.from_product(
            [["Close", "Volume"], tickers],
            names=["Price", "Ticker"],
        )
        data = {}
        for ticker in tickers:
            data[("Close",  ticker)] = base + hash(ticker) % 5
            data[("Volume", ticker)] = np.full(n_rows, 1_000_000.0)

        return pd.DataFrame(data, index=idx, columns=cols)

    def test_returns_list_of_dicts(self):
        from ai_analyzer import _COMMODITY_TICKERS
        tickers = [t for t, _, _ in _COMMODITY_TICKERS]
        fake_df = self._make_bulk_df(tickers)

        with patch("ai_analyzer.yf.download", return_value=fake_df):
            result = _build_commodity_candidates()

        assert isinstance(result, list)
        # Should have one entry per ticker (7 total)
        assert len(result) == len(tickers)
        for c in result:
            assert "ticker"        in c
            assert "current_price" in c
            assert "rsi"           in c
            assert "vol_ratio"     in c

    def test_returns_empty_on_download_failure(self):
        with patch("ai_analyzer.yf.download", side_effect=Exception("network error")):
            result = _build_commodity_candidates()
        assert result == []

    def test_skips_ticker_with_insufficient_data(self):
        """If a ticker has < 22 rows it should be skipped."""
        import pandas as pd
        import numpy as np
        from ai_analyzer import _COMMODITY_TICKERS

        tickers = [t for t, _, _ in _COMMODITY_TICKERS]
        n_rows  = 10   # too few rows → all tickers skipped
        idx     = pd.date_range("2024-01-01", periods=n_rows, freq="B")
        cols    = pd.MultiIndex.from_product([["Close", "Volume"], tickers])
        df      = pd.DataFrame(
            {(f, t): [100.0] * n_rows for f in ["Close", "Volume"] for t in tickers},
            index=idx, columns=cols,
        )
        with patch("ai_analyzer.yf.download", return_value=df):
            result = _build_commodity_candidates()

        assert result == []


class TestUnwinnablePicksAreRejected:
    """🔴 A long pick whose target sits at or below its entry can ONLY close at
    a loss. Two reached the synthetic account on 2026-08-03 (AMBA, entry 82.67
    → target 78.54) and were caught by position_audit AFTER delivery.

    Validation checked tickers and backfilled stops but never compared the
    levels, so the shape could still ship. Dropping is the only honest response:
    we cannot invent a target, and a pick that cannot win must not be published.
    """

    def _picks(self, **over):
        p = {"ticker": "AMBA", "entry_price": 82.67, "target_price": 78.54,
             "stop_loss": 75.68}
        p.update(over)
        return {"stocks": {"short_term": [
            p, {"ticker": "MSFT", "entry_price": 100.0,
                "target_price": 110.0, "stop_loss": 95.0}]}}

    def _run(self, picks):
        import ai_analyzer
        out = ai_analyzer._validate_and_clean_picks(picks, {"AMBA", "MSFT"})
        return [x["ticker"] for x in out["stocks"]["short_term"]]

    def test_a_target_below_entry_is_dropped(self):
        assert self._run(self._picks()) == ["MSFT"]

    def test_a_target_EQUAL_to_entry_is_dropped(self):
        """Zero upside is not a trade."""
        assert self._run(self._picks(target_price=82.67)) == ["MSFT"]

    def test_a_stop_at_or_above_entry_is_dropped(self):
        """Born stopped-out — the next price check closes it instantly."""
        assert self._run(self._picks(target_price=95.0, stop_loss=82.67)) == ["MSFT"]

    def test_a_healthy_pick_is_untouched(self):
        out = self._run(self._picks(target_price=95.0))
        assert out == ["AMBA", "MSFT"], "a valid pick was dropped"

    def test_a_pick_with_no_target_still_ships(self):
        """LONG-TERM picks carry no stop by design and may carry no target —
        absence is not a violation, and dropping them would silently delete a
        whole section."""
        assert "AMBA" in self._run(self._picks(target_price=None, stop_loss=None))

    def test_unparseable_levels_do_not_drop_the_pick(self):
        """Only a level we can actually compare justifies a drop."""
        assert "AMBA" in self._run(self._picks(target_price="n/a"))
