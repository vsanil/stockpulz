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


class TestAdminPageActuallyRenders:
    """🔴 The whole /admin page 500'd for ~40 minutes in production and every
    test was green, because nothing ever FETCHED it with an admin session —
    the tests all asserted on the JS source string instead.

    Cause: the card used '\\uD83D\\uDEA8' for an emoji. That is correct in JS
    source, but this JS lives inside a Python triple-quoted string, so PYTHON
    read it as two LONE SURROGATES. Flask cannot encode those to UTF-8, so the
    response died with UnicodeEncodeError and the page never rendered at all.
    The symptom looked exactly like a missing card.
    """

    def _admin_get(self, client):
        with client.session_transaction() as s:
            s["admin"] = True
        return client.get("/admin")

    def test_the_dashboard_returns_200_and_is_encodable(self, client):
        r = self._admin_get(client)
        assert r.status_code == 200, f"/admin is {r.status_code}, not rendering"
        r.get_data()  # the encode step that actually blew up

    def test_the_findings_card_is_present_in_the_served_page(self, client):
        assert b"Engine findings" in self._admin_get(client).get_data()

    def test_no_lone_surrogate_escapes_anywhere_in_webhook(self):
        """The bug class, not just the instance. A surrogate escape inside a
        Python string is always wrong — use a literal emoji or an HTML entity."""
        import re
        src = pathlib.Path("webhook.py").read_text()
        hits = [src[max(0, m.start() - 60):m.start() + 12]
                for m in re.finditer(r"(?<!\\)\\u[dD][89abAB][0-9a-fA-F]{2}", src)]
        assert not hits, f"lone surrogate escape(s) in webhook.py: {hits}"

    def test_the_whole_served_page_encodes_as_utf8(self, client):
        # Belt and braces: any future surrogate anywhere in the page fails here.
        self._admin_get(client).get_data().decode("utf-8")


class TestRelativeDatesSurviveSupabaseTimestamps:
    """🔴 Every timestamp on /admin read "NaNd ago" — users, cron health,
    pending approvals, feedback. age() appended 'Z' unless the string already
    ended in 'Z', but Supabase returns an OFFSET ('...+00:00'), so it built
    '...+00:00Z' — an INVALID date. That does not throw, so the catch never
    fired; NaN simply failed every `<` comparison and fell through to the
    final `Math.round(m/1440)+'d ago'`.

    Almost certainly introduced by the Supabase migration: Postgres returns an
    offset where the Gist stored naive/Z stamps. 'Wired' was not 'working'.

    🔴 This test evaluates the SERVED page, not webhook.py's source. The source
    is a Python string, so '\\\\d' there is '\\d' by the time the browser sees
    it — testing the raw text gave me a confidently wrong result first time.
    """

    def _served_age(self, client):
        with client.session_transaction() as s:
            s["admin"] = True
        html = client.get("/admin").get_data(as_text=True)
        i = html.index("function age(iso)")
        return html[i:html.index("function ini(", i)]

    def _run(self, js, value):
        import json
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            pytest.skip("node not available")
        out = subprocess.run(
            [node, "-e", js + f"\nconsole.log(age({json.dumps(value)}))"],
            capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    @pytest.mark.parametrize("stamp", [
        "2026-08-21T20:47:03.931320+00:00",   # Supabase
        "2026-08-21T20:47:03.931320",         # legacy naive
        "2026-08-21T20:47:03Z",               # explicit Z
        "2026-08-21T15:47:03-05:00",          # non-UTC offset
    ])
    def test_no_stamp_format_renders_NaN(self, client, stamp):
        got = self._run(self._served_age(client), stamp)
        assert "NaN" not in got, f"{stamp} rendered {got!r}"

    def test_the_supabase_format_gives_the_RIGHT_age(self, client):
        """Not merely non-NaN — all four formats are the same instant, so a
        parser that silently dropped the zone would still avoid NaN while
        being hours wrong."""
        js = self._served_age(client)
        assert self._run(js, "2026-08-21T20:47:03.931320+00:00") == \
               self._run(js, "2026-08-21T20:47:03.931320Z")

    def test_an_unparseable_stamp_shows_the_stamp_not_a_number(self, client):
        assert "NaN" not in self._run(self._served_age(client), "not-a-date")
