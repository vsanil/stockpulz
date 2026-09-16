"""The daily canary must have exactly ONE trigger.

Moved to cron-job.org job 8454784 (7:30 AM ET daily) on 2026-09-15. GitHub's
scheduler fired this workflow at 05:33, 15:38, 16:29 and 18:06 UTC against a
nominal 12:30 — and on 2026-09-15 had not fired it at all by 13:20.

The lateness is worse here than it looks. The canary's delivery checks exist to
confirm that the 7 AM ET morning picks actually went out; the 12:30 UTC slot was
chosen to sit ~30 min after that. A run at 18:06 grades a window that closed
seven hours earlier, and `check_endpoints` deliberately WAKES the free Render
instance, so a run at a random hour also pays a cold start the schedule was
never budgeted for.

If a `schedule:` cron is ever restored here, BOTH triggers fire and the canary
runs TWICE a day: two DM reports, two sets of MUTATING snapshot->act->restore
round-trips against production storage (paper buy, alert add/replace/remove,
watchlist add), and two deliberate Render wakes. Same reasoning that removed
synthetic_user's `0 12` cron the same day.

The ET anchor is load-bearing too: the morning relay it trails (cron-job.org job
7726933) is ET-anchored, so a fixed-UTC canary drifts an hour against it at every
DST change. 7:30 AM ET is the same calendar DAY in UTC under both EDT and EST,
and the job runs daily, so no weekday can shift — the trap that cost Monday its
prescreener for eight weeks.
"""
import os
import pathlib
import re
import sys

import yaml

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

WF = ROOT / ".github" / "workflows" / "canary.yml"
SELF_HEAL = ROOT / ".github" / "workflows" / "self_heal.yml"


def _on_block(doc):
    """PyYAML parses the bare key `on` as the BOOLEAN True, not the string."""
    return doc[True] if True in doc else doc["on"]


def _schedule_crons(text):
    """Only the crons under `on:` — never the prose explaining their removal.

    The header above the block quotes the times GitHub actually fired at while
    explaining why the cron is gone. A naive text scan flags its own
    justification; this repo has paid for that trap a dozen times.
    """
    block = text[text.index("on:"):text.index("jobs:")]
    return [m.group(1) for m in re.finditer(r'^\s*- cron:\s*"([^"]+)"', block, re.M)]


class TestTheCanaryHasExactlyOneTrigger:
    def test_there_is_no_github_schedule(self):
        doc = yaml.safe_load(WF.read_text())
        on = _on_block(doc)
        assert "schedule" not in on, (
            "a schedule: cron is back on canary.yml — with cron-job.org job "
            "8454784 also live the canary now runs TWICE a day, mutating "
            "production storage twice and sending two reports"
        )

    def test_no_cron_line_survives_under_on(self):
        """Belt and braces: catches a cron added under a key PyYAML tolerates."""
        assert _schedule_crons(WF.read_text()) == []

    def test_workflow_dispatch_is_still_the_entry_point(self):
        """cron-job.org POSTs the dispatch endpoint. Remove this and the only
        remaining trigger dies silently — the canary simply stops running."""
        on = _on_block(yaml.safe_load(WF.read_text()))
        assert "workflow_dispatch" in on


class TestTheReplacementTriggerIsRecorded:
    def test_the_header_names_the_cronjob_id_and_the_hour(self):
        """A trigger that lives outside the repo is invisible unless the repo
        says where it is. Someone reading a workflow with no schedule and no
        pointer concludes it is dead and 'fixes' it by adding a cron back."""
        head = WF.read_text()[:WF.read_text().index("jobs:")]
        assert "8454784" in head, "the cron-job.org job id is not recorded"
        assert "7:30 AM ET" in head, "the replacement schedule is not recorded"

    def test_the_header_says_not_to_re_add_a_cron(self):
        head = WF.read_text()[:WF.read_text().index("jobs:")]
        assert re.search(r"DO NOT RE-ADD.*schedule", head, re.S | re.I), (
            "nothing warns the next reader off restoring the cron"
        )


class TestSelfHealStillSeesTheCanary:
    """self_heal triggers on `workflow_run` by workflow NAME, not by event type.

    A dispatch-triggered run still completes and still fires workflow_run — but
    only because cron-job.org authenticates with a PAT. A run dispatched by the
    automatic GITHUB_TOKEN would NOT create downstream workflow runs, and the
    auto-fix net would go quiet with nothing reporting it.
    """

    def test_the_canary_name_is_unchanged(self):
        name = yaml.safe_load(WF.read_text())["name"]
        watched = _on_block(yaml.safe_load(SELF_HEAL.read_text()))["workflow_run"]["workflows"]
        assert name in watched, (
            f"self_heal watches {watched!r} and this workflow is now {name!r} — "
            "renaming a monitor silently stops the auto-fix net forever"
        )
