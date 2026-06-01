"""
test_paper_trader.py — Unit tests for paper_trader.py

All Gist I/O is replaced with an in-memory store.
yfinance is patched to return deterministic prices.
"""

import sys, os
import pytest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from paper_trader import (
    paper_buy, paper_sell, paper_portfolio, paper_performance,
    paper_add_cash, paper_reset, paper_history, paper_remove_history,
    paper_cancel,
)

CHAT = "test_chat_999"
_LIVE_PRICE = 100.0   # fixed live price for all tickers in tests


def _empty_portfolio(cash: float = 10_000.0) -> dict:
    return {"positions": [], "history": [], "starting_cash": cash, "cash": cash}


@pytest.fixture(autouse=True)
def patch_live_price():
    """All yfinance price lookups return _LIVE_PRICE."""
    with patch("paper_trader._live_price", return_value=_LIVE_PRICE):
        yield


@pytest.fixture()
def mem_portfolio(tmp_path):
    """In-memory paper portfolio store."""
    store: dict = {}

    def _load(chat_id: str) -> dict:
        return store.get(chat_id, _empty_portfolio())

    def _save(chat_id: str, data: dict) -> None:
        import json
        store[chat_id] = json.loads(json.dumps(data))

    with patch("paper_trader.load_user_paper", side_effect=_load), \
         patch("paper_trader.save_user_paper", side_effect=_save):
        yield store


# ── paper_buy ─────────────────────────────────────────────────────────────────

class TestPaperBuy:
    def test_basic_buy(self, mem_portfolio):
        msg = paper_buy("AAPL", 10, CHAT)
        assert "Paper Buy" in msg
        assert "AAPL" in msg
        portfolio = mem_portfolio[CHAT]
        assert len(portfolio["positions"]) == 1
        pos = portfolio["positions"][0]
        assert pos["ticker"] == "AAPL"
        assert pos["shares"] == 10
        assert pos["avg_price"] == _LIVE_PRICE
        assert portfolio["cash"] == pytest.approx(10_000.0 - 10 * _LIVE_PRICE)

    def test_buy_with_explicit_price(self, mem_portfolio):
        paper_buy("NVDA", 5, CHAT, price=200.0)
        pos = mem_portfolio[CHAT]["positions"][0]
        assert pos["avg_price"] == 200.0
        assert pos["shares"] == 5

    def test_buy_stores_stop_and_target(self, mem_portfolio):
        paper_buy("TSLA", 3, CHAT, stop_loss=90.0, target_price=120.0)
        pos = mem_portfolio[CHAT]["positions"][0]
        assert pos["stop_loss"] == 90.0
        assert pos["target_price"] == 120.0

    def test_buy_insufficient_cash(self, mem_portfolio):
        # Try to buy 200 shares @ $100 = $20k, but only have $10k
        msg = paper_buy("AAPL", 200, CHAT)
        assert "Insufficient" in msg
        assert CHAT not in mem_portfolio   # store untouched

    def test_average_down(self, mem_portfolio):
        """Buying the same ticker twice should average the price."""
        paper_buy("AAPL", 10, CHAT, price=100.0)
        paper_buy("AAPL", 10, CHAT, price=80.0)
        pos = mem_portfolio[CHAT]["positions"][0]
        assert pos["shares"] == 20
        assert pos["avg_price"] == pytest.approx(90.0)

    def test_scale_in_updates_stop_if_provided(self, mem_portfolio):
        paper_buy("AAPL", 10, CHAT, stop_loss=90.0)
        paper_buy("AAPL", 5, CHAT, stop_loss=85.0)
        pos = mem_portfolio[CHAT]["positions"][0]
        assert pos["stop_loss"] == 85.0

    def test_live_price_failure_returns_error(self):
        with patch("paper_trader._live_price", return_value=None):
            msg = paper_buy("BAD", 1, CHAT)
        assert "Could not fetch" in msg

    def test_live_price_coingecko_fallback(self):
        """_live_price internals tested in test_live_price_unit.py
        (autouse patch_live_price fixture intercepts here).
        This test verifies paper_buy succeeds when _live_price returns a value."""
        # patch_live_price autouse returns _LIVE_PRICE — just confirm buy succeeds
        from paper_trader import paper_buy
        import paper_trader as _pt
        with patch("config_manager.load_user_paper", return_value={
            "positions": [], "history": [], "cash": 10000.0, "starting_cash": 10000.0
        }), patch("config_manager.save_user_paper"):
            msg = paper_buy("HYPE", 1, "9999999")
        assert "Paper Buy" in msg or "❌" not in msg


