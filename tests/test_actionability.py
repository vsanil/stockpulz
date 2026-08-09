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
