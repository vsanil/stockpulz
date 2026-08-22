"""Merge rules for the Gist↔Supabase reconciliation.

Both stores held unique data after the stalled Aug-19 migration, so these rules
decide what survives. A wrong rule silently loses a user's alerts or double-
counts traffic — neither would be visible afterwards.
"""
import importlib.util
import os
import sys

import pytest

_P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "scripts", "reconcile_storage.py")
_spec = importlib.util.spec_from_file_location("reconcile_storage", _P)
rec = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rec)


def _alert(ticker, target, direction="below", set_at="2026-08-01T00:00:00"):
    return {"ticker": ticker, "target": target, "direction": direction,
            "set_at": set_at, "price_at_set": target * 1.1}


class TestMergeAlerts:
    def test_a_user_present_in_only_one_store_survives(self):
        out = rec._merge_alerts({"111": [_alert("AAPL", 100)]},
                                {"222": [_alert("MSFT", 200)]})
        assert set(out) == {"111", "222"}

    def test_a_user_in_BOTH_gets_the_union_of_their_alerts(self):
        out = rec._merge_alerts({"111": [_alert("AAPL", 100)]},
                                {"111": [_alert("MSFT", 200)]})
        assert {a["ticker"] for a in out["111"]} == {"AAPL", "MSFT"}

    def test_the_same_alert_in_both_is_not_duplicated(self):
        a = _alert("AAPL", 100)
        out = rec._merge_alerts({"111": [a]}, {"111": [dict(a)]})
        assert len(out["111"]) == 1, "the user would see the alert fire twice"

    def test_a_re_armed_alert_at_the_same_level_is_kept_separately(self):
        """Same ticker/direction/target but a later set_at is a NEW alert —
        collapsing them would erase a genuine re-arm."""
        out = rec._merge_alerts(
            {"111": [_alert("AAPL", 100, set_at="2026-08-01T00:00:00")]},
            {"111": [_alert("AAPL", 100, set_at="2026-08-20T00:00:00")]})
        assert len(out["111"]) == 2

    def test_history_keys_are_carried_across(self):
        out = rec._merge_alerts(
            {"_history_111": [{"ticker": "X", "triggered_at": "2026-08-01"}]},
            {"111": [_alert("AAPL", 100)]})
        assert "_history_111" in out and "111" in out

    def test_an_empty_side_changes_nothing(self):
        g = {"111": [_alert("AAPL", 100)]}
        assert rec._merge_alerts(g, {}) == g
        assert rec._merge_alerts({}, g) == g

    def test_a_non_list_value_does_not_crash_the_merge(self):
        out = rec._merge_alerts({"111": "corrupt"}, {"111": [_alert("A", 1)]})
        assert "111" in out


class TestMergeTraffic:
    def test_disjoint_hours_are_both_kept(self):
        out = rec._merge_traffic(
            {"2026-08": {"03": {"hits": 10, "cold": 2, "users": {"a": 10}}}},
            {"2026-08": {"14": {"hits": 5, "cold": 1, "users": {"b": 5}}}})
        assert set(out["2026-08"]) == {"03", "14"}

    def test_the_same_hour_in_both_stores_SUMS(self):
        """🔴 Each hit is written once, to whichever backend was live — the two
        sides are disjoint event streams. Overwriting loses one of them, which
        is the Aug 19 finding (807 hits in one store, 154 in the other)."""
        out = rec._merge_traffic(
            {"2026-08": {"03": {"hits": 10, "cold": 2, "users": {"a": 10}}}},
            {"2026-08": {"03": {"hits": 5, "cold": 1, "users": {"a": 3, "b": 2}}}})
        rec03 = out["2026-08"]["03"]
        assert rec03["hits"] == 15 and rec03["cold"] == 3
        assert rec03["users"] == {"a": 13, "b": 2}

    def test_a_month_present_in_only_one_store_survives(self):
        out = rec._merge_traffic({"2026-07": {"01": {"hits": 1}}},
                                 {"2026-08": {"02": {"hits": 2}}})
        assert set(out) == {"2026-07", "2026-08"}

    def test_the_gist_input_is_never_mutated(self):
        g = {"2026-08": {"03": {"hits": 10, "cold": 0, "users": {}}}}
        rec._merge_traffic(g, {"2026-08": {"03": {"hits": 5, "cold": 0, "users": {}}}})
        assert g["2026-08"]["03"]["hits"] == 10, "the source copy was mutated"

    def test_empty_sides_are_safe(self):
        assert rec._merge_traffic({}, {}) == {}
        g = {"2026-08": {"03": {"hits": 1}}}
        assert rec._merge_traffic(g, {}) == g


class TestStrategy:
    def test_every_drifting_file_has_exactly_one_strategy(self):
        drift = {"backtest_trades.json", "pending_users.json", "picks.json",
                 "price_alerts.json", "traffic_hours.json", "usage_counts.json",
                 "user_configs.json", "user_paper.json", "user_trades.json",
                 "weekly_picks.json"}
        covered = set(rec.GIST_WINS) | set(rec.KEEP_SUPABASE) | set(rec.MERGE)
        assert drift <= covered, f"unhandled: {sorted(drift - covered)}"
        for a, b in ((rec.GIST_WINS, rec.KEEP_SUPABASE),
                     (rec.GIST_WINS, rec.MERGE),
                     (rec.KEEP_SUPABASE, rec.MERGE)):
            assert not (set(a) & set(b)), "a file has two conflicting strategies"

    def test_usage_counts_is_kept_from_supabase(self):
        """It only ever existed there — the Gist copy is empty, and treating
        the Gist as authoritative would wipe it."""
        assert "usage_counts.json" in rec.KEEP_SUPABASE
        assert "usage_counts.json" not in rec.GIST_WINS
