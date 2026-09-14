"""Three defects found by reading the Render logs on 2026-09-14.

All three had been live for days and none was visible from inside the app:

1. Every boot printed "Keep-alive DISABLED — Starter plan never idles" and told
   the reader to set KEEPALIVE_ENABLED=1 "if this service is ever moved to the
   Free plan". It HAS been on Free since 2026-09-02, it does sleep, and
   CLAUDE.md says explicitly not to restore the keep-alive. So the operational
   log asserted a false fact and instructed the one action the decision forbids.

2. Every boot also logged an invalid-escape DeprecationWarning, from the CSS
   comment that WARNS AGAINST backslash escapes in the admin HTML string. A
   future Python turns that into a SyntaxError, i.e. the app stops importing.

3. /admin polled /admin/data every 60s unconditionally. Measured from the logs:
   unbroken 60s ticks for the whole time a tab was open. On Render Free that
   defeats the 15-min idle timer outright, so one forgotten tab pins the
   instance awake — ~744 h/mo against a 750 h ACCOUNT pool shared with three
   other apps — and MASKS every sleep bug while it does so.

The poll test DRIVES the served JavaScript under node rather than scanning for
a string: a source scan is what let the whole page 500 for 40 minutes with
twenty green tests.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest

import webhook

SRC = pathlib.Path(ROOT, "webhook.py").read_text()


def _admin_html(client):
    with client.session_transaction() as s:
        s["admin"] = True
    r = client.get("/admin")
    assert r.status_code == 200, f"/admin is {r.status_code}"
    return r.get_data(as_text=True)


class TestNoInvalidEscapeSequences:
    """The CLASS, not the instance — a sibling of the lone-surrogate guard.

    Python parses this file's triple-quoted admin page BEFORE the browser ever
    sees it, so any backslash escape written for CSS or JS is eaten first. Today
    that is a DeprecationWarning on every boot; in a future Python it is a
    SyntaxError and the app does not import at all.
    """

    def test_webhook_compiles_with_no_escape_warnings(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile(SRC, "webhook.py", "exec")
        bad = [w for w in caught if "escape" in str(w.message).lower()]
        assert not bad, (
            "invalid escape sequence(s) in webhook.py — Python eats the "
            "backslash before the browser sees it: "
            + "; ".join(f"line {w.lineno}: {w.message}" for w in bad)
        )

    def test_the_scan_can_actually_detect_an_offender(self):
        """A guard that cannot fail is not a guard."""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            compile('x = "a \\q b"', "fake.py", "exec")
        assert [w for w in caught if "escape" in str(w.message).lower()], (
            "the scan no longer detects a known-bad escape"
        )

    def test_no_escape_survives_as_a_control_character(self):
        """The SILENT half of the class, and the warning test cannot see it.

        Found by mutation: re-inserting the historical `content:'\\25B8'` bug
        produced NO warning at all, because `\\25` is a perfectly VALID Python
        OCTAL escape. It just resolves to chr(0x15) and renders the literal
        text 'B8' on the page — which is exactly what shipped once before.

        152 backslashes in this file's admin string are the correct doubled
        form (`\\\\'`), so banning backslashes outright is wrong. The precise
        invariant is about what SURVIVES parsing: a page built for a browser
        has no business containing control characters.
        """
        import ast

        offenders = []
        for node in ast.walk(ast.parse(SRC)):
            if not (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and len(node.value) > 2000):
                continue
            for ch in set(node.value):
                if ord(ch) < 32 and ch not in "\n\r\t":
                    offenders.append(f"line {node.lineno}: chr({ord(ch)})")
        assert not offenders, (
            "a backslash escape resolved to a control character in the admin "
            "page — Python ate it before the browser saw it: "
            + ", ".join(sorted(set(offenders)))
        )


class TestTheBootMessageDoesNotAssertAStalePlan:
    """A stale operational message is a defect in its own right.

    CLAUDE.md names this exact belief — "stock-agent is paid, so its cron jobs
    are irrelevant to the budget" — as what made the Sept 5 outage unreadable.
    It had a hidden `while paid` on it, and the downgrade invalidated both
    halves at once.
    """

    def _disabled_branch(self):
        """Only the BRANCH BODY — never the docstring above it.

        The self-flagging trap, caught while writing this test: the function's
        docstring legitimately explains that "a service that never idles costs
        ~744 h/month", and a scan that swallowed it flagged the very prose
        describing the cost. Anchor on the code, not on the explanation
        surrounding it.
        """
        src = SRC[SRC.index("def _keep_alive_loop"):]
        body = src[src.index("if not _KEEPALIVE_ENABLED:"):]
        return body[: body.index("url = os.environ.get")]

    def test_it_does_not_claim_the_service_never_idles(self):
        branch = self._disabled_branch().lower()
        assert "starter" not in branch, (
            "the boot log still claims the Starter plan; the service has been "
            "on Render FREE since 2026-09-02 and does sleep"
        )
        assert "never idles" not in branch

    def test_it_does_not_instruct_the_reader_to_re_enable_the_keepalive(self):
        """CLAUDE.md: 'DO NOT restore the keep-alive' — a 24/7 warm instance is
        ~744 h/mo of a 750 h account pool shared with three other apps. The old
        message told a future reader to do exactly that, on every boot."""
        branch = self._disabled_branch()
        assert "KEEPALIVE_ENABLED=1" not in branch, (
            "the boot log still recommends setting KEEPALIVE_ENABLED=1"
        )

    def test_it_still_says_why_it_is_off(self):
        """Raising a guard must not silence the feature: a bare 'DISABLED' with
        no reason invites the next reader to flip it back."""
        branch = self._disabled_branch()
        assert "750" in branch and "sleep" in branch.lower()

    def test_the_loop_still_returns_without_pinging(self, monkeypatch):
        """Non-regression on the property that actually matters."""
        calls = []
        monkeypatch.setattr(webhook.requests, "get",
                            lambda *a, **k: calls.append(a) or None)
        monkeypatch.setattr(webhook, "_KEEPALIVE_ENABLED", False)
        webhook._keep_alive_loop()
        assert calls == [], "the disabled loop issued a request"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestTheAdminPollPausesWhenHidden:
    """Driven under node against the SERVED page, not scanned for in source.

    Extracting from the response matters: the JS lives inside a Python string,
    so what the browser receives is not what the file contains.
    """

    def _snippet(self, client):
        html = _admin_html(client)
        start = html.index("var _lastLoad=")
        return html[start: html.index("</script>", start)]

    def _run(self, client, script):
        harness = """
