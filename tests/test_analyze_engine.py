"""The daily engine analysis — the standing agenda a session opens with.

🔴 The rule that makes it safe: engine changes are NEVER recommended from the
bot's win rate. Its trades are mechanical fills, and in July feeding them back
had a robot's stop-outs steering real recommendations.
"""
import importlib.util
import os
import sys

import pytest

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "scripts", "analyze_engine.py")
_spec = importlib.util.spec_from_file_location("analyze_engine", _P)
ae = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ae)


class TestTiers:
    def test_win_rate_questions_are_HELD_below_the_gate(self):
        f = ae._maturity([{"date": "2026-08-20"}] * 5)
        assert f.tier == "HOLD" and f.blocked_until
        assert "not" in f.action.lower() and "tune" in f.action.lower()

    def test_the_gate_matches_the_evaluator(self):
        """30 is the bar for calling something conclusive in a report the owner
        reads; it must be the bar here too."""
        assert ae.MIN_N == 30

    def test_a_matured_ledger_unblocks_the_question(self):
        import datetime as dt
        old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
        f = ae._maturity([{"date": old} for _ in range(35)])
        assert f.tier == "MEASURE" and not f.blocked_until

    def test_controls_never_count_toward_maturity(self):
        """Controls are the runners-up we did NOT pick — counting them would
        inflate the sample the honesty gate protects."""
        import datetime as dt
        old = (dt.date.today() - dt.timedelta(days=40)).isoformat()
        f = ae._maturity([{"date": old, "control": True} for _ in range(35)])
        assert f.tier == "HOLD", "controls inflated the matured count"


class TestLevelsGeometry:
    def _t(self, entry, stop, target):
        return {"entry_price": entry, "stop_loss": stop, "target_price": target}

    def test_it_reports_reward_to_risk(self):
        f = ae._levels_geometry([self._t(100, 95, 110)] * 4)
        assert f and "2.00:1" in f.evidence

    def test_it_compares_against_the_MEASURED_baseline_not_config(self):
        """A backtest's assumptions must come from what users actually got —
        reading config defaults manufactured a false finding once already."""
        f = ae._levels_geometry([self._t(100, 95, 110)] * 4)
        assert "1.9:1" in f.evidence and "config defaults" in f.evidence

    def test_too_few_positions_yields_nothing(self):
        assert ae._levels_geometry([self._t(100, 95, 110)]) is None

    def test_garbage_levels_do_not_crash_it(self):
        assert ae._levels_geometry(
            [{"entry_price": None}, {"entry_price": "x", "stop_loss": 1,
                                     "target_price": 2}] * 3) is None


class TestSafety:
    def test_the_no_win_rate_rule_is_stated_in_the_output(self):
        doc = ae.build(dry=True)
        assert "win rate is never an input" in doc.lower()

    def test_the_document_always_renders_even_with_no_data(self, monkeypatch):
        monkeypatch.setattr(ae, "_load", lambda: {
            "uid": "0", "log": {}, "paper": {}, "rows": []})
        doc = ae.build(dry=True)
        assert "Engine findings" in doc and "HOLD" in doc

    def test_tiers_are_ordered_act_first(self, monkeypatch):
        monkeypatch.setattr(ae, "_load", lambda: {
            "uid": "0", "log": {}, "paper": {}, "rows": []})
        doc = ae.build(dry=True)
        tiers = [l.split("]")[0].split("[")[1]
                 for l in doc.splitlines() if l.startswith("### [")]
        rank = {"ACT": 0, "MEASURE": 1, "HOLD": 2}
        assert tiers == sorted(tiers, key=lambda t: rank[t]), \
            "an ACT finding could be buried below a HOLD"


class TestLevelsSourceIsRecorded:
    """🔴 The exit-reason mix was CONFOUNDED until 2026-08-23.

    `_levels_for` substitutes ±5%/8% when a pick's levels do not bracket the
    actual fill. A stop-out on a SUBSTITUTED stop says nothing about the
    engine's published levels — only about the fallback. Without recording
    which was used, the stop:target ratio mixes two different measurements.
    """

    def _lf(self):
        import importlib.util
        import os
        import sys
        os.environ.setdefault("GIST_ID", "x")
        os.environ.setdefault("GH_GIST_TOKEN", "x")
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        spec = importlib.util.spec_from_file_location(
            "su", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "synthetic_user.py"))
        su = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(su)
        return su._levels_for

    @pytest.mark.parametrize("px,stop,target,want", [
        (100, 95, 110, "pick"),        # both bracket the fill
        (100, 105, 110, "stop"),       # stop above the fill
        (100, 95, 90, "target"),       # target below the fill
        (100, None, None, "both"),
        (1177.74, 1290, 1400, "stop"), # the live FICO case
    ])
    def test_it_reports_which_leg_was_substituted(self, px, stop, target, want):
        s, t, src = self._lf()(px, stop, target)
        assert src == want
        assert s < px < t, "the returned levels must still bracket the fill"

    def test_a_substituted_level_is_never_silently_inherited(self):
        """The whole point: an unusable pick level must not be passed through."""
        s, _t, src = self._lf()(100, 105, 110)
        assert s < 100 and src == "stop"

    def test_the_analysis_separates_pick_levels_from_the_fallback(self):
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "ae", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "analyze_engine.py"))
        ae2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ae2)
        seg = ae2._exit_mix_by_levels_source(
            {"closed": [{"levels_source": "pick"}, {"levels_source": "stop"}]},
            {"history": [{"levels_source": "pick"}, {}]})
        assert seg == {"pick": 2, "stop": 1, "unrecorded": 1}

    def test_pre_existing_trades_are_unrecorded_not_counted_as_pick(self):
        """Folding them into `pick` would overstate what the engine's own
        levels have actually been measured on."""
        import importlib.util
        import os
        spec = importlib.util.spec_from_file_location(
            "ae", os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "scripts", "analyze_engine.py"))
        ae2 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ae2)
        seg = ae2._exit_mix_by_levels_source({"closed": [{}, {}]}, {})
        assert seg == {"unrecorded": 2}


class TestStorageAcceptsTheField:
    """A stub narrower than production hides a signature change — the
    add_alert(kind=) and send_inline_keyboard(buttons=) lesson."""

    @pytest.mark.parametrize("mod,fn", [("trade_logger", "add_holding"),
                                        ("paper_trader", "paper_buy")])
    def test_the_writer_accepts_levels_source(self, mod, fn):
        import importlib
        import inspect
        f = getattr(importlib.import_module(mod), fn)
        assert "levels_source" in inspect.signature(f).parameters, \
            f"{mod}.{fn} cannot record where the levels came from"

    def test_paper_sell_carries_it_into_history(self):
        import inspect
        import paper_trader
        src = inspect.getsource(paper_trader.paper_sell)
        assert "levels_source" in src, \
            "a sold paper trade loses the levels source, so closed-trade " \
            "analysis silently reverts to unrecorded"
