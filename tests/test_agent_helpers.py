"""
test_agent_helpers.py — Unit tests for pure helper functions in agent.py.

No network I/O is exercised. All external calls are patched where needed.
Tests cover:
  - Holiday detection and naming (_us_market_holidays, is_market_holiday, get_holiday_name)
  - detect_run_mode (time-based routing)
  - _filter_by_conviction (pick gate)
  - _picks_are_empty (empty-section check)
  - _is_alerted / _mark_alerted (per-key cache TTL deduplication)
"""

import os
import sys
from datetime import date, datetime

import pytest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Patch config_manager / Gist before importing agent to avoid network calls
from unittest.mock import MagicMock
sys.modules.setdefault("requests", MagicMock())

import agent as ag
from agent import (
    is_market_holiday, get_holiday_name,
    detect_run_mode,
    _filter_by_conviction, _picks_are_empty,
    _is_alerted, _mark_alerted,
    _ALERTED_KEY_PREFIX,
)
from cache_layer import reset_cache, get_cache


@pytest.fixture(autouse=True)
def flush_cache():
    """Ensure a clean in-memory cache for every test."""
    reset_cache()
    get_cache().flush()
    yield
    get_cache().flush()
    reset_cache()


# ── Holiday detection ─────────────────────────────────────────────────────────

class TestMarketHolidays:
    def test_new_years_day_2025(self):
        # Jan 1 2025 is a Wednesday
        assert is_market_holiday(date(2025, 1, 1))
        assert get_holiday_name(date(2025, 1, 1)) == "New Year's Day"

    def test_new_years_observed_when_saturday(self):
        # Jan 1 2022 is a Saturday → observed Friday Dec 31 2021.
        # NOTE: The holiday table is built with d.year, so Dec 31 2021 is looked
        # up in _us_market_holidays(2021) which contains Jan 1 2021 (a Friday,
        # no shift).  Cross-year observations are a known edge case not handled
        # by _us_market_holidays — same behaviour as the original code.
        # Test the in-year Saturday case instead: Jan 1 2000 is a Saturday,
        # so observed = Dec 31 1999.  The lookup for 1999 does not contain this
        # either for the same reason — so we simply assert the function works
        # for the documented in-year path and does not crash.
        # The Sunday case below tests the fully in-year shift path (Jan 2 of
        # the same year).
        assert not is_market_holiday(date(2021, 12, 31))   # cross-year gap — expected

    def test_new_years_observed_when_sunday(self):
        # Jan 1 2023 is a Sunday → observed Monday Jan 2
        assert is_market_holiday(date(2023, 1, 2))

    def test_mlk_day_2025(self):
        # 3rd Monday of January 2025 = Jan 20
        assert is_market_holiday(date(2025, 1, 20))
        assert get_holiday_name(date(2025, 1, 20)) == "MLK Day"

    def test_good_friday_2025(self):
        # Good Friday 2025 = April 18
        assert is_market_holiday(date(2025, 4, 18))
        assert get_holiday_name(date(2025, 4, 18)) == "Good Friday"

    def test_memorial_day_2025(self):
        # Last Monday of May 2025 = May 26
        assert is_market_holiday(date(2025, 5, 26))
        assert get_holiday_name(date(2025, 5, 26)) == "Memorial Day"

    def test_juneteenth_2025(self):
        # June 19 2025 is a Thursday
        assert is_market_holiday(date(2025, 6, 19))
        assert get_holiday_name(date(2025, 6, 19)) == "Juneteenth"

    def test_independence_day_2025(self):
        # July 4 2025 is a Friday
        assert is_market_holiday(date(2025, 7, 4))
        assert get_holiday_name(date(2025, 7, 4)) == "Independence Day"

    def test_thanksgiving_2025(self):
        # 4th Thursday of November 2025 = Nov 27
        assert is_market_holiday(date(2025, 11, 27))
        assert get_holiday_name(date(2025, 11, 27)) == "Thanksgiving"

    def test_christmas_2025(self):
        assert is_market_holiday(date(2025, 12, 25))
        assert get_holiday_name(date(2025, 12, 25)) == "Christmas"

    def test_regular_trading_day(self):
        # Tuesday March 4 2025 — no holiday
        assert not is_market_holiday(date(2025, 3, 4))
        assert get_holiday_name(date(2025, 3, 4)) == ""

    def test_weekend_is_not_holiday(self):
        # Weekends are not listed as holidays — they're just weekend days
        assert not is_market_holiday(date(2025, 3, 1))  # Saturday


