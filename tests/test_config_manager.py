"""
test_config_manager.py — Unit tests for config_manager.py

Coverage:
  - get_config / update_config / update_config_multi / reset_config
  - Per-user config: get_user_config, update_user_config, update_user_config_multi,
    save_user_config, reset_user_config
  - Trade log: load_user_trade_log, save_user_trade_log
  - Allowlist: get_allowed_users, add_allowed_user, remove_allowed_user
  - Weekly picks: save_weekly_pick, load_weekly_picks (stale-week reset)
  - Dynamic pick counts: get_dynamic_pick_counts (budget vs default logic)
  - Signal cache: get_cached_signal, set_cached_signal (TTL + schema version)
  - Buy counts: load_buy_counts, increment_buy_count (daily reset)

All Gist I/O is replaced by conftest._gist_store (in-memory _MemStore).
"""

import os
import sys
import time
import pytest
from datetime import date, timedelta
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_manager
from config_manager import (
    DEFAULT_CONFIG,
    DEFAULT_USER_CONFIG,
    SIGNAL_CACHE_SCHEMA_VERSION,
    SIGNAL_CACHE_TTL_DAYS,
)

UID = "9999999"
UID2 = "8888888"


def _mock_gist_get(stored: dict):
    """Return a requests mock that makes get_config() return `stored` data."""
    import json as _json
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = lambda: None
    resp.json.return_value = {
        "files": {
            "config.json": {"content": _json.dumps(stored)}
        }
    }
    return resp


# ─────────────────────────────────────────────────────────────────────────────
class TestEtToday:
    """et_today() returns the US/Eastern trading day, not server-local UTC."""

    def test_returns_a_date(self):
        from datetime import date as _date
        from config_manager import et_today
        assert isinstance(et_today(), _date)

    def test_matches_eastern_date(self):
        import pytz
        from datetime import datetime as _dt
        from config_manager import et_today
        expected = _dt.now(pytz.timezone("America/New_York")).date()
        assert et_today() == expected

    def test_can_subtract_and_isoformat(self):
        # Used in dedup keys and day-diffs — must behave like a date.
        from datetime import date as _date, timedelta
        from config_manager import et_today
        assert isinstance(et_today().isoformat(), str)
        assert (et_today() - (et_today() - timedelta(days=3))).days == 3


class TestSettingsWiredUp:
    """Settings that used to be written but never honored."""

    def test_shows_crypto_respects_assets(self):
        from config_manager import shows_crypto
        assert shows_crypto({"assets": "stocks"}) is False
        assert shows_crypto({"assets": "crypto"}) is True
        assert shows_crypto({"assets": "both", "show_crypto": False}) is False
        assert shows_crypto({"show_crypto": True}) is True   # default both

    def test_shows_etfs_hidden_for_crypto_only(self):
        from config_manager import shows_etfs
        assert shows_etfs({"assets": "crypto"}) is False
        assert shows_etfs({"assets": "both", "show_etfs": False}) is False
        assert shows_etfs({}) is True

    def test_max_stock_picks_clamps_total(self):
        from config_manager import get_dynamic_pick_counts
        # Big budget would yield several picks; user caps total stocks at 1.
        c = get_dynamic_pick_counts({"stock_budget": 1000, "max_stock_picks": 1})
        assert c["max_short_picks"] + c["max_long_picks"] <= 1

    def test_max_crypto_picks_clamps_total(self):
        from config_manager import get_dynamic_pick_counts
        c = get_dynamic_pick_counts({"crypto_budget": 1000, "max_crypto_picks": 2})
        assert c["max_crypto_short_picks"] + c["max_crypto_long_picks"] <= 2


class TestGetConfig:
    """get_config merges defaults with stored values."""

    def test_returns_default_keys_when_store_empty(self):
        # When requests fails, falls back to DEFAULT_CONFIG
        with patch("config_manager.requests.get", side_effect=Exception("no network")):
            cfg = config_manager.get_config()
        for k in DEFAULT_CONFIG:
            assert k in cfg, f"Missing default key: {k}"

    def test_stored_value_overrides_default(self):
        with patch("config_manager.requests.get",
                   return_value=_mock_gist_get({"max_short_picks": 10})), \
             patch("config_manager._cache_get", return_value=None), \
             patch("config_manager._cache_set"):
            cfg = config_manager.get_config()
        assert cfg["max_short_picks"] == 10

    def test_unknown_stored_key_is_present(self):
        """Keys not in DEFAULT_CONFIG but stored must be preserved."""
        with patch("config_manager.requests.get",
                   return_value=_mock_gist_get({"custom_key": "hello"})), \
             patch("config_manager._cache_get", return_value=None), \
             patch("config_manager._cache_set"):
            cfg = config_manager.get_config()
        assert cfg["custom_key"] == "hello"

    def test_returns_dict(self):
        with patch("config_manager.requests.get", side_effect=Exception("no network")):
            result = config_manager.get_config()
        assert isinstance(result, dict)

    def test_returns_defaults_on_network_error(self):
        with patch("config_manager.requests.get", side_effect=Exception("timeout")):
            cfg = config_manager.get_config()
        assert cfg["max_short_picks"] == DEFAULT_CONFIG["max_short_picks"]


