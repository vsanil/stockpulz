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

    def _code(self):
        """The JS with `//` comment LINES stripped.

        ⚠️ Any scan whose subject is also discussed in prose must strip
        comments first. Banning `confirm(` tripped on the comment explaining
        why confirm was removed — the ELEVENTH time that trap has appeared
        here, and the second time inside the very file warning about it.
        Only whole comment lines are dropped, so a `//` inside a string
        literal (a URL) is untouched.
        """
        return "\n".join(l for l in self._js().splitlines()
                          if not l.lstrip().startswith("//"))

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

    # NOTE: `test_it_links_out_for_review_rather_than_merging_in_place` was
    # DELETED, not relaxed, on 2026-08-26. The owner asked to manage everything
    # from the dashboard; a test asserting the opposite contradicts current
    # intent, and a test that contradicts intent is worse than no test. What
    # mattered in it — that approving must not be a rubber stamp — is now
    # enforced by test_the_diff_itself_is_rendered_not_just_filenames below.

    def test_the_diff_itself_is_rendered_not_just_filenames(self):
        """The safety property that survived the redesign. A Merge button beside
        a one-line summary invites approving without reading — exactly what the
        findings card did when it showed raw function names. Showing the PATCH
        is what makes this a review surface rather than a rubber stamp."""
        js = self._js()
        assert "diffHtml(f.patch)" in js, "the card must render the actual patch"
        assert "Read the diff" in js

    def test_both_actions_are_offered_and_merge_says_it_deploys(self):
        js = self._js()
        assert "selfhealAct(" in js
        assert "Merge &amp; deploy" in js, "the button must say it deploys, not just 'merge'"
        assert "Discard" in js
        assert "Render deploys it to real users" in js

    def test_a_destructive_action_asks_first(self):
        """The INTENT survives; the mechanism changed 2026-09-13. A blocking
        confirm() was the gate and it is a SILENT failure path — a browser that
        suppresses dialogs makes it return false, so the handler returned with
        no fetch, no error and no trace. That is what made the owner's Merge
        click do nothing. Arming in-page cannot be suppressed."""
        js = self._code()
        assert "dataset.armed" in js, "the action must arm before it fires"
        assert "confirm(" not in js, \
            "a blocking confirm() can be suppressed — that is the silent no-op"

    def test_a_suppressed_dialog_can_no_longer_swallow_the_result(self):
        """alert() is suppressible too, so a GitHub 403 would vanish the same
        way the action did. Results render into the page instead."""
        js = self._code()
        assert "_shMsg(" in js
        assert "alert(" not in js, "a suppressed alert would hide a 403"

    def test_a_github_failure_re_enables_the_button(self):
        """A disabled button with no message is indistinguishable from a
        successful merge — the failure must be recoverable and visible."""
        js = self._js()
        i = js.index("if(!r.ok)")
        seg = js[i:i + 260]
        assert "disabled = false" in seg and "true" in seg

    def test_a_github_refusal_is_shown_not_swallowed(self):
        """GITHUB_TOKEN dispatches workflows, which IMPLIES merge scope but does
        not prove it — the Supabase RLS key passed every read probe while every
        write was denied. A 403 that renders as success leaves the branch
        unmerged and the owner believing otherwise."""
        js = self._js()
        assert "if(!r.ok)" in js and "HTTP " in js

    def test_the_patch_is_capped_and_says_when_it_truncated(self):
        """A silently partial diff beside a Merge button is worse than none."""
        src = WEB.read_text()
        i = src.index("def _build_selfheal_prs")
        body = src[i:src.index("def _build_audit_findings", i)]
        assert '"clipped"' in body
        assert "diff truncated here" in self._js()

    def test_newlines_use_fromCharCode_not_a_backslash_escape(self):
        """This JS lives in a Python triple-quoted string. A literal backslash-n
        is consumed by PYTHON and emits a real newline into a JS string literal
        — a syntax error that kills the entire script. Same family as the
        lone-surrogate emoji that 500'd the whole page."""
        js = self._js()
        assert "String.fromCharCode(10)" in js
        assert "split('\\n')" not in js

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


