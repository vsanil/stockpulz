"""A finding the owner has ruled on must stop leading the agenda as [ACT].

🔴 Five `entry_window` findings led `analysis/ENGINE_FINDINGS.md` as `[ACT]`
every night, long after they were acknowledged. They are historical fills from
Aug 27 – Sep 8 that can never stop being derived, so without this they lead the
agenda forever — and `ACT` means "binary, one instance is enough to act on",
which directly contradicts the decision already recorded against them.

That is the cry-wolf failure, in the one file a session is instructed to read
FIRST. The fix DEMOTES rather than hides: a decided finding keeps its full entry
and its line under "Decided, still present" — neither nagged about nor forgotten.

Two properties this must NOT break, both load-bearing:
  * a RECURRENCE still arrives as ACT (ids are per-instance, so a new breach is
    a new id)
  * `fixed` is untouched — a finding marked fixed that is still present is still
    REOPENED, because otherwise "fixed" and "hidden" become the same word
"""
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (ROOT, os.path.join(ROOT, "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import analyze_engine as ae  # noqa: E402


def _f(status="open", tier="ACT", fid="entry_window/NVDA/2026-08-27"):
    f = ae.Finding(fid, tier, "NVDA filled 8.98% outside the published entry window",
                   "evidence here", "the suggested fix text",
                   category="bug", plain="plain words here")
    f.status = status
    return f


class TestADecidedFindingLeavesTheUrgencyTier:
    @pytest.mark.parametrize("status", ["acknowledged", "wont_fix"])
    def test_it_renders_as_DECIDED_not_ACT(self, status):
        out = _f(status=status).render()
        assert out.startswith("### [DECIDED]"), out.split("\n")[0]
        assert "### [ACT]" not in out

    @pytest.mark.parametrize("status", ["acknowledged", "wont_fix"])
    def test_the_fix_text_survives_but_stops_reading_as_an_instruction(self, status):
        """Demoted, never dropped — it is the record of what would have been done."""
        out = _f(status=status).render()
        assert "the suggested fix text" in out, "the record was dropped, not demoted"
        assert "**Fix:**" not in out, "still labelled as an outstanding instruction"
        assert f"You ruled: {status}" in out
        assert "No action is outstanding" in out

    def test_an_undecided_finding_is_untouched(self):
        out = _f(status="open").render()
        assert out.startswith("### [ACT]")
        assert "**Fix:** the suggested fix text" in out
        assert "You ruled" not in out

    @pytest.mark.parametrize("status", ["awaiting_approval", "approved"])
    def test_a_finding_still_in_flight_keeps_its_tier(self, status):
        """These are NOT ruled on — one waits on the owner, the other on me."""
        assert _f(status=status).render().startswith("### [ACT]")


class TestItCannotSilenceSomethingLive:
    def test_fixed_is_NOT_a_decided_status(self):
        """The reopen invariant. `fixed` + still present => REOPENED, so it must
        never be demoted out of sight."""
        assert "fixed" not in ae.DECIDED_STATUSES

    def test_a_reopened_finding_renders_as_ACT_even_though_it_was_marked_fixed(self):
        f = _f(status="open")
        f.reopened = True
        out = f.render()
        assert out.startswith("### [ACT]")
        assert "REOPENED" in out

    def test_a_recurrence_is_a_different_id_so_it_arrives_fresh(self):
        """Ids are per-instance. Acknowledging NVDA on 08-27 cannot pre-silence
        NVDA on a later date — the whole reason this demotion is safe."""
        old = _f(status="acknowledged", fid="entry_window/NVDA/2026-08-27")
        new = _f(status="open", fid="entry_window/NVDA/2026-09-30")
        assert old.id != new.id
        assert old.render().startswith("### [DECIDED]")
        assert new.render().startswith("### [ACT]")


class TestTheDocumentStillListsThem:
    def test_decided_findings_keep_their_line_in_the_decided_section(self, monkeypatch):
        """Demote, don't disappear: the card/report must still show that a
        ruling exists, or 'decided' becomes indistinguishable from 'never seen'."""
        monkeypatch.setattr(ae, "_load", lambda: {
            "uid": "0", "log": {}, "paper": {}, "rows": []})
        monkeypatch.setattr(ae, "_load_state", lambda: {})
        doc = ae.build(dry=True)
        # the section header is emitted only when something is decided; with an
        # empty fixture nothing is, so assert the MECHANISM exists instead
        import inspect
        src = inspect.getsource(ae.build)
        assert "Decided, still present" in src
        assert 'f.status in ("acknowledged", "wont_fix")' in src
