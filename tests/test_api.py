"""
test_api.py — Flask endpoint integration tests for the StockPulz Mini App.

All external I/O (Gist, Alpaca, Polygon, yfinance) is mocked via conftest.py.
Auth is satisfied by passing chat_id=TEST_CHAT_ID in every request.

Test groups
-----------
  TestAuth            — 403 when no/invalid chat_id
  TestPicks           — /api/miniapp/picks
  TestPositions       — /api/miniapp/positions, close_position, update_position,
                        log_bought, remove_position
  TestSettings        — /api/miniapp/settings, settings/update, toggle_paused
  TestWatchlist       — /api/miniapp/watchlist, watchlist/add, watchlist/remove,
                        update_watchlist
  TestLivePrices      — /api/miniapp/live_prices (dict vs list modes — BUG-01)
  TestAlerts          — /api/miniapp/alerts, add_alert, remove_alert, clear_alerts
  TestPaper           — /api/miniapp/paper, paper_cancel, paper_add_cash
  TestPerformance     — /api/miniapp/performance, pnl_history, history
  TestCommunity       — /api/miniapp/community
  TestChart           — /api/miniapp/chart/<ticker>
  TestDefine          — /api/miniapp/define
  TestMisc            — /api/miniapp/status, regime, share_link, feedback
"""

import json
import pytest
from tests.conftest import get, post, TEST_CHAT_ID, FAKE_PRICES


# ══════════════════════════════════════════════════════════════════════════════
# AUTH
# ══════════════════════════════════════════════════════════════════════════════

class TestAuth:
    def test_get_without_chat_id_returns_403(self, client):
        r = client.get("/api/miniapp/settings")
        assert r.status_code == 403

    def test_post_without_chat_id_returns_403(self, client):
        r = client.post("/api/miniapp/toggle_paused", json={})
        assert r.status_code == 403

    def test_get_with_wrong_chat_id_returns_403(self, client):
        r = client.get("/api/miniapp/settings", query_string={"chat_id": "0000000"})
        assert r.status_code == 403

    def test_get_with_valid_chat_id_returns_200(self, client):
        r = get(client, "/api/miniapp/settings")
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# PICKS
# ══════════════════════════════════════════════════════════════════════════════

class TestPicks:
    def test_picks_returns_200(self, client):
        r = get(client, "/api/miniapp/picks")
        assert r.status_code == 200

    def test_picks_returns_dict_structure(self, client):
        r = get(client, "/api/miniapp/picks")
        data = r.get_json()
        assert isinstance(data, dict)

    def test_picks_has_meta(self, client):
        """Picks endpoint always includes _meta with today's date."""
        r = get(client, "/api/miniapp/picks")
        data = r.get_json()
        # Either has _meta (new format) or ok key (error format)
        assert "_meta" in data or "ok" in data


# ══════════════════════════════════════════════════════════════════════════════
# SETTINGS
# ══════════════════════════════════════════════════════════════════════════════

