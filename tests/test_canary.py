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
