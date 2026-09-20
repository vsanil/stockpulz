"""sim_portfolio — the synthetic trader's book and its cash-flow-matched SPY twin.

The twin is the point: it receives the SAME dollars on the SAME days, so the only
difference between the two curves is what was bought. Every test here pins a
property that, if broken, would make the Phase-1 headline lie in one direction.
"""
import json
import math

import sim_portfolio as sp


def _doc(eq=10_000.0):
    return sp.new_doc("900000001", "2026-09-22", eq)


class TestTheTwinMirrorsCashFlows:
    def test_a_buy_puts_the_same_dollars_into_spy(self):
        d = sp.record_buy(_doc(), "AAPL", 1_000.0, 500.0, "2026-09-22")
        assert d["twin_cash"] == 9_000.0
        assert d["twin_lots"]["AAPL"]["units"] == 2.0          # $1000 / $500
        assert d["flows"][-1]["kind"] == "buy" and d["flows"][-1]["usd"] == 1000.0

    def test_a_sell_closes_the_lot_at_the_NEW_spy_price(self):
        d = sp.record_buy(_doc(), "AAPL", 1_000.0, 500.0, "2026-09-22")
        d = sp.record_sell(d, "AAPL", 1_200.0, 550.0, "2026-10-01", outcome="target")
        # 9,000 cash + 2 units × $550 = $1,100 back: twin +$100, bot +$200
        assert d["twin_cash"] == 10_100.0
        assert "AAPL" not in d["twin_lots"]
        assert d["flows"][-1]["outcome"] == "target" and d["flows"][-1]["twin_lot"] is True

    def test_a_pre_book_position_has_no_twin_lot_and_the_twin_is_untouched(self):
        """Legacy positions liquidated after the book starts must neither help
        nor hurt the twin — they were never matched."""
        d = sp.record_sell(_doc(), "OLD", 480.0, 500.0, "2026-09-23")
        assert d["twin_cash"] == 10_000.0
        assert d["flows"][-1]["twin_lot"] is False

    def test_a_second_buy_of_the_same_ticker_adds_to_the_lot(self):
        d = sp.record_buy(_doc(), "X", 500.0, 500.0, "2026-09-22")
        d = sp.record_buy(d, "X", 500.0, 250.0, "2026-09-23")
        assert d["twin_lots"]["X"]["units"] == 3.0 and d["twin_lots"]["X"]["usd"] == 1000.0

    def test_garbage_inputs_record_NOTHING(self):
        """Unmeasurable is never a flow — an invented twin buy would fabricate alpha."""
        for usd, px in ((None, 500), (0, 500), (float("nan"), 500), (100, None), (100, 0), (100, float("inf"))):
            d = sp.record_buy(_doc(), "X", usd, px, "2026-09-22")
            assert d["twin_cash"] == 10_000.0 and not d["twin_lots"] and not d["flows"]

    def test_a_sell_with_no_spy_price_KEEPS_the_lot(self):
        d = sp.record_buy(_doc(), "X", 1_000.0, 500.0, "2026-09-22")
        d = sp.record_sell(d, "X", 900.0, None, "2026-09-23")
        assert "X" in d["twin_lots"], "losing the lot would silently shrink the twin"
        assert d["twin_cash"] == 9_000.0


class TestSnapshots:
    def test_one_point_per_date_and_the_later_mark_wins(self):
        d = sp.snapshot(_doc(), "2026-09-22", 10_000.0, 500.0, 0)
        d = sp.snapshot(d, "2026-09-22", 10_050.0, 500.0, 1)      # re-run the same day
        assert len(d["snapshots"]) == 1 and d["snapshots"][0]["bot"] == 10_050.0

    def test_returns_are_relative_to_the_starting_equity(self):
        d = sp.record_buy(_doc(), "X", 5_000.0, 500.0, "2026-09-22")
        d = sp.snapshot(d, "2026-09-23", 10_500.0, 550.0, 1)      # SPY +10% on half the book
        s = d["snapshots"][-1]
        assert s["bot_ret_pct"] == 5.0
        assert s["twin"] == 10_500.0 and s["twin_ret_pct"] == 5.0

    def test_nothing_is_written_when_the_twin_cannot_be_marked(self):
        d = sp.record_buy(_doc(), "X", 5_000.0, 500.0, "2026-09-22")
        d = sp.snapshot(d, "2026-09-23", 10_500.0, None, 1)
        assert d["snapshots"] == []

    def test_nothing_is_written_for_a_nan_bot_equity(self):
        d = sp.snapshot(_doc(), "2026-09-23", float("nan"), 500.0, 0)
        assert d["snapshots"] == []

    def test_the_curve_is_capped_and_sorted(self):
        d = _doc()
        for i in range(sp.MAX_SNAPSHOTS + 5):
            sp.snapshot(d, f"2030-01-{i:05d}", 10_000.0, 500.0, 0)
        assert len(d["snapshots"]) == sp.MAX_SNAPSHOTS
        assert d["snapshots"][0]["date"] == "2030-01-00005"          # oldest dropped

    def test_flows_are_capped(self):
        d = _doc()
        for i in range(sp.MAX_FLOWS + 3):
            sp.record_buy(d, f"T{i}", 1.0, 500.0, "2026-09-22")
        assert len(d["flows"]) == sp.MAX_FLOWS


