"""
test_trade_logger_unit.py — Unit tests for trade_logger.py

Coverage:
  - open_trades:               logs picks, skips duplicates
  - check_and_close_trades:    closes on stop, target, 28-day expiry; leaves live trades open
  - add_holding:               creates new entry, updates levels on duplicate, adds overrides
  - get_performance_stats:     win-rate, avg return, streak, by_outcome (via trade_logger)

All storage is mocked via conftest._gist_store (in-memory).
"""

import os
import sys
import pytest
from datetime import date, timedelta
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import trade_logger
import config_manager

UID  = "9999999"
UID2 = "8888888"


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _sample_picks(st_tickers=("AAPL",), lt_tickers=()):
    """Return a minimal picks dict for trade_logger.open_trades()."""
    def _s(ticker, tf):
        return {"ticker": ticker, "entry_price": 100.0, "target_price": 120.0,
                "stop_loss": 88.0, "conviction": 3, "thesis": "Test."}

    return {
        "stocks": {
            "short_term": [_s(t, "short_term") for t in st_tickers],
            "long_term":  [_s(t, "long_term")  for t in lt_tickers],
        },
        "crypto": {"short_term": [], "long_term": []},
    }


def _open_trade(ticker, entry=100.0, target=120.0, stop=88.0,
               days_ago=0, asset_type="stock"):
    opened = (date.today() - timedelta(days=days_ago)).isoformat()
    return {
        "ticker":       ticker,
        "asset_type":   asset_type,
        "entry_price":  entry,
        "target_price": target,
        "stop_loss":    stop,
        "allocation":   100.0,   # $100 allocation for easy P&L math
        "conviction":   3,
        "thesis":       "Test.",
        "timeframe":    "short_term",
        "opened_date":  opened,
    }


def _closed_trade(ticker, return_pct, outcome="target", days_ago=1):
    closed_date = (date.today() - timedelta(days=days_ago)).isoformat()
    return {
        "ticker":       ticker,
        "asset_type":   "stock",
        "entry_price":  100.0,
        "target_price": 115.0,
        "stop_loss":    90.0,
        "allocation":   100.0,
        "conviction":   3,
        "timeframe":    "short_term",
        "opened_date":  (date.today() - timedelta(days=days_ago + 3)).isoformat(),
        "closed_date":  closed_date,
        "closed_price": 100.0 * (1 + return_pct / 100),
        "outcome":      outcome,
        "return_pct":   return_pct,
        "gain_usd":     round(100.0 * return_pct / 100, 2),
    }


