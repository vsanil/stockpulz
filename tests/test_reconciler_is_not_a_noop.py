"""🔴 scripts/reconcile_storage.py was a NO-OP from 7fee123 (Aug 22) until
Aug 23, and a dispatched run reported clean success while reconciling nothing.

Cause: `_sb_read` was inserted into the MIDDLE of `main()`. main() became three
statements that parse argv and return None; the real logic sat after
_sb_read's `return`, unreachable. Nothing printed, exit code 0.

Same family as the documented decorator gotcha — never insert a helper `def`
into the middle of an existing function. py_compile cannot see it, and neither
can a workflow that only checks the exit code.
"""
import ast
import pathlib

import pytest

SRC = pathlib.Path("scripts/reconcile_storage.py")


def _fn(name):
    tree = ast.parse(SRC.read_text())
    return next(f for f in ast.walk(tree)
                if isinstance(f, ast.FunctionDef) and f.name == name)


class TestMainActuallyDoesSomething:
    def test_main_has_a_real_body(self):
        """It had 22 statements originally and 3 while broken."""
        assert len(_fn("main").body) >= 10, \
            "main() has been gutted — its body is orphaned again"

    def test_main_reads_both_backends(self):
        body = ast.dump(_fn("main"))
        assert "GistBackend" in body and "SupabaseBackend" in body, \
            "main() no longer opens both stores"

    def test_no_statements_are_unreachable_after_a_return(self):
        """The exact shape of the bug: code sitting after a return."""
        for fn in ("main", "_sb_read"):
            stmts = _fn(fn).body
            for i, node in enumerate(stmts[:-1]):
                assert not isinstance(node, ast.Return), (
                    f"{fn}() has {len(stmts) - i - 1} unreachable statement(s) "
                    f"after a return on line {node.lineno}"
                )

    def test_the_helper_is_defined_outside_main(self):
        assert _fn("_sb_read").col_offset == 0, \
            "_sb_read is nested inside another function again"


class TestTargetedReconcileIsPossible:
    """A blanket run is unsafe: picks.json is GIST_WINS but the Gist copy can
    be the SMALLER one (2222 vs 14935 bytes measured Aug 23), so overwriting
    would lose data. --only exists so one file can be fixed on its own."""

    def test_only_flag_exists(self):
        assert "--only" in SRC.read_text()

    def test_only_filters_both_loops(self):
        body = SRC.read_text()
        assert body.count("if only and name not in only:") >= 2, \
            "--only must gate the KEEP_SUPABASE loop too, or it silently lies"

    def test_the_workflow_passes_it_through(self):
        wf = pathlib.Path(".github/workflows/verify_storage.yml").read_text()
        assert wf.count("--only") == 2, \
            "both the dry-run and apply branches must forward --only"

    def test_findings_state_is_reconciled(self):
        """The dispositions were written to the Gist from a local shell while
        production reads Supabase; without this entry they never arrive."""
        assert "engine_findings_state.json" in SRC.read_text()