class TestSummary:
    def test_inactive_until_the_first_snapshot(self):
        s = sp.summary(_doc())
        assert s["active"] is False and "No equity curve yet" in s["note"]
        assert sp.summary(None)["active"] is False

    def test_alpha_is_bot_minus_twin(self):
        d = sp.record_buy(_doc(), "X", 5_000.0, 500.0, "2026-09-22")
        d = sp.snapshot(d, "2026-09-23", 10_800.0, 550.0, 1)      # bot +8, twin +5
        s = sp.summary(d)
        assert s["alpha_pct"] == 3.0
        assert s["buys"] == 1 and s["sells"] == 0 and s["days"] == 1
        assert s["sample_warning"], "one day must be flagged as directional"

    def test_the_summary_is_json_safe_with_no_nan_anywhere(self):
        d = sp.snapshot(_doc(), "2026-09-23", 10_000.0, 500.0, 0)
        d["snapshots"][0]["bot_ret_pct"] = float("nan")             # a poisoned stored row
        json.dumps(sp.summary(d), allow_nan=False)                    # raises on NaN

    def test_max_drawdown_is_order_dependent_and_measured_from_the_peak(self):
        assert sp.max_drawdown_pct([100, 150, 75, 120]) == 50.0
        assert sp.max_drawdown_pct([75, 100, 150]) == 0.0
        assert sp.max_drawdown_pct([]) is None
        assert sp.max_drawdown_pct([float("nan"), 100, 90]) == 10.0


class TestBotEquity:
    def test_cash_plus_marked_positions(self):
        paper = {"cash": 5_000.0, "positions": [{"ticker": "A", "shares": 10, "avg_price": 100.0}]}
        assert sp.bot_equity(paper, lambda t: 110.0) == 6_100.0

    def test_any_unpriceable_position_makes_the_mark_unusable(self):
        """A partial mark reads as a drawdown that never happened."""
        paper = {"cash": 5_000.0, "positions": [{"ticker": "A", "shares": 10, "avg_price": 100.0},
                                                 {"ticker": "B", "shares": 1, "avg_price": 50.0}]}
        assert sp.bot_equity(paper, lambda t: 110.0 if t == "A" else None) is None

    def test_a_garbage_tick_is_rejected_against_the_entry(self):
        """The Jul-3 holiday feed: $0.01 for a $100 stock must not mark the book."""
        paper = {"cash": 0.0, "positions": [{"ticker": "A", "shares": 10, "avg_price": 100.0}]}
        assert sp.bot_equity(paper, lambda t: 0.01) is None

    def test_an_empty_book_is_its_cash(self):
        assert sp.bot_equity({"cash": 1234.5, "positions": []}, lambda t: None) == 1234.5


class TestStorage:
    def test_update_and_load_round_trip_through_the_store(self, _gist_store):
        sp.update("900000001", lambda d: sp.new_doc("900000001", "2026-09-22", 10_000.0))
        doc = sp.load("900000001")
        assert doc and doc["starting_equity"] == 10_000.0

    def test_a_none_from_the_mutator_declines_the_write(self, _gist_store):
        assert sp.load("555") is None
        sp.update("555", lambda d: None)
        assert sp.load("555") is None

    def test_books_are_keyed_by_account_so_arms_can_coexist(self, _gist_store):
        sp.update("1", lambda d: sp.new_doc("1", "2026-09-22", 10_000.0))
        sp.update("2", lambda d: sp.new_doc("2", "2026-09-22", 20_000.0))
        assert sp.load("1")["starting_equity"] == 10_000.0
        assert sp.load("2")["starting_equity"] == 20_000.0

    def test_the_engine_never_imports_the_book(self):
        """MEASUREMENT, never INPUT — the contamination evaluate_picks was built to avoid."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        for f in ("screener.py", "ai_analyzer.py", "agent.py", "position_sizer.py"):
            assert "sim_portfolio" not in (root / f).read_text(), f