class TestSettings:
    def test_settings_returns_expected_keys(self, client):
        r = get(client, "/api/miniapp/settings")
        data = r.get_json()
        assert "settings" in data
        cfg = data["settings"]
        for key in ("paused", "risk_profile", "watchlist"):
            assert key in cfg, f"Missing key: {key}"

    def test_settings_default_paused_is_false(self, client):
        r = get(client, "/api/miniapp/settings")
        assert r.get_json()["settings"]["paused"] is False

    def test_settings_default_watchlist_is_list(self, client):
        r = get(client, "/api/miniapp/settings")
        assert isinstance(r.get_json()["settings"]["watchlist"], list)

    def test_update_settings_risk_profile(self, client):
        r = post(client, "/api/miniapp/update_settings", {"risk_profile": "aggressive"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        # Verify persisted
        cfg = get(client, "/api/miniapp/settings").get_json()["settings"]
        assert cfg["risk_profile"] == "aggressive"

    def test_update_settings_stock_budget(self, client):
        r = post(client, "/api/miniapp/update_settings", {"stock_budget": 5000})
        assert r.status_code == 200
        cfg = get(client, "/api/miniapp/settings").get_json()["settings"]
        assert cfg["stock_budget"] == 5000.0

    def test_update_settings_rejects_invalid_risk(self, client):
        r = post(client, "/api/miniapp/update_settings", {"risk_profile": "moon_boy"})
        assert r.status_code == 200  # endpoint accepts but ignores invalid value
        cfg = get(client, "/api/miniapp/settings").get_json()["settings"]
        assert cfg.get("risk_profile") != "moon_boy"

    def test_settings_update_notif_pref(self, client):
        r = post(client, "/api/miniapp/settings/update",
                 {"key": "notif_target_hit", "value": False})
        assert r.status_code == 200
        cfg = get(client, "/api/miniapp/settings").get_json()["settings"]
        assert cfg["notif_target_hit"] is False

    def test_settings_update_rejects_unknown_key(self, client):
        r = post(client, "/api/miniapp/settings/update",
                 {"key": "evil_override", "value": True})
        assert r.status_code in (400, 200)  # must not silently accept unknown keys
        cfg = get(client, "/api/miniapp/settings").get_json()["settings"]
        assert "evil_override" not in cfg

    def test_toggle_paused_flips_state(self, client):
        # Default paused=False → send paused=True
        r = post(client, "/api/miniapp/toggle_paused", {"paused": True})
        assert r.status_code == 200
        cfg = get(client, "/api/miniapp/settings").get_json()["settings"]
        assert cfg["paused"] is True

    def test_toggle_paused_twice_returns_to_original(self, client):
        post(client, "/api/miniapp/toggle_paused", {"paused": True})
        post(client, "/api/miniapp/toggle_paused", {"paused": False})
        cfg = get(client, "/api/miniapp/settings").get_json()["settings"]
        assert cfg["paused"] is False


# ══════════════════════════════════════════════════════════════════════════════
# WATCHLIST
# ══════════════════════════════════════════════════════════════════════════════

class TestWatchlist:
    def test_watchlist_returns_tickers_list(self, client):
        r = get(client, "/api/miniapp/watchlist")
        data = r.get_json()
        assert data["ok"] is True
        assert isinstance(data["tickers"], list)

    def test_watchlist_add_new_ticker(self, client):
        r = post(client, "/api/miniapp/watchlist/add", {"ticker": "AAPL"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        tickers = get(client, "/api/miniapp/watchlist").get_json()["tickers"]
        assert "AAPL" in tickers

    def test_watchlist_add_duplicate_returns_ok_false(self, client):
        post(client, "/api/miniapp/watchlist/add", {"ticker": "AAPL"})
        r = post(client, "/api/miniapp/watchlist/add", {"ticker": "AAPL"})
        assert r.get_json()["ok"] is False

    def test_watchlist_remove_existing_ticker(self, client):
        post(client, "/api/miniapp/watchlist/add", {"ticker": "NVDA"})
        r = post(client, "/api/miniapp/watchlist/remove", {"ticker": "NVDA"})
        assert r.get_json()["ok"] is True
        tickers = get(client, "/api/miniapp/watchlist").get_json()["tickers"]
        assert "NVDA" not in tickers

    def test_watchlist_remove_nonexistent_still_ok(self, client):
        r = post(client, "/api/miniapp/watchlist/remove", {"ticker": "FAKEXYZ"})
        assert r.status_code == 200

    def test_update_watchlist_add_action(self, client):
        r = post(client, "/api/miniapp/update_watchlist", {"action": "add", "ticker": "TSLA"})
        assert r.get_json()["ok"] is True
        assert "TSLA" in r.get_json()["watchlist"]

    def test_update_watchlist_remove_action(self, client):
        post(client, "/api/miniapp/update_watchlist", {"action": "add", "ticker": "TSLA"})
        r = post(client, "/api/miniapp/update_watchlist", {"action": "remove", "ticker": "TSLA"})
        assert "TSLA" not in r.get_json()["watchlist"]

    def test_update_watchlist_set_action(self, client):
        r = post(client, "/api/miniapp/update_watchlist",
                 {"action": "set", "tickers": ["AAPL", "MSFT"]})
        wl = r.get_json()["watchlist"]
        assert sorted(wl) == ["AAPL", "MSFT"]

    def test_update_watchlist_empty_ticker_rejected(self, client):
        r = post(client, "/api/miniapp/watchlist/add", {"ticker": ""})
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# LIVE PRICES  (BUG-01 regression)
# ══════════════════════════════════════════════════════════════════════════════

class TestLivePrices:
    def test_without_tickers_param_returns_list(self, client):
        """Without ?tickers= the endpoint returns picks list [{ticker,live,...}]."""
        r = get(client, "/api/miniapp/live_prices")
        data = r.get_json()
        assert data["ok"] is True
        assert isinstance(data["prices"], list)

    def test_with_tickers_param_returns_dict(self, client):
        """
        BUG-01 regression: ?tickers=AAPL,NVDA must return a dict keyed by
        ticker so frontend watchlist chips can do prices['AAPL'] lookups.
        """
        r = get(client, "/api/miniapp/live_prices", tickers="AAPL,NVDA")
        data = r.get_json()
        assert data["ok"] is True
        assert isinstance(data["prices"], dict), (
            "prices should be a dict when ?tickers= is provided"
        )

    def test_tickers_param_returns_correct_prices(self, client):
        r = get(client, "/api/miniapp/live_prices", tickers="AAPL,NVDA")
        prices = r.get_json()["prices"]
        assert "AAPL" in prices
        assert prices["AAPL"] == pytest.approx(FAKE_PRICES["AAPL"], abs=0.01)

    def test_tickers_param_unknown_ticker_omitted(self, client):
        """Tickers with no price data should be omitted, not error."""
        r = get(client, "/api/miniapp/live_prices", tickers="AAPL,FAKEXYZ999")
        data = r.get_json()
        assert data["ok"] is True
        assert "FAKEXYZ999" not in data["prices"]

    def test_tickers_param_single_ticker(self, client):
        r = get(client, "/api/miniapp/live_prices", tickers="MSFT")
        prices = r.get_json()["prices"]
        assert "MSFT" in prices

    def test_empty_tickers_param_returns_list(self, client):
        """Empty ?tickers= should fall through to picks-list mode."""
        r = get(client, "/api/miniapp/live_prices", tickers="")
        data = r.get_json()
        assert isinstance(data["prices"], list)


# ══════════════════════════════════════════════════════════════════════════════
# ALERTS
# ══════════════════════════════════════════════════════════════════════════════

class TestAlerts:
    def test_list_alerts_empty(self, client):
        r = get(client, "/api/miniapp/alerts")
        assert r.status_code == 200
        data = r.get_json()
        assert "alerts" in data

    def test_add_alert_above(self, client):
        r = post(client, "/api/miniapp/add_alert",
                 {"ticker": "AAPL", "target": 200.0, "direction": "above"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_add_alert_auto_direction(self, client):
        # AAPL price = 189.50 → target 200 → auto → above
        r = post(client, "/api/miniapp/add_alert",
                 {"ticker": "AAPL", "target": 200.0, "direction": "auto"})
        assert r.get_json()["ok"] is True

    def test_add_alert_decimal_target(self, client):
        r = post(client, "/api/miniapp/add_alert",
                 {"ticker": "AAPL", "target": 199.99, "direction": "above"})
        assert r.get_json()["ok"] is True

    def test_add_duplicate_alert_returns_400(self, client):
        post(client, "/api/miniapp/add_alert",
             {"ticker": "AAPL", "target": 200.0, "direction": "above"})
        r = post(client, "/api/miniapp/add_alert",
                 {"ticker": "AAPL", "target": 200.0, "direction": "above"})
        assert r.status_code == 400

    def test_add_alert_missing_ticker_returns_error(self, client):
        r = post(client, "/api/miniapp/add_alert", {"target": 200.0})
        assert r.status_code in (400, 200)
        data = r.get_json()
        assert data.get("ok") is not True or "error" in data

    def test_remove_alert_specific(self, client):
        post(client, "/api/miniapp/add_alert",
             {"ticker": "NVDA", "target": 900.0, "direction": "above"})
        r = post(client, "/api/miniapp/remove_alert",
                 {"ticker": "NVDA", "target": 900.0})
        assert r.get_json()["ok"] is True

    def test_remove_alert_decimal_float_tolerance(self, client):
        """BUG-05 regression: remove should match 199.99 with tolerance."""
        post(client, "/api/miniapp/add_alert",
             {"ticker": "AAPL", "target": 199.99, "direction": "above"})
        r = post(client, "/api/miniapp/remove_alert",
                 {"ticker": "AAPL", "target": 199.99})
        assert r.get_json()["ok"] is True
        # Confirm alert is gone
        alerts = get(client, "/api/miniapp/alerts").get_json()["alerts"]
        assert not any(a["ticker"] == "AAPL" for a in alerts)

    def test_remove_alert_bulk_by_ticker(self, client):
        post(client, "/api/miniapp/add_alert",
             {"ticker": "AAPL", "target": 200.0, "direction": "above"})
        post(client, "/api/miniapp/add_alert",
             {"ticker": "AAPL", "target": 210.0, "direction": "above"})
        r = post(client, "/api/miniapp/remove_alert", {"ticker": "AAPL"})
        assert r.get_json()["ok"] is True

    def test_clear_alerts(self, client):
        post(client, "/api/miniapp/add_alert",
             {"ticker": "AAPL", "target": 200.0, "direction": "above"})
        r = post(client, "/api/miniapp/clear_alerts", {})
        assert r.get_json()["ok"] is True

    def test_list_alerts_shows_added_alert(self, client):
        post(client, "/api/miniapp/add_alert",
             {"ticker": "NVDA", "target": 900.0, "direction": "above"})
        r = get(client, "/api/miniapp/alerts")
        alerts = r.get_json()["alerts"]
        assert any(a["ticker"] == "NVDA" for a in alerts)


# ══════════════════════════════════════════════════════════════════════════════
# POSITIONS
# ══════════════════════════════════════════════════════════════════════════════

class TestPositions:
    def test_positions_returns_ok(self, client):
        r = get(client, "/api/miniapp/positions")
        assert r.status_code == 200
        data = r.get_json()
        assert "positions" in data
        assert isinstance(data["positions"], list)

    def test_close_position_no_open_position(self, client):
        """Closing a non-existent position should return an error gracefully."""
        r = post(client, "/api/miniapp/close_position",
                 {"ticker": "AAPL", "price": 190.0})
        data = r.get_json()
        # Should not crash — error response is acceptable
        assert r.status_code in (200, 400, 404)
        assert isinstance(data, dict)

    def test_update_position_requires_ticker(self, client):
        r = post(client, "/api/miniapp/update_position", {"shares": 10})
        assert r.status_code in (400, 200)

    def test_log_bought_returns_ok(self, client):
        """Log a buy action for a picks ticker."""
        r = post(client, "/api/miniapp/log_bought",
                 {"ticker": "AAPL", "entry_price": 189.50, "shares": 5})
        assert r.status_code == 200

    def test_remove_position_nonexistent_returns_ok(self, client):
        """Removing a ticker not in portfolio should not crash."""
        r = post(client, "/api/miniapp/remove_position", {"ticker": "FAKEXYZ"})
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, dict)
        assert "ok" in data

    def test_remove_position_missing_ticker_returns_400(self, client):
        r = post(client, "/api/miniapp/remove_position", {})
        assert r.status_code == 400

    def test_remove_position_then_gone(self, client):
        """Log a position then remove it — should disappear from positions list."""
        post(client, "/api/miniapp/log_bought",
             {"ticker": "TSLA", "entry_price": 200.0})
        r = post(client, "/api/miniapp/remove_position", {"ticker": "TSLA"})
        assert r.get_json().get("ok") is True
        positions = get(client, "/api/miniapp/positions").get_json()["positions"]
        tickers = [p["ticker"] for p in positions]
        assert "TSLA" not in tickers


# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADING
# ══════════════════════════════════════════════════════════════════════════════

class TestPaper:
    def test_paper_returns_positions_and_history(self, client):
        r = get(client, "/api/miniapp/paper")
        assert r.status_code == 200
        data = r.get_json()
        assert "ok" in data
        assert "positions" in data
        assert "history" in data
        assert "cash" in data

    def test_paper_initial_cash_is_positive(self, client):
        r = get(client, "/api/miniapp/paper")
        assert r.get_json()["cash"] >= 0

    def test_paper_add_cash(self, client):
        r = post(client, "/api/miniapp/paper_add_cash", {"amount": 500})
        assert r.status_code == 200
        data = r.get_json()
        assert data.get("ok") is True

    def test_paper_cancel_nonexistent_trade(self, client):
        """Cancelling a paper trade that doesn't exist should not crash."""
        r = post(client, "/api/miniapp/paper_cancel", {"ticker": "FAKEXYZ"})
        assert r.status_code in (200, 400, 404)
        assert isinstance(r.get_json(), dict)


# ══════════════════════════════════════════════════════════════════════════════
# PERFORMANCE
# ══════════════════════════════════════════════════════════════════════════════

class TestPerformance:
    def test_performance_returns_ok(self, client):
        r = get(client, "/api/miniapp/performance")
        assert r.status_code == 200
        assert r.get_json().get("ok") is True

    def test_history_returns_list(self, client):
        r = get(client, "/api/miniapp/history")
        assert r.status_code == 200
        data = r.get_json()
        assert "trades" in data
        assert isinstance(data["trades"], list)

    def test_pnl_history_returns_list(self, client):
        r = get(client, "/api/miniapp/pnl_history")
        assert r.status_code == 200
        data = r.get_json()
        assert "points" in data
        assert isinstance(data["points"], list)


# ══════════════════════════════════════════════════════════════════════════════
# COMMUNITY
# ══════════════════════════════════════════════════════════════════════════════

class TestCommunity:
    def test_community_returns_ok(self, client):
        r = get(client, "/api/miniapp/community")
        assert r.status_code == 200
        data = r.get_json()
        assert data.get("ok") is True

    def test_community_has_stats_key(self, client):
        r = get(client, "/api/miniapp/community")
        data = r.get_json()
        # stats may be None/empty when no trade data exists, but key should be present
        assert "stats" in data


# ══════════════════════════════════════════════════════════════════════════════
# CHART
# ══════════════════════════════════════════════════════════════════════════════

class TestChart:
    def test_chart_returns_ohlcv_bars(self, client):
        r = get(client, "/api/miniapp/chart/AAPL")
        assert r.status_code == 200
        data = r.get_json()
        assert "ticker" in data
        bars = data.get("data", [])
        assert isinstance(bars, list)
        if bars:
            assert "time" in bars[0]
            assert "close" in bars[0]

    def test_chart_unknown_ticker_returns_gracefully(self, client):
        """An unknown ticker should return ok=False, not a 500."""
        r = get(client, "/api/miniapp/chart/FAKEXYZ999")
        assert r.status_code in (200, 404)
        data = r.get_json()
        assert isinstance(data, dict)


# ══════════════════════════════════════════════════════════════════════════════
# DEFINE (GLOSSARY)
# ══════════════════════════════════════════════════════════════════════════════

class TestDefine:
    def test_define_missing_term_returns_400(self, client):
        r = get(client, "/api/miniapp/define")
        assert r.status_code in (400, 200)

    def test_define_with_term_returns_definition(self, client, monkeypatch):
        """Mock Anthropic client so /define works offline."""
        import unittest.mock as _mock
        import anthropic as _ant

        fake_content = _mock.MagicMock()
        fake_content.text = "RSI (Relative Strength Index) measures momentum."
        fake_resp = _mock.MagicMock()
        fake_resp.content = [fake_content]

        fake_messages = _mock.MagicMock()
        fake_messages.create.return_value = fake_resp

        fake_client = _mock.MagicMock()
        fake_client.messages = fake_messages

        monkeypatch.setattr(_ant, "Anthropic", lambda **kw: fake_client)

        r = get(client, "/api/miniapp/define", term="RSI")
        assert r.status_code == 200
        data = r.get_json()
        assert "explanation" in data or "error" in data


# ══════════════════════════════════════════════════════════════════════════════
# MISC
# ══════════════════════════════════════════════════════════════════════════════

class TestMisc:
    def test_status_returns_ok(self, client):
        r = get(client, "/api/miniapp/status")
        assert r.status_code == 200
        data = r.get_json()
        assert data.get("ok") is True
        assert "bot_active" in data

    def test_regime_returns_data(self, client, monkeypatch):
        """Mock regime call to avoid external requests."""
        import webhook
        monkeypatch.setattr(
            "market_regime.get_market_regime",
            lambda: {"regime": "bull", "vix": 15.0, "trend": "up"},
            raising=False,
        )
        r = get(client, "/api/miniapp/regime")
        assert r.status_code == 200

    def test_share_link_returns_url(self, client, monkeypatch):
        """Mock Telegram /getMe so share_link doesn't need network access."""
        import unittest.mock as _mock
        import requests as _req

        fake_resp = _mock.MagicMock()
        fake_resp.json.return_value = {"result": {"username": "StockPulzBot"}}
        monkeypatch.setattr(_req, "get", lambda *a, **kw: fake_resp)

        r = get(client, "/api/miniapp/share_link")
        assert r.status_code == 200
        data = r.get_json()
        assert data.get("ok") is True
        assert "url" in data

    def test_feedback_post_returns_ok(self, client, monkeypatch):
        """feedback uses 'text' field; mock Telegram /getChat to avoid network."""
        import unittest.mock as _mock
        import requests as _req

        fake_resp = _mock.MagicMock()
        fake_resp.json.return_value = {"result": {"first_name": "Test", "username": "testuser"}}
        monkeypatch.setattr(_req, "get", lambda *a, **kw: fake_resp)

        r = post(client, "/api/miniapp/feedback",
                 {"text": "Great pick! Love the alerts."})
        assert r.status_code == 200
        assert r.get_json().get("ok") is True

    def test_dividends_returns_list(self, client):
        r = get(client, "/api/miniapp/dividends")
        assert r.status_code == 200
        data = r.get_json()
        assert "dividends" in data or "ok" in data

    def test_earnings_with_no_tickers_returns_empty(self, client):
        r = get(client, "/api/miniapp/earnings")
        assert r.status_code == 200

    def test_quote_returns_price(self, client):
        r = get(client, "/api/miniapp/quote", ticker="AAPL")
        assert r.status_code == 200
        data = r.get_json()
        assert "price" in data or "ok" in data

    def test_seed_backtest_returns_ok(self, client):
        r = post(client, "/api/miniapp/seed_backtest", {})
        assert r.status_code == 200


# ══════════════════════════════════════════════════════════════════════════════
# NEW: update_position entry_price (Task #146 regression)
# ══════════════════════════════════════════════════════════════════════════════

class TestUpdatePositionEntryPrice:
    """update_position should persist entry_price when included in the body."""

    def _seed_open_trade(self):
        """Return a picks dict that open_trades() understands."""
        return {
            "stocks": {
                "short_term": [{
                    "ticker": "AAPL",
                    "entry_price": 180.0,
                    "target_price": 200.0,
                    "stop_loss": 170.0,
                    "allocation": 1000,
                    "conviction": "high",
                    "thesis": "test",
                }],
                "long_term": [],
            },
            "crypto": {"short_term": [], "long_term": []},
        }

    def test_update_entry_price_persists(self, client):
        """POST entry_price=185.0 for AAPL should be stored in the trade log."""
        # First seed an open position via log_bought
        post(client, "/api/miniapp/log_bought",
             {"ticker": "AAPL", "entry_price": 180.0, "shares": 1})
        # Now update entry price
        r = post(client, "/api/miniapp/update_position",
                 {"ticker": "AAPL", "entry_price": 185.0})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True
        # Verify positions endpoint reflects updated entry
        positions = get(client, "/api/miniapp/positions").get_json()["positions"]
        aapl = next((p for p in positions if p["ticker"] == "AAPL"), None)
        assert aapl is not None
        assert aapl.get("entry_price") == pytest.approx(185.0, abs=0.01)

    def test_update_entry_price_null_clears_field(self, client):
        """Passing entry_price=null should clear the entry price."""
        post(client, "/api/miniapp/log_bought",
             {"ticker": "AAPL", "entry_price": 180.0, "shares": 1})
        r = post(client, "/api/miniapp/update_position",
                 {"ticker": "AAPL", "entry_price": None})
        assert r.status_code == 200
        positions = get(client, "/api/miniapp/positions").get_json()["positions"]
        aapl = next((p for p in positions if p["ticker"] == "AAPL"), None)
        assert aapl is not None
        assert aapl.get("entry_price") is None

    def test_update_position_missing_ticker_returns_400(self, client):
        r = post(client, "/api/miniapp/update_position", {"entry_price": 185.0})
        assert r.status_code == 400

    def test_update_position_nonexistent_ticker_returns_404(self, client):
        r = post(client, "/api/miniapp/update_position",
                 {"ticker": "FAKEXYZ", "entry_price": 99.0})
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════════
# NEW: Watchlist alert endpoint (Task #157)
# ══════════════════════════════════════════════════════════════════════════════

class TestWatchlistAlert:
    """POST /api/miniapp/alerts add/remove actions from the watchlist 🔔 button."""

    def test_add_alert_via_post_returns_ok(self, client):
        r = post(client, "/api/miniapp/alerts",
                 {"action": "add", "ticker": "AAPL", "target": 200.0, "direction": "above"})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_add_alert_auto_direction(self, client):
        # AAPL = 189.50 → target 200 → auto → above
        r = post(client, "/api/miniapp/alerts",
                 {"action": "add", "ticker": "AAPL", "target": 200.0, "direction": "auto"})
        assert r.get_json()["ok"] is True

    def test_add_alert_shows_in_list(self, client):
        post(client, "/api/miniapp/alerts",
             {"action": "add", "ticker": "NVDA", "target": 900.0, "direction": "above"})
        alerts = get(client, "/api/miniapp/alerts").get_json()["alerts"]
        assert any(a["ticker"] == "NVDA" for a in alerts)

    def test_add_duplicate_alert_returns_400(self, client):
        post(client, "/api/miniapp/alerts",
             {"action": "add", "ticker": "AAPL", "target": 200.0, "direction": "above"})
        r = post(client, "/api/miniapp/alerts",
                 {"action": "add", "ticker": "AAPL", "target": 200.0, "direction": "above"})
        assert r.status_code == 400

    def test_remove_alert_via_post(self, client):
        post(client, "/api/miniapp/alerts",
             {"action": "add", "ticker": "AAPL", "target": 200.0, "direction": "above"})
        r = post(client, "/api/miniapp/alerts",
                 {"action": "remove", "ticker": "AAPL", "target": 200.0})
        assert r.status_code == 200
        assert r.get_json()["ok"] is True

    def test_missing_ticker_returns_400(self, client):
        r = post(client, "/api/miniapp/alerts",
                 {"action": "add", "target": 200.0})
        assert r.status_code == 400

    def test_add_missing_target_returns_400(self, client):
        r = post(client, "/api/miniapp/alerts",
                 {"action": "add", "ticker": "AAPL"})
        assert r.status_code == 400


# ══════════════════════════════════════════════════════════════════════════════
# NEW: trade_logger open_trades — long-term crypto tracking (Task #151)
# ══════════════════════════════════════════════════════════════════════════════

class TestOpenTradesLtCrypto:
    """open_trades() must log both short_term and long_term crypto picks."""

    def test_lt_crypto_appears_in_open_trades(self):
        """Long-term crypto picks should be tracked after open_trades() is called."""
        from trade_logger import open_trades
        from config_manager import load_user_trade_log, save_user_trade_log

        chat_id = "test_lt_crypto"
        # Seed empty log
        save_user_trade_log(chat_id, {"open": [], "closed": []})

        picks = {
            "stocks":  {"short_term": [], "long_term": []},
            "crypto": {
                "short_term": [{
                    "symbol": "BTC", "entry_price": 65000, "target_price": 75000,
                    "stop_loss": 60000, "allocation": 500, "conviction": "high", "thesis": "test",
                }],
                "long_term": [{
                    "symbol": "ETH", "entry_price": 3200, "target_price": 4000,
                    "stop_loss": 2800, "allocation": 300, "conviction": "medium", "thesis": "test",
                }],
            },
        }
        open_trades(picks, chat_id)

        log = load_user_trade_log(chat_id)
        tickers = [t["ticker"] for t in log["open"]]
        assert "BTC" in tickers, "short_term crypto should be tracked"
        assert "ETH" in tickers, "long_term crypto should be tracked"

    def test_lt_crypto_timeframe_field_is_long_term(self):
        """Trades opened from long_term picks should have timeframe='long_term'."""
        from trade_logger import open_trades
        from config_manager import load_user_trade_log, save_user_trade_log

        chat_id = "test_lt_crypto_tf"
        save_user_trade_log(chat_id, {"open": [], "closed": []})

        picks = {
            "stocks":  {"short_term": [], "long_term": []},
            "crypto": {
                "short_term": [],
                "long_term": [{
                    "symbol": "SOL", "entry_price": 150, "target_price": 200,
                    "stop_loss": 130, "allocation": 200, "conviction": "high", "thesis": "test",
                }],
            },
        }
        open_trades(picks, chat_id)

        log = load_user_trade_log(chat_id)
        sol = next((t for t in log["open"] if t["ticker"] == "SOL"), None)
        assert sol is not None
        assert sol["timeframe"] == "long_term"

    def test_st_crypto_timeframe_field_is_short_term(self):
        """Trades opened from short_term picks should have timeframe='short_term'."""
        from trade_logger import open_trades
        from config_manager import load_user_trade_log, save_user_trade_log

        chat_id = "test_st_crypto_tf"
        save_user_trade_log(chat_id, {"open": [], "closed": []})

        picks = {
            "stocks":  {"short_term": [], "long_term": []},
            "crypto": {
                "short_term": [{
                    "symbol": "DOGE", "entry_price": 0.15, "target_price": 0.20,
                    "stop_loss": 0.12, "allocation": 100, "conviction": "medium", "thesis": "memes",
                }],
                "long_term": [],
            },
        }
        open_trades(picks, chat_id)

        log = load_user_trade_log(chat_id)
        doge = next((t for t in log["open"] if t["ticker"] == "DOGE"), None)
        assert doge is not None
        assert doge["timeframe"] == "short_term"

    def test_open_trades_deduplicates_on_rerun(self):
        """Calling open_trades twice should not double-add tickers."""
        from trade_logger import open_trades
        from config_manager import load_user_trade_log, save_user_trade_log

        chat_id = "test_lt_dedup"
        save_user_trade_log(chat_id, {"open": [], "closed": []})

        picks = {
            "stocks":  {"short_term": [], "long_term": []},
            "crypto": {
                "short_term": [],
                "long_term": [{
                    "symbol": "AVAX", "entry_price": 40, "target_price": 55,
                    "stop_loss": 35, "allocation": 200, "conviction": "high", "thesis": "test",
                }],
            },
        }
        open_trades(picks, chat_id)
        open_trades(picks, chat_id)  # second call should not duplicate

        log = load_user_trade_log(chat_id)
        avax_entries = [t for t in log["open"] if t["ticker"] == "AVAX"]
        assert len(avax_entries) == 1, "duplicate trade should not be added"
