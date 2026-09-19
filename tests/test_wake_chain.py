"""The cold-start wake chain: cron-job.org → GitHub dispatch → runner curl.

🔴 Why a chain at all, and why neither half works alone — both measured, not
assumed:

  cron-job.org   fires punctually to the second (20 jobs prove it), but its HTTP
                 client is REFUSED by Render's sleeping edge in ~2 s (http=503,
                 dozens of observations Sept 4-5 and again Sept 15). It can
                 MAINTAIN warmth; it can never ESTABLISH it.
  GitHub cron    a runner's curl CAN boot the instance, but `schedule:` fires
                 1.6-6 h late on this repo, so it cannot hit an hour.
  dispatch       starts in SECONDS. That is the trick: punctuality from one,
                 reach from the other.

Proven end-to-end 2026-09-19 against a genuinely cold instance (0 Render log
lines for the preceding 4 h, against a control window that returned 9):
dispatch 19:02:08 → runner curl 19:02:12 → Render 'Running gunicorn' 19:02:25 →
listening 19:03:03. 55 s door to door.

⚠️ What these tests can and cannot do. Neither schedule lives in this repo, so
nothing here can assert that the cron job exists or points at this workflow —
that is verified by read-back against the cron-job.org API. What IS pinned is
the part a careless edit would silently break: the workflow must stay
dispatch-only, and it must not re-acquire the 30 s timeout that made a
SUCCESSFUL wake report as a failure.
"""
import os
import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = ROOT / ".github" / "workflows" / "keepwarm.yml"


@pytest.fixture(scope="module")
def doc():
    return yaml.safe_load(WF.read_text())


@pytest.fixture(scope="module")
def run_step():
    """The shell body of the wake step."""
    d = yaml.safe_load(WF.read_text())
    steps = d["jobs"]["wake"]["steps"]
    return "\n".join(s.get("run", "") for s in steps)


class TestItStaysDispatchOnly:
    def test_there_is_no_schedule_cron(self, doc):
        on = doc[True] if True in doc else doc["on"]
        assert "schedule" not in on, (
            "a `schedule:` here fires 1.6-6 h late on this repo, which is the "
            "whole reason the wake is dispatched from cron-job.org instead"
        )

    def test_workflow_dispatch_is_present(self, doc):
        on = doc[True] if True in doc else doc["on"]
        assert "workflow_dispatch" in on, (
            "cron-job.org POSTs the dispatch endpoint — removing this kills the "
            "only trigger and the wake silently stops happening"
        )


class TestTheTimeoutClearsTheColdStart:
    """🔴 The regression that made a working mechanism look broken."""

    def test_curl_waits_longer_than_a_measured_cold_start(self, run_step):
        times = [int(m) for m in re.findall(r"--max-time\s+(\d+)", run_step)]
        assert times, "no --max-time at all — curl would use its default"
        assert max(times) >= 90, (
            f"longest curl budget is {max(times)}s; the measured cold start is "
            "~55 s and the old 30 s value aborted mid-boot"
        )

    def test_the_step_does_not_abort_on_a_timeout(self, run_step):
        """`bash -e` + a curl timeout killed the step BEFORE it could report, so
        a run that HAD woken the instance was marked a failure."""
        assert "set -e" not in run_step or "set -uo pipefail" in run_step
        assert "|| echo" in run_step, (
            "curl's failure must be caught so the step can still report; a "
            "timeout here is an expected outcome, not an error"
        )

    def test_a_non_200_warns_rather_than_failing_the_run(self, run_step):
        """The boot is ASYNCHRONOUS — curl can give up while Render keeps
        starting. Failing the run on that trains you to ignore red."""
        assert "::warning::" in run_step
        assert "::error::" not in run_step.split("RENDER_EXTERNAL_URL")[-1], (
            "only the missing-URL case may be a hard error"
        )


class TestItVerifiesRatherThanAssumes:
    def test_it_reads_back_after_the_wake(self, run_step):
        """A first response is not evidence the instance is serving — this repo
        has been caught by 'wrote N' lines that never persisted."""
        # Anchored on the ASSIGNMENT, not on the word "curl" — the step's own
        # comments say "curl" several times, so a count matched prose and let a
        # mutation that replaced the read-back with a literal `VERIFY=200`
        # sail through. An assertion that passes on the wrong answer is not a
        # guard; this repo has paid for that shape a dozen times.
        assert re.search(r"VERIFY=\$\(\s*curl", run_step), (
            "the read-back is not an actual curl — a hardcoded verify value "
            "would report success without contacting the service"
        )
        assert len(re.findall(r"=\$\(\s*curl", run_step)) >= 2, (
            "only one real curl: the initial poke with no read-back"
        )

    def test_it_reports_the_elapsed_time(self, run_step):
        """The cold-start figure has gone stale twice in CLAUDE.md and been
        re-quoted as measured. Producing it fresh on every wake is the fix."""
        assert "elapsed" in run_step.lower()

    def test_an_already_warm_instance_is_distinguished_from_a_wake(self, run_step):
        """'Already warm' and 'woken from cold' are different facts; collapsing
        them would hide the day the wake silently stopped working."""
        assert "already warm" in run_step.lower()
        assert "woken" in run_step.lower()
