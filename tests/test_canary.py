"""Smoke tests for the daily canary's pure helpers (scripts/canary.py)."""
import os
import importlib.util
import datetime

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "canary.py")
_spec = importlib.util.spec_from_file_location("canary", _PATH)
canary = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(canary)


class TestCanaryHelpers:
    def test_fin_rejects_nan_inf_none(self):
        assert canary._fin(1.5)
        assert not canary._fin(None)
        assert not canary._fin(float("nan"))
        assert not canary._fin(float("inf"))

    def test_pos_requires_positive_finite(self):
        assert canary._pos(1.0)
        assert not canary._pos(0)
        assert not canary._pos(-1)
        assert not canary._pos(float("nan"))

    def test_expected_delivery_is_a_weekday(self):
        d = canary._expected_delivery_date()
        assert datetime.date.fromisoformat(d).weekday() < 5   # never a Sat/Sun

    def test_check_shows_pass_wording_on_pass_and_fail_wording_on_fail(self):
        """A check used to carry ONE note, so a warning-worded note printed on a
        PASS line — price_guard read "lets a $0.01/spike through" while green,
        the exact opposite of what happened."""
        canary.RESULTS.clear()
        canary._check("ok.case",  True,  "observed the good thing", fail_detail="the bad thing happened")
        canary._check("bad.case", False, "observed the good thing", fail_detail="the bad thing happened")
        notes = {name: note for name, _ok, note in canary.RESULTS}
        assert notes["ok.case"]  == "observed the good thing"
        assert notes["bad.case"] == "the bad thing happened"
        canary.RESULTS.clear()

    def test_price_guard_notes_are_not_warnings_when_green(self):
        canary.RESULTS.clear()
        canary.check_price_guard()
        for name, ok, note in canary.RESULTS:
            assert ok, f"{name} unexpectedly failed"
            assert "lets a $0.01" not in note and "rejects a real" not in note, \
                f"{name} printed failure wording on a PASS line: {note}"
        canary.RESULTS.clear()
