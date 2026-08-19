"""get_recent_stats — the numbers users are shown as a track record.

This feeds the morning message's performance bar and /stats. It had ZERO
coverage, and two of the rules it enforces exist because they were violated in
production:

  • The synthetic bot's 22 closed trades (13.6% win rate, −4.03% avg, while SPY
    was +1.53%) were being shown to users as the community record AND injected
    into Claude's stock-picking prompt, where the "BOT TRACK RECORD" block
    emits directives like "raise minimum threshold". A robot's mechanical
    stop-outs were steering real picks.
  • The panel once read "100% win rate" off three winners — far more misleading
    to a prospective user than showing nothing at all.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import config_manager
import performance_tracker as pt


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(pt, "_spy_return", lambda period: 1.5)


def _t(ret, asset="stock", days_ago=1, **extra):
    from datetime import date, timedelta
    return {"ticker": "AAPL", "return_pct": ret, "asset_type": asset,
            "closed_date": (date.today() - timedelta(days=days_ago)).isoformat(),
            **extra}


def _log(trades):
    return [{"closed": trades}]


class TestHonestyFloor:
    def test_below_the_floor_returns_None_rather_than_a_flattering_number(self):
        """Three winners must not render as '100% win rate'."""
        assert pt.get_recent_stats(_log([_t(5), _t(6), _t(7)])) is None

    def test_at_the_floor_it_reports(self):
        assert pt.get_recent_stats(_log([_t(5)] * pt._MIN_RECENT_TRADES)) is not None

    def test_the_floor_matches_its_sibling_constant(self):
        assert pt._MIN_RECENT_TRADES == 5 and pt._MIN_COMMUNITY_TRADES == 10, \
            "these gates are quoted in CLAUDE.md; changing one silently changes " \
            "what a stranger is shown as a track record"

    def test_no_trades_at_all_is_None_not_a_zero_row(self):
        assert pt.get_recent_stats([]) is None
        assert pt.get_recent_stats(_log([])) is None


class TestSyntheticTradesAreExcluded:
    """🔴 The bot's fills must never appear in a user-facing record."""

    def _mixed(self):
        bot = [_t(-4.0, source=config_manager.SYNTHETIC_SOURCE) for _ in range(20)]
        human = [_t(10.0) for _ in range(6)]
        return _log(bot + human)

    def test_bot_trades_do_not_enter_the_aggregate(self):
        s = pt.get_recent_stats(self._mixed())["total"]
        assert s["count"] == 6, f"synthetic trades leaked in: count={s['count']}"
        assert s["win_rate"] == 100.0
        assert s["avg_return"] == 10.0

    def test_bot_trades_alone_fall_below_the_floor(self):
        """20 bot trades must NOT satisfy the honesty gate on their own."""
        bot = [_t(-4.0, source=config_manager.SYNTHETIC_SOURCE) for _ in range(20)]
        assert pt.get_recent_stats(_log(bot)) is None

    def test_the_filter_is_human_trades_not_an_inline_check(self, monkeypatch):
        called = []
        real = config_manager.human_trades
        monkeypatch.setattr(pt, "human_trades",
                            lambda ts: (called.append(len(ts)), real(ts))[1])
        pt.get_recent_stats(self._mixed())
        assert called, "get_recent_stats bypassed the canonical human_trades filter"


class TestMedianVsAverage:
    """The morning bar uses median_return deliberately — one +441% crypto
    winner made avg_return misleading."""

    def test_a_single_outlier_moves_the_average_but_not_the_median(self):
        s = pt.get_recent_stats(_log([_t(2), _t(3), _t(4), _t(5), _t(441)]))["total"]
        assert s["median_return"] == 4.0
        assert s["avg_return"] > 90, "the outlier should dominate the mean"

    def test_both_keys_are_always_present(self):
        s = pt.get_recent_stats(_log([_t(5)] * 5))["total"]
        assert "avg_return" in s and "median_return" in s

    def test_an_even_count_medians_the_two_middle_values(self):
        s = pt.get_recent_stats(_log([_t(1), _t(2), _t(4), _t(8), _t(16), _t(32)]))["total"]
        assert s["median_return"] == 6.0     # (4 + 8) / 2


