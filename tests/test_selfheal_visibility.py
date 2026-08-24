"""Self-heal fixes must be VISIBLE, not just written.

🔴 The incident this closes: for four days (2026-08-21 → 08-24) self_heal wrote
real fixes to branches and proposed none of them, because its test gate had no
pytest installed and so could never report green. The DMs that should have
flagged it discarded Telegram's response, so nobody could tell whether they had
even been sent. Three fixes sat unreviewed.

A branch nobody is told about is a fix that does not exist.
"""
import pathlib

import pytest

WF = pathlib.Path(".github/workflows/self_heal.yml")
WEB = pathlib.Path("webhook.py")
CAN = pathlib.Path("scripts/canary.py")


class TestTheAdminCard:
    def _js(self):
        src = WEB.read_text()
        i = src.index("function selfhealCard(")
        return src[i:src.index("function age_(", i)]

    def test_it_renders_even_when_empty(self):
        """An invisible card is indistinguishable from a broken one."""
        js = self._js()
        assert "Nothing waiting" in js
        # Assert NO early bail-out of any spelling, not one exact string: a
        # mutation using `if(!rows.length) return '';` walked straight past the
        # narrower check.
        import re
        assert not re.search(r"return\s*''\s*;", js), \
            "the card must never return empty — that is the invisible-card bug"

    def test_it_links_out_for_review_rather_than_merging_in_place(self):
        """Merging deploys to production, and the diff lives where it can
        actually be read. A button beside a one-line summary invites approving
        without reading — the failure the findings card already hit."""
        js = self._js()
        assert "Review &amp; merge on GitHub" in js
        assert "setFinding" not in js and "fetch(" not in js

    def test_it_warns_that_merging_deploys(self):
        js = self._js()
        assert "merging deploys to production" in js
        assert "Read the diff before merging" in js

    def test_already_merged_branches_are_hidden(self):
        """ahead_by == 0 means it is already in main — showing it is noise."""
        src = WEB.read_text()
        assert 'if not cmp_.get("ahead_by"):' in src

    def test_the_card_is_wired_into_the_dashboard(self):
        assert "selfhealCard(d.selfheal)" in WEB.read_text()

    def test_github_being_down_cannot_break_the_dashboard(self):
        src = WEB.read_text()
        i = src.index("def _build_selfheal_prs")
        body = src[i:src.index("def _build_audit_findings", i)]
        assert "except Exception" in body and "timeout=" in body

    def test_it_is_cached_so_the_dashboard_does_not_hammer_github(self):
        assert "_SELFHEAL_CACHE" in WEB.read_text()


class TestTheReminderCannotFeedItself:
    """🔴 The design constraint that matters most here. A canary FAILURE
    triggers self_heal, which writes another branch. So a failing "you have
    unmerged branches" check would manufacture the very condition it reports,
    every day, forever."""

    def _fn(self):
        src = CAN.read_text()
        i = src.index("def check_selfheal_unmerged")
        return src[i:src.index("def check_storage_surfaces", i)]

    def test_it_never_reports_a_failure(self):
        body = self._fn()
        import re
        # Every _check call in this function must pass True as the verdict.
        for m in re.finditer(r'_check\("selfheal\.unmerged",\s*(\w+)', body):
            assert m.group(1) == "True", \
                "a failing reminder would trigger self_heal and create more branches"

    def test_it_says_why_it_cannot_fail(self):
        assert "MUST NEVER FAIL" in self._fn()

    def test_it_is_silent_when_there_is_nothing_waiting(self):
        """A check that speaks up every day trains you to ignore it."""
        assert "no unreviewed auto-fixes" in self._fn()

    def test_an_unreachable_github_is_NOT_VERIFIED_not_a_clean_pass(self):
        body = self._fn()
        assert body.count("NOT VERIFIED") >= 2

    def test_it_is_registered(self):
        assert "check_selfheal_unmerged," in CAN.read_text()
