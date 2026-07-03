"""Smoke tests for the synthetic-user bot helpers (scripts/synthetic_user.py)."""
import os
import importlib.util

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "scripts", "synthetic_user.py")
_spec = importlib.util.spec_from_file_location("synthetic_user", _PATH)
su = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(su)


class TestSyntheticUserHelpers:
    def test_pos(self):
        assert su._pos(1.0)
        assert not su._pos(0)
        assert not su._pos(None)
        assert not su._pos(float("nan"))

    def test_universe_all_sections_deduped_ordered(self):
        picks = {
            "stocks": {"short_term": [{"ticker": "AAA", "entry_price": 1, "stop_loss": 0.9,
                                       "target_price": 1.2}], "long_term": []},
            "crypto": {"short_term": [{"symbol": "BTC", "entry_price": 60000}], "long_term": []},
            "etfs":   {"short_term": [{"ticker": "XLV", "entry_price": 100}], "long_term": []},
            "commodities": {"short_term": [], "long_term": []},
        }
        uni = su._universe(picks)
        assert [u["t"] for u in uni] == ["AAA", "BTC", "XLV"]   # all sections, ordered
        assert uni[1]["atype"] == "crypto"                       # crypto typed correctly
