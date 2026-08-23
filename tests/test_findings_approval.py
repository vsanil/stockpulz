"""The approval workflow: propose → approve → implement.

🔴 What this guards, stated plainly: it is a RECORD and an AUDIT, not a lock.
Nothing can technically stop an agent editing a file. What it CAN do is make
"implemented without approval" detectable — a finding whose condition
disappears while still `awaiting_approval` never passed through `approved`, so
it is marked `resolved_UNAPPROVED` and stays that way until a human looks.
That converts an unenforceable promise into a checkable invariant.
"""
import importlib.util
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _mod(name, path, tmp_state):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, "scripts", path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.STATE = str(tmp_state)
    return m


@pytest.fixture
def fw(tmp_path):
    return _mod("fw", "findings.py", tmp_path / "s.json")


class TestLifecycle:
    def test_propose_then_approve(self, fw):
        assert fw.propose("x/1", "add a floor", "screener.py") == 0
        st = json.load(open(fw.STATE))
        assert st["x/1"]["status"] == "awaiting_approval"
        assert st["x/1"]["proposed_change"] == "add a floor"

        assert fw.approve("x/1", note="go ahead") == 0
        st = json.load(open(fw.STATE))
        assert st["x/1"]["status"] == "approved"
        assert st["x/1"]["approved_on"] and st["x/1"]["approved_note"] == "go ahead"

    def test_approving_something_never_proposed_is_refused(self, fw):
        assert fw.approve("nope/1") == 1

    def test_approving_an_unproposed_but_known_finding_is_refused(self, fw):
        json.dump({"x/1": {"status": "open"}}, open(fw.STATE, "w"))
        assert fw.approve("x/1") == 1, "approval must follow a concrete proposal"

    def test_re_proposing_after_approval_is_refused(self, fw):
        fw.propose("x/1", "a", "f.py"); fw.approve("x/1")
        assert fw.propose("x/1", "different", "f.py") == 1, \
            "a changed plan must not silently inherit the old consent"

    def test_a_re_proposal_before_approval_CLEARS_stale_consent(self, fw):
        """If the plan changes, prior consent does not carry over."""
        fw.propose("x/1", "a", "f.py")
        json.load(open(fw.STATE))
        fw.propose("x/1", "b", "f.py")
        st = json.load(open(fw.STATE))
        assert "approved_on" not in st["x/1"] and st["x/1"]["proposed_change"] == "b"

    def test_reject_records_the_reason(self, fw):
        fw.propose("x/1", "a", "f.py")
        assert fw.reject("x/1", note="not worth it") == 0
        st = json.load(open(fw.STATE))
        assert st["x/1"]["status"] == "wont_fix" and st["x/1"]["note"] == "not worth it"

    def test_status_exits_nonzero_when_a_violation_exists(self, fw, capsys):
        json.dump({"x/1": {"status": "resolved_UNAPPROVED",
                           "resolved_on": "2026-08-23"}}, open(fw.STATE, "w"))
        assert fw.status() == 1
        assert "WITHOUT APPROVAL" in capsys.readouterr().out


class TestTheInvariant:
    """The part that makes the workflow more than an honour system."""

    @pytest.fixture
    def ae(self, tmp_path):
        return _mod("ae_ap", "analyze_engine.py", tmp_path / "s.json")

    def test_disappearing_while_awaiting_consent_is_flagged(self, ae):
        state = {"x/1": {"status": "awaiting_approval"}}
        ae._apply_state([], state, "2026-08-23")
        assert state["x/1"]["status"] == "resolved_UNAPPROVED", \
            "an unapproved implementation went unrecorded"

    def test_disappearing_AFTER_approval_resolves_normally(self, ae):
        state = {"x/1": {"status": "approved"}}
        ae._apply_state([], state, "2026-08-23")
        assert state["x/1"]["status"] == "resolved"

    def test_an_ordinary_open_finding_resolves_normally(self, ae):
        state = {"x/1": {"status": "open"}}
        ae._apply_state([], state, "2026-08-23")
        assert state["x/1"]["status"] == "resolved"

    def test_the_violation_is_NOT_cleared_on_a_later_run(self, ae):
        """It stays until a human looks — auto-clearing would hide it."""
        state = {"x/1": {"status": "resolved_UNAPPROVED", "resolved_on": "2026-08-22"}}
        ae._apply_state([], state, "2026-08-23")
        assert state["x/1"]["status"] == "resolved_UNAPPROVED"

    def test_awaiting_approval_stays_in_the_worklist(self, ae):
        """It is waiting on the OWNER, so it must surface at sign-in."""
        assert "awaiting_approval" in ae.WORKLIST_STATUSES

    def test_approved_leaves_the_worklist(self, ae):
        """The decision is made; it is mine to implement."""
        assert "approved" not in ae.WORKLIST_STATUSES