# ─────────────────────────────────────────────────────────────────────────────
class TestOpenTrades:
    """open_trades logs picks and skips duplicates."""

    def test_logs_st_pick(self, _gist_store):
        trade_logger.open_trades(_sample_picks(st_tickers=("AAPL",)), UID)
        log = config_manager.load_user_trade_log(UID)
        tickers = {t["ticker"] for t in log["open"]}
        assert "AAPL" in tickers

    def test_logs_lt_pick(self, _gist_store):
        trade_logger.open_trades(_sample_picks(lt_tickers=("NVDA",)), UID)
        log = config_manager.load_user_trade_log(UID)
        tickers = {t["ticker"] for t in log["open"]}
        assert "NVDA" in tickers

    def test_skips_duplicate_ticker(self, _gist_store):
        trade_logger.open_trades(_sample_picks(st_tickers=("AAPL",)), UID)
        trade_logger.open_trades(_sample_picks(st_tickers=("AAPL",)), UID)
        log = config_manager.load_user_trade_log(UID)
        count = sum(1 for t in log["open"] if t["ticker"] == "AAPL")
        assert count == 1

    def test_multiple_tickers_all_logged(self, _gist_store):
        trade_logger.open_trades(_sample_picks(st_tickers=("AAPL", "MSFT", "NVDA")), UID)
        log = config_manager.load_user_trade_log(UID)
        tickers = {t["ticker"] for t in log["open"]}
        assert {"AAPL", "MSFT", "NVDA"}.issubset(tickers)

    def test_logs_crypto_pick(self, _gist_store):
        picks = _sample_picks()
        picks["crypto"] = {
            "short_term": [{"symbol": "BTC", "entry_price": 65000.0,
                            "target_price": 72000.0, "stop_loss": 60000.0,
                            "conviction": 3, "thesis": ""}],
            "long_term": []
        }
        trade_logger.open_trades(picks, UID)
        log = config_manager.load_user_trade_log(UID)
        tickers = {t["ticker"] for t in log["open"]}
        assert "BTC" in tickers

    def test_no_error_on_empty_picks(self, _gist_store):
        empty = {"stocks": {"short_term": [], "long_term": []},
                 "crypto": {"short_term": [], "long_term": []}}
        trade_logger.open_trades(empty, UID)   # should not raise
        log = config_manager.load_user_trade_log(UID)
        assert log["open"] == []

    def test_trade_has_opened_date(self, _gist_store):
        trade_logger.open_trades(_sample_picks(st_tickers=("AAPL",)), UID)
        log = config_manager.load_user_trade_log(UID)
        assert log["open"][0]["opened_date"] == date.today().isoformat()

    def test_users_isolated(self, _gist_store):
        trade_logger.open_trades(_sample_picks(st_tickers=("AAPL",)), UID)
        trade_logger.open_trades(_sample_picks(st_tickers=("TSLA",)), UID2)
        log1 = config_manager.load_user_trade_log(UID)
        log2 = config_manager.load_user_trade_log(UID2)
        tickers1 = {t["ticker"] for t in log1["open"]}
        tickers2 = {t["ticker"] for t in log2["open"]}
        assert "AAPL" in tickers1 and "TSLA" not in tickers1
        assert "TSLA" in tickers2 and "AAPL" not in tickers2


