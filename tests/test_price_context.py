"""price_context — the chart notes Claude reads for every stock candidate.

Was at 4% coverage. It feeds the pick prompt, so a wrong classification here
doesn't show a bad number to a user — it quietly degrades what the model is
told, which is the failure mode this project keeps shipping.

Every test drives the pure path (`hist=` supplied), so none of it touches the
network or depends on market hours.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd
import pytest

from price_context import get_price_context


def _hist(closes, vols=None, highs=None, lows=None, opens=None):
    n = len(closes)
    return pd.DataFrame({
        "Close":  closes,
        "Open":   opens if opens is not None else closes,
        "High":   highs if highs is not None else [c * 1.01 for c in closes],
        "Low":    lows  if lows  is not None else [c * 0.99 for c in closes],
        "Volume": vols  if vols  is not None else [1_000_000] * n,
    }, index=pd.date_range("2026-01-01", periods=n, freq="D"))


class TestInsufficientData:
    """Returns {} rather than half-computed context — a partial chart note is
    worse than none, because the model cannot tell it is partial."""

    @pytest.mark.parametrize("n", [0, 1, 19])
    def test_under_20_bars_returns_empty(self, n):
        assert get_price_context("X", _hist([100.0] * n)) == {}

    def test_20_bars_is_enough(self):
        assert get_price_context("X", _hist([100.0] * 20)) != {}

    def test_a_broken_frame_returns_empty_not_an_exception(self):
        bad = pd.DataFrame({"Close": [1, 2, 3]})      # missing High/Low/Volume
        assert get_price_context("X", bad) == {}


class TestMovingAverageClassification:
    def test_below_200ma_reads_bearish(self):
        closes = [200.0] * 200 + [100.0] * 30       # long decline
        ctx = get_price_context("X", _hist(closes))
        assert ctx["ma_position"] == "below 200MA (bearish)"

    def test_above_all_mas_reads_bullish(self):
        closes = [float(100 + i) for i in range(240)]   # steady climb
        ctx = get_price_context("X", _hist(closes))
        assert ctx["ma_position"] == "above all MAs (bullish)"

    def test_the_position_always_reaches_the_context_string(self):
        ctx = get_price_context("X", _hist([float(100 + i) for i in range(240)]))
        assert ctx["ma_position"] in ctx["context_str"]


class TestTrend:
    """±3% over the window. The thresholds are what the model reads as
    'uptrend'/'downtrend', so they are worth pinning."""

    def test_a_rise_over_3pct_is_an_uptrend(self):
        closes = [100.0] * 40 + [110.0]            # +10% vs 21 bars back
        assert get_price_context("X", _hist(closes))["trend_1m"] == "uptrend"

    def test_a_fall_over_3pct_is_a_downtrend(self):
        closes = [100.0] * 40 + [90.0]
        assert get_price_context("X", _hist(closes))["trend_1m"] == "downtrend"

    def test_small_moves_are_flat(self):
        closes = [100.0] * 40 + [101.0]            # +1%
        assert get_price_context("X", _hist(closes))["trend_1m"] == "flat"

    def test_too_little_history_is_flat_not_an_error(self):
        ctx = get_price_context("X", _hist([100.0] * 25))
        assert ctx["trend_3m"] == "flat"           # needs 63+ bars


class TestSupportResistanceAndBreakout:
    def test_support_is_never_above_resistance(self):
        ctx = get_price_context("X", _hist([100.0 + (i % 7) for i in range(60)]))
        assert ctx["support"] <= ctx["resistance"]

    def test_breakout_needs_BOTH_a_new_high_and_volume(self):
        """Price alone is not a breakout — the volume confirmation is the point."""
        closes = [100.0] * 39 + [120.0]
        quiet  = [1_000_000] * 39 + [1_000_000]     # no volume surge
        loud   = [1_000_000] * 39 + [5_000_000]
        assert get_price_context("X", _hist(closes, vols=quiet))["breakout"] is False
        assert get_price_context("X", _hist(closes, vols=loud))["breakout"] is True

    def test_volume_alone_is_not_a_breakout(self):
        closes = [100.0] * 40                        # flat price
        loud   = [1_000_000] * 39 + [9_000_000]
        assert get_price_context("X", _hist(closes, vols=loud))["breakout"] is False

    def test_a_breakout_replaces_the_resistance_note(self):
        closes = [100.0] * 39 + [120.0]
        loud   = [1_000_000] * 39 + [5_000_000]
        s = get_price_context("X", _hist(closes, vols=loud))["context_str"]
        assert "BREAKOUT" in s and "resistance ~$" not in s


class TestVolumeBalance:
    def test_up_day_volume_dominance_reads_accumulation(self):
        n = 40
        closes = [101.0] * n
        opens  = [100.0] * n                        # every day closes up
        vols   = [2_000_000] * n
        ctx = get_price_context("X", _hist(closes, vols=vols, opens=opens))
        assert "accumulation" in ctx["volume_trend"]

    def test_down_day_dominance_reads_distribution(self):
        n = 40
        closes = [99.0] * n
        opens  = [100.0] * n                        # every day closes down
        ctx = get_price_context("X", _hist(closes, opens=opens))
        assert "distribution" in ctx["volume_trend"]


class TestContextString:
    """This string IS the feature — it is what reaches the prompt."""

    def test_it_is_non_empty_and_carries_the_trend(self):
        ctx = get_price_context("X", _hist([float(100 + i) for i in range(80)]))
        assert ctx["context_str"]
        assert "1M " in ctx["context_str"] and "3M " in ctx["context_str"]

    def test_neutral_volume_is_omitted_rather_than_stated(self):
        """Prompt space is scarce; only a real signal earns a clause."""
        ctx = get_price_context("X", _hist([100.0 + (i % 3) for i in range(60)]))
        if ctx["volume_trend"] == "neutral volume":
            assert "neutral volume" not in ctx["context_str"]
