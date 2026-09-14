"""A win RATE is only shown once the sample can support it.

🔴 Measured 2026-09-13: the 95% CI on the pick win rate at n=46 was
30.2-57.8% — a ±14-point interval that cannot distinguish a good engine from
a coin flip. The morning bar's floor was **5 trades**, so every user saw a
percentage like "60%" computed from five closed trades, every day, presented
as a track record. The community panel's floor was 10.

30 is the SAME bar this repo already uses for "conclusive" everywhere else
(evaluate_picks._MIN_N, performance_context._MIN_DIRECTIVE_N). The display
layer was the outlier.

🔑 The counts, the median and the SPY comparison are FACTS and stay. Only the
derived percentage is gated — blanking the whole bar would read as "no data"
when there is some.
"""
import performance_tracker as pt


def _t(ret, day="2026-09-10"):
    return {"ticker": "X", "return_pct": ret, "closed_date": day,
            "entry_price": 100, "exit_price": 100 + ret}


def _log(trades):
    return [{"closed": trades}]


class TestTheGateItself:
    def test_it_matches_the_conclusive_bar_used_everywhere_else(self):
        from scripts.evaluate_picks import _MIN_N
        assert pt._MIN_WIN_RATE_N == _MIN_N, \
            "if 30 is the bar for calling something conclusive, it is the bar for showing a rate"

    def test_it_is_above_the_render_floor(self):
        """The record renders from 5; the RATE needs more."""
        assert pt._MIN_WIN_RATE_N > pt._MIN_RECENT_TRADES


class TestRecentStats:
    def test_a_small_sample_is_flagged_inconclusive(self):
        s = pt.get_recent_stats(_log([_t(5)] * 6))
        assert s["total"]["win_rate_conclusive"] is False

    def test_a_large_sample_is_conclusive(self):
        s = pt.get_recent_stats(_log([_t(5)] * pt._MIN_WIN_RATE_N))
        assert s["total"]["win_rate_conclusive"] is True

    def test_the_rate_is_still_COMPUTED_so_no_consumer_breaks(self):
        """The flag decides rendering; removing the key would KeyError two
        live call sites and every admin view."""
        s = pt.get_recent_stats(_log([_t(5)] * 6))
        assert isinstance(s["total"]["win_rate"], float)

    def test_the_facts_survive_below_the_gate(self):
        """Counts and median are observations, not inferences."""
        s = pt.get_recent_stats(_log([_t(10)] * 3 + [_t(-5)] * 3))["total"]
        assert s["wins"] == 3 and s["losses"] == 3
        assert s["median_return"] is not None


class TestCommunityStats:
    def test_the_public_track_record_is_gated_too(self):
        """A 'community track record' is MORE claim-shaped than a personal bar,
        and its floor was 10."""
        s = pt.build_community_stats(_log([_t(5)] * 12))
        assert s["win_rate_conclusive"] is False
        s = pt.build_community_stats(_log([_t(5)] * pt._MIN_WIN_RATE_N))
        assert s["win_rate_conclusive"] is True


class TestTheMorningBar:
    """The bar every user reads daily — DRIVEN, not source-scanned. A rendered
    assertion is what proves the flag is actually honoured; the earlier version
    of these tests only read the source, which is how a page once 500'd for 40
    minutes with every test green."""

    def _render(self, n_each, conclusive):
        from formatters import format_daily_message
        from tests.test_formatters import _picks, _cfg
        total = {"wins": n_each, "losses": n_each, "win_rate": 50.0,
                 "avg_return": 1.5, "median_return": 1.2, "count": n_each * 2,
                 "win_rate_conclusive": conclusive}
        return format_daily_message(_picks(), _cfg(),
                                    recent_stats={"days": 30, "total": total,
                                                  "spy_return": 0.8})

    def test_below_the_gate_the_RATE_is_absent(self):
        out = self._render(3, False)
        assert "3W/3L" in out, "the record is a fact and must stay"
        assert "50.0%" not in out, "a rate off 6 trades must not be shown"

    def test_below_the_gate_the_FACTS_still_render(self):
        """Blanking the bar would read as 'no data' when there is some."""
        out = self._render(3, False)
        assert "1.2" in out and "median" in out
        assert "SPY" in out, "the benchmark is the honest context — never hide it"

    def test_above_the_gate_the_rate_returns(self):
        out = self._render(20, True)
        assert "20W/20L" in out and "50.0%" in out

    def test_the_gate_is_what_decides_it(self):
        """Same counts, opposite flag -> opposite rendering. Pins that the FLAG
        drives it, not the sample size being re-derived somewhere else."""
        assert "50.0%" not in self._render(20, False)
        assert "50.0%" in self._render(20, True)
