"""self_heal must never reach production without a human (PR-gated Aug 23)."""
import pathlib
import re

import pytest

WF = pathlib.Path(__file__).resolve().parent.parent / ".github/workflows/self_heal.yml"


class TestSelfHealIsPRGated:
    def _src(self):
        return WF.read_text()

    def test_no_path_pushes_to_main(self):
        """🔴 The whole point. Until 2026-08-23 a green test run pushed straight
        to main and Render deployed it, so an LLM's change could reach real
        users with the suite as the only gate."""
        src = self._src()
        offenders = [l.strip() for l in src.splitlines()
                     if re.search(r"push\s+.*origin\s+.*(HEAD:main|\bmain\b)", l)
                     and not l.strip().startswith("#")]
        assert not offenders, f"a path still writes to main: {offenders}"

    def test_it_pushes_to_a_branch_instead(self):
        assert 'git push -u origin "$BR"' in self._src()

    def test_a_green_fix_is_PROPOSED_not_merged(self):
        src = self._src()
        assert "outcome=proposed" in src
        assert "outcome=merged" not in src.split("Post-deploy health check")[0], \
            "the ship step can still report a merge"

    def test_it_falls_back_to_a_link_when_PR_creation_is_blocked(self):
        """`pull-requests: write` is declared, but the REPO setting may be off.
        Falling back beats demanding a broader permission than this needs."""
        src = self._src()
        assert "gh pr create" in src and "compare/main" in src

    def test_every_outcome_notifies(self):
        """'Remind me every time' — silence must never be ambiguous."""
        src = self._src()
        for outcome in ("proposed", "branch", "testfix", "nofix"):
            assert f"{outcome})" in src, f"{outcome} has no notification branch"
        assert src.count("sendMessage") >= 2, "start and finish DMs must both exist"

    def test_the_review_warning_is_in_the_message(self):
        assert "no human in the loop" in self._src()