var __loads = 0;
var __intervals = [];
var __handlers = {};
var document = {
  hidden: false,
  addEventListener: function (n, f) { __handlers[n] = f; },
  getElementById: function () { return { textContent: '' }; }
};
function load() { __loads++; }
function setInterval(f, ms) { __intervals.push({ fn: f, ms: ms }); }
"""
        path = pathlib.Path(os.environ.get("TMPDIR", "/tmp"), "sp_poll_probe.js")
        path.write_text(harness + self._snippet(client) + "\n" + script)
        out = subprocess.run(["node", str(path)], capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout.strip().splitlines()[-1])

    def test_a_hidden_tab_does_not_poll(self, client):
        """The whole point. One forgotten tab held the free instance awake
        around the clock and hid every sleep bug while doing it."""
        r = self._run(client, """
document.hidden = true;
__intervals.forEach(function (i) { i.fn(); i.fn(); i.fn(); });
console.log(JSON.stringify({ loads: __loads }));
""")
        assert r["loads"] == 1, (
            f"a hidden tab fetched {r['loads'] - 1} time(s); it must fetch none"
        )

    def test_a_visible_tab_still_polls(self, client):
        """Pausing must not become 'never refreshes' — the dashboard is the
        owner's live view."""
        r = self._run(client, """
__intervals.forEach(function (i) { i.fn(); i.fn(); });
console.log(JSON.stringify({ loads: __loads }));
""")
        assert r["loads"] == 3, f"visible tab loaded {r['loads']} times, expected 3"

    def test_the_interval_is_still_sixty_seconds(self, client):
        r = self._run(client, "console.log(JSON.stringify({ms: __intervals[0].ms}));")
        assert r["ms"] == 60000

    def test_returning_to_a_stale_tab_refreshes_at_once(self, client):
        """A paused poll must not turn into stale data on return."""
        r = self._run(client, """
document.hidden = true;
__intervals.forEach(function (i) { i.fn(); });
_lastLoad = Date.now() - 10 * 60 * 1000;
document.hidden = false;
__handlers['visibilitychange']();
console.log(JSON.stringify({ loads: __loads }));
""")
        assert r["loads"] == 2, (
            "returning to a tab whose data is 10 minutes old did not refresh"
        )

    def test_flicking_between_tabs_does_not_refetch(self, client):
        """visibilitychange fires on every switch; without the age check that
        is one fetch per alt-tab, which is worse than the 60s poll."""
        r = self._run(client, """
for (var i = 0; i < 8; i++) {
  document.hidden = true;  __handlers['visibilitychange']();
  document.hidden = false; __handlers['visibilitychange']();
}
console.log(JSON.stringify({ loads: __loads }));
""")
        assert r["loads"] == 1, (
            f"8 quick tab switches caused {r['loads'] - 1} extra fetches"
        )

    def test_becoming_hidden_never_fetches(self, client):
        r = self._run(client, """
_lastLoad = Date.now() - 10 * 60 * 1000;
document.hidden = true;
__handlers['visibilitychange']();
console.log(JSON.stringify({ loads: __loads }));
""")
        assert r["loads"] == 1, "going hidden triggered a fetch"


class TestThePollWiringReachesTheServedPage:
    """Runs without node, so the class is never left entirely unguarded."""

    def test_the_served_page_gates_the_poll_on_visibility(self, client):
        html = _admin_html(client)
        assert "document.hidden" in html, (
            "the served /admin page has no visibility gate on its poll"
        )
        assert not re.search(r"setInterval\(\s*load\s*,", html), (
            "setInterval still calls load directly, so the poll cannot pause"
        )