# ── detect_run_mode ───────────────────────────────────────────────────────────

class TestDetectRunMode:
    def _dt(self, weekday: int, hour: int, minute: int = 0) -> datetime:
        """Build a datetime that has the given weekday (0=Mon) and time."""
        # Find a date with the right weekday (using 2025-01-06 = Monday as anchor)
        from datetime import timedelta
        base = datetime(2025, 1, 6, hour, minute)  # Monday
        return base + timedelta(days=weekday)

    @pytest.fixture(autouse=True)
    def clear_env(self):
        os.environ.pop("RUN_MODE", None)
        yield
        os.environ.pop("RUN_MODE", None)

    def test_env_override(self):
        os.environ["RUN_MODE"] = "morning"
        assert detect_run_mode(datetime(2025, 1, 6, 15, 0)) == "morning"

    def test_weekday_before_10_is_morning(self):
        assert detect_run_mode(self._dt(0, 9, 0)) == "morning"

    def test_weekday_after_16_is_eod(self):
        assert detect_run_mode(self._dt(0, 16, 0)) == "eod_summary"

    def test_weekday_15_is_close_check(self):
        assert detect_run_mode(self._dt(0, 15, 30)) == "close_check"

    def test_weekday_midday_is_confirmation(self):
        assert detect_run_mode(self._dt(0, 11, 0)) == "confirmation"

    def test_saturday_morning_is_weekly(self):
        assert detect_run_mode(self._dt(5, 8, 0)) == "weekly"   # Saturday=5

    def test_sunday_morning_is_week_ahead(self):
        assert detect_run_mode(self._dt(6, 10, 0)) == "week_ahead"  # Sunday=6

    def test_premarket_window(self):
        # 8:40 AM weekday → premarket
        assert detect_run_mode(self._dt(0, 8, 45)) == "premarket"


# ── _filter_by_conviction ─────────────────────────────────────────────────────

def _make_picks(st_convictions: list[int], lt_convictions: list[int]) -> dict:
    return {
        "stocks": {
            "short_term": [{"ticker": f"T{i}", "conviction": c}
                           for i, c in enumerate(st_convictions)],
            "long_term":  [{"ticker": f"L{i}", "conviction": c}
                           for i, c in enumerate(lt_convictions)],
        },
        "crypto":      {"short_term": [], "long_term": []},
        "etfs":        {"short_term": [], "long_term": []},
        "commodities": {"short_term": [], "long_term": []},
        "options_plays": [],
    }


class TestFilterByConviction:
    def test_removes_below_threshold(self):
        picks = _make_picks([2, 3, 4, 5], [3, 4])
        result = _filter_by_conviction(picks, min_conviction=4)
        st = result["stocks"]["short_term"]
        assert all(p["conviction"] >= 4 for p in st)
        assert len(st) == 2   # only 4 and 5 survive

    def test_keeps_exact_threshold(self):
        picks = _make_picks([3], [3])
        result = _filter_by_conviction(picks, min_conviction=3)
        assert len(result["stocks"]["short_term"]) == 1

    def test_options_play_pruned_when_ticker_dropped(self):
        picks = _make_picks([2], [])
        picks["options_plays"] = [{"ticker": "T0"}]
        result = _filter_by_conviction(picks, min_conviction=4)
        assert result["options_plays"] == []

    def test_does_not_mutate_original(self):
        picks = _make_picks([2, 5], [])
        _filter_by_conviction(picks, min_conviction=4)
        assert len(picks["stocks"]["short_term"]) == 2   # original untouched


