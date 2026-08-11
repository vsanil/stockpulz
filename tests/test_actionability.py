"""Actionability: could a user have ACTED on the pick at the published price?

Deliberately separate from the evaluator, which asks whether picks were RIGHT
and needs ~1,500 observations. These are descriptive, so a few dozen fills are
already informative — and they separate "the engine chose badly" from "the stop
was too tight", which a win rate conflates.
"""
import os, sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from actionability import analyse, entry_slippage, stop_distances, outcome_mix


def _pick(t, d="2026-08-05", entry=100.0, tf="short_term"):
    return {"date": d, "ticker": t, "entry": entry, "timeframe": tf}


class TestEntryWindow:
    def test_fill_above_the_promised_window_is_flagged(self):
        """The real ANET case: published $190.51, filled $195.13 (+2.43%) with
        the message promising 'skip if above 2%'. A user obeying the instruction
        would have skipped it — so the pick was not actionable as published."""
        r = analyse([_pick("ANET", entry=190.51)],
                    [{"ticker": "ANET", "opened_date": "2026-08-05", "entry_price": 195.13}], [])
        assert r["entry"]["outside_window"] == 1
        assert r["entry"]["examples"][0]["slippage_pct"] == 2.43

    def test_filling_cheaper_is_never_a_breach(self):
        """Below the window is a better fill, not a broken promise."""
        r = analyse([_pick("X")], [{"ticker": "X", "opened_date": "2026-08-05",
                                    "entry_price": 90.0}], [])
        assert r["entry"]["outside_window"] == 0
        assert r["entry"]["median_slippage_pct"] == -10.0

    def test_long_term_picks_get_the_wider_window(self):
        pos = [{"ticker": "L", "opened_date": "2026-08-05", "entry_price": 102.5}]
        st = analyse([_pick("L", tf="short_term")], pos, [])
        lt = analyse([_pick("L", tf="long_term")], pos, [])
        assert st["entry"]["outside_window"] == 1, "2.5% breaches a 2% window"
        assert lt["entry"]["outside_window"] == 0, "2.5% is inside a 3% window"

    def test_one_observation_per_pick_not_per_fill(self):
        """🔴 The bot holds the same ticker as BOTH a real and a paper position,
        and both join to one pick. Counting both would weight whichever picks it
        happened to buy twice — reachability is a property of the pick."""
        r = analyse([_pick("A")], [
            {"ticker": "A", "opened_date": "2026-08-05", "entry_price": 105.0},
            {"ticker": "A", "bought_date": "2026-08-05", "avg_price": 105.0}], [])
        assert r["entry"]["n"] == 1

    def test_positions_with_no_matching_pick_are_ignored(self):
        r = analyse([_pick("A")], [{"ticker": "ZZZ", "opened_date": "2026-08-05",
                                    "entry_price": 10.0}], [])
        assert r["entry"]["n"] == 0 and r["entry"]["outside_pct"] is None

    def test_controls_are_not_treated_as_picks(self):
        led = [{**_pick("C"), "control": True}]
        r = analyse(led, [{"ticker": "C", "opened_date": "2026-08-05", "entry_price": 110.0}], [])
        assert r["entry"]["n"] == 0


class TestStops:
    def test_a_stop_inside_daily_noise_is_flagged(self):
        r = analyse([], [{"ticker": "T", "entry_price": 100.0, "stop_loss": 98.0}], [])
        assert r["stops"]["tight"] == 1

    def test_a_normal_stop_is_not_flagged(self):
        r = analyse([], [{"ticker": "T", "entry_price": 100.0, "stop_loss": 94.0}], [])
        assert r["stops"]["tight"] == 0 and r["stops"]["median_stop_pct"] == 6.0

    def test_inverted_stops_are_skipped_not_counted_as_zero(self):
        """Those are an INTEGRITY problem (position_audit's job); silently
        folding them in here as 0% would drag the median toward nonsense."""
        r = analyse([], [{"ticker": "T", "entry_price": 100.0, "stop_loss": 120.0}], [])
        assert r["stops"]["n"] == 0


class TestOutcomeGap:
    def test_all_manual_closes_are_reported_as_unusable(self):
        """🔴 The bot sells through close_trade, which stamps outcome='manual'
        whether it hit target or stop. It KNOWS why and does not record it —
        so report the gap instead of inventing a stop/target split."""
        m = outcome_mix([{"outcome": "manual"}] * 5)
        assert m["usable"] is False and "not record" in m["note"]

    def test_real_outcomes_become_usable(self):
        m = outcome_mix([{"outcome": "target"}, {"outcome": "stop"}])
        assert m["usable"] is True and m["counts"] == {"target": 1, "stop": 1}


