"""The one-time traffic cleanup removes only what Full Sweep runs can account for.

A run made 33 counted requests, all cold, all under the owner's id. In each hour
the cleanup removes at most 33 × the runs that started there, plus the previous
hour's unused capacity (a run starting at :59 lands its requests in the next
hour), and only the owner's COLD hits. Everything else stays.
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "clean_traffic", os.path.join(ROOT, "scripts", "clean_traffic.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)

OWNER = "111"
M = "2026-11"


def _h(hits, cold, **users):
    return {"hits": hits, "cold": cold, "users": users}


def _plan(hours, runs):
    return ct.plan({M: hours}, {(M, h): n for h, n in runs.items()}, OWNER)[1]


class TestPlan:
    def test_an_hour_explained_by_its_runs_is_removed(self):
        assert _plan({"20": _h(66, 66, **{OWNER: 66})}, {"20": 2}) == {(M, "20"): 66}

    def test_owner_activity_beyond_the_runs_is_kept(self):
        assert _plan({"20": _h(70, 70, **{OWNER: 70})}, {"20": 2}) == {(M, "20"): 66}

    def test_warm_owner_hits_are_never_removed(self):
        """Every sweep hit was cold. Warm owner hits are the owner using the app."""
        assert _plan({"15": _h(66, 10, **{OWNER: 66})}, {"15": 2}) == {(M, "15"): 10}

    def test_a_run_spills_into_the_next_hour(self):
        """Started 20:59, counted at 21:00."""
        assert _plan({"21": _h(33, 33, **{OWNER: 33})}, {"20": 1}) == {(M, "21"): 33}

    def test_spill_lasts_one_hour_only(self):
        assert _plan({"22": _h(33, 33, **{OWNER: 33})}, {"20": 1}) == {}

    def test_capacity_used_in_its_own_hour_does_not_spill_again(self):
        hours = {"20": _h(33, 33, **{OWNER: 33}), "21": _h(33, 33, **{OWNER: 33})}
        assert _plan(hours, {"20": 1}) == {(M, "20"): 33}

    def test_an_hour_with_no_nearby_run_is_never_touched(self):
        assert _plan({"03": _h(5, 5, **{OWNER: 5})}, {}) == {}

    def test_other_users_are_never_counted_as_the_sweep(self):
        assert _plan({"04": _h(2, 2, anon=2)}, {"04": 1}) == {}


class TestApply:
    def test_it_removes_exactly_the_planned_hits_and_keeps_everyone_else(self, _gist_store):
        _gist_store.write("traffic_hours.json", {M: {
            "20": _h(68, 68, **{OWNER: 66, "anon": 2}),
            "21": _h(33, 33, **{OWNER: 33}),
        }})
        assert ct.apply_plan({(M, "20"): 66, (M, "21"): 33}, OWNER) == 99
        after = _gist_store.load("traffic_hours.json")[M]
        assert after["20"] == _h(2, 2, anon=2)
        assert "21" not in after, "an hour left with nothing is removed, not kept at zero"

    def test_it_never_removes_more_than_is_there(self, _gist_store):
        _gist_store.write("traffic_hours.json", {M: {"20": _h(10, 10, **{OWNER: 10})}})
        assert ct.apply_plan({(M, "20"): 66}, OWNER) == 10
        assert "20" not in _gist_store.load("traffic_hours.json")[M]