class TestStatsMath:
    def test_win_rate_counts_zero_as_a_loss_not_a_win(self):
        """A flat trade is not a win — it must not inflate the record."""
        s = pt.get_recent_stats(_log([_t(0), _t(0), _t(0), _t(10), _t(10)]))["total"]
        assert s["wins"] == 2 and s["losses"] == 3
        assert s["win_rate"] == 40.0

    def test_expectancy_matches_win_rate_times_gain_plus_loss(self):
        s = pt.get_recent_stats(_log([_t(10), _t(10), _t(10), _t(-5), _t(-5)]))["total"]
        # 0.6 * 10 + 0.4 * -5 = 4.0
        assert s["expectancy"] == pytest.approx(4.0, abs=0.01)
        assert s["avg_gain"] == 10.0 and s["avg_loss"] == -5.0

    def test_an_all_losing_book_reports_zero_percent_not_None(self):
        s = pt.get_recent_stats(_log([_t(-3)] * 5))["total"]
        assert s["win_rate"] == 0.0 and s["avg_gain"] == 0.0


class TestWindowAndSplit:
    def test_trades_outside_the_window_are_excluded(self):
        recent = [_t(10, days_ago=1) for _ in range(5)]
        old = [_t(-50, days_ago=90) for _ in range(20)]
        s = pt.get_recent_stats(_log(recent + old), days=30)["total"]
        assert s["count"] == 5 and s["avg_return"] == 10.0

    def test_stocks_and_crypto_split_out_separately(self):
        res = pt.get_recent_stats(_log(
            [_t(10, "stock")] * 3 + [_t(-4, "crypto")] * 3))
        assert res["stocks"]["count"] == 3 and res["crypto"]["count"] == 3
        assert res["total"]["count"] == 6

    def test_an_empty_category_is_None_not_a_zeroed_row(self):
        res = pt.get_recent_stats(_log([_t(10, "stock")] * 5))
        assert res["crypto"] is None, "an empty category must not render as 0%"

    def test_a_missing_return_pct_is_skipped(self):
        rows = [_t(10) for _ in range(5)] + [{"ticker": "X", "closed_date": "2099-01-01"}]
        assert pt.get_recent_stats(_log(rows))["total"]["count"] == 5

    def test_an_unavailable_spy_benchmark_is_None_not_zero(self, monkeypatch):
        """0.0 would read as 'the market was flat' — a claim we cannot make."""
        monkeypatch.setattr(pt, "_spy_return", lambda period: None)
        assert pt.get_recent_stats(_log([_t(5)] * 5))["spy_return"] is None


class _FastInfo:
    def __init__(self, px):
        self.last_price = px