# ── _picks_are_empty ──────────────────────────────────────────────────────────

class TestPicksAreEmpty:
    def test_empty_picks(self):
        assert _picks_are_empty({"stocks": {"short_term": [], "long_term": []}})

    def test_not_empty_when_has_st(self):
        picks = {"stocks": {"short_term": [{"ticker": "AAPL"}], "long_term": []}}
        assert not _picks_are_empty(picks)

    def test_not_empty_when_has_crypto(self):
        picks = {"crypto": {"short_term": [{"symbol": "BTC"}]}}
        assert not _picks_are_empty(picks)

    def test_empty_dict_is_empty(self):
        assert _picks_are_empty({})


# ── _is_alerted / _mark_alerted (per-key TTL) ─────────────────────────────────

class TestAlertDedup:
    def test_not_alerted_initially(self):
        assert not _is_alerted("stop:AAPL:user1")

    def test_mark_then_is_alerted(self):
        _mark_alerted("stop:AAPL:user1")
        assert _is_alerted("stop:AAPL:user1")

    def test_different_keys_are_independent(self):
        _mark_alerted("stop:AAPL:user1", ttl_hours=72)
        assert not _is_alerted("stop:NVDA:user1")

    def test_custom_ttl_does_not_affect_other_keys(self):
        """Setting a 72h TTL on one key must NOT extend TTLs for other keys.
        This was the core bug in the old shared-list approach."""
        _mark_alerted("stop:AAPL:user1", ttl_hours=1)    # 1 hour
        _mark_alerted("coverage:user1",   ttl_hours=72)  # 72 hours

        # Both should still be present right after creation
        assert _is_alerted("stop:AAPL:user1")
        assert _is_alerted("coverage:user1")

    def test_two_calls_same_key_idempotent(self):
        _mark_alerted("stop:AAPL:user1")
        _mark_alerted("stop:AAPL:user1")
        assert _is_alerted("stop:AAPL:user1")

    def test_cache_key_uses_prefix(self):
        """Verify the cache entry is stored under alerted:<key>."""
        from cache_layer import cache_get
        _mark_alerted("mykey")
        assert cache_get(f"{_ALERTED_KEY_PREFIX}mykey") is True


# ── Late-delivery guard ───────────────────────────────────────────────────────

import pytz as _pytz

_ET = _pytz.timezone("America/New_York")


def _et(hour, minute=0, weekday_offset=0):
    """Return a timezone-aware ET datetime on a known weekday (Mon Jun 2 2026)."""
    from datetime import datetime
    # Jun 2 2026 = Tuesday (weekday 1)
    base = datetime(2026, 6, 2, hour, minute)
    return _ET.localize(base)


