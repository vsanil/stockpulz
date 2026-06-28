"""
test_commands.py — Tests for the mini-app command dispatcher and recent bug fixes.

Covers
------
  TestRunCommand          — /api/miniapp/run_command (auth, dispatch, edge cases)
  TestRunCommandQuick     — Commands that return synchronous replies (/stats, /history…)
  TestSettingsDisplayName — display_name allowlist fix (was failing "Could not save name")
  TestLogBoughtTimeframe  — timeframe field stored in log_bought (LT/ST filter fix)
  TestPositionsTimeframe  — LT/ST portfolio filter: normalised timeframe values visible
  TestTradeLoggerUnit     — trade_logger.add_holding() timeframe_override parameter
  TestPricesEndpoint      — /api/miniapp/live_prices returns picks list (powers /prices card view)
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from tests.conftest import get, post, TEST_CHAT_ID, FAKE_PRICES


# ══════════════════════════════════════════════════════════════════════════════
# RUN COMMAND — auth + request validation
# ══════════════════════════════════════════════════════════════════════════════

class TestRunCommand:
    """run_command endpoint handles auth and bad inputs before dispatching."""

    def test_run_command_requires_auth(self, client):
        r = client.post("/api/miniapp/run_command", json={"command": "/stats"})
        assert r.status_code == 403

    def test_run_command_missing_command_returns_400(self, client):
        r = post(client, "/api/miniapp/run_command", {})
        assert r.status_code == 400
        assert "command" in r.get_json().get("error", "")

    def test_run_command_empty_command_returns_400(self, client):
        r = post(client, "/api/miniapp/run_command", {"command": ""})
        assert r.status_code == 400

    def test_run_command_prepends_slash_when_missing(self, client, monkeypatch):
        """Command without leading slash should be handled — slash auto-prepended."""
        calls = []
        def _fake_handle(cmd, chat_id=None):
            calls.append(cmd)
            return f"Handled: {cmd}"

        # Must patch webhook's own reference — it was imported with `from … import`
        monkeypatch.setattr("webhook.handle_incoming_command", _fake_handle)
        r = post(client, "/api/miniapp/run_command", {"command": "stats"})
        assert r.status_code == 200
        # The command must have been normalised to /stats
        assert any(c.startswith("/") for c in calls), f"Expected /stats, got {calls}"

    def test_run_command_returns_reply_key(self, client, monkeypatch):
        monkeypatch.setattr(
            "webhook.handle_incoming_command",
            lambda cmd, chat_id=None: "📊 No closed trades yet.",
        )
        r = post(client, "/api/miniapp/run_command", {"command": "/stats"})
        data = r.get_json()
        assert data.get("ok") is True
        assert "reply" in data
        assert isinstance(data["reply"], str)

    def test_run_command_error_in_handler_returns_500(self, client, monkeypatch):
        def _boom(cmd, chat_id=None):
            raise RuntimeError("Something exploded")

        monkeypatch.setattr("webhook.handle_incoming_command", _boom)
        r = post(client, "/api/miniapp/run_command", {"command": "/stats"})
        assert r.status_code == 500

    def test_run_command_none_reply_returns_empty_string(self, client, monkeypatch):
        """handle_incoming_command returning None must not cause KeyError."""
        monkeypatch.setattr(
            "webhook.handle_incoming_command",
            lambda cmd, chat_id=None: None,
        )
        r = post(client, "/api/miniapp/run_command", {"command": "/history"})
        data = r.get_json()
        assert data.get("ok") is True
        assert data.get("reply") == ""  # coerced from None


# ══════════════════════════════════════════════════════════════════════════════
# RUN COMMAND — quick synchronous commands (mocked handler)
# ══════════════════════════════════════════════════════════════════════════════

# Commands that should complete fast with no external API calls
_QUICK_CMDS = [
    "/stats",
    "/history",
    "/positions",
    "/accuracy",
    "/streak",
    "/week",
    "/top",
    "/journal",
]


class TestRunCommandQuick:
    """Each quick command dispatches and returns a reply (not async)."""

    @pytest.mark.parametrize("cmd", _QUICK_CMDS)
    def test_quick_command_returns_ok_and_reply(self, client, monkeypatch, cmd):
        # Patch webhook's reference (imported with `from … import`)
        monkeypatch.setattr(
            "webhook.handle_incoming_command",
            lambda c, chat_id=None: f"📊 {c} output",
        )
        r = post(client, "/api/miniapp/run_command", {"command": cmd})
        data = r.get_json()
        assert r.status_code == 200, f"{cmd} returned {r.status_code}"
        assert data.get("ok") is True, f"{cmd} ok=False: {data}"
        assert "reply" in data, f"{cmd} missing reply key: {data}"

    def test_prices_command_fallback_via_run_command(self, client, monkeypatch):
        """
        /prices is intercepted client-side, but if run_command is called with it
        directly it should still return 200 (not 500).
        """
        monkeypatch.setattr(
            "webhook.handle_incoming_command",
            lambda c, chat_id=None: "Live prices…",
        )
        r = post(client, "/api/miniapp/run_command", {"command": "/prices"})
        assert r.status_code == 200

    def test_async_timeout_returns_async_flag(self, client, monkeypatch):
        """
        When handle_incoming_command exceeds the 22s join timeout the endpoint
        must return async=True rather than crashing.
        We simulate this by making the thread appear still-alive after join().
        """
        import threading as _threading

        class _SlowThread(_threading.Thread):
            def join(self, timeout=None):
                # Don't actually wait — simulate timeout by doing nothing
                # is_alive() returns True if start() was called but run()
                # hasn't finished; we override is_alive() to fake it.
                pass

            def is_alive(self):
                return True  # pretend it timed out

        original_thread = _threading.Thread
        monkeypatch.setattr(
            "webhook.handle_incoming_command",
            lambda c, chat_id=None: "slow result",
        )
        # Patch threading.Thread used inside miniapp_run_command
        import webhook
        monkeypatch.setattr(webhook.threading, "Thread", _SlowThread)

        r = post(client, "/api/miniapp/run_command", {"command": "/today"})
        data = r.get_json()
        assert r.status_code == 200
        assert data.get("async") is True, f"Expected async=True, got: {data}"


# ══════════════════════════════════════════════════════════════════════════════
# BUG FIX: display_name saves correctly via settings/update
# ══════════════════════════════════════════════════════════════════════════════

class TestSettingsDisplayName:
    """
    Regression: before fix, saving display_name returned "key 'display_name' not
    allowed" because it was missing from the allowlist in miniapp_settings_update().
    """

    def test_display_name_is_in_allowlist(self, client):
        r = post(client, "/api/miniapp/settings/update",
                 {"key": "display_name", "value": "Vsanil"})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.get_json()}"
        data = r.get_json()
        assert data.get("ok") is True, f"display_name rejected: {data}"

    def test_display_name_save_returns_correct_value(self, client):
        """settings/update returns the saved key+value in response body."""
        r = post(client, "/api/miniapp/settings/update",
                 {"key": "display_name", "value": "TestUser"})
        data = r.get_json()
        assert data.get("ok") is True
        assert data.get("value") == "TestUser"
        assert data.get("key") == "display_name"

    def test_display_name_empty_string_allowed(self, client):
        r = post(client, "/api/miniapp/settings/update",
                 {"key": "display_name", "value": ""})
        assert r.status_code == 200

    def test_unknown_key_still_rejected(self, client):
        r = post(client, "/api/miniapp/settings/update",
                 {"key": "hacked_key", "value": "evil"})
        assert r.status_code == 400

    def test_notif_keys_still_work(self, client):
        """Allowlist additions must not break existing allowed keys."""
        for key in ("notif_target_hit", "notif_weekly_recap", "auto_stop_alerts"):
            r = post(client, "/api/miniapp/settings/update",
                     {"key": key, "value": False})
            assert r.status_code == 200, f"Existing key {key!r} rejected"


class TestUpdateSettingsParse:
    """
    /update_settings used float() + bare-except, so "$1.5k"/"5%" silently dropped
    while the response still said ok:true. Now parse_money handles them and any
    unparseable field is reported.
    """

    def test_money_string_with_symbols_saves(self, client):
        from config_manager import get_user_config
        from tests.conftest import TEST_CHAT_ID
        r = post(client, "/api/miniapp/update_settings", {"stock_budget": "$1.5k"})
        assert r.status_code == 200 and r.get_json().get("ok") is True
        assert get_user_config(TEST_CHAT_ID)["stock_budget"] == 1500.0

    def test_percent_string_saves(self, client):
        from config_manager import get_user_config
        from tests.conftest import TEST_CHAT_ID
        r = post(client, "/api/miniapp/update_settings", {"stop_loss_pct": "8%"})
        assert r.status_code == 200
        assert get_user_config(TEST_CHAT_ID)["stop_loss_pct"] == 8.0

    def test_unparseable_value_reported_not_silently_dropped(self, client):
        r = post(client, "/api/miniapp/update_settings", {"crypto_budget": "abc"})
        assert r.status_code == 200
        assert "crypto_budget" in (r.get_json().get("warning") or "")


# ══════════════════════════════════════════════════════════════════════════════
# BUG FIX: log_bought stores timeframe so LT/ST portfolio filter works
# ══════════════════════════════════════════════════════════════════════════════

class TestLogBoughtTimeframe:
    """
    Regression: before fix, confirmTradeLog() never sent timeframe to the API,
    so all holdings had timeframe=None and LT/ST filter buttons showed empty.
    """

    def test_log_bought_with_timeframe_long_term(self, client):
        r = post(client, "/api/miniapp/log_bought", {
            "ticker": "AAPL",
            "entry_price": 189.50,
            "timeframe": "long_term",
        })
        assert r.status_code == 200
        data = r.get_json()
        assert "trade" in data or data.get("ok") is True

    def test_log_bought_with_timeframe_short_term(self, client):
        r = post(client, "/api/miniapp/log_bought", {
            "ticker": "NVDA",
            "entry_price": 875.0,
            "timeframe": "short_term",
        })
        assert r.status_code == 200

    def test_log_bought_timeframe_persists_in_open_trades(self, client):
        """Timeframe stored in trade must be retrievable via positions endpoint."""
        post(client, "/api/miniapp/log_bought", {
            "ticker": "MSFT",
            "entry_price": 415.0,
            "timeframe": "long_term",
        })
        positions = get(client, "/api/miniapp/positions").get_json()["positions"]
        msft = next((p for p in positions if p["ticker"] == "MSFT"), None)
        assert msft is not None, "MSFT not found in positions"
        # Timeframe should be stored (either 'long_term' or legacy 'LT' is fine)
        tf = msft.get("timeframe", "")
        assert tf in ("long_term", "LT"), f"Unexpected timeframe: {tf!r}"

    def test_log_bought_without_timeframe_still_works(self, client):
        """Backward-compat: log_bought without timeframe should not error."""
        r = post(client, "/api/miniapp/log_bought", {
            "ticker": "TSLA",
            "entry_price": 172.0,
        })
        assert r.status_code == 200

    def test_log_bought_asset_type_crypto_stored(self, client):
        r = post(client, "/api/miniapp/log_bought", {
            "ticker": "BTC",
            "asset_type": "crypto",
            "entry_price": 68000.0,
            "timeframe": "long_term",
        })
        assert r.status_code == 200

    def test_log_bought_short_term_timeframe_alias(self, client):
        """Frontend may send 'ST' (legacy) — should not break storage."""
        r = post(client, "/api/miniapp/log_bought", {
            "ticker": "AAPL",
            "entry_price": 190.0,
            "timeframe": "ST",
        })
        # endpoint should accept and not 500
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# BUG FIX: Positions list returns timeframe so filter buttons work
# ══════════════════════════════════════════════════════════════════════════════

class TestPositionsTimeframe:
    """Positions endpoint returns timeframe field so frontend LT/ST filter works."""

    def _add(self, client, ticker, timeframe):
        post(client, "/api/miniapp/log_bought", {
            "ticker": ticker,
            "entry_price": FAKE_PRICES.get(ticker, 100.0),
            "timeframe": timeframe,
        })

    def test_positions_have_timeframe_key(self, client):
        self._add(client, "AAPL", "short_term")
        positions = get(client, "/api/miniapp/positions").get_json()["positions"]
        assert len(positions) > 0
        for p in positions:
            assert "timeframe" in p, f"Position {p['ticker']} missing timeframe key"

    def test_lt_positions_distinguishable_from_st(self, client):
        self._add(client, "AAPL", "short_term")
        self._add(client, "MSFT", "long_term")
        positions = get(client, "/api/miniapp/positions").get_json()["positions"]

        tfs = {p["ticker"]: p.get("timeframe") for p in positions}
        # AAPL should be short_term (or legacy 'ST')
        assert tfs.get("AAPL") in ("short_term", "ST"), f"AAPL: {tfs.get('AAPL')}"
        # MSFT should be long_term (or legacy 'LT')
        assert tfs.get("MSFT") in ("long_term", "LT"), f"MSFT: {tfs.get('MSFT')}"


# ══════════════════════════════════════════════════════════════════════════════
# TRADE LOGGER UNIT TESTS
# ══════════════════════════════════════════════════════════════════════════════

class TestTradeLoggerUnit:
    """Unit tests for trade_logger.add_holding() and open_trades()."""

    @pytest.fixture(autouse=True)
    def _setup(self, _gist_store):
        """Seed an allowed user so config_manager works."""
        yield

    def _chat(self):
        return TEST_CHAT_ID

    def test_add_holding_stores_timeframe_override(self):
        """timeframe_override should be stored in the trade dict."""
        from trade_logger import add_holding, remove_holding
        from config_manager import load_user_trade_log

        chat = self._chat()
        trade, existed = add_holding(
            "AAPL", chat,
            entry_override=190.0,
            timeframe_override="long_term",
        )
        assert not existed
        assert trade["timeframe"] == "long_term"

        log = load_user_trade_log(chat)
        aapl = next((t for t in log["open"] if t["ticker"] == "AAPL"), None)
        assert aapl is not None
        assert aapl["timeframe"] == "long_term"

        remove_holding("AAPL", chat)  # cleanup

    def test_add_holding_timeframe_none_when_not_provided(self):
        """Without timeframe_override, timeframe stays None (no picks data)."""
        from trade_logger import add_holding, remove_holding

        chat = self._chat()
        trade, existed = add_holding("NVDA", chat)
        assert not existed
        assert trade["timeframe"] is None
        remove_holding("NVDA", chat)

    def test_bought_falls_back_to_live_price_when_not_in_picks(self, monkeypatch):
        """
        /bought TICKER for a symbol not in today's picks must store a real
        entry_price (the live price), not None — otherwise every %-based
        alert/nudge for that position is dead. AAPL=189.50 in FAKE_PRICES.
        """
        import cmd_trade_exec
        import trade_logger
        import market_data

        # add_holding returns a trade with NO entry (ticker not in today's picks).
        fake_trade = {"ticker": "AAPL", "entry_price": None, "stop_loss": None, "target_price": None}
        saved = {}
        monkeypatch.setattr(cmd_trade_exec, "load_picks", lambda: {})
        monkeypatch.setattr(cmd_trade_exec, "_resolve_ticker_candidates", lambda t: [])
        monkeypatch.setattr(trade_logger, "add_holding",
                            lambda ticker, chat_id, picks=None, **kw: (fake_trade, False))
        monkeypatch.setattr(market_data, "validate_ticker", lambda t: True)
        monkeypatch.setattr(market_data, "get_live_price", lambda t: 199.99)
        monkeypatch.setattr(cmd_trade_exec, "load_user_trade_log", lambda cid: {"open": [fake_trade]})
        monkeypatch.setattr(cmd_trade_exec, "save_user_trade_log", lambda cid, log: saved.update(log=log))

        cmd_trade_exec._execute_bought("AAPL", "999")  # no explicit price

        aapl = next(t for t in saved["log"]["open"] if t["ticker"] == "AAPL")
        assert aapl["entry_price"] == 199.99            # live-price fallback, not None

    def test_parse_money_unifies_input_formats(self):
        """parse_money tolerates $ , % and k/m suffixes — one parser everywhere."""
        from cmd_helpers import parse_money
        assert parse_money("1,500") == 1500.0
        assert parse_money("$7") == 7.0
        assert parse_money("7%") == 7.0
        assert parse_money("1.5k") == 1500.0
        assert parse_money("25k") == 25000.0
        assert parse_money("2m") == 2_000_000.0
        assert parse_money("  88.5  ") == 88.5
        assert parse_money("abc") is None
        assert parse_money("") is None
        assert parse_money(None) is None

    def test_add_holding_etf_pick_typed_etf(self):
        """An ETF pick must log as asset_type='etf', not 'stock'."""
        from trade_logger import add_holding, remove_holding
        chat = self._chat()
        remove_holding("SOXX", chat)
        picks = {"etfs": {"short_term": [{"ticker": "SOXX", "entry_price": 200.0}]}}
        trade, _ = add_holding("SOXX", chat, picks=picks)
        assert trade["asset_type"] == "etf"
        remove_holding("SOXX", chat)

    def test_add_holding_crypto_not_in_picks_uses_canonical(self):
        """A crypto ticker absent from picks must still classify as crypto."""
        from trade_logger import add_holding, remove_holding
        chat = self._chat()
        remove_holding("BTC", chat)
        trade, _ = add_holding("BTC", chat, picks={})   # not in any pick section
        assert trade["asset_type"] == "crypto"          # via price_checker.CRYPTO_SYMBOLS
        remove_holding("BTC", chat)

    def test_add_holding_asset_type_override_wins(self):
        from trade_logger import add_holding, remove_holding
        chat = self._chat()
        remove_holding("XYZ", chat)
        trade, _ = add_holding("XYZ", chat, picks={}, asset_type_override="commodity")
        assert trade["asset_type"] == "commodity"
        remove_holding("XYZ", chat)

    def test_add_holding_short_term_override(self):
        from trade_logger import add_holding, remove_holding

        chat = self._chat()
        trade, _ = add_holding("TSLA", chat, timeframe_override="short_term")
        assert trade["timeframe"] == "short_term"
        remove_holding("TSLA", chat)

    def test_add_holding_timeframe_from_picks_data(self):
        """timeframe extracted from picks dict when timeframe_override is None."""
        from trade_logger import add_holding, remove_holding

        chat = self._chat()
        fake_picks = {
            "stocks": {
                "long_term": [
                    {"ticker": "AAPL", "entry_price": 190, "target_price": 210, "stop_loss": 180}
                ],
                "short_term": [],
            },
            "crypto": {"short_term": [], "long_term": []},
            "etfs": {"short_term": [], "long_term": []},
        }
        trade, _ = add_holding("AAPL", chat, picks=fake_picks)
        assert trade["timeframe"] == "long_term"
        remove_holding("AAPL", chat)

    def test_add_holding_override_wins_over_picks(self):
        """timeframe_override must beat the timeframe extracted from picks."""
        from trade_logger import add_holding, remove_holding

        chat = self._chat()
        fake_picks = {
            "stocks": {
                "long_term": [
                    {"ticker": "AAPL", "entry_price": 190, "target_price": 210, "stop_loss": 180}
                ],
                "short_term": [],
            },
            "crypto": {"short_term": [], "long_term": []},
            "etfs": {"short_term": [], "long_term": []},
        }
        # Picks say long_term, override says short_term → override wins
        trade, _ = add_holding("AAPL", chat, picks=fake_picks,
                               timeframe_override="short_term")
        assert trade["timeframe"] == "short_term"
        remove_holding("AAPL", chat)

    def test_add_holding_duplicate_updates_levels(self):
        """add_holding on an existing ticker updates levels but returns existed=True."""
        from trade_logger import add_holding, remove_holding

        chat = self._chat()
        add_holding("AAPL", chat, entry_override=190.0)
        trade, existed = add_holding("AAPL", chat, entry_override=195.0)
        assert existed is True
        assert trade["entry_price"] == pytest.approx(195.0)
        remove_holding("AAPL", chat)

    def test_open_trades_stores_timeframe_from_picks(self):
        """open_trades() must store timeframe in each trade entry."""
        from trade_logger import open_trades
        from config_manager import load_user_trade_log

        chat = self._chat()
        picks = {
            "stocks": {
                "short_term": [
                    {"ticker": "SPY", "entry_price": 500, "target_price": 530, "stop_loss": 480,
                     "conviction": "high", "thesis": "Momentum", "allocation": 200}
                ],
                "long_term": [
                    {"ticker": "QQQ", "entry_price": 430, "target_price": 470, "stop_loss": 410,
                     "conviction": "medium", "thesis": "Growth", "allocation": 300}
                ],
            },
            "crypto": {"short_term": [], "long_term": []},
        }
        open_trades(picks, chat)

        log = load_user_trade_log(chat)
        by_ticker = {t["ticker"]: t for t in log["open"]}

        assert by_ticker["SPY"]["timeframe"] == "short_term"
        assert by_ticker["QQQ"]["timeframe"] == "long_term"

    def test_check_and_close_stops(self):
        """Closing a stop-loss hit records correct outcome and return_pct."""
        from trade_logger import open_trades, check_and_close_trades
        from config_manager import load_user_trade_log

        chat = self._chat()
        picks = {
            "stocks": {
                "short_term": [
                    {"ticker": "MSFT", "entry_price": 415.0, "target_price": 450.0,
                     "stop_loss": 400.0, "conviction": "high", "thesis": "T", "allocation": 500}
                ],
                "long_term": [],
            },
            "crypto": {"short_term": [], "long_term": []},
        }
        open_trades(picks, chat)

        # Simulate price hitting stop loss
        closed = check_and_close_trades({"MSFT": 395.0}, chat)
        assert len(closed) == 1
        assert closed[0]["outcome"] == "stop"
        assert closed[0]["return_pct"] < 0

    def test_get_performance_stats_empty(self):
        """get_performance_stats on a fresh user returns None."""
        from trade_logger import get_performance_stats

        chat = self._chat()
        stats = get_performance_stats(chat)
        assert stats is None

    def test_remove_holding_returns_true(self):
        from trade_logger import add_holding, remove_holding

        chat = self._chat()
        add_holding("AAPL", chat, entry_override=190.0)
        result = remove_holding("AAPL", chat)
        assert result is True

    def test_remove_holding_nonexistent_returns_false(self):
        from trade_logger import remove_holding

        chat = self._chat()
        result = remove_holding("FAKEXYZ", chat)
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# /PRICES STRUCTURED ENDPOINT (powers mini-app card view)
# ══════════════════════════════════════════════════════════════════════════════

class TestPricesEndpoint:
    """live_prices without ?tickers returns structured list for /prices card view."""

    # Fake picks returned by the monkeypatched load_picks().
    # load_picks() makes its own direct HTTP request to the Gist API and therefore
    # cannot be seeded through the in-memory _gist_store — we patch it directly.
    _FAKE_PICKS = {
        "_saved_date": "2099-01-01",  # never stale
        "stocks": {
            "short_term": [
                {"ticker": "AAPL", "entry_price": 189.50, "target_price": 210.0,
                 "stop_loss": 175.0, "conviction": "high", "thesis": "Momentum"},
            ],
            "long_term": [
                {"ticker": "MSFT", "entry_price": 415.0, "target_price": 460.0,
                 "stop_loss": 395.0, "conviction": "medium", "thesis": "Growth"},
            ],
        },
        "crypto": {
            "short_term": [
                {"symbol": "BTC", "entry_price": 67000, "target_price": 75000,
                 "stop_loss": 62000, "conviction": "high", "thesis": "Halving"},
            ],
            "long_term": [],
        },
        "etfs": {"short_term": [], "long_term": []},
        "commodities": {"short_term": [], "long_term": []},
    }

    def _patch_picks(self, monkeypatch):
        """Patch config_manager.load_picks to return _FAKE_PICKS without HTTP calls."""
        import config_manager
        monkeypatch.setattr(config_manager, "load_picks", lambda: self._FAKE_PICKS)

    def test_live_prices_no_param_returns_list(self, client):
        r = get(client, "/api/miniapp/live_prices")
        data = r.get_json()
        assert data["ok"] is True
        assert isinstance(data["prices"], list)

    def test_live_prices_with_picks_returns_tickers(self, client, monkeypatch):
        self._patch_picks(monkeypatch)
        r = get(client, "/api/miniapp/live_prices")
        data = r.get_json()
        tickers = [p["ticker"] for p in data["prices"]]
        # AAPL is in picks, FAKE_PRICES has it → should appear
        assert "AAPL" in tickers

    def test_live_prices_includes_type_field(self, client, monkeypatch):
        self._patch_picks(monkeypatch)
        r = get(client, "/api/miniapp/live_prices")
        prices = r.get_json()["prices"]
        assert prices, "Expected non-empty prices list with patched picks"
        for p in prices:
            assert "type" in p, f"Missing 'type' field in price entry: {p}"
            assert p["type"] in (
                "st_picks", "lt_picks", "crypto", "etf_picks", "commodities"
            ), f"Unexpected type: {p['type']}"

    def test_live_prices_includes_entry_and_live(self, client, monkeypatch):
        self._patch_picks(monkeypatch)
        r = get(client, "/api/miniapp/live_prices")
        prices = r.get_json()["prices"]
        aapl = next((p for p in prices if p["ticker"] == "AAPL"), None)
        assert aapl is not None, "AAPL should be in prices when picks are patched"
        assert "entry" in aapl
        assert "live" in aapl

    def test_live_prices_pct_sign_matches_direction(self, client, monkeypatch):
        """If live > entry, pct should be positive."""
        self._patch_picks(monkeypatch)
        r = get(client, "/api/miniapp/live_prices")
        prices = r.get_json()["prices"]
        for p in prices:
            if p.get("live") and p.get("entry") and p.get("pct") is not None:
                expected_positive = float(p["live"]) > float(p["entry"])
                actual_positive = p["pct"] > 0
                assert expected_positive == actual_positive, (
                    f"{p['ticker']}: live={p['live']} entry={p['entry']} pct={p['pct']}"
                )

    def test_live_prices_with_tickers_param_still_dict(self, client):
        """?tickers= mode must return a dict (BUG-01 regression)."""
        r = get(client, "/api/miniapp/live_prices", tickers="AAPL,NVDA")
        data = r.get_json()
        assert isinstance(data["prices"], dict)
        assert "AAPL" in data["prices"]

    def test_live_prices_empty_picks_returns_empty_list(self, client):
        """When no picks are stored the endpoint returns an empty list gracefully."""
        r = get(client, "/api/miniapp/live_prices")
        data = r.get_json()
        assert data["ok"] is True
        assert isinstance(data["prices"], list)