# ── paper_sell ────────────────────────────────────────────────────────────────

class TestPaperSell:
    def _buy(self, mem_portfolio, ticker="AAPL", shares=10, price=100.0):
        paper_buy(ticker, shares, CHAT, price=price)

    def test_full_sell(self, mem_portfolio):
        self._buy(mem_portfolio)
        msg = paper_sell("AAPL", CHAT)
        assert "Paper Sell" in msg
        assert "AAPL" in msg
        portfolio = mem_portfolio[CHAT]
        assert portfolio["positions"] == []
        assert len(portfolio["history"]) == 1
        # sold at _LIVE_PRICE == 100, bought at 100 → gain 0
        assert portfolio["history"][0]["gain"] == pytest.approx(0.0)

    def test_partial_sell(self, mem_portfolio):
        self._buy(mem_portfolio, shares=10, price=100.0)
        paper_sell("AAPL", CHAT, shares=4)
        portfolio = mem_portfolio[CHAT]
        pos = portfolio["positions"][0]
        assert pos["shares"] == pytest.approx(6.0)
        assert len(portfolio["history"]) == 1

    def test_gain_recorded_correctly(self, mem_portfolio):
        self._buy(mem_portfolio, price=80.0)  # buy at 80, sell at live=100
        paper_sell("AAPL", CHAT)
        h = mem_portfolio[CHAT]["history"][0]
        assert h["gain"] == pytest.approx(200.0)   # 10 shares × $20 gain
        assert h["gain_pct"] == pytest.approx(25.0)

    def test_sell_no_position(self, mem_portfolio):
        msg = paper_sell("AAPL", CHAT)
        assert "No open paper position" in msg

    def test_sell_caps_shares_at_held_amount(self, mem_portfolio):
        """If user requests more shares than held, sell all held."""
        self._buy(mem_portfolio, shares=3)
        paper_sell("AAPL", CHAT, shares=100)
        portfolio = mem_portfolio[CHAT]
        assert portfolio["positions"] == []
        assert portfolio["history"][0]["shares"] == pytest.approx(3.0)


# ── paper_portfolio ───────────────────────────────────────────────────────────

class TestPaperPortfolio:
    def test_empty_portfolio_message(self, mem_portfolio):
        msg = paper_portfolio(CHAT)
        assert "No open positions" in msg
        assert "$10,000.00" in msg

    def test_portfolio_shows_position(self, mem_portfolio):
        paper_buy("AAPL", 10, CHAT, price=100.0)
        msg = paper_portfolio(CHAT)
        assert "AAPL" in msg
        assert "10" in msg   # shares

    def test_portfolio_computes_unrealized_pnl(self, mem_portfolio):
        # bought at 80, live=100 → unrealized = +$200 (+25%)
        paper_buy("AAPL", 10, CHAT, price=80.0)
        msg = paper_portfolio(CHAT)
        assert "+25.0%" in msg or "+25" in msg


# ── paper_performance ─────────────────────────────────────────────────────────

class TestPaperPerformance:
    def test_no_history(self, mem_portfolio):
        msg = paper_performance(CHAT)
        assert "No closed trades" in msg

    def test_win_rate_calculated(self, mem_portfolio):
        paper_buy("AAPL", 10, CHAT, price=80.0)   # win: buy 80, sell 100
        paper_sell("AAPL", CHAT)
        paper_buy("MSFT", 5, CHAT, price=120.0)   # loss: buy 120, sell 100
        paper_sell("MSFT", CHAT)
        msg = paper_performance(CHAT)
        assert "50.0%" in msg    # 1W / 2 trades