class TestTheMergeEndpoint:
    """Managing fixes from /admin means the dashboard can now MERGE TO MAIN,
    which Render auto-deploys. Everything here guards that button."""

    def _admin(self, client):
        with client.session_transaction() as s:
            s["admin"] = True
        return client

    def test_it_refuses_a_branch_that_is_not_a_self_heal_branch(self, client, monkeypatch):
        """🔴 The load-bearing guard. `<path:branch>` accepts slashes, so it also
        accepts `main` — without the prefix check this endpoint would merge or
        DELETE any ref in the repo. A UI that only offers self-heal branches is
        not a permission check."""
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        for ref in ("main", "refs/heads/main"):
            r = self._admin(client).post(f"/admin/selfheal/{ref}/discard")
            assert r.status_code == 403, f"{ref} was not refused (got {r.status_code})"

    def test_an_unknown_action_is_rejected(self, client, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        r = self._admin(client).post("/admin/selfheal/auto/self-heal-x/nuke")
        assert r.status_code == 400

    def test_the_route_does_not_swallow_the_action_segment(self, client, monkeypatch):
        """<path:> is greedy. If it captured 'self-heal-x/merge' as the branch,
        the action would be lost and the endpoint would misbehave silently."""
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        seen = {}

        class _R:
            status_code = 201
            text = ""
            def json(self): return {"sha": "abc123"}

        def _post(url, **kw):
            seen["url"] = url
            seen["json"] = kw.get("json") or {}
            return _R()

        monkeypatch.setattr("webhook.requests.post", _post)
        r = self._admin(client).post("/admin/selfheal/auto/self-heal-x/merge")
        assert r.status_code == 200, r.get_data(as_text=True)
        assert seen["json"].get("head") == "auto/self-heal-x", seen
        assert seen["json"].get("base") == "main"

    def test_a_github_403_surfaces_as_an_error_not_a_success(self, client, monkeypatch):
        """The Supabase RLS lesson: a write denial that reads as success is the
        worst available failure mode."""
        monkeypatch.setenv("GITHUB_TOKEN", "t")

        class _R:
            status_code = 403
            text = "Resource not accessible by integration"
            def json(self): return {}

        monkeypatch.setattr("webhook.requests.post", lambda *a, **k: _R())
        r = self._admin(client).post("/admin/selfheal/auto/self-heal-x/merge")
        assert r.status_code == 502
        body = r.get_json() or {}
        assert "403" in str(body.get("error", ""))
        assert "not accessible" in str(body.get("detail", ""))

    def test_discard_reports_the_sha_so_the_branch_is_restorable(self, client, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t")

        class _Get:
            status_code = 200
            def json(self): return {"object": {"sha": "deadbeef"}}

        class _Del:
            status_code = 204
            text = ""
            def json(self): return {}

        monkeypatch.setattr("webhook.requests.get", lambda *a, **k: _Get())
        monkeypatch.setattr("webhook.requests.delete", lambda *a, **k: _Del())
        r = self._admin(client).post("/admin/selfheal/auto/self-heal-x/discard")
        assert r.status_code == 200
        body = r.get_json() or {}
        assert body.get("sha") == "deadbeef"
        assert "deadbeef" in body.get("note", ""), "the note must carry the restore command"

    def test_it_requires_an_admin_session(self, client, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "t")
        r = client.post("/admin/selfheal/auto/self-heal-x/merge")   # no session
        assert r.status_code != 200


class TestTheDashboardStillRenders:
    """🔴 The /admin page 500'd for ~40 minutes once while every test was green,
    because they all scanned the JS SOURCE and nothing ever FETCHED the page."""

    def _get(self, client):
        with client.session_transaction() as s:
            s["admin"] = True
        return client.get("/admin")

    def test_it_returns_200_and_encodes(self, client):
        r = self._get(client)
        assert r.status_code == 200
        r.get_data().decode("utf-8")

    def test_the_selfheal_card_is_in_the_served_page(self, client):
        assert b"Self-heal fixes awaiting review" in self._get(client).get_data()

    def test_the_merge_button_reaches_the_browser(self, client):
        assert b"selfhealAct(" in self._get(client).get_data()


class TestMonitorNamesActuallyMatch:
    """🔴 self_heal triggers on workflow_run by NAME. Rename a monitor and
    self-heal silently stops firing — forever, with no error anywhere. Nothing
    asserted these matched until 2026-08-26."""

    def test_every_trigger_name_is_a_real_workflow_name(self):
        import re
        wf = WF.read_text()
        m = re.search(r"workflows:\s*\[([^\]]*)\]", wf)
        assert m, "self_heal no longer declares a workflow_run trigger list"
        wanted = [s.strip().strip('"').strip("'") for s in m.group(1).split(",")]
        wanted = [w for w in wanted if w]
        assert len(wanted) == 4, f"expected 4 monitors, got {wanted}"

        actual = {}
        for p in pathlib.Path(".github/workflows").glob("*.yml"):
            nm = re.search(r"(?m)^name:\s*(.+?)\s*$", p.read_text())
            if nm:
                actual[nm.group(1).strip().strip('"').strip("'")] = p.name
        missing = [w for w in wanted if w not in actual]
        assert not missing, (
            f"self_heal triggers on {missing}, which match NO workflow name. "
            f"It will never fire for them. Known names: {sorted(actual)}")

    def test_the_four_monitors_are_still_the_intended_ones(self):
        wf = WF.read_text()
        for name in ("Daily Canary", "Synthetic User", "Full Sweep", "Pick Evaluation"):
            assert name in wf, f"{name} is no longer a self-heal trigger"


class TestTheDmSaysWhetherAPrExists:
    """`gh pr create` fails silently when the repo setting is off, and the
    fallback is a compare LINK — no review thread, no checks, nothing in the PR
    list. Reporting both as 'proposed' overstates it, the same way /health
    asserting 200 hid stale code."""

    def test_the_kind_is_recorded(self):
        wf = WF.read_text()
        assert "prkind=" in wf
        assert '*"/pull/"*' in wf, "must detect a real PR by its /pull/ URL"

    def test_the_message_distinguishes_them(self):
        wf = WF.read_text()
        assert "Pull request opened" in wf
        assert "NO pull request" in wf

    def test_it_points_at_the_dashboard_too(self):
        assert "/admin" in WF.read_text()


class TestFindingsAwaitingReminder:
    """Same structural rule as TestTheReminderCannotFeedItself: a canary
    FAILURE triggers self_heal, so this reminder must never fail."""

    def _fn(self):
        src = CAN.read_text()
        i = src.index("def check_findings_awaiting")
        return src[i:src.index("def check_storage_surfaces", i)]

    def test_it_never_reports_a_failure(self):
        import re
        for m in re.finditer(r'_check\("findings\.awaiting",\s*(\w+)', self._fn()):
            assert m.group(1) == "True", \
                "a failing reminder would trigger self_heal and create work forever"

    def test_it_is_silent_when_nothing_is_waiting(self):
        assert "no decisions waiting" in self._fn()

    def test_an_unreachable_store_is_NOT_VERIFIED(self):
        assert "NOT VERIFIED" in self._fn()

    def test_it_reads_the_clock_the_writer_uses(self):
        """proposed_on is stamped with et_today(); a UTC date here would make
        the age wrong for 4-5 hours every night — the class this repo has hit
        five times."""
        body = self._fn()
        assert "et_today()" in body
        assert "date.today()" not in body

    def test_it_is_registered(self):
        assert "check_findings_awaiting," in CAN.read_text()

    def test_it_reports_what_is_waiting(self, monkeypatch):
        import importlib.util, os
        spec = importlib.util.spec_from_file_location(
            "can_x", os.path.join(os.getcwd(), "scripts", "canary.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        seen = []
        monkeypatch.setattr(m, "_check", lambda n, ok, d="", **k: seen.append((n, ok, d)))
        # Patch `storage`, NOT canary — the import is function-local, the scope
        # trap that once let a "patched" test write to the live gist.
        self._patch_store(monkeypatch, {"i/1": {"status": "awaiting_approval",
                                                "proposed_on": "2026-08-20"},
                                        "i/2": {"status": "approved"}})
        m.check_findings_awaiting()
        assert seen and seen[0][1] is True, "must never fail"
        assert "1 change(s)" in seen[0][2]
        assert "i/1" in seen[0][2]

    def _patch_store(self, monkeypatch, payload):
        import storage

        class _B:
            def read_strict(self, _f):
                if isinstance(payload, Exception):
                    raise payload
                return payload

        monkeypatch.setattr(storage, "get_storage_backend", lambda: _B())

    def test_an_unreadable_store_does_NOT_report_nothing_waiting(self, monkeypatch):
        """🔴 Found by RUNNING the check, not by its tests. The first version
        called get_finding_dispositions(), which ends in `or {}` — so a store it
        could not read returned empty and the check reported a clean
        "no decisions waiting". A pending approval would have been invisible
        exactly when storage was broken. Same false pass as `prices.cg_cache`,
        where two failures agreeing read as success."""
        import importlib.util, os
        spec = importlib.util.spec_from_file_location(
            "can_y", os.path.join(os.getcwd(), "scripts", "canary.py"))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        seen = []
        monkeypatch.setattr(m, "_check", lambda n, ok, d="", **k: seen.append((n, ok, d)))
        self._patch_store(monkeypatch, OSError("gist unreachable"))
        m.check_findings_awaiting()
        assert seen and seen[0][1] is True, "must never fail, even unreadable"
        assert "NOT VERIFIED" in seen[0][2]
        assert "no decisions waiting" not in seen[0][2], \
            "an unreadable store must never read as 'nothing to approve'"

    def test_it_does_not_use_the_swallowing_reader(self):
        """⚠ Comments and docstrings are STRIPPED first. The fix's own comment
        names `get_finding_dispositions` while explaining its removal, so a
        naive scan flags itself — that trap has now appeared eight times in
        this repo. Scan code, never prose."""
        import io, tokenize
        body = self._fn()
        code = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(body).readline):
                if tok.type not in (tokenize.COMMENT, tokenize.STRING):
                    code.append(tok.string)
        except (tokenize.TokenError, IndentationError):
            # A sliced function body may not tokenise cleanly; fall back to
            # dropping comment lines, which is enough for this assertion.
            code = [ln.split("#", 1)[0] for ln in body.splitlines()]
        src = " ".join(code)
        assert "read_strict" in src
        assert "get_finding_dispositions" not in src, \
            "that helper ends in `or {}` and hides an unreadable store"


class TestDuplicateFixesAreNotReProposed:
    """🔴 Branch names are `auto/self-heal-$(date +%s)` — unique by construction
    — and nothing checked whether an unmerged branch already carried the same
    fix. So a PERSISTENT failure produced one branch PER RUN: the deliberate
    canary check made 6 in 5 days, and `synthetic_user` runs 8x a weekday, so a
    stuck failure there compounds far faster.
    """

    def _ship(self):
        wf = WF.read_text()
        i = wf.index("- name: Ship")
        return wf[i:wf.index("- name: Post-deploy health check", i)]

    def test_it_compares_the_FULL_diff_not_just_filenames(self):
        """Measured on the 6 real branches: four had a byte-identical diff, and
        the two that differed genuinely did MORE — one added a CLAUDE.md note,
        one fixed a second real bug. Matching on filenames would have swallowed
        both. Only an exact repeat may be suppressed."""
        b = self._ship()
        assert "git hash-object --stdin" in b
        assert b.count("git hash-object --stdin") >= 2, "must hash ours AND theirs"

    def test_both_sides_are_measured_from_their_merge_base(self):
        """main moves under old branches; a two-dot diff against current main
        would include unrelated drift and never match."""
        b = self._ship()
        assert b.count("git merge-base origin/main") >= 2

    def test_the_check_runs_BEFORE_the_branch_is_pushed(self):
        """A duplicate must create no branch at all — detecting it after the
        push would leave exactly the litter this removes."""
        b = self._ship()
        assert "outcome=duplicate" in b and 'git push -u origin "$BR"' in b
        assert b.index("outcome=duplicate") < b.index('git push -u origin "$BR"')

    def test_it_does_not_match_the_branch_against_itself(self):
        b = self._ship()
        assert '[ "$R" = "$BR" ] && continue' in b

    def test_a_duplicate_sends_NO_telegram_message(self):
        """Otherwise dedup just moves the spam from branches to Telegram."""
        wf = WF.read_text()
        i = wf.index("- name: Notify admin — result")
        notify = wf[i:]
        assert "duplicate)" in notify
        assert 'MSG="" ;;' in notify

    def test_an_empty_message_is_never_curled(self):
        wf = WF.read_text()
        i = wf.index("- name: Notify admin — result")
        notify = wf[i:]
        assert 'if [ -z "$MSG" ]; then' in notify
        assert notify.index('if [ -z "$MSG" ]; then') < notify.index("sendMessage")

    def test_the_workflow_is_still_valid_yaml(self):
        import yaml
        assert yaml.safe_load(WF.read_text())


class TestDocsOnlyCommitsDoNotRedeploy:
    """The daily engine-analysis commit changes ONLY analysis/ENGINE_FINDINGS.md —
    regenerated output the web service never reads. Without `[skip render]`,
    Render rebuilt and RESTARTED the live service every night at ~00:00 UTC for
    a markdown change. Found 2026-09-13 while chasing "why is /admin cold"."""

    def test_the_analysis_commit_skips_render(self):
        wf = pathlib.Path(".github/workflows/analyze_engine.yml").read_text()
        line = next(l for l in wf.splitlines() if "git commit" in l and "-m" in l)
        assert "[skip render]" in line, \
            "a docs-only commit must not restart the production web service"

    def test_it_does_NOT_skip_ci(self):
        """`[skip ci]` would also silence the test workflow on that push;
        only Render's redeploy is the waste."""
        wf = pathlib.Path(".github/workflows/analyze_engine.yml").read_text()
        assert "[skip ci]" not in wf


class TestAdminDataPrefetchesConcurrently:
    """/admin/data reads ~10 distinct storage files. They used to run SERIALLY,
    ~100 ms each from Render, on every open after the 20 s cache expired —
    that was the whole "not warmed up" feeling (the server was warm). Now they
    are prefetched in parallel so the builders hit a warm cache."""

    def _admin(self, client):
        with client.session_transaction() as s:
            s["admin"] = True
        return client

    def test_every_file_admin_data_reads_is_in_the_prefetch_set(self, client, monkeypatch):
        """The guard that matters: a new builder that reads a new file must
        fail here, or it silently reintroduces one serial round trip."""
        import config_manager as cm, webhook
        seen = set()
        real = cm._load_gist_file          # conftest's in-memory store
        monkeypatch.setattr(cm, "_load_gist_file",
                            lambda fn, *a, **k: (seen.add(fn), real(fn, *a, **k))[1])
        assert self._admin(client).get("/admin/data").status_code == 200
        missing = seen - set(webhook._ADMIN_PREFETCH)
        assert not missing, f"/admin/data reads files the prefetch does not cover: {missing}"

    def test_it_runs_after_the_feedback_write_and_before_the_first_builder(self):
        """mark_feedback_read() WRITES feedback.json and invalidates its cache
        entry. Prefetching before it would cache the pre-write copy."""
        src = WEB.read_text()
        body = src[src.index("def admin_data():"):src.index("@app.route", src.index("def admin_data():"))]
        i_write = body.index("mark_feedback_read()")
        i_pre   = body.index("_prefetch_admin_reads()")
        i_build = body.index("_build_audit_findings()")
        assert i_write < i_pre < i_build

    def test_it_is_actually_concurrent(self):
        """A prefetch that loops serially saves nothing — the serial I/O IS the bug."""
        import ast
        tree = ast.parse(WEB.read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_prefetch_admin_reads")
        names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)} | \
                {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        assert "ThreadPoolExecutor" in names

    def test_a_failing_file_cannot_take_the_dashboard_down(self, client, monkeypatch):
        import config_manager as cm
        real = cm._load_gist_file
        def _boom(fn, *a, **k):
            if fn == "traffic_hours.json":
                raise RuntimeError("supabase down")
            return real(fn, *a, **k)
        monkeypatch.setattr(cm, "_load_gist_file", _boom)
        assert self._admin(client).get("/admin/data").status_code == 200
