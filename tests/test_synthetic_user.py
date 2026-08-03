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


class TestStateSaveIsVerified:
    """A silently-failed state save is how a live position gets orphaned — the
    gist rate-limits rapid PATCHes, so the write MUST be checked and retried."""

    def test_save_returns_false_and_warns_on_persistent_failure(self, monkeypatch, capsys):
        class _Resp:
            status_code = 403
            text = "rate limited"
        monkeypatch.setattr(su.requests, "patch", lambda *a, **k: _Resp())
        monkeypatch.setattr(su.time if hasattr(su, "time") else su, "sleep", lambda *_: None,
                            raising=False)
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda *_: None)
        assert su._save_state("900000001", {"real": [], "paper": []}) is False
        assert "STATE SAVE FAILED" in capsys.readouterr().out

    def test_save_returns_true_on_success(self, monkeypatch):
        class _Resp:
            status_code = 200
            text = "ok"
        monkeypatch.setattr(su.requests, "patch", lambda *a, **k: _Resp())
        assert su._save_state("900000001", {"real": ["X"], "paper": []}) is True

    def test_save_retries_then_succeeds(self, monkeypatch):
        calls = {"n": 0}
        class _Resp:
            def __init__(self, code): self.status_code, self.text = code, "x"
        def _patch(*a, **k):
            calls["n"] += 1
            return _Resp(200 if calls["n"] >= 2 else 409)
        monkeypatch.setattr(su.requests, "patch", _patch)
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda *_: None)
        assert su._save_state("900000001", {"real": [], "paper": []}) is True
        assert calls["n"] == 2, "must retry a throttled write, not give up"


class TestLevelsBracketFill:
    """Pick levels are relative to the PICK's entry. If the live fill has moved
    past one, inheriting it blindly creates a position born already stopped-out —
    manage closes it instantly and books a fabricated loss (live: paper FICO
    filled at $1,177.74 carrying the pick's $1,290 stop)."""

    def test_stop_above_fill_is_replaced(self):
        s, t = su._levels_for(1177.74, 1290.0, 1540.0)   # the real FICO case
        assert s < 1177.74 < t
        assert s == round(1177.74 * 0.95, 4)

    def test_target_below_fill_is_replaced(self):
        s, t = su._levels_for(100.0, 90.0, 95.0)
        assert s < 100.0 < t
        assert t == round(100.0 * 1.08, 4)

    def test_valid_pick_levels_are_kept(self):
        s, t = su._levels_for(100.0, 92.0, 115.0)
        assert (s, t) == (92.0, 115.0), "must not override levels that already bracket the fill"

    def test_missing_levels_are_filled(self):
        s, t = su._levels_for(50.0, None, None)          # LT picks carry no stop
        assert s < 50.0 < t
