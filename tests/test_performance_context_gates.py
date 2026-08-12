"""The performance block injected into the STOCK-PICKING PROMPT.

This is the only place where measurement feeds back into what real users are
shown, so its sample gates must be at least as conservative as the numbers we
publish — not less. Until 2026-08-12 the directive gate was `>= 2`: once five
trades closed, the model began receiving instructions ("raise minimum
threshold") derived from two samples per conviction bucket. That is the same
closed loop cut in July after the synthetic bot's mechanical fills steered real
picks, reintroduced at a smaller sample size.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import performance_context as pc


class TestGatesAreConservative:
    def test_directive_gate_matches_the_conclusive_bar(self):
        """If 30 is the bar for calling something conclusive in a report the
        OWNER reads, it is the bar for telling the MODEL to change behaviour."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "ev", os.path.join(ROOT, "scripts", "evaluate_picks.py"))
        ev = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(ev)
        assert pc._MIN_DIRECTIVE_N == ev._MIN_N == 30

    def test_context_gate_matches_the_public_bar(self):
        import performance_tracker as pt
        assert pc._MIN_CONTEXT_TRADES == pt._MIN_COMMUNITY_TRADES == 10

    def test_the_two_sample_directive_cannot_return(self):
        import inspect
        src = inspect.getsource(pc)
        assert ">= 2]" not in src, "the 2-sample directive gate is back"
        assert "len(all_trades) < 5" not in src, "the 5-trade context gate is back"

    def test_directives_are_gated_on_the_constant_not_a_literal(self):
        import inspect
        src = inspect.getsource(pc.get_performance_context)
        block = src[src.index("low_conv"):src.index("if type_lines")]
        assert block.count("_MIN_DIRECTIVE_N") == 2
        assert "raise minimum threshold" in block


class TestNoDirectivesOnThinData:
    def _ctx(self, monkeypatch, n_per_bucket):
        """Build n closed trades in conviction bucket 2, all losers."""
        trades = [{"return_pct": -5.0, "conviction": 2, "closed_date": "2026-08-01",
                   "ticker": f"T{i}", "asset_type": "stock", "outcome": "stop"}
                  for i in range(n_per_bucket)]
        monkeypatch.setattr(pc, "get_allowed_users", lambda: ["1"])
        monkeypatch.setattr(pc, "load_user_trade_log", lambda uid: {"closed": trades})
        monkeypatch.setattr(pc, "human_trades", lambda ts: ts)
        return pc.get_performance_context(lookback_days=3650)

    def test_a_handful_of_losers_produces_NO_directive(self, monkeypatch):
        """The exact live hazard: a few bad trades telling the model to raise
        its threshold. Stats may render; an INSTRUCTION must not."""
        ctx = self._ctx(monkeypatch, 12)
        assert ctx, "12 trades should still produce the descriptive block"
        assert "raise minimum threshold" not in ctx

    def test_below_the_context_gate_nothing_is_injected_at_all(self, monkeypatch):
        assert self._ctx(monkeypatch, 9) == ""

    def test_a_real_sample_DOES_produce_the_directive(self, monkeypatch):
        """Raising the gate must not silently disable the feature forever."""
        ctx = self._ctx(monkeypatch, 30)
        assert "raise minimum threshold" in ctx