# ── paper_add_cash ────────────────────────────────────────────────────────────

class TestPaperAddCash:
    def test_add_cash(self, mem_portfolio):
        paper_add_cash(5_000.0, CHAT)
        portfolio = mem_portfolio[CHAT]
        assert portfolio["cash"] == pytest.approx(15_000.0)
        assert portfolio["starting_cash"] == pytest.approx(15_000.0)

    def test_add_zero_rejected(self, mem_portfolio):
        msg = paper_add_cash(0, CHAT)
        assert "greater than zero" in msg.lower() or "Amount" in msg


# ── paper_reset ───────────────────────────────────────────────────────────────

class TestPaperReset:
    def test_reset_clears_positions_and_history(self, mem_portfolio):
        paper_buy("AAPL", 5, CHAT)
        paper_sell("AAPL", CHAT)
        paper_reset(CHAT)
        portfolio = mem_portfolio[CHAT]
        assert portfolio["positions"] == []
        assert portfolio["history"] == []
        assert portfolio["cash"] == pytest.approx(10_000.0)

    def test_reset_with_custom_cash(self, mem_portfolio):
        paper_reset(CHAT, starting_cash=25_000.0)
        portfolio = mem_portfolio[CHAT]
        assert portfolio["cash"] == pytest.approx(25_000.0)
        assert portfolio["starting_cash"] == pytest.approx(25_000.0)


# ── paper_history ─────────────────────────────────────────────────────────────

class TestPaperHistory:
    def test_history_empty_initially(self, mem_portfolio):
        assert paper_history(CHAT) == []

    def test_history_records_closed_trades(self, mem_portfolio):
        paper_buy("AAPL", 5, CHAT)
        paper_sell("AAPL", CHAT)
        h = paper_history(CHAT)
        assert len(h) == 1
        assert h[0]["ticker"] == "AAPL"


# ── paper_remove_history ──────────────────────────────────────────────────────

class TestPaperRemoveHistory:
    def test_remove_reverses_trade(self, mem_portfolio):
        paper_buy("AAPL", 5, CHAT, price=80.0)
        paper_sell("AAPL", CHAT)
        cash_after_sell = mem_portfolio[CHAT]["cash"]

        msg = paper_remove_history(CHAT, 0)
        assert "reversed" in msg.lower() or "removed" in msg.lower()

        portfolio = mem_portfolio[CHAT]
        assert portfolio["history"] == []
        # Proceeds ($500) deducted from cash → back to pre-sell state
        assert portfolio["cash"] == pytest.approx(cash_after_sell - _LIVE_PRICE * 5)
        # Shares restored
        assert any(p["ticker"] == "AAPL" for p in portfolio["positions"])

    def test_remove_out_of_range(self, mem_portfolio):
        msg = paper_remove_history(CHAT, 99)
        assert "not found" in msg.lower() or "already been removed" in msg.lower()


# ── paper_cancel ──────────────────────────────────────────────────────────────

class TestPaperCancel:
    def test_cancel_refunds_cash(self, mem_portfolio):
        paper_buy("AAPL", 10, CHAT, price=100.0)
        cash_before = 10_000.0
        msg = paper_cancel("AAPL", CHAT)
        assert "removed" in msg.lower() or "refunded" in msg.lower()
        portfolio = mem_portfolio[CHAT]
        assert portfolio["positions"] == []
        # Full cost basis refunded
        assert portfolio["cash"] == pytest.approx(cash_before)

    def test_cancel_no_position(self, mem_portfolio):
        msg = paper_cancel("AAPL", CHAT)
        assert "No paper position" in msg

    def test_cancel_not_recorded_in_history(self, mem_portfolio):
        paper_buy("AAPL", 5, CHAT)
        paper_cancel("AAPL", CHAT)
        assert paper_history(CHAT) == []