class TestWeeklyRecap:
    """build_weekly_recap — the numbers in the Saturday recap message.

    150 lines, zero coverage. It aggregates a week of picks against live
    prices, so every failure mode here is a wrong figure a user reads as the
    bot's record for the week.
    """

    @pytest.fixture
    def wired(self, monkeypatch):
        import yfinance
        box = {"prices": {}, "cg": {}}
        monkeypatch.setattr(yfinance, "Ticker",
                            lambda t: type("T", (), {
                                "fast_info": _FastInfo(box["prices"].get(t))})())
        monkeypatch.setattr(pt, "_spy_return", lambda p: 0.5)

        class _Resp:
            status_code = 200

            def raise_for_status(self): pass

            def json(self): return box["cg"]

        monkeypatch.setattr(pt.requests, "get", lambda *a, **k: _Resp())
        return box

    def _picks(self, monkeypatch, weekly):
        monkeypatch.setattr(pt, "load_weekly_picks", lambda: weekly)

    def test_no_picks_this_week_returns_None(self, monkeypatch, wired):
        self._picks(monkeypatch, {})
        assert pt.build_weekly_recap() is None

    def test_a_stock_return_is_measured_against_the_AVERAGE_entry(
            self, monkeypatch, wired):
        """A ticker picked twice in one week has two entries; the recap must
        use their mean, not the first or last."""
        self._picks(monkeypatch, {
            "2026-08-17": {"stocks": {"short_term": [
                {"ticker": "AAPL", "entry_price": 100.0}]}},
            "2026-08-18": {"stocks": {"short_term": [
                {"ticker": "AAPL", "entry_price": 200.0}]}},
        })
        wired["prices"] = {"AAPL": 150.0, "SPY": 500.0}
        res = pt.build_weekly_recap()
        assert res["stocks"]["avg_return"] == 0.0, \
            "avg entry of 100 and 200 is 150 — a 150 price is flat, not +50%"

    def test_best_and_worst_are_reported(self, monkeypatch, wired):
        self._picks(monkeypatch, {"2026-08-17": {"stocks": {"short_term": [
            {"ticker": "WIN", "entry_price": 100.0},
            {"ticker": "LOSE", "entry_price": 100.0}]}}})
        wired["prices"] = {"WIN": 110.0, "LOSE": 90.0, "SPY": 500.0}
        s = pt.build_weekly_recap()["stocks"]
        assert s["best"][0] == "WIN" and s["worst"][0] == "LOSE"
        assert s["count"] == 2

    def test_one_unpriceable_ticker_does_not_sink_the_whole_recap(
            self, monkeypatch, wired):
        """Degrade per ticker — the same rule the EOD summary needed."""
        self._picks(monkeypatch, {"2026-08-17": {"stocks": {"short_term": [
            {"ticker": "GOOD", "entry_price": 100.0},
            {"ticker": "DEAD", "entry_price": 100.0}]}}})
        wired["prices"] = {"GOOD": 110.0, "SPY": 500.0}   # DEAD returns None
        s = pt.build_weekly_recap()["stocks"]
        assert s["count"] == 1 and s["best"][0] == "GOOD"

    def test_crypto_is_priced_through_coingecko_and_split_out(
            self, monkeypatch, wired):
        self._picks(monkeypatch, {"2026-08-17": {
            "stocks": {"short_term": [{"ticker": "AAPL", "entry_price": 100.0}]},
            "crypto": {"short_term": [
                {"symbol": "btc", "id": "bitcoin", "entry_price": 60000.0}]}}})
        wired["prices"] = {"AAPL": 110.0, "SPY": 500.0}
        wired["cg"] = {"bitcoin": {"usd": 66000.0}}
        res = pt.build_weekly_recap()
        assert res["stocks"]["count"] == 1
        assert res["crypto"]["count"] == 1
        assert res["crypto"]["avg_return"] == 10.0

    def test_a_coingecko_outage_leaves_stocks_intact(self, monkeypatch, wired):
        def boom(*a, **k):
            raise RuntimeError("429")
        monkeypatch.setattr(pt.requests, "get", boom)
        self._picks(monkeypatch, {"2026-08-17": {
            "stocks": {"short_term": [{"ticker": "AAPL", "entry_price": 100.0}]},
            "crypto": {"short_term": [
                {"symbol": "BTC", "id": "bitcoin", "entry_price": 60000.0}]}}})
        wired["prices"] = {"AAPL": 110.0, "SPY": 500.0}
        res = pt.build_weekly_recap()
        assert res["stocks"]["count"] == 1, "a crypto outage took the stocks with it"
        assert res["crypto"] is None

    def test_an_empty_category_is_None_not_a_zero_row(self, monkeypatch, wired):
        self._picks(monkeypatch, {"2026-08-17": {"stocks": {"short_term": [
            {"ticker": "AAPL", "entry_price": 100.0}]}}})
        wired["prices"] = {"AAPL": 110.0, "SPY": 500.0}
        assert pt.build_weekly_recap()["crypto"] is None

    def test_an_unavailable_spy_benchmark_is_None_not_zero(self, monkeypatch, wired):
        monkeypatch.setattr(pt, "_spy_return", lambda p: None)
        self._picks(monkeypatch, {"2026-08-17": {"stocks": {"short_term": [
            {"ticker": "AAPL", "entry_price": 100.0}]}}})
        wired["prices"] = {"AAPL": 110.0, "SPY": 500.0}
        assert pt.build_weekly_recap()["spy_return"] is None

    def test_a_pick_with_no_entry_price_is_skipped_not_counted_as_zero(
            self, monkeypatch, wired):
        self._picks(monkeypatch, {"2026-08-17": {"stocks": {"short_term": [
            {"ticker": "AAPL", "entry_price": 100.0},
            {"ticker": "NOENTRY"}]}}})
        wired["prices"] = {"AAPL": 110.0, "NOENTRY": 50.0, "SPY": 500.0}
        assert pt.build_weekly_recap()["stocks"]["count"] == 1