# ─────────────────────────────────────────────────────────────────────────────
class TestCheckAndCloseTrades:
    """check_and_close_trades closes on stop/target/expiry, keeps live trades open."""

    def _seed(self, trades: list):
        """Seed the in-memory store with pre-built open trades."""
        log = {"open": trades, "closed": []}
        config_manager.save_user_trade_log(UID, log)

    def test_target_hit_closes_trade(self, _gist_store):
        self._seed([_open_trade("AAPL", entry=100, target=120, stop=88)])
        closed = trade_logger.check_and_close_trades({"AAPL": 125.0}, UID)
        assert len(closed) == 1
        assert closed[0]["outcome"] == "target"
        assert closed[0]["ticker"] == "AAPL"

    def test_stop_hit_closes_trade(self, _gist_store):
        self._seed([_open_trade("AAPL", entry=100, target=120, stop=88)])
        closed = trade_logger.check_and_close_trades({"AAPL": 85.0}, UID)
        assert len(closed) == 1
        assert closed[0]["outcome"] == "stop"

    def test_stop_takes_priority_over_target(self, _gist_store):
        """If somehow both would trigger, stop wins (stop checked first)."""
        # Current price below stop — must close as stop
        self._seed([_open_trade("AAPL", entry=100, target=80, stop=88)])
        closed = trade_logger.check_and_close_trades({"AAPL": 75.0}, UID)
        assert closed[0]["outcome"] == "stop"

    def test_live_trade_stays_open(self, _gist_store):
        self._seed([_open_trade("AAPL", entry=100, target=120, stop=88)])
        closed = trade_logger.check_and_close_trades({"AAPL": 105.0}, UID)
        assert closed == []
        log = config_manager.load_user_trade_log(UID)
        assert len(log["open"]) == 1

    def test_expired_after_28_days(self, _gist_store):
        self._seed([_open_trade("AAPL", entry=100, target=120, stop=88, days_ago=28)])
        closed = trade_logger.check_and_close_trades({"AAPL": 105.0}, UID)
        assert len(closed) == 1
        assert closed[0]["outcome"] == "expired"

    def test_not_expired_at_27_days(self, _gist_store):
        self._seed([_open_trade("AAPL", entry=100, target=120, stop=88, days_ago=27)])
        closed = trade_logger.check_and_close_trades({"AAPL": 105.0}, UID)
        assert closed == []

    def test_missing_price_skipped(self, _gist_store):
        """Tickers not in current_prices dict should remain open."""
        self._seed([_open_trade("AAPL")])
        closed = trade_logger.check_and_close_trades({}, UID)
        assert closed == []
        log = config_manager.load_user_trade_log(UID)
        assert len(log["open"]) == 1

    def test_return_pct_calculated_correctly(self, _gist_store):
        self._seed([_open_trade("AAPL", entry=100)])
        trade_logger.check_and_close_trades({"AAPL": 120.0}, UID)  # target hit
        log = config_manager.load_user_trade_log(UID)
        closed = log["closed"]
        assert len(closed) == 1
        assert abs(closed[0]["return_pct"] - 20.0) < 0.01

    def test_closed_trade_moved_from_open(self, _gist_store):
        self._seed([_open_trade("AAPL")])
        trade_logger.check_and_close_trades({"AAPL": 130.0}, UID)  # above target
        log = config_manager.load_user_trade_log(UID)
        assert len(log["open"]) == 0
        assert len(log["closed"]) == 1

    def test_partial_closure_one_of_two(self, _gist_store):
        """If only one trade hits target, the other stays open."""
        self._seed([
            _open_trade("AAPL", entry=100, target=120, stop=88),
            _open_trade("MSFT", entry=200, target=220, stop=185),
        ])
        # AAPL hits target, MSFT doesn't
        closed = trade_logger.check_and_close_trades({"AAPL": 125.0, "MSFT": 210.0}, UID)
        assert len(closed) == 1
        assert closed[0]["ticker"] == "AAPL"
        log = config_manager.load_user_trade_log(UID)
        assert len(log["open"]) == 1
        assert log["open"][0]["ticker"] == "MSFT"


# ─────────────────────────────────────────────────────────────────────────────
class TestGetPerformanceStats:
    """get_performance_stats computes win rate, streak, best/worst, etc."""

    def _seed_closed(self, trades: list):
        log = {"open": [], "closed": trades}
        config_manager.save_user_trade_log(UID, log)

    def test_returns_none_when_no_closed_trades(self, _gist_store):
        result = trade_logger.get_performance_stats(UID)
        assert result is None

    def test_win_rate_all_winners(self, _gist_store):
        self._seed_closed([
            _closed_trade("AAPL", 10.0),
            _closed_trade("NVDA", 15.0),
        ])
        stats = trade_logger.get_performance_stats(UID)
        assert stats["win_rate"] == 100

    def test_win_rate_all_losers(self, _gist_store):
        self._seed_closed([
            _closed_trade("AAPL", -5.0, outcome="stop"),
            _closed_trade("NVDA", -8.0, outcome="stop"),
        ])
        stats = trade_logger.get_performance_stats(UID)
        assert stats["win_rate"] == 0

    def test_win_rate_mixed(self, _gist_store):
        self._seed_closed([
            _closed_trade("AAPL", 10.0),
            _closed_trade("NVDA", -5.0, outcome="stop"),
        ])
        stats = trade_logger.get_performance_stats(UID)
        assert stats["win_rate"] == 50

    def test_best_and_worst_picks(self, _gist_store):
        self._seed_closed([
            _closed_trade("AAPL", 20.0),
            _closed_trade("NVDA", -8.0, outcome="stop"),
            _closed_trade("MSFT", 5.0),
        ])
        stats = trade_logger.get_performance_stats(UID)
        assert stats["best"][0] == "AAPL"
        assert stats["worst"][0] == "NVDA"

    def test_streak_consecutive_wins(self, _gist_store):
        """Streak counts consecutive wins from the most recent trade backward."""
        self._seed_closed([
            _closed_trade("AAPL", -5.0, outcome="stop", days_ago=5),  # oldest
            _closed_trade("NVDA", 10.0, days_ago=3),
            _closed_trade("MSFT", 8.0,  days_ago=1),   # most recent
        ])
        stats = trade_logger.get_performance_stats(UID)
        assert stats["streak"] == 2   # MSFT + NVDA

    def test_streak_broken_by_loss(self, _gist_store):
        self._seed_closed([
            _closed_trade("AAPL", 5.0,   days_ago=2),
            _closed_trade("NVDA", -3.0, outcome="stop", days_ago=1),
        ])
        stats = trade_logger.get_performance_stats(UID)
        assert stats["streak"] == 0

    def test_count_matches_closed_trades(self, _gist_store):
        trades = [_closed_trade(f"T{i}", 5.0) for i in range(5)]
        self._seed_closed(trades)
        stats = trade_logger.get_performance_stats(UID)
        assert stats["count"] == 5

    def test_by_outcome_targets_and_stops(self, _gist_store):
        self._seed_closed([
            _closed_trade("A", 10.0, outcome="target"),
            _closed_trade("B", -5.0, outcome="stop"),
            _closed_trade("C", -1.0, outcome="expired"),
        ])
        stats = trade_logger.get_performance_stats(UID)
        assert stats["targets_hit"] == 1
        assert stats["stops_hit"] == 1
        assert stats["expired"] == 1