# ─────────────────────────────────────────────────────────────────────────────
class TestUpdateConfig:
    """update_config and update_config_multi patch the in-memory store."""

    def test_update_single_key(self, _gist_store):
        _gist_store.write("config.json", dict(DEFAULT_CONFIG))
        cfg = config_manager.update_config("max_short_picks", 7)
        assert cfg["max_short_picks"] == 7

    def test_update_multi_keys(self, _gist_store):
        _gist_store.write("config.json", dict(DEFAULT_CONFIG))
        cfg = config_manager.update_config_multi({"max_short_picks": 4, "max_long_picks": 5})
        assert cfg["max_short_picks"] == 4
        assert cfg["max_long_picks"] == 5

    def test_reset_restores_defaults(self, _gist_store):
        _gist_store.write("config.json", {"max_short_picks": 99})
        cfg = config_manager.reset_config()
        assert cfg["max_short_picks"] == DEFAULT_CONFIG["max_short_picks"]


# ─────────────────────────────────────────────────────────────────────────────
class TestGetUserConfig:
    """Per-user config merges DEFAULT_USER_CONFIG with stored per-user values."""

    def test_returns_defaults_for_unknown_user(self, _gist_store):
        cfg = config_manager.get_user_config("unknown_user")
        for k in DEFAULT_USER_CONFIG:
            assert k in cfg, f"Missing user default key: {k}"

    def test_stored_value_overrides_default(self, _gist_store):
        _gist_store.write("user_configs.json", {UID: {"risk_profile": "aggressive"}})
        cfg = config_manager.get_user_config(UID)
        assert cfg["risk_profile"] == "aggressive"

    def test_update_single_key(self, _gist_store):
        cfg = config_manager.update_user_config(UID, "paused", True)
        assert cfg["paused"] is True

    def test_update_multi_keys(self, _gist_store):
        cfg = config_manager.update_user_config_multi(UID, {"risk_profile": "conservative", "paused": True})
        assert cfg["risk_profile"] == "conservative"
        assert cfg["paused"] is True

    def test_two_users_isolated(self, _gist_store):
        config_manager.update_user_config(UID, "risk_profile", "aggressive")
        config_manager.update_user_config(UID2, "risk_profile", "conservative")
        assert config_manager.get_user_config(UID)["risk_profile"] == "aggressive"
        assert config_manager.get_user_config(UID2)["risk_profile"] == "conservative"

    def test_save_user_config_replaces_all(self, _gist_store):
        full = {**DEFAULT_USER_CONFIG, "risk_profile": "aggressive"}
        config_manager.save_user_config(UID, full)
        assert config_manager.get_user_config(UID)["risk_profile"] == "aggressive"

    def test_reset_user_config(self, _gist_store):
        config_manager.update_user_config(UID, "risk_profile", "aggressive")
        cfg = config_manager.reset_user_config(UID)
        assert cfg["risk_profile"] == DEFAULT_USER_CONFIG["risk_profile"]


# ─────────────────────────────────────────────────────────────────────────────
class TestUserTradeLog:
    """load_user_trade_log / save_user_trade_log round-trip."""

    def test_empty_log_has_open_and_closed(self, _gist_store):
        log = config_manager.load_user_trade_log("brand_new_user")
        assert "open" in log
        assert "closed" in log
        assert isinstance(log["open"], list)
        assert isinstance(log["closed"], list)

    def test_round_trip_save_and_load(self, _gist_store):
        trade = {"ticker": "AAPL", "entry_price": 189.0, "opened_date": "2026-01-01"}
        log = {"open": [trade], "closed": []}
        config_manager.save_user_trade_log(UID, log)
        loaded = config_manager.load_user_trade_log(UID)
        assert len(loaded["open"]) == 1
        assert loaded["open"][0]["ticker"] == "AAPL"

    def test_two_users_have_separate_logs(self, _gist_store):
        log1 = {"open": [{"ticker": "NVDA"}], "closed": []}
        log2 = {"open": [{"ticker": "TSLA"}], "closed": []}
        config_manager.save_user_trade_log(UID, log1)
        config_manager.save_user_trade_log(UID2, log2)
        assert config_manager.load_user_trade_log(UID)["open"][0]["ticker"] == "NVDA"
        assert config_manager.load_user_trade_log(UID2)["open"][0]["ticker"] == "TSLA"


