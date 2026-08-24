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


class TestTheGateCanActuallyRun:
    """🔴 The gate could never pass. self_heal installed only
    `-r requirements.txt`, but pytest is not a runtime dependency and is not in
    that file — test.yml has always installed it separately. So the hard gate
    died with `No module named pytest`, reported green=false, and the run fell
    to `outcome=branch` every single time.

    Measured: five consecutive runs (2026-08-21 → 08-24) wrote real fixes to
    branches that were NEVER proposed for review, and the DM blamed red tests
    that had not run. A gate that cannot pass is not a gate — it is an off
    switch that reports as caution.
    """

    def _wf(self):
        return pathlib.Path(".github/workflows/self_heal.yml").read_text()

    def test_pytest_is_installed(self):
        wf = self._wf()
        assert "pip install -r requirements.txt pytest" in wf, \
            "the gate has no test runner — it can only ever report failure"

    def test_a_missing_runner_is_not_reported_as_red_tests(self):
        """An infrastructure fault and a failing suite need different messages;
        conflating them is what hid this for four days."""
        wf = self._wf()
        assert "gate=unrunnable" in wf and "gate=red" in wf
        assert "pytest --version" in wf, "it must check the runner exists first"

    def test_the_unrunnable_case_says_so_in_the_DM(self):
        wf = self._wf()
        assert "THE TEST GATE COULD NOT RUN" in wf

    def test_pytest_and_check_js_are_reported_independently(self):
        """`a && b` hides which one failed."""
        wf = self._wf()
        assert 'PT=$?' in wf and 'JS=$?' in wf


class TestTheNotifierCanReportItsOwnFailure:
    """A fire-and-forget writer needs an independent check that its output
    exists — the same class as the usage counter that recorded nothing for a
    week, and the traffic tracker that never wrote a row."""

    def _wf(self):
        return pathlib.Path(".github/workflows/self_heal.yml").read_text()

    def test_the_telegram_response_is_not_discarded(self):
        wf = self._wf()
        i = wf.index("Notify admin — result")
        tail = wf[i:]
        assert "%{http_code}" in tail, "the send result must be captured"
        assert 'if [ "$CODE" != "200" ]' in tail, "a failed send must be reported"

    def test_no_parse_mode_anywhere(self):
        """snake_case filenames give odd underscore counts, which Telegram
        rejects with a 400 — silently, when the response is thrown away.
        Delivery beats bold text."""
        wf = self._wf()
        assert "-d parse_mode" not in wf, \
            "formatting must not be able to cost delivery"

    def test_the_opening_message_does_not_promise_a_merge(self):
        """It said "I'll merge + deploy it and confirm" long after the PR gate
        made that impossible — the first message a human sees was false."""
        # Anchor on the MESSAGE PAYLOAD, not the surrounding block: the comment
        # explaining this fix quotes the old false promise, so a block scan
        # flags itself. Sixth time that trap has appeared in this repo.
        import re
        wf = self._wf()
        i = wf.index("Notify admin — fixing")
        block = wf[i:wf.index("- name: Branch", i)]
        text = re.search(r'--data-urlencode "text=([^"]*)"', block).group(1)
        assert "merge + deploy" not in text, f"the opening DM still promises a merge: {text!r}"
        assert "nothing merges or deploys on its own" in text