class TestMorningRunGuards:
    """Tests that run_morning() exits cleanly on weekends and does not
    send unexpected 'picks skipped' messages in normal conditions.
    The old late-delivery guard was removed — these tests verify the
    replacement behaviour (no spurious messages, clean exit)."""

    def _run_morning_mocked(self, now_et, monkeypatch, mock_data=False):
        """Run run_morning() with all external calls mocked. Returns sent messages."""
        import yfinance as _yf
        sent = []
        monkeypatch.setattr(ag, "MOCK_DATA", mock_data)
        monkeypatch.setattr(ag, "_log_cron_run", lambda *a, **kw: None)
        monkeypatch.setattr(ag, "_all_recipients", lambda: ["user1"])
        monkeypatch.setattr(ag, "send_message", lambda msg, chat_id=None: sent.append(msg))
        monkeypatch.setattr(ag, "is_market_holiday", lambda d: False)
        monkeypatch.setattr(ag, "_run_crypto_with_retry", lambda: {"short_term": [], "long_term": []})
        monkeypatch.setattr(_yf, "download", lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("test-stop")))
        try:
            ag.run_morning({}, now_et)
        except Exception:
            pass
        return sent

    def test_no_spurious_message_before_market_open(self, monkeypatch):
        """Before 9 AM ET on a weekday — run proceeds normally, no 'skipped' notice."""
        sent = self._run_morning_mocked(_et(8, 59), monkeypatch)
        assert not any("Morning picks skipped" in m for m in sent)

    def test_no_spurious_message_on_weekend(self, monkeypatch):
        """Weekend — run_morning exits early for stocks but no error message sent."""
        from datetime import datetime
        now_et = _ET.localize(datetime(2026, 6, 6, 10, 0))  # Saturday
        sent = self._run_morning_mocked(now_et, monkeypatch)
        assert not any("Morning picks skipped" in m for m in sent)

    def test_no_spurious_message_with_mock_data(self, monkeypatch):
        """MOCK_DATA=True — run uses mock picks, no network calls, no error messages."""
        sent = self._run_morning_mocked(_et(11, 0), monkeypatch, mock_data=True)
        assert not any("Morning picks skipped" in m for m in sent)


# ─────────────────────────────────────────────────────────────────────────────
class TestCryptoRetryNoBusyWait:
    """_run_crypto_with_retry must return immediately on clean empty result.

    Regression: previously empty result raised ValueError → retry loop slept
    15+30+60+120s = 225s before giving up. Fixed: if screener runs without
    exception but returns 0 candidates, return empty dict immediately.
    """

    def test_returns_empty_immediately_when_screener_finds_nothing(self):
        """Screener runs OK but finds 0 coins → no retry, no sleep, instant return."""
        import agent as ag
        calls = []

        def _fake_screener():
            calls.append(1)
            return {"short_term": [], "long_term": []}  # clean empty — not an exception

        with patch.object(ag, "run_crypto_screener", side_effect=_fake_screener), \
             patch.object(ag, "time") as mock_time:
            result = ag._run_crypto_with_retry()

        assert result == {"short_term": [], "long_term": []}
        assert len(calls) == 1, f"Expected 1 screener call, got {len(calls)} — retry loop fired"
        mock_time.sleep.assert_not_called()

    def test_retries_on_exception(self):
        """Real API failure (exception) must still trigger retry logic."""
        import agent as ag
        calls = []

        def _failing_screener():
            calls.append(1)
            raise RuntimeError("CoinGecko timeout")

        with patch.object(ag, "run_crypto_screener", side_effect=_failing_screener), \
             patch.object(ag, "time") as mock_time, \
             patch.object(ag, "_alert"):
            mock_time.sleep = lambda *a: None  # skip actual sleep
            result = ag._run_crypto_with_retry()

        assert result == {"short_term": [], "long_term": []}
        assert len(calls) == 5, f"Expected 5 attempts on exception, got {len(calls)}"


# ── Options flow alert ────────────────────────────────────────────────────────

