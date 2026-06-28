"""
conftest.py — shared fixtures for the StockPulz test suite.

Strategy
--------
* All Gist / network I/O is replaced with an in-memory dict store so tests
  run offline and don't touch production data.
* _miniapp_auth() is patched to return a fixed test chat_id, bypassing
  Telegram signature validation entirely.
* External price APIs (Alpaca, Polygon, yfinance) are patched to return
  deterministic values so tests are fast and repeatable.

Usage
-----
Fixtures are auto-discovered by pytest. Just import what you need:

    def test_something(client, auth_chat_id):
        resp = client.get(f"/api/miniapp/watchlist?chat_id={auth_chat_id}")
        ...
"""

from __future__ import annotations

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock

# ── Make the project root importable ────────────────────────────────────────
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# ── Test constants ────────────────────────────────────────────────────────────
TEST_CHAT_ID   = "9999999"
FAKE_PRICES    = {
    "AAPL": 189.50,
    "NVDA": 875.25,
    "BTC":  68000.0,
    "MSFT": 415.10,
    "TSLA": 172.30,
}


# ── In-memory Gist store ─────────────────────────────────────────────────────
class _MemStore:
    """Thread-local in-memory replacement for GitHub Gist storage."""

    def __init__(self):
        self._data: dict[str, dict] = {}

    def load(self, filename: str) -> dict | None:
        return self._data.get(filename)

    def write(self, filename: str, data: dict) -> None:
        # Store a deep copy so mutations don't bleed between calls
        self._data[filename] = json.loads(json.dumps(data))

    def reset(self):
        self._data.clear()


_store = _MemStore()


# ── Session-scoped env vars ───────────────────────────────────────────────────
@pytest.fixture(scope="session", autouse=True)
def _env_vars():
    """Set minimal env vars so imports don't explode."""
    env_patch = {
        "TELEGRAM_BOT_TOKEN": "fake_bot_token",
        "TELEGRAM_CHAT_ID":   TEST_CHAT_ID,
        "FLASK_SECRET_KEY":   "test_secret",
        "GH_GIST_TOKEN":      "fake_gist_token",
        "GIST_ID":            "fake_gist_id",
        "ANTHROPIC_API_KEY":  "fake_anthropic_key",
        "ALPACA_KEY_ID":      "fake_alpaca_key",
        "ALPACA_SECRET_KEY":  "fake_alpaca_secret",
        "POLYGON_API_KEY":    "fake_polygon_key",
    }
    with patch.dict(os.environ, env_patch):
        yield


# ── Mini-app auth bypass ──────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _miniapp_auth_bypass(monkeypatch):
    """
    Tests authenticate by passing chat_id directly (the get/post helpers inject
    TEST_CHAT_ID). Bypass the Telegram initData HMAC check, but KEEP the allowlist
    gate so the rejection tests (no/invalid chat_id → 403) still pass. The real
    HMAC verification is covered directly by TestMiniappAuthSecurity.
    """
    try:
        import webhook
    except Exception:
        yield
        return
    from flask import request as _req

    def _fake_auth():
        if _req.method == "POST":
            body = _req.get_json(silent=True) or {}
            cid  = str(body.get("chat_id", "") or _req.args.get("chat_id", "")).strip()
        else:
            cid = str(_req.args.get("chat_id", "")).strip()
        if not cid or cid == "0":
            return None
        allowed = webhook.get_allowed_users()
        owner   = os.environ.get("TELEGRAM_CHAT_ID", "")
        if cid not in allowed and cid != owner:
            return None
        return cid

    monkeypatch.setattr(webhook, "_miniapp_auth", _fake_auth)
    yield


# ── Storage patches ───────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _gist_store(monkeypatch):
    """Replace _load_gist_file / _write_gist_file with in-memory store."""
    _store.reset()

    import config_manager
    monkeypatch.setattr(config_manager, "_load_gist_file", _store.load)
    monkeypatch.setattr(config_manager, "_write_gist_file", _store.write)

    import price_alert_manager
    monkeypatch.setattr(price_alert_manager, "_load_gist_file", _store.load)  # type: ignore
    monkeypatch.setattr(price_alert_manager, "_write_gist_file", _store.write)  # type: ignore

    # Seed a minimal config so get_allowed_users() returns TEST_CHAT_ID
    _store.write("config.json", {
        "allowed_users": [TEST_CHAT_ID],
    })
    yield _store


# ── Price API patches ─────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _mock_prices(monkeypatch):
    """Patch all live-price functions to return FAKE_PRICES without network calls."""
    import market_data

    def _fake_get_live_price(ticker: str) -> float | None:
        return FAKE_PRICES.get(ticker.upper())

    def _fake_get_live_prices(tickers: list) -> dict:
        return {t.upper(): FAKE_PRICES[t.upper()] for t in tickers if t.upper() in FAKE_PRICES}

    def _fake_get_ohlcv(ticker: str, days: int = 92) -> list | None:
        # Return 5 days of synthetic daily bars
        base = FAKE_PRICES.get(ticker.upper(), 100.0)
        return [
            {"time": f"2025-01-{d:02d}", "open": base, "high": base * 1.02,
             "low": base * 0.98, "close": base * (1 + (d - 3) * 0.005), "volume": 1_000_000}
            for d in range(1, 6)
        ]

    monkeypatch.setattr(market_data, "get_live_price",  _fake_get_live_price)
    monkeypatch.setattr(market_data, "get_live_prices", _fake_get_live_prices)
    monkeypatch.setattr(market_data, "get_ohlcv",       _fake_get_ohlcv)
    monkeypatch.setattr(market_data, "validate_ticker", lambda t: True)

    # Also patch the alert manager's internal price fetcher
    import price_alert_manager
    monkeypatch.setattr(price_alert_manager, "_current_price",
                        lambda ticker: FAKE_PRICES.get(ticker.upper()))
    yield


# ── Flask test client ─────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def app():
    """Create the Flask app in testing mode (session-scoped for speed)."""
    import webhook
    webhook.app.config["TESTING"] = True
    webhook.app.config["SECRET_KEY"] = "test_secret"
    return webhook.app


@pytest.fixture
def client(app, _gist_store, _mock_prices):
    """Per-test Flask test client with auth pre-wired."""
    with app.test_client() as c:
        yield c


# ── Auth helper ───────────────────────────────────────────────────────────────
@pytest.fixture
def auth_chat_id():
    return TEST_CHAT_ID


@pytest.fixture
def qs(auth_chat_id):
    """Query string dict that satisfies _miniapp_auth() for GET requests."""
    return {"chat_id": auth_chat_id}


@pytest.fixture
def post_auth(auth_chat_id):
    """Base JSON body dict that satisfies _miniapp_auth() for POST requests."""
    return {"chat_id": auth_chat_id}


# ── Convenience helpers ───────────────────────────────────────────────────────
def get(client, url, **params):
    """GET with auth chat_id auto-injected."""
    params.setdefault("chat_id", TEST_CHAT_ID)
    return client.get(url, query_string=params)


def post(client, url, body: dict | None = None):
    """POST with auth chat_id auto-injected into JSON body."""
    data = {"chat_id": TEST_CHAT_ID}
    if body:
        data.update(body)
    return client.post(url, json=data)
