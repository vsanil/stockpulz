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


def _expected_days_to_lt(opened):
    """First long-term day = the one-year anniversary PLUS one, since selling
    ON the anniversary is still short-term. Computed here independently of
    config_manager so the test does not check the helper against itself."""
    from datetime import date, timedelta
    try:
        anniv = opened.replace(year=opened.year + 1)
    except ValueError:                      # Feb 29
        anniv = opened.replace(year=opened.year + 1, month=3, day=1)
    return (anniv + timedelta(days=1) - date.today()).days


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

    def test_the_one_year_ANNIVERSARY_is_still_short_term(self, client, log):
        """🔴 Corrected Aug 19. The IRS test is held MORE THAN one year, and the
        holding period starts the day after acquisition — so selling on the
        anniversary is short-term. The old `days_held >= 365` called it
        long-term, taxing it at 15% instead of ~35%: it UNDER-stated what was
        owed, the harmful direction for anyone planning around it."""
        log["closed"] = [_closed("ANNIV", "2025-01-01", "2026-01-01", 100.0),
                         _closed("PLUS1", "2025-01-01", "2026-01-02", 100.0)]
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["st_count"] == 1 and r["lt_count"] == 1, \
            "the anniversary itself must be short-term; one day later is long"

    def test_a_leap_year_span_is_measured_on_the_calendar_not_in_days(
            self, client, log):
        """366 days across a leap year is still exactly one year — short-term.
        A day count cannot express this, which is why the rule is date-based."""
        log["closed"] = [_closed("LEAP", "2024-01-01", "2025-01-01", 100.0)]
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["st_count"] == 1 and r["lt_count"] == 0

    def test_synthetic_bot_trades_never_enter_a_tax_figure(self, client, log):
        """A robot's mechanical fills carry no real tax liability."""
        import config_manager
        log["closed"] = [_closed("REAL", "2025-01-01", "2025-03-01", 100.0),
                         _closed("BOT", "2025-01-01", "2025-03-01", 5000.0,
                                 source=config_manager.SYNTHETIC_SOURCE)]
        r = get(client, "/api/miniapp/tax_lots").get_json()["realized"]
        assert r["total_gain"] == 100.0, "a bot trade entered the tax total"

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
        opened = date.today() - timedelta(days=330)
        log["open"] = [_open("AAPL", opened.isoformat())]
        tip = get(client, "/api/miniapp/tax_lots").get_json()["open_fifo_lifo"][0]
        assert f"Turns LT in {_expected_days_to_lt(opened)}d" in tip["tax_tip"], \
            f"got {tip['tax_tip']!r}"

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
        young = date.today() - timedelta(days=100)
        log["open"] = [_open("A", young.isoformat()),
                       _open("B", (date.today() - timedelta(days=400)).isoformat())]
        lots = {l["ticker"]: l
                for g in get(client, "/api/miniapp/tax_lots").get_json()["open_fifo_lifo"]
                for l in g["lots"]}
        assert lots["A"]["days_to_lt"] == _expected_days_to_lt(young)
        assert lots["A"]["is_lt"] is False
        assert lots["B"]["days_to_lt"] is None and lots["B"]["is_lt"] is True

    def test_the_countdown_is_one_day_LONGER_than_the_old_naive_count(
            self, client, log):
        """Documents the correction: `365 - days_held` was a day short, because
        the anniversary itself is still short-term."""
        from datetime import date, timedelta
        opened = date.today() - timedelta(days=200)
        log["open"] = [_open("A", opened.isoformat())]
        lot = get(client, "/api/miniapp/tax_lots").get_json()[
            "open_fifo_lifo"][0]["lots"][0]
        assert lot["days_to_lt"] == (365 - 200) + 1


class TestAuth:
    def test_it_requires_authentication(self, client):
        assert client.get("/api/miniapp/tax_lots").status_code == 403