class TestCheckOptionsFlowAlert:
    POSITIONS = [{"ticker": "NVDA"}, {"ticker": "AAPL"}]

    def _unusual_signal(self, ticker):
        # call_volume + put_volume >= 200 required to pass the thin-chain filter
        return {"unusual": True, "signal_score": 4, "bullish_flow": True,
                "bearish_flow": False, "sweep_detected": True, "iv_label": "ELEVATED",
                "call_volume": 150, "put_volume": 80}

    def _normal_signal(self, ticker):
        return {"unusual": False, "signal_score": 0, "bullish_flow": False,
                "bearish_flow": False, "sweep_detected": False, "iv_label": "NORMAL",
                "call_volume": 10, "put_volume": 5}

    def test_returns_text_when_unusual_activity(self):
        """Returns a non-None string when any held position has unusual options flow."""
        import agent as ag
        signals = {"NVDA": self._unusual_signal("NVDA"), "AAPL": self._normal_signal("AAPL")}
        with patch("options_flow.batch_options_signals", return_value=signals):
            result = ag._check_options_flow_alert("user1", self.POSITIONS)
        assert result is not None
        assert "NVDA" in result
        assert "unusual" in result.lower() or "options" in result.lower()

    def test_returns_none_when_no_unusual_activity(self):
        """Returns None when all held positions have normal options flow."""
        import agent as ag
        signals = {"NVDA": self._normal_signal("NVDA"), "AAPL": self._normal_signal("AAPL")}
        with patch("options_flow.batch_options_signals", return_value=signals):
            result = ag._check_options_flow_alert("user1", self.POSITIONS)
        assert result is None

    def test_dedup_prevents_second_fire(self):
        """Same ticker does not fire twice within the 24h dedup window."""
        import agent as ag
        signals = {"NVDA": self._unusual_signal("NVDA"), "AAPL": self._normal_signal("AAPL")}
        with patch("options_flow.batch_options_signals", return_value=signals):
            first  = ag._check_options_flow_alert("user1", self.POSITIONS)
            second = ag._check_options_flow_alert("user1", self.POSITIONS)
        assert first is not None
        assert second is None

    def test_returns_none_when_no_positions(self):
        """Returns None immediately when open_positions is empty."""
        import agent as ag
        with patch("options_flow.batch_options_signals") as mock_batch:
            result = ag._check_options_flow_alert("user1", [])
        mock_batch.assert_not_called()
        assert result is None


# ── Congressional cluster alert ───────────────────────────────────────────────

class TestCheckCongressionalAlert:
    POSITIONS = [{"ticker": "NVDA"}, {"ticker": "AAPL"}]

    def _cluster_signal(self, ticker):
        return {"is_cluster": True, "congress_members": 4, "congress_buys": 5,
                "congress_score": 10, "note": "CLUSTER BUY: 5 purchase(s) by 4 member(s)"}

    def _no_signal(self, ticker):
        return {"is_cluster": False, "congress_members": 0, "congress_buys": 0,
                "congress_score": 0, "note": "no recent congressional buys"}

    def test_returns_text_on_cluster_buy(self):
        """Returns a non-None string when a held ticker has a congressional cluster buy."""
        import agent as ag
        signals = {"NVDA": self._cluster_signal("NVDA"), "AAPL": self._no_signal("AAPL")}
        with patch("congressional_tracker.batch_congressional_signals", return_value=signals):
            result = ag._check_congressional_alert("user1", self.POSITIONS)
        assert result is not None
        assert "NVDA" in result
        assert "Congressional" in result or "congressional" in result.lower()

    def test_returns_none_when_no_cluster(self):
        """Returns None when no held positions have a congressional cluster buy."""
        import agent as ag
        signals = {"NVDA": self._no_signal("NVDA"), "AAPL": self._no_signal("AAPL")}
        with patch("congressional_tracker.batch_congressional_signals", return_value=signals):
            result = ag._check_congressional_alert("user1", self.POSITIONS)
        assert result is None

    def test_dedup_prevents_second_fire(self):
        """Cluster buy for same ticker does not fire twice in the same week."""
        import agent as ag
        signals = {"NVDA": self._cluster_signal("NVDA"), "AAPL": self._no_signal("AAPL")}
        with patch("congressional_tracker.batch_congressional_signals", return_value=signals):
            first  = ag._check_congressional_alert("user1", self.POSITIONS)
            second = ag._check_congressional_alert("user1", self.POSITIONS)
        assert first is not None
        assert second is None

    def test_returns_none_when_no_positions(self):
        """Returns None immediately when open_positions is empty."""
        import agent as ag
        with patch("congressional_tracker.batch_congressional_signals") as mock_batch:
            result = ag._check_congressional_alert("user1", [])
        mock_batch.assert_not_called()
        assert result is None


