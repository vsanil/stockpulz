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


class TestTestAccountSeparation:
    """The bot trades on a VIRTUAL test account; the owner's account must stay
    clean AND its pre-split positions must keep being managed (not orphaned)."""

    def test_state_file_is_per_account(self, monkeypatch):
        monkeypatch.setattr(su, "_OWNER_ID", "1699321994")
        monkeypatch.setattr(su, "_TRADE_ID", "900000001")
        owner = su._state_file("1699321994")
        test  = su._state_file("900000001")
        assert owner != test, "shared state would let manage erase the owner's tickers"
        assert owner == "synthetic_state.json"          # legacy name → wind-down works
        assert test == "synthetic_state_900000001.json"

    def test_owner_legacy_state_still_addressable(self, monkeypatch):
        """manage must still find the owner's legacy state to sell those positions
        at target/stop — otherwise the account switch orphans them forever."""
        monkeypatch.setattr(su, "_OWNER_ID", "1699321994")
        assert su._state_file("1699321994") == "synthetic_state.json"

    def test_trade_account_defaults_to_virtual_not_owner(self):
        from config_manager import DEFAULT_TEST_CHAT_ID, is_test_user
        assert is_test_user(DEFAULT_TEST_CHAT_ID)
        assert not is_test_user("1699321994")           # owner is never a test user


class TestPaperCoversEveryPick:
    """Paper-trading EVERY pick is what makes the pick-quality evaluation
    statistically meaningful — 2 arbitrary picks/day would never reach N."""

    def test_all_picks_are_paper_candidates(self):
        picks = {
            "stocks": {"short_term": [{"ticker": "AAA", "entry_price": 1},
                                      {"ticker": "BBB", "entry_price": 2}], "long_term": []},
            "crypto": {"short_term": [{"symbol": "BTC", "entry_price": 60000}], "long_term": []},
            "etfs":   {"short_term": [{"ticker": "XLV", "entry_price": 100}], "long_term": []},
            "commodities": {"short_term": [{"ticker": "USO", "entry_price": 70}], "long_term": []},
        }
        uni = su._universe(picks)
        assert len(uni) == 5                      # every section represented
        assert su._MAX_PAPER >= len(uni)          # cap never truncates a normal day
