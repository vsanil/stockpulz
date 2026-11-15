"""The one-time traffic cleanup removes only what Full Sweep runs fully explain.

A real interaction must never be deleted to make the numbers tidy, so an hour
is cleaned only when the owner's hits there are an exact multiple of the sweep
runs that started in it, and every one of them is cold.
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "clean_traffic", os.path.join(ROOT, "scripts", "clean_traffic.py"))
ct = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ct)

OWNER = "111"


def _h(hits, cold, **users):
    return {"hits": hits, "cold": cold, "users": users}


class TestPlan:
    def test_an_hour_fully_explained_by_runs_is_removed(self):
        data = {"2026-11": {"20": _h(66, 66, **{OWNER: 66})}}
        _, rm = ct.plan(data, {("2026-11", "20"): 2}, OWNER)
        assert rm == {("2026-11", "20"): 66}

    def test_a_remainder_means_a_real_interaction_and_the_hour_is_kept(self):
        data = {"2026-11": {"20": _h(67, 67, **{OWNER: 67})}}
        _, rm = ct.plan(data, {("2026-11", "20"): 2}, OWNER)
        assert rm == {}

    def test_warm_owner_hits_are_not_the_sweep(self):
        """Every sweep hit was cold. Warm owner hits are the owner using the app."""
        data = {"2026-11": {"15": _h(66, 10, **{OWNER: 66})}}
        _, rm = ct.plan(data, {("2026-11", "15"): 2}, OWNER)
        assert rm == {}

    def test_an_hour_with_no_sweep_run_is_never_touched(self):
        data = {"2026-11": {"03": _h(5, 5, **{OWNER: 5})}}
        _, rm = ct.plan(data, {}, OWNER)
        assert rm == {}

    def test_other_users_are_never_counted_as_the_sweep(self):
        data = {"2026-11": {"04": _h(2, 2, anon=2)}}
        _, rm = ct.plan(data, {("2026-11", "04"): 1}, OWNER)
        assert rm == {}


class TestApply:
    def test_it_removes_exactly_the_planned_hits_and_keeps_everyone_else(self, _gist_store):
        _gist_store.write("traffic_hours.json", {"2026-11": {
            "20": _h(68, 68, **{OWNER: 66, "anon": 2}),
            "21": _h(33, 33, **{OWNER: 33}),
        }})
        assert ct.apply_plan({("2026-11", "20"): 66, ("2026-11", "21"): 33}, OWNER) == 99
        after = _gist_store.load("traffic_hours.json")["2026-11"]
        assert after["20"] == _h(2, 2, anon=2)
        assert "21" not in after, "an hour left with nothing is removed, not kept at zero"

    def test_it_never_removes_more_than_is_there(self, _gist_store):
        _gist_store.write("traffic_hours.json", {"2026-11": {"20": _h(10, 10, **{OWNER: 10})}})
        assert ct.apply_plan({("2026-11", "20"): 66}, OWNER) == 10
        assert "20" not in _gist_store.load("traffic_hours.json")["2026-11"]