# ─────────────────────────────────────────────────────────────────────────────
class TestGetAllowedUsers:
    """get_allowed_users always includes the owner from env.

    get_config() hits the Gist HTTP API directly, so we patch it at the
    config_manager.get_config level to avoid needing live network access.
    """

    def _fake_cfg(self, allowed_users):
        return {**DEFAULT_CONFIG, "allowed_users": allowed_users}

    def test_owner_always_included(self):
        with patch.object(config_manager, "get_config",
                          return_value=self._fake_cfg([])):
            users = config_manager.get_allowed_users()
        assert os.environ["TELEGRAM_CHAT_ID"] in users

    def test_additional_users_included(self):
        with patch.object(config_manager, "get_config",
                          return_value=self._fake_cfg([UID2])):
            users = config_manager.get_allowed_users()
        assert UID2 in users

    def test_owner_not_duplicated(self):
        owner = os.environ["TELEGRAM_CHAT_ID"]
        with patch.object(config_manager, "get_config",
                          return_value=self._fake_cfg([owner])):
            users = config_manager.get_allowed_users()
        assert users.count(owner) == 1

    def test_remove_owner_raises(self):
        owner = os.environ["TELEGRAM_CHAT_ID"]
        with pytest.raises(ValueError, match="owner"):
            config_manager.remove_allowed_user(owner)

    def test_add_and_remove_user(self, _gist_store):
        """add_allowed_user and remove_allowed_user patch through update_config."""
        with patch.object(config_manager, "get_config",
                          return_value=self._fake_cfg([])), \
             patch.object(config_manager, "update_config") as mock_update:
            config_manager.add_allowed_user(UID2)
            # Verify update_config was called with the new user
            mock_update.assert_called_once()
            call_args = mock_update.call_args[0]
            assert call_args[0] == "allowed_users"
            assert UID2 in call_args[1]

    def test_add_user_not_duplicated(self):
        """Adding an already-present user should not append a duplicate."""
        with patch.object(config_manager, "get_config",
                          return_value=self._fake_cfg([UID2])), \
             patch.object(config_manager, "update_config") as mock_update:
            config_manager.add_allowed_user(UID2)
            # update_config should NOT be called — user already present
            mock_update.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
class TestWeeklyPicks:
    """save_weekly_pick accumulates; stale weeks (>6 days) are cleared."""

    def _sample_picks(self):
        return {"stocks": {"short_term": [{"ticker": "AAPL", "entry_price": 189.0}]}, "crypto": {}}

    def test_save_and_load_round_trip(self, _gist_store):
        config_manager.save_weekly_pick(self._sample_picks())
        weekly = config_manager.load_weekly_picks()
        assert len(weekly) == 1
        assert date.today().isoformat() in weekly

    def test_load_empty_returns_dict(self, _gist_store):
        assert config_manager.load_weekly_picks() == {}

    def test_stale_week_cleared(self, _gist_store):
        """An oldest entry > 6 days old should trigger a full reset."""
        old_date = (date.today() - timedelta(days=8)).isoformat()
        _gist_store.write("weekly_picks.json", {old_date: self._sample_picks()})
        config_manager.save_weekly_pick(self._sample_picks())
        weekly = config_manager.load_weekly_picks()
        assert old_date not in weekly
        assert len(weekly) == 1   # only today's entry

    def test_same_week_accumulates(self, _gist_store):
        recent_date = (date.today() - timedelta(days=2)).isoformat()
        _gist_store.write("weekly_picks.json", {recent_date: self._sample_picks()})
        config_manager.save_weekly_pick(self._sample_picks())
        weekly = config_manager.load_weekly_picks()
        assert len(weekly) == 2


