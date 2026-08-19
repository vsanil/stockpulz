"""/api/miniapp/my_performance — the user's own realised track record.

77 lines, no test hit its path. These are the numbers a user judges the bot by,
and max_drawdown in particular is order-dependent: it walks a cumulative P&L
curve, so a broken sort produces a plausible-looking but wrong risk figure.
"""
import pytest

from tests.conftest import get


def _closed(ticker, ret, gain, date, **extra):
    return {"ticker": ticker, "return_pct": ret, "gain_usd": gain,
            "closed_date": date, **extra}


@pytest.fixture
def log(monkeypatch):
    store = {"open": [], "closed": []}
    import config_manager
    monkeypatch.setattr(config_manager, "load_user_trade_log", lambda uid: store)
    return store


def _summary(client):
    return get(client, "/api/miniapp/my_performance").get_json()["summary"]


class TestSummary:
    def test_win_rate_counts_a_flat_trade_as_a_loss(self, client, log):
        """Consistent with performance_tracker — a 0% trade is not a win."""
        log["closed"] = [_closed("A", 10, 100, "2026-01-01"),
                         _closed("B", 0, 0, "2026-01-02"),
                         _closed("C", -5, -50, "2026-01-03")]
        s = _summary(client)
        assert s["wins"] == 1 and s["losses"] == 2
        assert s["win_rate"] == 33

    def test_totals_and_average_are_reported(self, client, log):
        log["closed"] = [_closed("A", 10, 100, "2026-01-01"),
                         _closed("B", -4, -40, "2026-01-02")]
        s = _summary(client)
        assert s["total_trades"] == 2
        assert s["total_pnl_usd"] == 60.0
        assert s["avg_return"] == 3.0

    def test_best_and_worst_are_identified(self, client, log):
        log["closed"] = [_closed("MID", 5, 50, "2026-01-01"),
                         _closed("TOP", 40, 400, "2026-01-02"),
                         _closed("BOT", -20, -200, "2026-01-03")]
        s = _summary(client)
        assert s["best"]["ticker"] == "TOP" and s["worst"]["ticker"] == "BOT"

    def test_backtest_trades_never_enter_the_users_record(self, client, log):
        log["closed"] = [_closed("REAL", 10, 100, "2026-01-01"),
                         _closed("SIM", 999, 9999, "2026-01-02", source="backtest")]
        s = _summary(client)
        assert s["total_trades"] == 1 and s["total_pnl_usd"] == 100.0

    def test_gain_is_inferred_when_only_prices_are_stored(self, client, log):
        log["closed"] = [{"ticker": "A", "return_pct": 10, "closed_date": "2026-01-01",
                          "entry_price": 100.0, "exit_price": 110.0, "shares": 10}]
        assert _summary(client)["total_pnl_usd"] == 100.0

    def test_an_empty_log_returns_an_empty_summary_not_an_error(self, client, log):
        r = get(client, "/api/miniapp/my_performance").get_json()
        assert r["ok"] and r["trades"] == [] and r["summary"] == {}


class TestMaxDrawdown:
    """Order-dependent: the curve is walked chronologically, so a broken sort
    yields a wrong number that still looks reasonable."""

    def test_drawdown_is_the_worst_peak_to_trough_on_the_cumulative_curve(
            self, client, log):
        # cum: +100, +300, +150, +250  → peak 300, trough 150 → dd = 150
        log["closed"] = [_closed("A", 5, 100, "2026-01-01"),
                         _closed("B", 5, 200, "2026-01-02"),
                         _closed("C", -5, -150, "2026-01-03"),
                         _closed("D", 5, 100, "2026-01-04")]
        assert _summary(client)["max_drawdown"] == 150.0

    def test_it_is_computed_in_DATE_order_not_storage_order(self, client, log):
        """Same trades, shuffled in storage — the answer must not move.

        The data is chosen so the two orders genuinely DISAGREE. An earlier
        version of this test shuffled trades that happened to yield the same
        drawdown either way, so deleting the sort still passed — the mutation
        is what exposed it. Chronologically (+100, −50, +100, −50) the worst
        peak-to-trough is 50; with the losses first (−50, −50, +100, +100) it
        is 100.
        """
        log["closed"] = [_closed("B", -5, -50, "2026-01-02"),
                         _closed("D", -5, -50, "2026-01-04"),
                         _closed("A", 5, 100, "2026-01-01"),
                         _closed("C", 5, 100, "2026-01-03")]
        assert _summary(client)["max_drawdown"] == 50.0, \
            "drawdown was computed in storage order, not chronological order"

    def test_a_monotonically_rising_curve_has_no_drawdown(self, client, log):
        log["closed"] = [_closed("A", 5, 100, "2026-01-01"),
                         _closed("B", 5, 100, "2026-01-02")]
        assert _summary(client)["max_drawdown"] == 0.0

    def test_an_all_losing_book_reports_the_full_decline(self, client, log):
        log["closed"] = [_closed("A", -5, -100, "2026-01-01"),
                         _closed("B", -5, -100, "2026-01-02")]
        s = _summary(client)
        assert s["max_drawdown"] == 200.0 and s["total_pnl_usd"] == -200.0


class TestTradeList:
    def test_trades_are_returned_most_recent_first(self, client, log):
        log["closed"] = [_closed("OLD", 1, 10, "2026-01-01"),
                         _closed("NEW", 2, 20, "2026-01-05")]
        rows = get(client, "/api/miniapp/my_performance").get_json()["trades"]
        assert [r["ticker"] for r in rows] == ["NEW", "OLD"]

    def test_absent_prices_render_as_null_not_zero(self, client, log):
        """$0.00 would read as a real fill price; null renders as unknown."""
        log["closed"] = [_closed("A", 5, 50, "2026-01-01")]
        row = get(client, "/api/miniapp/my_performance").get_json()["trades"][0]
        assert row["entry_price"] is None and row["shares"] is None

    def test_the_exit_reason_is_carried_through(self, client, log):
        """stop vs target vs manual is the only way exits stay measurable."""
        log["closed"] = [_closed("A", -5, -50, "2026-01-01", outcome="stop")]
        assert get(client, "/api/miniapp/my_performance").get_json()[
            "trades"][0]["outcome"] == "stop"


class TestAuth:
    def test_it_requires_authentication(self, client):
        assert client.get("/api/miniapp/my_performance").status_code == 403
