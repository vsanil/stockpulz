"""The app must tell users the hour it actually delivers.

CLAUDE.md has carried the rule "all user-facing strings must say 7 AM ET —
never 8 AM ET" for months, and nothing enforced it. On 2026-09-14 the repo was
telling users BOTH: seven files said 7 AM, five said 8 AM — including the
ONBOARDING card (the first thing a new user reads) and the PUBLIC LANDING PAGE
(docs/index.html), which promised "Every morning at 8:00 AM ET".

Why 7 is the true answer, and why it is DST-safe: the morning delivery is
triggered by cron-job.org job 7726933, scheduled `hours=[7]` in
America/New_York, Mon-Fri. Being ET-anchored, it is 7:00 AM ET year-round —
11:00 UTC under EDT, 12:00 UTC under EST. The `0 11 * * 1-5` line in
daily_run.yml is only a run-mode RESOLVER mapping; it is NOT an active GitHub
schedule (verified against the workflow's `on.schedule` block), so it does not
drift the delivery hour in winter.

This is a claim defect, not a typo: a user told 8:00 who gets picks at 7:00 has
been given a promise the app does not keep, in the same breath as the levels
and alerts it does.
"""
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DELIVERY_HOUR_ET = 7

# Built from an int so the banned literal never appears in this file. The
# self-flagging trap has bitten this repo a dozen times: a scan whose subject is
# also discussed in its own prose flags itself.
_WRONG = str(DELIVERY_HOUR_ET + 1)
BANNED = re.compile(rf"\b{_WRONG}(?::00)?\s?(?:AM|am)\s?ET\b")
RIGHT = re.compile(rf"\b{DELIVERY_HOUR_ET}(?::00)?\s?(?:AM|am)\s?ET\b")

SKIP_DIRS = {"tests", ".git", "node_modules", "graphify-out", "analysis", "__pycache__"}


def _sources():
    for path in list(ROOT.rglob("*.py")) + list(ROOT.rglob("*.html")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


class TestNoStringPromisesTheWrongHour:
    def test_nothing_claims_an_hour_the_app_does_not_deliver(self):
        offenders = []
        for path in _sources():
            for i, line in enumerate(path.read_text(errors="ignore").splitlines(), 1):
                if BANNED.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{i}: {line.strip()[:80]}")
        assert not offenders, (
            f"{len(offenders)} string(s) promise an hour the app does not "
            f"deliver — the morning run fires at {DELIVERY_HOUR_ET}:00 AM ET:\n  "
            + "\n  ".join(offenders)
        )

    def test_the_scan_can_actually_detect_an_offender(self):
        """A guard that cannot fail is not a guard."""
        for probe in (f"{_WRONG} AM ET", f"{_WRONG}:00 AM ET", f"at {_WRONG}:00 am ET."):
            assert BANNED.search(probe), f"scan missed {probe!r}"

    def test_the_scan_does_not_flag_the_correct_hour(self):
        assert not BANNED.search(f"{DELIVERY_HOUR_ET}:00 AM ET")


class TestThePitchSurfacesStateTheRightHour:
    """Positive assertions: it is not enough that the wrong hour is absent —
    the surfaces a new user actually reads must state the right one."""

    def test_the_onboarding_card_states_the_delivery_hour(self):
        src = (ROOT / "cmd_settings.py").read_text()
        card = src[src.index("Your daily schedule"):][:400]
        assert RIGHT.search(card), (
            "the onboarding card no longer tells a new user when picks arrive"
        )

    def test_the_public_landing_page_states_the_delivery_hour(self):
        html = (ROOT / "docs" / "index.html").read_text()
        assert len(RIGHT.findall(html)) >= 2, (
            "the landing page should state the delivery hour where it describes "
            "the daily rhythm"
        )

    def test_the_resolver_mapping_is_not_an_active_github_schedule(self):
        """If `0 11 * * 1-5` ever became a real GH schedule, delivery would
        drift to 6:00 AM ET every winter and every string here would be wrong
        for four months of the year."""
        wf = (ROOT / ".github" / "workflows" / "daily_run.yml").read_text()
        block = wf[wf.index("  schedule:"):wf.index("  workflow_dispatch:")]
        assert '- cron: "0 11 * * 1-5"' not in block, (
            "the morning hour is now on GitHub's UTC scheduler, so it shifts an "
            "hour at every DST change — the ET-anchored cron-job.org trigger is "
            "what keeps the advertised hour true"
        )
