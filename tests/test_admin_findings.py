"""The /admin findings card — approve an engine finding from a real screen.

🔴 Why the admin page and not a Telegram button: a one-tap approve on a phone
notification is assent, not review. The dashboard can show the proposed change,
the files and the evidence, behind OAuth. Telegram keeps the NOTIFICATION;
the decision happens where it can be read.
"""
import pathlib
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
        assert "findingsCard(d.findings," in self._src()

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


class TestCardIsVisibleWhenEmpty:
    """🔴 The card rendered `''` with nothing pending, so the owner went to
    /admin, saw no card at all, and reasonably concluded the feature was not
    built. An invisible control is indistinguishable from a broken one — the
    resting state has to SAY there is nothing waiting."""

    def _js(self):
        src = pathlib.Path("webhook.py").read_text()
        i = src.index("function findingsCard(")
        return src[i:src.index("\nasync function setFinding", i)]

    def test_an_empty_list_still_renders_the_card(self):
        js = self._js()
        assert "if(!rows||!rows.length) return '';" not in js, (
            "the empty case must not bail out — that is the invisible-card bug"
        )
        assert "Nothing awaiting your approval" in js

    def test_the_empty_state_reports_how_many_were_decided(self):
        # Otherwise "nothing pending" is ambiguous between "you cleared them"
        # and "the store is empty because it was silently wiped".
        assert "decidedN" in self._js()

    def test_decided_findings_are_counted_but_never_listed(self):
        src = pathlib.Path("webhook.py").read_text()
        i = src.index('"findings": _findings')
        blk = src[max(0, i - 900):i + 120]
        assert "findings_decided" in blk
        # The list itself stays restricted to items awaiting a decision.
        assert "_pending" in blk and "awaiting_approval" in blk

    def test_the_repo_file_store_is_fully_gone(self):
        """Render's filesystem is ephemeral: an approval written to a repo file
        vanishes on redeploy and the GH Actions job never sees it."""
        for f in ("scripts/analyze_engine.py", "scripts/findings.py",
                  "webhook.py"):
            assert "analysis/findings_state.json" not in pathlib.Path(f).read_text(), f
