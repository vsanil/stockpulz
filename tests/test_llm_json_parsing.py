"""Parsing model output — the silent-degradation class.

Models routinely return correct JSON wrapped in ```json fences. `json.loads` on
that fails at char 0 with the misleading "Expecting value: line 1 column 1", and
every caller here swallows the exception — so the feature does nothing and the
logs look like noise. Live on 2026-08-11 at five sites; the worst was cmd_nlp,
where EVERY natural-language command fell through to the chat fallback and no
intent ever executed ("bought 10 AAPL at 220" returned commentary instead of
logging the position).
"""
import ast
import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from llm_client import strip_fences


class TestStripFences:
    @pytest.mark.parametrize("raw,want", [
        ('```json\n{"intent": "bought"}\n```', '{"intent": "bought"}'),
        ('```JSON\n{"a":1}\n```',              '{"a":1}'),      # casing varies
        ('```\n{"a":1}\n```',                  '{"a":1}'),      # no language tag
        ('Here you go:\n```json\n{"a":1}\n```', '{"a":1}'),     # leading prose
        ('```json\n{"a":1}',                   '{"a":1}'),      # truncated response
        ('  {"a": 1}  ',                       '{"a": 1}'),
    ])
    def test_unwraps(self, raw, want):
        assert strip_fences(raw) == want
        json.loads(strip_fences(raw))

    @pytest.mark.parametrize("raw", ['{"a":1}', '[{"t":"AAPL"}]', '{"nested": {"b": [1,2]}}'])
    def test_bare_json_is_untouched(self, raw):
        """Load-bearing: ai_analyzer's three callers are on the morning-pick
        critical path and golden-tested. Input that already parsed must be
        returned byte-identical, so this change cannot move a pick."""
        assert strip_fences(raw) == raw

    def test_the_exact_live_payload(self):
        """Captured from the real API on 2026-08-11 — the model parsed the
        request correctly and the result was thrown away."""
        raw = '```json\n{"intent": "bought", "ticker": "AAPL", "price": 220, "shares": 10}\n```'
        assert json.loads(strip_fences(raw))["intent"] == "bought"
        with pytest.raises(json.JSONDecodeError):
            json.loads(raw.strip())          # what the code used to do

    def test_empty_and_none_are_safe(self):
        assert strip_fences("") == ""
        assert strip_fences(None) is None


class TestNoUnstrippedLLMJsonParse:
    """🔴 The guard that keeps the class closed.

    Five sites drifted because the fix existed (ai_analyzer._strip_fences) but
    was applied unevenly — one solved problem in five different states. Same
    shape as tests/test_no_shadow_imports.py: scan the source, keep it at zero.
    """

    def _offenders(self):
        bad = []
        for path in sorted(ROOT.glob("*.py")) + sorted((ROOT / "scripts").glob("*.py")):
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute) and fn.attr == "loads"):
                    continue
                if not node.args:
                    continue
                src = ast.unparse(node.args[0])
                # Only care about parsing a model response.
                if "content[0].text" not in src:
                    continue
                if "strip_fences" not in src:
                    bad.append(f"{path.name}:{node.lineno}  {src[:70]}")
        return bad

    def test_every_llm_json_parse_strips_fences(self):
        bad = self._offenders()
        assert not bad, (
            "json.loads on model output without strip_fences — fenced JSON will "
            "raise at char 0 and the caller will swallow it:\n  " + "\n  ".join(bad))

    def test_the_scan_can_actually_detect_an_offender(self):
        """A guard that cannot fail is not a guard."""
        tree = ast.parse('import json\nx = json.loads(message.content[0].text.strip())\n')
        found = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "loads" and n.args
                 and "content[0].text" in ast.unparse(n.args[0])
                 and "strip_fences" not in ast.unparse(n.args[0])]
        assert len(found) == 1


class TestOneDefinition:
    def test_ai_analyzer_delegates_rather_than_duplicating(self):
        import ai_analyzer
        import llm_client
        assert ai_analyzer._strip_fences is llm_client.strip_fences


class TestPortfolioGuidanceBudget:
    """The token budget was flat at 250 while the response is one line PER
    POSITION. Past ~8 positions it truncated mid-string, the JSON failed to
    parse, and ALL guidance was dropped — so the feature silently vanished for
    exactly the users with the most positions to guide. The owner's own account
    (20 open) needs ~500 tokens and never got guidance.
    """

    def test_budget_scales_with_the_portfolio(self):
        budget = lambda n: min(2000, 250 + 40 * n)
        assert budget(1) >= 250
        assert budget(20) >= 500, "the owner's real portfolio must fit"
        assert budget(20) == 1050
        assert budget(100) == 2000, "bounded — cannot run away on a huge account"
        assert budget(5) < budget(25), "must grow with position count"

    def test_the_call_site_actually_scales(self):
        """The capability existing while the caller passes a constant WAS the
        bug in close_trade's outcome field — assert the wiring, not the helper."""
        import inspect, re
        import cmd_trades
        src = inspect.getsource(cmd_trades)
        # Scope to the guidance call only. A blanket "no max_tokens=250 anywhere"
        # is WRONG: the /review coach insight legitimately uses a flat 250 because
        # its output is fixed-size prose ("2-3 sentences max") regardless of how
        # many trades go in — truncation there clips a sentence, it does not
        # discard a parsed object.
        block = src[src.index("Ask Haiku for one-line guidance"):]
        block = block[:block.index("Exposure summary")]
        assert "max_tokens=min(2000, 250 + 40 * len(position_data))" in block
        assert not re.search(r"max_tokens=\d+\s*,", block), \
            "the guidance call must scale with position count, not use a constant"

    def test_truncated_guidance_salvages_complete_pairs(self):
        from cmd_trades import _salvage_guidance
        truncated = ('{\n "AAPL": "Hold — 3% from target",\n'
                     ' "NVDA": "Take profit — at target",\n "MSFT": "Watch — near st')
        got = _salvage_guidance(truncated)
        assert got == {"AAPL": "Hold — 3% from target",
                       "NVDA": "Take profit — at target"}, got
        assert "MSFT" not in got, "an incomplete pair must not be half-reported"

    def test_salvage_is_safe_on_garbage(self):
        from cmd_trades import _salvage_guidance
        for junk in ("", "not json at all", "{", None):
            assert _salvage_guidance(junk) == {}

    def test_salvage_handles_escaped_quotes(self):
        from cmd_trades import _salvage_guidance
        assert _salvage_guidance(r'{"AAPL": "Hold \"tight\" here", "X": "tr') \
            == {"AAPL": r'Hold \"tight\" here'}