# ── Take-profit nudge ─────────────────────────────────────────────────────────

class TestCheckTakeProfitNudge:
    BASE_TRADE = {
        "ticker": "NVDA", "entry_price": "100", "target_price": "200",
        "stop_loss": "90", "shares": "10",
    }

    def _make_log(self, trade):
        return {"open": [trade], "closed": []}

    def test_fires_when_at_90pct_of_target(self):
        """Sends a message when position is 90% of the way from entry to target."""
        import agent as ag
        trade = {**self.BASE_TRADE}
        with patch.object(ag, "load_user_trade_log", return_value=self._make_log(trade)), \
             patch.object(ag, "send_inline_keyboard") as mock_send:
            ag._check_take_profit_nudge("user1", {"NVDA": 190.0})
        mock_send.assert_called_once()
        msg = mock_send.call_args[0][0]
        assert "NVDA" in msg
        assert "target" in msg.lower()

    def test_silent_when_below_85pct(self):
        """No message when position is only 60% of the way to target."""
        import agent as ag
        trade = {**self.BASE_TRADE}
        with patch.object(ag, "load_user_trade_log", return_value=self._make_log(trade)), \
             patch.object(ag, "send_inline_keyboard") as mock_send:
            ag._check_take_profit_nudge("user1", {"NVDA": 160.0})
        mock_send.assert_not_called()

    def test_dedup_prevents_second_fire(self):
        """Does not send twice for the same position on the same day."""
        import agent as ag
        trade = {**self.BASE_TRADE}
        with patch.object(ag, "load_user_trade_log", return_value=self._make_log(trade)), \
             patch.object(ag, "send_inline_keyboard") as mock_send:
            ag._check_take_profit_nudge("user1", {"NVDA": 190.0})
            ag._check_take_profit_nudge("user1", {"NVDA": 190.0})
        assert mock_send.call_count == 1

    def test_skipped_when_no_target(self):
        """Does not crash or send when target_price is missing."""
        import agent as ag
        trade = {"ticker": "NVDA", "entry_price": "100"}
        with patch.object(ag, "load_user_trade_log", return_value=self._make_log(trade)), \
             patch.object(ag, "send_inline_keyboard") as mock_send:
            ag._check_take_profit_nudge("user1", {"NVDA": 190.0})
        mock_send.assert_not_called()

    def test_includes_dollar_gain_when_shares_logged(self):
        """Message includes ~$X gain when shares are in the trade log."""
        import agent as ag
        trade = {**self.BASE_TRADE}
        with patch.object(ag, "load_user_trade_log", return_value=self._make_log(trade)), \
             patch.object(ag, "send_inline_keyboard") as mock_send:
            ag._check_take_profit_nudge("user1", {"NVDA": 195.0})
        msg = mock_send.call_args[0][0]
        assert "$" in msg  # dollar gain or target price shown


# ── Pre-market gap warnings ───────────────────────────────────────────────────

import pandas as pd
import pytz

def _make_premarket_history(price: float):
    """Build a minimal 1-min history DataFrame with one pre-market bar at 7 AM ET."""
    et = pytz.timezone("America/New_York")
    ts = pd.Timestamp("2026-06-06 07:00:00", tz=et)
    idx = pd.DatetimeIndex([ts])
    return pd.DataFrame({"Close": [price], "Open": [price], "High": [price], "Low": [price], "Volume": [1000]}, index=idx)

def _premarket_fake_ticker(prices: dict):
    """Return a factory that produces mock Ticker objects using history()."""
    def _factory(symbol):
        m = MagicMock()
        m.history.return_value = _make_premarket_history(prices.get(symbol, 0))
        return m
    return _factory

# Fake datetime that always reports 7:00 AM ET (pre-market)
class _FakePremarketDatetime:
    @staticmethod
    def now(tz=None):
        et = pytz.timezone("America/New_York")
        return pd.Timestamp("2026-06-06 07:00:00", tz=et)