class TestHonesty:
    def test_small_samples_carry_a_warning(self):
        r = analyse([_pick("A")], [{"ticker": "A", "opened_date": "2026-08-05",
                                    "entry_price": 101.0}], [])
        assert "not conclusive" in r["sample_warning"]

    def test_the_warning_clears_once_the_sample_is_real(self):
        led = [_pick(f"T{i}", d=f"2026-07-{i:02d}") for i in range(1, 32)]
        pos = [{"ticker": f"T{i}", "opened_date": f"2026-07-{i:02d}", "entry_price": 101.0}
               for i in range(1, 32)]
        assert analyse(led, pos, [])["sample_warning"] is None


class TestClosedPositionsStayInTheDenominator:
    """🔴 The regression this class exists for.

    COHR was the worst entry-window breach on record (+5.45% against a 3%
    promise). The moment the synthetic bot SOLD it, the observation vanished and
    the headline improved from 15% to 8.7% — not because anything got better,
    but because the evidence left the denominator.

    Two causes, both fixed: paper_sell discarded `bought_date` (so a closed
    paper trade had no ledger join key at all), and _build_actionability never
    passed paper history in. A metric about a PROMISE must not get quieter every
    time a trade closes — that fails in the flattering direction.
    """

    def test_a_closed_paper_trade_still_counts_as_a_breach(self):
        led = [_pick("COHR", d="2026-08-05", entry=328.25, tf="long_term")]
        hist = [{"ticker": "COHR", "bought_date": "2026-08-05",
                 "buy_price": 346.15, "sell_price": 351.0,
                 "closed_date": "2026-08-10"}]
        r = analyse(led, hist, [])
        assert r["entry"]["n"] == 1, "closed paper trade dropped out of the denominator"
        assert r["entry"]["outside_window"] == 1
        assert r["entry"]["worst_pct"] == 5.45
        assert r["entry"]["closed_n"] == 1

    def test_buy_price_is_accepted_as_the_fill(self):
        """Paper history spells the fill `buy_price`; positions spell it
        `avg_price`/`entry_price`. Reading only the latter silently skipped
        every closed paper trade even once it was passed in."""
        led = [_pick("A", d="2026-08-05", entry=100.0)]
        r = analyse(led, [{"ticker": "A", "bought_date": "2026-08-05", "buy_price": 103.0}], [])
        assert r["entry"]["n"] == 1 and r["entry"]["outside_window"] == 1

    def test_open_and_closed_records_of_one_pick_count_once(self):
        """The bot holds a ticker as both a real and a paper position, and the
        paper leg later closes. That is one PICK, so one observation."""
        led = [_pick("A", d="2026-08-05", entry=100.0)]
        pos = [{"ticker": "A", "opened_date": "2026-08-05", "entry_price": 103.0},
               {"ticker": "A", "bought_date": "2026-08-05", "buy_price": 103.0,
                "closed_date": "2026-08-10"}]
        assert analyse(led, pos, [])["entry"]["n"] == 1

    def test_a_fill_with_no_open_date_is_counted_as_missing_not_dropped(self):
        """Legacy history rows predate the bought_date fix. They cannot be
        joined — but they must be REPORTED, or unjoinable breaches quietly
        flatter the rate."""
        led = [_pick("A", d="2026-08-05", entry=100.0)]
        r = analyse(led, [{"ticker": "A", "buy_price": 120.0, "closed_date": "2026-08-10"}], [])
        assert r["entry"]["n"] == 0
        assert r["entry"]["undated"] == 1

    def test_closed_date_is_never_used_as_the_join_key(self):
        """Joining a SALE date to a pick date would match the wrong pick
        whenever a ticker is picked more than once."""
        led = [_pick("A", d="2026-08-10", entry=100.0)]
        r = analyse(led, [{"ticker": "A", "buy_price": 999.0, "closed_date": "2026-08-10"}], [])
        assert r["entry"]["n"] == 0, "sale date was used as the open date"

    def test_the_same_stop_is_not_counted_twice(self):
        led = [_pick("A", d="2026-08-05", entry=100.0)]
        pos = [{"ticker": "A", "bought_date": "2026-08-05", "entry_price": 100.0, "stop_loss": 95.0},
               {"ticker": "A", "bought_date": "2026-08-05", "buy_price": 100.0, "stop_loss": 95.0,
                "closed_date": "2026-08-10"}]
        assert analyse(led, pos, [])["stops"]["n"] == 1


class TestPaperSellPreservesTheJoinKey:
    def test_history_record_carries_bought_date_and_levels(self):
        """Root cause: paper_sell wrote ticker/shares/prices/gain only, so a sold
        paper position lost the open date that joins it to its pick."""
        import inspect, paper_trader
        src = inspect.getsource(paper_trader.paper_sell)
        body = src[src.index('data["history"].append'):]
        for field in ("bought_date", "stop_loss", "target_price"):
            assert field in body, f"paper_sell history record drops {field}"
