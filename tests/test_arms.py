"""A/B arms: parallel engine variants on the same market day.

The arms exist to remove the regime confound ("v1 in July vs v2 in August").
They cannot fix sample size — only picks where arms DISAGREE carry information —
so these tests pin the two things that make the experiment meaningful at all:
the production path is untouched, and the arms genuinely differ.
"""
import os, sys, importlib.util
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import screener
import config_manager as cm

_spec = importlib.util.spec_from_file_location(
    "run_arms", os.path.join(ROOT, "scripts", "run_arms.py"))
arms = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(arms)


class TestArmSafety:
    def test_every_arm_has_its_own_account(self):
        ids = list(cm.ARM_CHAT_IDS.values())
        assert len(ids) == len(set(ids)), "two arms sharing an account would mix their results"

    def test_arm_accounts_are_test_users(self):
        assert all(cm.is_test_user(v) for v in cm.ARM_CHAT_IDS.values())

    def test_arm_accounts_are_not_real_users(self):
        """The independent fact — a real user is one who receives messages.
        is_test_user() is DERIVED from ARM_CHAT_IDS, so it cannot catch this."""
        allowed = set(cm.get_allowed_users())
        for name, cid in cm.ARM_CHAT_IDS.items():
            assert cid not in allowed, f"arm {name} points at an allow-listed user"

    def test_refuses_to_run_an_arm_aimed_at_a_real_account(self, monkeypatch):
        monkeypatch.setattr(arms, "ARM_CHAT_IDS", dict(cm.ARM_CHAT_IDS, rogue="55501"))
        monkeypatch.setattr(screener, "STRATEGIES",
                            dict(screener.STRATEGIES, rogue=screener.DEFAULT_STRATEGY))
        monkeypatch.setattr(arms, "STRATEGIES", screener.STRATEGIES)
        monkeypatch.setattr(cm, "get_allowed_users", lambda: ["55501"])
        called = {"screened": False}
        monkeypatch.setattr(screener, "run_screener",
                            lambda **k: called.__setitem__("screened", True))
        with pytest.raises(SystemExit):
            arms.run_arm("rogue", dry=True)
        assert not called["screened"], "it must refuse BEFORE doing any work"

    def test_arms_never_write_production_picks(self):
        """Scan CALL SITES, not prose — the module docstring legitimately says
        'never writes picks.json', which a naive substring check flags."""
        import ast
        src = open(os.path.join(ROOT, "scripts", "run_arms.py")).read()
        tree = ast.parse(src)
        called = {getattr(n.func, "id", getattr(getattr(n.func, "attr", None), "__str__", lambda: None)())
                  for n in ast.walk(tree) if isinstance(n, ast.Call)}
        called |= {getattr(n.func, "attr", None)
                   for n in ast.walk(tree) if isinstance(n, ast.Call)}
        assert "save_picks" not in called, "an arm must never write production picks"
        assert "PICKS_FILENAME" not in {getattr(n, "id", None) for n in ast.walk(tree)}

    def test_arms_never_open_a_real_position(self):
        src = open(os.path.join(ROOT, "scripts", "run_arms.py")).read()
        for forbidden in ("add_holding", "close_trade", "open_trades"):
            assert forbidden not in src, f"arms are paper-only; found {forbidden}"


class TestArmPromptDivergence:
    def test_default_injects_nothing(self):
        assert screener.DEFAULT_STRATEGY.prompt_directive == ""

    def test_each_arm_has_a_directive(self):
        for n in ("breakout", "pullback"):
            assert len(screener.STRATEGIES[n].prompt_directive) > 50

    def test_arms_tell_claude_opposite_things(self):
        bo = screener.STRATEGIES["breakout"].prompt_directive.lower()
        pb = screener.STRATEGIES["pullback"].prompt_directive.lower()
        assert "breakout" in bo and "do not" in bo
        assert "mean-reversion" in pb or "pullback" in pb
        assert bo != pb


class TestLedgerIsArmAware:
    def _ev(self):
        s = importlib.util.spec_from_file_location(
            "ev", os.path.join(ROOT, "scripts", "evaluate_picks.py"))
        m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
        return m

    def test_same_ticker_from_two_arms_is_two_observations(self):
        """Without the arm in the key the second arm's pick is silently dropped
        and the comparison quietly measures nothing."""
        ev = self._ev()
        picks = {"stocks": {"short_term": [
            {"ticker": "AAA", "entry_price": 10.0, "target_price": 12.0,
             "stop_loss": 9.0, "conviction": 4}], "long_term": []}}
        led = {}
        assert ev.record_picks(led, picks, "2026-08-10", arm="breakout") == 1
        assert ev.record_picks(led, picks, "2026-08-10", arm="pullback") == 1
        assert ev.record_picks(led, picks, "2026-08-10", arm=None) == 1
        assert len(led["picks"]) == 3
        assert {r.get("arm") for r in led["picks"]} == {"breakout", "pullback", None}

    def test_the_same_arm_twice_is_still_deduped(self):
        ev = self._ev()
        picks = {"stocks": {"short_term": [
            {"ticker": "AAA", "entry_price": 10.0}], "long_term": []}}
        led = {}
        ev.record_picks(led, picks, "2026-08-10", arm="breakout")
        assert ev.record_picks(led, picks, "2026-08-10", arm="breakout") == 0
        assert len(led["picks"]) == 1