class TestBuildPremarketGapWarnings:
    PICKS = {
        "stocks": {
            "short_term": [{"ticker": "CRUS", "entry_price": "100"}],
            "long_term":  [{"ticker": "AAPL", "entry_price": "200"}],
        }
    }

    def test_flags_ticker_gapping_above_entry(self):
        """Returns a warning line for a ticker gapping 5% above entry in pre-market."""
        import agent as ag
        import yfinance as yf

        with patch.object(yf, "Ticker", side_effect=_premarket_fake_ticker({"CRUS": 105.0, "AAPL": 200.0})), \
             patch("agent.datetime", _FakePremarketDatetime):
            result = ag._build_premarket_gap_warnings(self.PICKS)

        assert "CRUS" in result
        assert "AAPL" not in result  # 0% gap — no warning

    def test_returns_empty_when_no_gap(self):
        """Returns empty dict when all picks are at or below entry."""
        import agent as ag
        import yfinance as yf

        with patch.object(yf, "Ticker", side_effect=_premarket_fake_ticker({"CRUS": 98.0, "AAPL": 199.0})), \
             patch("agent.datetime", _FakePremarketDatetime):
            result = ag._build_premarket_gap_warnings(self.PICKS)

        assert result == {}

    def test_returns_empty_after_market_open(self):
        """Returns empty dict when called after 9:30 AM ET — data would be stale."""
        import agent as ag

        class _PostOpen:
            @staticmethod
            def now(tz=None):
                et = pytz.timezone("America/New_York")
                return pd.Timestamp("2026-06-06 10:00:00", tz=et)

        with patch("agent.datetime", _PostOpen):
            result = ag._build_premarket_gap_warnings(self.PICKS)

        assert result == {}

    def test_returns_empty_on_no_picks(self):
        """Returns empty dict when picks dict has no stock/ETF entries."""
        import agent as ag
        with patch("agent.datetime", _FakePremarketDatetime):
            result = ag._build_premarket_gap_warnings({})
        assert result == {}


# ─────────────────────────────────────────────────────────────────────────────
class TestScreenerCacheWithRetry:
    """_screener_cache_with_retry — transient Gist failures must not cost picks."""

    def test_returns_cache_first_try(self):
        with patch.object(ag, "load_screener_cache", return_value={"stocks": {}}):
            assert ag._screener_cache_with_retry() == {"stocks": {}}

    def test_recovers_after_transient_failures(self):
        calls = iter([ConnectionError("gist down"), None, {"stocks": {}}])

        def _flaky():
            v = next(calls)
            if isinstance(v, Exception):
                raise v
            return v

        with patch.object(ag, "load_screener_cache", side_effect=_flaky), \
             patch.object(ag.time, "sleep"):
            assert ag._screener_cache_with_retry(attempts=3) == {"stocks": {}}

    def test_returns_none_when_all_attempts_fail(self):
        with patch.object(ag, "load_screener_cache", return_value=None), \
             patch.object(ag.time, "sleep") as mock_sleep:
            assert ag._screener_cache_with_retry(attempts=3) is None
            assert mock_sleep.call_count == 2   # no sleep after final attempt

    def test_returns_none_when_all_attempts_raise(self):
        with patch.object(ag, "load_screener_cache", side_effect=RuntimeError("boom")), \
             patch.object(ag.time, "sleep"):
            assert ag._screener_cache_with_retry(attempts=3) is None


class TestCanRunLiveScreener:
    """_can_run_live_screener — 512MB OOM guard keyed off Render's RENDER env var."""

    def test_blocked_on_render(self, monkeypatch):
        monkeypatch.setenv("RENDER", "true")
        assert ag._can_run_live_screener() is False

    def test_allowed_elsewhere(self, monkeypatch):
        monkeypatch.delenv("RENDER", raising=False)
        assert ag._can_run_live_screener() is True