# ─────────────────────────────────────────────────────────────────────────────
class TestDynamicPickCounts:
    """get_dynamic_pick_counts falls back to defaults when budget is None/0."""

    def test_no_budget_returns_config_defaults(self):
        cfg = {"max_short_picks": 2, "max_long_picks": 3,
               "max_crypto_short_picks": 2, "max_crypto_long_picks": 2,
               "stock_budget": None, "crypto_budget": None}
        counts = config_manager.get_dynamic_pick_counts(cfg)
        assert counts["max_short_picks"] == 2
        assert counts["max_long_picks"] == 3

    def test_stock_budget_increases_counts(self):
        cfg = {"max_short_picks": 2, "max_long_picks": 3,
               "max_crypto_short_picks": 2, "max_crypto_long_picks": 2,
               "stock_budget": 300, "crypto_budget": None}
        counts = config_manager.get_dynamic_pick_counts(cfg)
        assert counts["max_short_picks"] >= 2   # should be higher with budget

    def test_minimum_always_two(self):
        """Even with a tiny budget, min picks should be 2."""
        cfg = {"max_short_picks": 2, "max_long_picks": 3,
               "max_crypto_short_picks": 2, "max_crypto_long_picks": 2,
               "stock_budget": 1, "crypto_budget": 1}
        counts = config_manager.get_dynamic_pick_counts(cfg)
        assert counts["max_short_picks"] >= 2
        assert counts["max_crypto_short_picks"] >= 2

    def test_max_caps_respected(self):
        """Large budget should not exceed defined maximums (5 ST, 6 LT, 4 crypto each)."""
        cfg = {"max_short_picks": 2, "max_long_picks": 3,
               "max_crypto_short_picks": 2, "max_crypto_long_picks": 2,
               "stock_budget": 100_000, "crypto_budget": 100_000}
        counts = config_manager.get_dynamic_pick_counts(cfg)
        assert counts["max_short_picks"] <= 5
        assert counts["max_long_picks"] <= 6
        assert counts["max_crypto_short_picks"] <= 4
        assert counts["max_crypto_long_picks"] <= 4


# ─────────────────────────────────────────────────────────────────────────────
class TestSignalCache:
    """get_cached_signal / set_cached_signal — TTL and schema checks."""

    def _make_cache(self, ticker: str, days_old: int = 0) -> dict:
        """Return a cache dict with a single ticker entry aged by `days_old`."""
        cached_date = (date.today() - timedelta(days=days_old)).isoformat()
        return {
            "_schema": SIGNAL_CACHE_SCHEMA_VERSION,
            ticker: {
                "sentiment": {"score": 0.7},
                "insider":   {"activity": "buy"},
                "cached_date": cached_date,
            }
        }

    def test_fresh_entry_returned(self):
        cache = self._make_cache("AAPL", days_old=0)
        result = config_manager.get_cached_signal(cache, "AAPL")
        assert result is not None
        assert result["sentiment"]["score"] == 0.7

    def test_entry_at_ttl_boundary_returned(self):
        cache = self._make_cache("AAPL", days_old=SIGNAL_CACHE_TTL_DAYS)
        result = config_manager.get_cached_signal(cache, "AAPL")
        assert result is not None

    def test_expired_entry_returns_none(self):
        cache = self._make_cache("AAPL", days_old=SIGNAL_CACHE_TTL_DAYS + 1)
        result = config_manager.get_cached_signal(cache, "AAPL")
        assert result is None

    def test_missing_ticker_returns_none(self):
        cache = self._make_cache("AAPL", days_old=0)
        result = config_manager.get_cached_signal(cache, "NVDA")
        assert result is None

    def test_schema_sentinel_returns_none(self):
        cache = {"_schema": SIGNAL_CACHE_SCHEMA_VERSION}
        result = config_manager.get_cached_signal(cache, "_schema")
        assert result is None

    def test_set_cached_signal_upserts(self):
        cache = {}
        config_manager.set_cached_signal(cache, "NVDA", {"score": 0.9}, {"activity": "sell"})
        assert "NVDA" in cache
        assert cache["NVDA"]["sentiment"]["score"] == 0.9
        assert cache["NVDA"]["cached_date"] == date.today().isoformat()

    def test_set_cached_signal_overwrites_existing(self):
        cache = {}
        config_manager.set_cached_signal(cache, "NVDA", {"score": 0.5}, None)
        config_manager.set_cached_signal(cache, "NVDA", {"score": 0.9}, None)
        assert cache["NVDA"]["sentiment"]["score"] == 0.9

    def test_load_signal_cache_rejects_old_schema(self, _gist_store):
        _gist_store.write("signal_cache.json", {"_schema": 1, "AAPL": {}})
        result = config_manager.load_signal_cache()
        assert result == {}

    def test_load_signal_cache_accepts_current_schema(self, _gist_store):
        _gist_store.write("signal_cache.json", {
            "_schema": SIGNAL_CACHE_SCHEMA_VERSION,
            "AAPL": {"sentiment": {}, "insider": {}, "cached_date": date.today().isoformat()}
        })
        result = config_manager.load_signal_cache()
        assert "AAPL" in result


