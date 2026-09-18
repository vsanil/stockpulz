"""The entry-window finding must not tell the reader to widen the window.

🔴 It did, nightly, for five days after that advice was rejected. The text read
"either widen the published window in formatters.entry_window_pct to match
measured reality" — proposed 2026-09-13, investigated the same day, and REJECTED:
DOT's 11.22% was a real overnight crypto move, not a pricing bug, so there was
nothing to accommodate, and widening would have legitimised the bad fill while
quietly weakening a promise the morning message makes to users.

That matters more than a wrong comment, because these findings lead
`analysis/ENGINE_FINDINGS.md` — the file a session is instructed to read BEFORE
answering the first request. A stale instruction there is an instruction.

The text also has to distinguish WHEN, which is the whole question. Before
2026-09-15 the bot both ignored the window and bought 4-6 h late, so a breach
measured its own execution lag; after it, the same number is genuine evidence
about the levels.
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import analyze_engine as ae  # noqa: E402


class TestTheRejectedAdviceIsGone:
    @pytest.mark.parametrize("date", ["2026-08-27", "2026-09-30"])
    def test_no_variant_ever_tells_you_to_widen_the_window(self, date):
        """Both branches, because only one of them used to exist."""
        txt = ae._entry_window_fix(date).lower()
        assert "do not widen" in txt, "the prohibition is missing"
        # the rejected instruction, in the shape it was actually written
        assert "either widen" not in txt
        assert "to match measured reality" not in txt or "do not widen" in txt

    @pytest.mark.parametrize("date", ["2026-08-27", "2026-09-30"])
    def test_it_names_the_one_definition_as_read_only(self, date):
        txt = ae._entry_window_fix(date)
        assert "formatters.entry_window_pct" in txt, (
            "the constant must still be named — the reader has to know where the "
            "promise lives, they just must not change it"
        )


class TestItSaysWhichKindOfBreachThisIs:
    """A fix suggestion that cannot tell history from news is how the old text
    sent a reader toward a rejected change."""

    def test_a_fill_before_the_cutoff_is_historical_and_acknowledge_only(self):
        txt = ae._entry_window_fix("2026-09-08")      # the DOT breach
        assert txt.startswith("HISTORICAL")
        assert "acknowledge" in txt.lower()
        assert "cannot be un-made" in txt

    def test_a_fill_after_the_cutoff_is_real_evidence_about_the_levels(self):
        txt = ae._entry_window_fix("2026-09-16")
        assert txt.startswith("NEW")
        assert "REAL EVIDENCE" in txt
        assert "levels" in txt.lower()

    def test_the_boundary_date_itself_counts_as_new(self):
        """The cutoff is the first day BOTH properties held, so it is not history."""
        assert ae._entry_window_fix(ae._BOT_OBEYS_WINDOW_SINCE).startswith("NEW")

    def test_the_historical_branch_explains_the_execution_lag(self):
        """Without this the reader cannot tell why an old number is untrustworthy."""
        txt = ae._entry_window_fix("2026-08-27")
        assert "late" in txt and "execution" in txt.lower()

    def test_an_unusable_date_does_not_crash_and_reads_as_historical(self):
        """`ex.get("date", "?")` can yield '?'. Unmeasurable must degrade to the
        conservative branch, never raise inside the nightly report builder."""
        txt = ae._entry_window_fix("?")
        assert txt.startswith("HISTORICAL")


class TestTheFindingActuallyUsesIt:
    def test_the_builder_calls_the_helper_rather_than_inlining_prose(self):
        """Anchored on the AST, not the source text — this file's own docstring
        quotes the banned phrase while explaining its removal, which is the
        self-flagging trap this repo has paid for a dozen times."""
        import ast
        import pathlib
        src = pathlib.Path(ROOT, "scripts", "analyze_engine.py").read_text()
        called = {
            n.func.id
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
        }
        assert "_entry_window_fix" in called, (
            "the helper exists but nothing calls it — a fix wired at one layer only, "
            "which is how --summary reached the CLI but never the workflow"
        )
