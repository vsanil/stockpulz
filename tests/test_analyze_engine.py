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
