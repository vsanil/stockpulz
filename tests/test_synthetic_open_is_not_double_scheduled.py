"""The synthetic bot's OPEN phase must have exactly ONE trigger.

Moved to cron-job.org job 8449904 (8:00 AM ET weekdays) on 2026-09-15. GitHub's
scheduler was running only 3 of the 8 jobs it was asked for each weekday and
firing `open` 4-6 HOURS late — 17:53 UTC on Mon 09-14, 16:04 on Fri 09-11,
against a nominal 12:00.

That matters more than ordinary lateness: `open` is the phase that BUYS. A run
at 2 PM ET buys against an entry window published at 7 AM, which does not model
a real user and contaminates the reachability metric the engine findings are
built on — the "filled X% outside the published entry window" findings were
measuring the bot's execution lag as much as the engine's levels.

If a `0 12` cron is ever restored here, BOTH triggers fire and the bot opens
twice in a day — double positions, double cash drain, and a doubled denominator
in every metric derived from its fills. Same reasoning that keeps the morning
relay off GitHub's scheduler.
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WF = ROOT / ".github" / "workflows" / "synthetic_user.yml"


def _schedule_block(text):
    """Only the `on.schedule:` crons — never the prose explaining them.

    The comment above the block names the removed cron while explaining WHY it
    was removed; a naive scan flags its own justification. This repo has paid
    for that trap a dozen times.
    """
    start = text.index("on:")
    end = text.index("jobs:")
    block = text[start:end]
    return [m.group(1) for m in re.finditer(r'^\s*- cron:\s*"([^"]+)"', block, re.M)]


class TestOpenHasExactlyOneTrigger:
    def test_the_noon_cron_is_not_scheduled_on_github(self):
        crons = _schedule_block(WF.read_text())
        assert "0 12 * * 1-5" not in crons, (
            "the OPEN phase is scheduled on BOTH GitHub and cron-job.org — the "
            "bot will open twice a day, double-buying and doubling the "
            f"denominator of every fill-derived metric. crons found: {crons}"
        )

    def test_the_manage_cron_is_still_scheduled(self):
        """Removing the wrong one would stop the bot ever selling at target."""
        crons = _schedule_block(WF.read_text())
        assert "0 14-20 * * 1-5" in crons, f"manage lost its schedule: {crons}"

    def test_workflow_dispatch_still_accepts_a_phase_input(self):
        """cron-job.org drives it through workflow_dispatch with phase=open.
        Drop the input and the dispatch silently runs the default, manage.

        Parsed, not substring-matched: `"phase:" in text` also matches
        `nophase:`, so renaming the input passed the first version of this test.
        An assertion that accepts the wrong answer is not a guard.
        """
        import yaml
        doc = yaml.safe_load(WF.read_text())
        # PyYAML parses a bare `on:` key as the boolean True.
        on = doc.get("on", doc.get(True))
        inputs = (on or {}).get("workflow_dispatch", {}).get("inputs", {})
        assert "phase" in inputs, (
            f"workflow_dispatch no longer declares a 'phase' input, so the "
            f"cron-job.org dispatch silently runs the default (manage) and the "
            f"bot never buys. inputs found: {sorted(inputs)}"
        )

    def test_the_phase_resolver_still_maps_the_noon_cron(self):
        """Deliberately KEPT: `github.event.schedule` is the cron STRING, so the
        resolver must still recognise it if the schedule is ever restored, and a
        manual dispatch passes phase explicitly. Removing the mapping would make
        a restored cron silently run `manage`."""
        assert "0 12 * * 1-5" in WF.read_text(), (
            "the phase resolver no longer recognises the noon cron"
        )

    def test_the_scan_can_detect_a_restored_cron(self):
        """A guard that cannot fail is not a guard."""
        faked = WF.read_text().replace(
            '    - cron: "0 14-20 * * 1-5"',
            '    - cron: "0 12 * * 1-5"\n    - cron: "0 14-20 * * 1-5"', 1)
        assert "0 12 * * 1-5" in _schedule_block(faked)
