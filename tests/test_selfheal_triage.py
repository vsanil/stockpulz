"""🔴 A feedback loop, observed live 2026-09-06.

`canary.check_selfheal_health` reports on the SELF-HEALER. Its docstring already
promised "this reports to the OWNER, not to self-heal — asking a broken
self-healer to heal itself is not a plan." **Nothing enforced it.** A red check
makes the canary exit non-zero, `self_heal.yml` triggers on
`workflow_run.conclusion == 'failure'`, and the healer was summoned to fix a
report that it was broken.

The look-back is SEVEN DAYS, so the red outlives its cause by a week — and every
canary run in that week re-armed a path that ends in an auto-merge to `main` and
a Render deploy. Measured: credits ran out 22:08 UTC 2026-09-05, five self-heal
runs failed, and the loop was still live at 05:13 UTC on 09-06, hours after the
credit was restored. It was spending the new credits on findings already fixed.

🔑 The rule: **a monitor that reports on the repair system must not be able to
trigger the repair system.**
"""
import importlib.util
import pathlib
import re

import pytest
import yaml

from owner_only_checks import OWNER_ONLY_CHECKS, all_owner_only

WF = pathlib.Path(".github/workflows/self_heal.yml")


@pytest.fixture(scope="module")
def canary():
    sp = importlib.util.spec_from_file_location("canary_mod", "scripts/canary.py")
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wf_text():
    return WF.read_text()


class TestTheDecision:
    def test_only_owner_only_failures_stand_self_heal_down(self):
        assert all_owner_only({"selfheal.healthy"})
        assert all_owner_only({"selfheal.healthy", "cron.all_modes_firing"})

    def test_one_actionable_failure_is_enough_to_run_it(self):
        """The loop must not become an excuse to skip real work."""
        assert not all_owner_only({"selfheal.healthy", "picks.math"})

    def test_an_ordinary_failure_runs_self_heal(self):
        assert not all_owner_only({"prices.stocks"})

    def test_an_EMPTY_set_FAILS_OPEN(self):
        """⚠️ The direction that matters. 'Nothing parsed' is not 'nothing
        actionable' — reading it as a skip would silently disable the auto-fix
        net, which is strictly worse than the loop this fixes."""
        assert not all_owner_only(set())
        assert not all_owner_only([])

    def test_blank_and_whitespace_names_do_not_fake_a_skip(self):
        assert not all_owner_only({"", "   "})


class TestTheGateSurvivesRefactoring:
    def test_self_heal_cannot_run_without_the_triage_job(self, wf_text):
        """🔑 Why triage is its OWN JOB: a step-level `if:` would have to be
        repeated on every step, and the next step someone adds would silently
        bypass it. `needs:` cannot be forgotten."""
        d = yaml.safe_load(wf_text)
        assert d["jobs"]["self-heal"]["needs"] == "triage"
        assert "needs.triage.outputs.actionable == 'true'" in d["jobs"]["self-heal"]["if"]

    def test_a_manual_dispatch_still_proceeds(self, wf_text):
        """The manual path is how the healer gets exercised without waiting for
        a real outage. Gating it would remove the only safe way to test it."""
        assert 'if [ "${{ github.event_name }}" = "workflow_dispatch" ]' in wf_text
        seg = wf_text.split('workflow_dispatch" ]', 1)[1][:200]
        assert "actionable=true" in seg

    def test_standing_down_is_announced_not_silent(self, wf_text):
        """A silent skip is how 'the net is down and nobody knows' happens —
        the exact failure this workflow exists to prevent."""
        steps = yaml.safe_load(wf_text)["jobs"]["triage"]["steps"]
        told = [st for st in steps
                if "actionable == 'false'" in str(st.get("if", ""))]
        assert told, "nothing runs when self-heal stands down"
        assert any("sendMessage" in str(st.get("run", "")) for st in told), \
            "the stand-down step must actually message the owner"


class TestTheRegexActuallyMatchesTheCanary:
    """🔑 The gate is only as good as its parse. This reads the pattern OUT OF
    the workflow and runs it against output the canary really produces, so the
    two cannot drift apart."""

    def _pattern(self, wf_text):
        m = re.search(r're\.findall\(r"([^"]+)", log\)', wf_text)
        assert m, "the triage job's FAIL-name regex could not be located"
        return m.group(1)

    def test_the_regex_matches_the_real_format(self, canary, wf_text, capsys):
        canary.RESULTS.clear()
        canary._check("selfheal.healthy", False, "", fail_detail="5/50 runs FAILED in 7d")
        canary._check("cron.all_modes_firing", False, "", fail_detail="TRIGGER(S) DEAD")
        out = capsys.readouterr().out
        assert set(re.findall(self._pattern(wf_text), out)) == {
            "selfheal.healthy", "cron.all_modes_firing"}

    def test_it_does_NOT_match_passing_checks(self, canary, wf_text, capsys):
        """A PASS line whose note mentions failure must not read as a failure —
        that would stand self-heal down on a healthy run."""
        canary.RESULTS.clear()
        canary._check("selfheal.healthy", True, "50 runs in 7d, latest success")
        out = capsys.readouterr().out
        assert re.findall(self._pattern(wf_text), out) == []


class TestTheListNamesRealChecks:
    def test_every_owner_only_name_is_a_check_the_canary_emits(self):
        """A typo here does not fail loudly — it just means the gate never
        matches and the loop returns. So pin the names against the source."""
        src = pathlib.Path("scripts/canary.py").read_text()
        missing = [n for n in OWNER_ONLY_CHECKS if f'"{n}"' not in src]
        assert not missing, f"not emitted by canary.py: {missing}"

    def test_the_list_stays_small(self):
        """⚠️ Every addition removes something from self-heal's reach. It needs
        a stated reason in owner_only_checks.py, not a quiet append."""
        assert len(OWNER_ONLY_CHECKS) <= 4
