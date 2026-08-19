"""/api/miniapp/tax_lots — realized ST/LT split, tax estimates, FIFO/LIFO tips.

119 lines and no test hit its path. full_sweep drives it in production, so it
returns 200 — but a 200 only proves it does not crash, never that the money
figures are right. Every number here is one a user could plan a tax decision
around.

⚠️ The endpoint labels itself "rough federal tax estimates (guidance only)" and
these tests pin the code's stated rules, not tax law.
"""
import pytest

from tests.conftest import get, TEST_CHAT_ID


def _closed(ticker, opened, closed_d, gain=None, **extra):
    row = {"ticker": ticker, "opened_date": opened, "closed_date": closed_d, **extra}
    if gain is not None:
        row["gain_usd"] = gain
    return row


def _open(ticker, opened, shares=10, entry=100.0):
    return {"ticker": ticker, "opened_date": opened,
            "shares": shares, "entry_price": entry}


@pytest.fixture
def log(monkeypatch):
    """Install a trade log for the authenticated user."""
    store = {"open": [], "closed": []}
    import config_manager
    monkeypatch.setattr(config_manager, "load_user_trade_log", lambda uid: store)
    return store


class TestRealizedSplit:
    def test_a_hold_over_a_year_counts_as_long_term(self, client, log):
        log["closed"] = [_closed("AAPL", "2024-01-01", "2025-06-01", 500.0)]
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["lt_count"] == 1 and r["st_count"] == 0
        assert r["lt_gain"] == 500.0

    def test_a_short_hold_counts_as_short_term(self, client, log):
        log["closed"] = [_closed("AAPL", "2025-01-01", "2025-03-01", 500.0)]
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["st_count"] == 1 and r["st_gain"] == 500.0

    def test_the_boundary_is_365_days_as_the_code_defines_it(self, client, log):
        """Pins the implementation's own rule. NOTE: the IRS test is 'more than
        one year', so a 365-day hold is arguably short-term — flagged, not
        silently changed, because it moves a tax figure."""
        log["closed"] = [_closed("A", "2024-01-01", "2024-12-31", 100.0),   # 365d
                         _closed("B", "2024-01-01", "2024-12-30", 100.0)]   # 364d
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["lt_count"] == 1 and r["st_count"] == 1

    def test_gain_is_derived_when_gain_usd_is_absent(self, client, log):
        log["closed"] = [_closed("AAPL", "2025-01-01", "2025-03-01",
                                 entry_price=100.0, exit_price=110.0, shares=10)]
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["st_gain"] == 100.0, "(110 - 100) * 10"

    def test_backtest_trades_are_excluded_from_the_tax_figures(self, client, log):
        """Simulated trades carry no tax liability and must never appear."""
        log["closed"] = [_closed("REAL", "2025-01-01", "2025-03-01", 100.0),
                         _closed("SIM", "2025-01-01", "2025-03-01", 9999.0,
                                 source="backtest")]
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["total_gain"] == 100.0, "a backtest trade entered the tax total"

    def test_losses_do_not_produce_a_negative_tax_estimate(self, client, log):
        """A loss owes no tax — a negative estimate would read as a refund."""
        log["closed"] = [_closed("AAPL", "2025-01-01", "2025-03-01", -500.0)]
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["st_gain"] == -500.0 and r["st_tax_est"] == 0.0

    def test_the_estimate_rates_are_the_documented_35_and_15(self, client, log):
        log["closed"] = [_closed("S", "2025-01-01", "2025-03-01", 1000.0),
                         _closed("L", "2023-01-01", "2025-03-01", 1000.0)]
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["st_tax_est"] == 350.0 and r["lt_tax_est"] == 150.0

    def test_an_undated_trade_does_not_crash_the_endpoint(self, client, log):
        log["closed"] = [{"ticker": "X", "gain_usd": 50.0}]
        res = get(client, "/api/miniapp/tax_lots")
        assert res.status_code == 200
        assert res.get_json()["realized"]["st_count"] == 1, \
            "an undated lot must default to short-term, never silently vanish"

    def test_an_empty_log_returns_zeros_not_an_error(self, client, log):
        r = get(client, "/api/miniapp/tax_lots").get_json()
        assert r["ok"] and r["realized"]["total_gain"] == 0.0
        assert r["lots"] == [] and r["open_fifo_lifo"] == []


class TestOpenLotGuidance:
    def test_a_lot_near_the_one_year_mark_is_flagged_to_wait(self, client, log):
        from datetime import date, timedelta
        opened = (date.today() - timedelta(days=330)).isoformat()
        log["open"] = [_open("AAPL", opened)]
        tip = get(client, "/api/miniapp/tax_lots").get_json()["open_fifo_lifo"][0]
        assert "Turns LT in 35d" in tip["tax_tip"]

    def test_a_young_lot_gets_no_wait_nudge(self, client, log):
        from datetime import date, timedelta
        log["open"] = [_open("AAPL", (date.today() - timedelta(days=30)).isoformat())]
        tip = get(client, "/api/miniapp/tax_lots").get_json()["open_fifo_lifo"][0]
        assert tip["tax_tip"] == "", "a 335-day wait is not actionable guidance"

    def test_an_existing_LT_lot_advises_selling_it_first(self, client, log):
        from datetime import date, timedelta
        log["open"] = [_open("AAPL", (date.today() - timedelta(days=400)).isoformat()),
                       _open("AAPL", (date.today() - timedelta(days=10)).isoformat())]
        tip = get(client, "/api/miniapp/tax_lots").get_json()["open_fifo_lifo"][0]
        assert tip["lt_count"] == 1 and tip["st_count"] == 1
        assert "sell LT first" in tip["tax_tip"]

    def test_lots_are_grouped_per_ticker(self, client, log):
        from datetime import date, timedelta
        d = (date.today() - timedelta(days=10)).isoformat()
        log["open"] = [_open("AAPL", d), _open("AAPL", d), _open("MSFT", d)]
        groups = get(client, "/api/miniapp/tax_lots").get_json()["open_fifo_lifo"]
        by = {g["ticker"]: g for g in groups}
        assert len(by["AAPL"]["lots"]) == 2 and len(by["MSFT"]["lots"]) == 1

    def test_days_to_lt_counts_down_and_clears_once_long_term(self, client, log):
        from datetime import date, timedelta
        log["open"] = [_open("A", (date.today() - timedelta(days=100)).isoformat()),
                       _open("B", (date.today() - timedelta(days=400)).isoformat())]
        lots = {l["ticker"]: l
                for g in get(client, "/api/miniapp/tax_lots").get_json()["open_fifo_lifo"]
                for l in g["lots"]}
        assert lots["A"]["days_to_lt"] == 265 and lots["A"]["is_lt"] is False
        assert lots["B"]["days_to_lt"] is None and lots["B"]["is_lt"] is True


class TestAuth:
    def test_it_requires_authentication(self, client):
        assert client.get("/api/miniapp/tax_lots").status_code == 403
