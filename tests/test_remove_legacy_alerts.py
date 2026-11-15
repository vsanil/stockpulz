"""The one-time removal of 15 system-made alerts from May 2026 touches nothing else.

It deletes a real user's data, so the selection is the whole safety story: an
alert goes only if it is on the reviewed list, has no `auto` marker, and was
set in May 2026.
"""
import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "remove_legacy_alerts", os.path.join(ROOT, "scripts", "remove_legacy_alerts.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


def _a(ticker, target, set_at="2026-05-15T03:23:38", direction="below", **kw):
    return {"ticker": ticker, "direction": direction, "target": target,
            "set_at": set_at, "price_at_set": 1.0, **kw}


class TestSelection:
    def test_the_list_is_exactly_the_fifteen_reviewed(self):
        assert len(rl.LEGACY) == 15 and len(set(rl.LEGACY)) == 15
        assert {t for t, _d, _x in rl.LEGACY} == {"CHD", "DXCM", "MMM", "CAH", "NEAR", "CVX"}

    def test_every_listed_alert_is_selected(self):
        alerts = [_a(t, x, direction=d) for t, d, x in rl.LEGACY]
        kept, removed = rl.split(alerts)
        assert kept == [] and len(removed) == 15

    def test_the_three_hand_set_alerts_from_the_same_days_stay(self):
        hand = [_a("AAPL", 174), _a("GLD", 210), _a("MSFT", 390)]
        assert rl.split(hand) == (hand, [])

    def test_a_listed_level_set_later_is_someone_setting_it_again(self):
        """Same ticker and price, but set in November — that is a person."""
        assert not rl.is_legacy(_a("CVX", 179.63, set_at="2026-11-10T10:00:00"))

    def test_an_automatic_alert_is_never_touched(self):
        assert not rl.is_legacy(_a("CVX", 179.63, auto=True, kind="stop"))

    def test_a_different_level_or_direction_is_never_touched(self):
        assert not rl.is_legacy(_a("CVX", 179.70))
        assert not rl.is_legacy(_a("CVX", 179.63, direction="above"))
        assert not rl.is_legacy(_a("TSLA", 179.63))

    def test_junk_is_kept_not_deleted(self):
        for junk in (None, "x", {"ticker": "CVX"}, _a("CVX", "abc"), _a("CVX", None)):
            assert not rl.is_legacy(junk)
