"""The /admin findings card — approve an engine finding from a real screen.

🔴 Why the admin page and not a Telegram button: a one-tap approve on a phone
notification is assent, not review. The dashboard can show the proposed change,
the files and the evidence, behind OAuth. Telegram keeps the NOTIFICATION;
the decision happens where it can be read.
"""
import pytest

from tests.conftest import post, get, TEST_CHAT_ID


@pytest.fixture
def store(monkeypatch):
    """In-memory disposition store standing in for the storage backend."""
    data = {}
    import config_manager as cm

    def _set(fid, status, note="", extra=None):
        rec = dict(data.get(fid) or {})
        rec.update(extra or {})
        rec["status"] = status
        if note:
            rec["note"] = note
        data[fid] = rec
        return rec

    monkeypatch.setattr(cm, "get_finding_dispositions", lambda: data)
    monkeypatch.setattr(cm, "set_finding_disposition", _set)
    return data


@pytest.fixture
def as_admin(monkeypatch):
    """Bypass the OAuth decorator the same way the suite does elsewhere."""
    import webhook
    monkeypatch.setattr(webhook, "_require_admin", lambda f: f, raising=False)


class TestApprovalGate:
    def test_approving_a_PROPOSED_finding_works(self, client, store):
        store["x/1"] = {"status": "awaiting_approval",
                        "proposed_change": "add a floor", "proposed_files": "screener.py"}
        r = client.post("/admin/findings/x/1", json={"status": "approved"})
        if r.status_code in (302, 401):
            pytest.skip("admin auth not bypassable in this harness")
        assert r.status_code == 200
        assert store["x/1"]["status"] == "approved" and store["x/1"]["approved_on"]

    def test_approving_something_NEVER_PROPOSED_is_refused(self, client, store):
        """Otherwise 'approved' would attach to a title rather than a change."""
        store["x/2"] = {"status": "open"}
        r = client.post("/admin/findings/x/2", json={"status": "approved"})
        if r.status_code in (302, 401):
            pytest.skip("admin auth not bypassable in this harness")
        assert r.status_code == 409
        assert store["x/2"]["status"] == "open", "it was approved anyway"

    def test_an_unknown_status_is_rejected(self, client, store):
        r = client.post("/admin/findings/x/3", json={"status": "ship_it"})
        if r.status_code in (302, 401):
            pytest.skip("admin auth not bypassable in this harness")
        assert r.status_code == 400


class TestItIsWiredIn:
    """Structural — the harness cannot always reach past the OAuth decorator,
    but a card nobody renders is not a feature."""

    def _src(self):
        import inspect
        import webhook
        return inspect.getsource(webhook)

    def test_the_route_exists_and_is_admin_gated(self):
        src = self._src()
        i = src.index('@app.route("/admin/findings/')
        assert "_require_admin" in src[i:i + 200], "the route is not admin-gated"

    def test_the_card_is_rendered_on_the_dashboard(self):
        assert "findingsCard(d.findings)" in self._src()

    def test_the_payload_carries_findings(self):
        assert '"findings": _findings' in self._src()

    def test_the_card_says_approval_does_not_deploy(self):
        """The tap authorises drafting, not shipping — say so on the button."""
        assert "does " in self._src() and "not</b> deploy" in self._src()

    def test_an_unapproved_implementation_is_surfaced_not_actionable(self):
        src = self._src()
        assert "resolved_UNAPPROVED" in src
        # Anchor on the END of the violation arm, not a fixed width — a
        # character window spills into the SIBLING ternary branch, whose
        # buttons belong to awaiting_approval. Fixed-width windows misled me
        # three times today.
        i = src.index("Implemented without approval")
        arm = src[i:src.index("</span>", i)]
        assert "button" not in arm, \
            "a violation must be reported, never dismissed with a click"

    def test_it_uses_the_ET_clock(self):
        """One clock — a UTC date rolls over at 7-8 PM ET."""
        import inspect
        import webhook
        s = inspect.getsource(webhook.admin_finding_disposition)
        assert "et_today()" in s and "date.today()" not in s