# ─────────────────────────────────────────────────────────────────────────────
class TestAddHolding:
    """add_holding creates entries and handles overrides."""

    def test_new_ticker_added(self, _gist_store):
        trade, existed = trade_logger.add_holding("AAPL", UID)
        assert trade["ticker"] == "AAPL"
        assert existed is False
        log = config_manager.load_user_trade_log(UID)
        assert any(t["ticker"] == "AAPL" for t in log["open"])

    def test_duplicate_returns_existed_true(self, _gist_store):
        trade_logger.add_holding("AAPL", UID)
        _, existed = trade_logger.add_holding("AAPL", UID)
        assert existed is True

    def test_duplicate_not_added_twice(self, _gist_store):
        trade_logger.add_holding("AAPL", UID)
        trade_logger.add_holding("AAPL", UID)
        log = config_manager.load_user_trade_log(UID)
        count = sum(1 for t in log["open"] if t["ticker"] == "AAPL")
        assert count == 1

    def test_entry_override_applied(self, _gist_store):
        trade, _ = trade_logger.add_holding("AAPL", UID, entry_override=150.0)
        assert trade["entry_price"] == 150.0

    def test_stop_override_applied(self, _gist_store):
        trade_logger.add_holding("AAPL", UID)
        trade, _ = trade_logger.add_holding("AAPL", UID, stop_override=140.0)
        assert trade["stop_loss"] == 140.0

    def test_ticker_uppercased(self, _gist_store):
        trade, _ = trade_logger.add_holding("aapl", UID)
        assert trade["ticker"] == "AAPL"

    def test_picks_levels_used_when_available(self, _gist_store):
        picks = {
            "stocks": {
                "short_term": [{
                    "ticker": "NVDA", "entry_price": 875.0,
                    "target_price": 950.0, "stop_loss": 840.0,
                    "conviction": 5, "thesis": "AI demand."
                }],
                "long_term": [],
            },
            "crypto": {"short_term": [], "long_term": []},
        }
        trade, existed = trade_logger.add_holding("NVDA", UID, picks=picks)
        assert not existed
        assert trade["entry_price"] == 875.0
        assert trade["target_price"] == 950.0
        assert trade["stop_loss"] == 840.0