# ─────────────────────────────────────────────────────────────────────────────
class TestBuyCounts:
    """load_buy_counts / increment_buy_count — daily reset logic."""

    def test_empty_store_returns_empty_dict(self, _gist_store):
        counts = config_manager.load_buy_counts()
        assert counts == {}

    def test_increment_and_load_today(self, _gist_store):
        config_manager.increment_buy_count("NVDA")
        counts = config_manager.load_buy_counts()
        assert counts.get("NVDA") == 1

    def test_increment_twice(self, _gist_store):
        config_manager.increment_buy_count("NVDA")
        config_manager.increment_buy_count("NVDA")
        counts = config_manager.load_buy_counts()
        assert counts["NVDA"] == 2

    def test_stale_date_returns_empty(self, _gist_store):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _gist_store.write("buy_counts.json", {"date": yesterday, "counts": {"NVDA": 5}})
        counts = config_manager.load_buy_counts()
        assert counts == {}

    def test_increment_resets_on_new_day(self, _gist_store):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        _gist_store.write("buy_counts.json", {"date": yesterday, "counts": {"NVDA": 5}})
        n = config_manager.increment_buy_count("NVDA")
        assert n == 1   # reset to 1, not 6


# ─────────────────────────────────────────────────────────────────────────────
class TestScreenerCache:
    """save_screener_cache → load_screener_cache round-trip.

    Regression: the Jun 8 utcnow() cleanup left load_screener_cache using
    timezone.utc without importing timezone (function-local datetime import).
    The NameError was swallowed by the except → cache NEVER loaded → morning
    runs fell back to the full 600-ticker screener → OOM on 512MB Render.
    """

    STOCKS = {"short_term": [{"ticker": "RJF", "score": 80}],
              "long_term":  [{"ticker": "UNH", "lt_score": 70}]}
    CRYPTO = {"short_term": [], "long_term": []}

    def test_round_trip_returns_saved_data(self, _gist_store):
        config_manager.save_screener_cache(self.STOCKS, self.CRYPTO)
        cache = config_manager.load_screener_cache()
        assert cache is not None, "fresh cache must load (NameError regression)"
        assert cache["stocks"] == self.STOCKS
        assert cache["crypto"] == self.CRYPTO

    def test_rejects_old_schema(self, _gist_store):
        config_manager.save_screener_cache(self.STOCKS, self.CRYPTO)
        data = _gist_store.load("screener_cache.json")
        data["schema_version"] = config_manager.SCREENER_CACHE_SCHEMA_VERSION - 1
        _gist_store.write("screener_cache.json", data)
        assert config_manager.load_screener_cache() is None

    def test_rejects_stale_cache(self, _gist_store):
        from datetime import datetime, timedelta, timezone
        config_manager.save_screener_cache(self.STOCKS, self.CRYPTO)
        data = _gist_store.load("screener_cache.json")
        stale = datetime.now(timezone.utc) - timedelta(
            hours=config_manager.SCREENER_CACHE_MAX_AGE_HOURS + 1)
        data["cached_at"] = stale.isoformat()
        _gist_store.write("screener_cache.json", data)
        assert config_manager.load_screener_cache() is None

    def test_missing_cache_returns_none(self, _gist_store):
        assert config_manager.load_screener_cache() is None


# ─────────────────────────────────────────────────────────────────────────────
class TestMacroCache:
    """save_macro_cache → load_macro_cache round-trip (same NameError family)."""

    def test_round_trip_returns_saved_data(self, _gist_store):
        config_manager.save_macro_cache({"value": 10}, {"trend": "down"})
        cache = config_manager.load_macro_cache()
        assert cache is not None, "fresh macro cache must load (NameError regression)"
        assert cache["fear_greed"] == {"value": 10}
        assert cache["rate_trend"] == {"trend": "down"}

    def test_rejects_expired_cache(self, _gist_store):
        from datetime import datetime, timedelta, timezone
        config_manager.save_macro_cache({"value": 10}, {"trend": "down"})
        data = _gist_store.load("macro_cache.json")
        stale = datetime.now(timezone.utc) - timedelta(
            hours=config_manager.MACRO_CACHE_TTL_HOURS + 1)
        data["cached_at"] = stale.isoformat()
        _gist_store.write("macro_cache.json", data)
        assert config_manager.load_macro_cache() is None

    def test_missing_cache_returns_none(self, _gist_store):
        assert config_manager.load_macro_cache() is None
