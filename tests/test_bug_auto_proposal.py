"""A technical BUG finding now proposes its own fix, and approving it builds.

This closes the half of the loop that was missing. A finding could be detected,
surfaced and ruled on, and NOTHING ever turned one into a change: the only path
to a pull request was self-heal, which fires on a MONITOR FAILURE, and a
finding is not one. So a defect could sit on the dashboard indefinitely.

🔴 BUGS ONLY (owner's split, 2026-09-20). A technical bug restores intended
behaviour and changes no strategy, so one instance is enough. A decision-engine
change alters what real users are told to buy and needs the n>=30 evidence the
tournament produces — auto-proposing one would be tuning on noise, the loop
this program exists to end.
"""
import ast
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ae():
    spec = importlib.util.spec_from_file_location("ae_prop", ROOT / "scripts" / "analyze_engine.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _f(ae, fid="integrity/abc", tier="ACT", category="bug", status="open"):
    x = ae.Finding(fid, tier, "AMBA target is below its entry",
                   "2 positions, target 78.54 vs entry 82.67", category=category,
                   fix="Drop it in ai_analyzer._validate_and_clean_picks",
                   where="ai_analyzer.py", plain="A pick that could only ever lose.")
    x.status = status
    return x


class TestOnlyBugsAreProposed:
    def test_an_open_bug_gets_a_concrete_proposal(self):
        ae = _ae()
        state = {}
        assert ae._propose_bug_fixes([_f(ae)], state, "2026-09-20") == 1
        rec = state["integrity/abc"]
        assert rec["proposed_change"] == "Drop it in ai_analyzer._validate_and_clean_picks"
        assert rec["proposed_files"] == "ai_analyzer.py"
        assert rec["proposed_summary"] == "A pick that could only ever lose."
        assert rec["proposed_by"] == "auto"

    def test_an_ENGINE_finding_is_never_proposed(self):
        """It changes what real users are told to buy, and needs outcome
        evidence over time. The tournament supplies that; Phase 3 uses it."""
        ae = _ae()
        state = {}
        assert ae._propose_bug_fixes([_f(ae, category="engine")], state, "2026-09-20") == 0
        assert state == {}

    def test_an_unclassified_finding_is_never_proposed(self):
        ae = _ae()
        state = {}
        f = _f(ae); f.category = None
        assert ae._propose_bug_fixes([f], state, "2026-09-20") == 0

    def test_only_ACT_findings_are_proposed(self):
        """MEASURE and HOLD are below the bar to act on at all."""
        ae = _ae()
        for tier in ("MEASURE", "HOLD"):
            state = {}
            assert ae._propose_bug_fixes([_f(ae, tier=tier)], state, "2026-09-20") == 0

    def test_a_decided_finding_is_never_re_proposed(self):
        ae = _ae()
        for status in ("acknowledged", "wont_fix", "approved", "fixed"):
            state = {}
            assert ae._propose_bug_fixes([_f(ae, status=status)], state, "2026-09-20") == 0

    def test_it_is_idempotent(self):
        """A second run the same day must not overwrite a proposal the owner
        may already be looking at."""
        ae = _ae()
        state = {}
        ae._propose_bug_fixes([_f(ae)], state, "2026-09-20")
        state["integrity/abc"]["proposed_change"] = "edited by hand"
        assert ae._propose_bug_fixes([_f(ae)], state, "2026-09-21") == 0
        assert state["integrity/abc"]["proposed_change"] == "edited by hand"

    def test_a_metric_is_never_proposed(self):
        ae = _ae()
        m = ae.Finding("metric:x", "ACT", "t", "e", "fix", kind="metric")
        m.status = "open"
        state = {}
        assert ae._propose_bug_fixes([m], state, "2026-09-20") == 0


class TestTheUnapprovedInvariantSurvives:
    """🚨 The check that makes the approval workflow more than an honour
    system: a finding that DISAPPEARS while awaiting consent was implemented
    without it. If an automatic proposal set `awaiting_approval`, every bug
    finding whose condition cleared on its own would raise a false accusation
    and the invariant would start crying wolf."""

    def test_an_auto_proposal_does_NOT_move_the_status(self):
        ae = _ae()
        state = {}
        ae._propose_bug_fixes([_f(ae)], state, "2026-09-20")
        assert state["integrity/abc"].get("status") != "awaiting_approval"

    def test_an_auto_proposed_finding_that_vanishes_resolves_normally(self):
        ae = _ae()
        state = {}
        ae._propose_bug_fixes([_f(ae)], state, "2026-09-20")
        state["integrity/abc"]["status"] = "open"
        out = ae._apply_state([], state, "2026-09-21")      # condition gone
        assert out["integrity/abc"]["status"] == "resolved"

    def test_a_HUMAN_proposal_that_vanishes_still_raises_the_alarm(self):
        """The other direction — a guard that never fires both ways is not a
        guard."""
        ae = _ae()
        state = {"integrity/abc": {"status": "awaiting_approval",
                                   "proposed_change": "something I planned"}}
        out = ae._apply_state([], state, "2026-09-21")
        assert out["integrity/abc"]["status"] == "resolved_UNAPPROVED"


class TestItIsOptIn:
    def test_build_proposes_nothing_unless_asked(self):
        """Running the script by hand must not write proposals — the same
        reason --notify is opt-in, and a local run resolves to the ROLLBACK
        store, not production."""
        src = (ROOT / "scripts" / "analyze_engine.py").read_text()
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "build")
        args = {a.arg for a in fn.args.args}
        assert "propose" in args, "build() must take the flag"
        # every keyword default is False -> nothing proposes unless asked
        assert all(isinstance(d, ast.Constant) and d.value is False for d in fn.args.defaults)
        assert "if propose:" in ast.unparse(fn), "proposing must be gated on the flag"

    def test_only_CI_passes_the_flag(self):
        wf = (ROOT / ".github/workflows/analyze_engine.yml").read_text()
        assert "--propose" in wf and "--notify" in wf


class TestApprovalAttachesToAConcreteChange:
    def _admin(self, client):
        with client.session_transaction() as s:
            s["admin"] = True
        return client

    def test_approving_a_bare_finding_is_still_refused(self, client, monkeypatch):
        """Approval must never attach to a title."""
        import webhook
        monkeypatch.setattr(webhook, "_dispatch_finding_fix", lambda *a: {"ok": True})
        import config_manager as cm
        monkeypatch.setattr(cm, "get_finding_dispositions", lambda: {"x/1": {"status": "open"}})
        r = self._admin(client).post("/admin/findings/x/1", json={"status": "approved"})
        assert r.status_code == 409 and "nothing proposed" in r.get_json()["error"]

    def test_an_AUTO_proposed_finding_can_be_approved(self, client, monkeypatch):
        """The gate used to test the STATUS STRING, which would have refused a
        finding carrying a perfectly concrete plan."""
        import webhook, config_manager as cm
        sent = []
        monkeypatch.setattr(webhook, "_dispatch_finding_fix",
                            lambda fid, rec: sent.append((fid, rec)) or {"ok": True})
        monkeypatch.setattr(cm, "get_finding_dispositions",
                            lambda: {"x/1": {"status": "open",
                                             "proposed_change": "do the thing",
                                             "proposed_files": "a.py",
                                             "proposed_summary": "plain words"}})
        r = self._admin(client).post("/admin/findings/x/1", json={"status": "approved"})
        assert r.status_code == 200 and r.get_json()["build"] == {"ok": True}
        assert sent and sent[0][0] == "x/1"

    def test_the_decision_is_saved_even_when_the_build_cannot_start(self, client, monkeypatch):
        """A failed dispatch must never lose the owner's ruling, and must never
        look like a fix on its way."""
        import webhook, config_manager as cm
        monkeypatch.setattr(webhook, "_dispatch_finding_fix",
                            lambda fid, rec: {"ok": False, "error": "GitHub refused"})
        monkeypatch.setattr(cm, "get_finding_dispositions",
                            lambda: {"x/1": {"status": "open", "proposed_change": "do it"}})
        d = self._admin(client).post("/admin/findings/x/1", json={"status": "approved"}).get_json()
        assert d["ok"] is True and d["status"] == "approved"
        assert d["build"]["ok"] is False and "refused" in d["build"]["error"]

    def test_declining_never_starts_a_build(self, client, monkeypatch):
        import webhook, config_manager as cm
        sent = []
        monkeypatch.setattr(webhook, "_dispatch_finding_fix",
                            lambda fid, rec: sent.append(fid) or {"ok": True})
        monkeypatch.setattr(cm, "get_finding_dispositions",
                            lambda: {"x/1": {"status": "open", "proposed_change": "do it"}})
        for status in ("wont_fix", "acknowledged", "open"):
            self._admin(client).post("/admin/findings/x/1", json={"status": status})
        assert sent == [], "only approval buys a model call"

    def test_a_missing_token_is_reported_not_swallowed(self, monkeypatch):
        import webhook
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        out = webhook._dispatch_finding_fix("x/1", {"proposed_change": "c"})
        assert out["ok"] is False and "no build was started" in out["error"]


class TestTheCardOffersTheRightVerbs:
    def _admin(self, client):
        with client.session_transaction() as s:
            s["admin"] = True
        return client

    def test_an_open_finding_with_a_plan_offers_approve(self, client):
        html = self._admin(client).get("/admin").get_data(as_text=True)
        assert "hasPlan" in html and "Approve &amp; build" in html

    def test_an_open_finding_without_a_plan_still_only_acknowledges(self, client):
        html = self._admin(client).get("/admin").get_data(as_text=True)
        i = html.index("var hasPlan")
        assert "Acknowledge" in html[i:i + 1600]

    def test_an_automatic_proposal_says_no_human_reviewed_it(self, client):
        html = self._admin(client).get("/admin").get_data(as_text=True)
        assert "proposed_by === 'auto'" in html and "no human reviewed it" in html

    def test_results_render_in_page_never_through_a_dialog(self, client):
        """A browser can suppress dialogs and Telegram's in-app browser blocks
        them outright, so a suppressed alert is indistinguishable from a click
        that did nothing — the defect that made the merge button look broken."""
        html = self._admin(client).get("/admin").get_data(as_text=True)
        # 🔴 Slice to the END of the function, not a byte window. The first
        # version used html[i:i+1800], which ran past setFinding into the NEXT
        # handler and failed on ITS alert() — the fixed-offset anchoring trap.
        # It did surface a real defect (setAudit had the same flaw), but a
        # guard must fail for the reason it claims.
        i = html.index("async function setFinding")
        body = html[i:html.index("\nasync function", i + 10)]
        assert "alert(" not in body and "_findMsg(" in body

    def test_no_admin_decision_handler_uses_a_dialog(self, client):
        """The same class, swept. Found because the guard above tripped on it."""
        html = self._admin(client).get("/admin").get_data(as_text=True)
        for fn in ("setFinding", "setAudit"):
            i = html.index("async function " + fn)
            body = html[i:html.index("\nasync function", i + 10)]
            assert "alert(" not in body, f"{fn} still fails through a dialog"

    def test_a_failed_build_is_reported_distinctly_from_a_saved_decision(self, client):
        html = self._admin(client).get("/admin").get_data(as_text=True)
        i = html.index("async function setFinding")
        assert "did NOT start" in html[i:i + 1800]


class TestSelfHealAcceptsAFinding:
    def _wf(self):
        import yaml
        return yaml.safe_load((ROOT / ".github/workflows/self_heal.yml").read_text())

    def _py(self):
        import re
        wf = self._wf()
        step = next(s for s in wf["jobs"]["self-heal"]["steps"]
                    if s.get("name", "").startswith("Claude Code"))
        m = re.search(r"python3 - <<'PY'\n(.*?)\nPY\n", step["run"], re.S)
        assert m, "the prompt builder is gone"
        return m.group(1)

    def test_the_workflow_takes_the_approved_change(self):
        inputs = self._wf()[True]["workflow_dispatch"]["inputs"]
        for k in ("finding_id", "finding_summary", "finding_change", "finding_files"):
            assert k in inputs

    def test_the_prompt_builder_compiles(self):
        compile(self._py(), "<heredoc>", "exec")

    def test_the_finding_prompt_treats_the_proposal_as_a_hypothesis(self):
        """The proposal is the finding's OWN fix text and no human verified the
        ENGINEERING. One of them recommended widening the published entry
        window, a fix this project had investigated and REJECTED."""
        py = self._py()
        assert "HYPOTHESIS, NOT AN INSTRUCTION" in py
        assert "make NO code change" in py and "That is a SUCCESS" in py

    def test_the_finding_prompt_still_demands_a_regression_test_and_both_gates(self):
        py = self._py()
        assert "regression test" in py
        assert "pytest tests/ -q" in py and "check_js.py" in py

    def test_it_never_pushes_or_merges_itself(self):
        py = self._py()
        assert py.count("Do NOT push") >= 2, "both prompts must forbid pushing"

    def test_the_change_is_never_interpolated_into_a_shell_word(self):
        """The fix text carries backticks and unicode arrows, and this repo has
        already shipped a commit whose backticks the shell EXECUTED."""
        import re
        step = next(s for s in self._wf()["jobs"]["self-heal"]["steps"]
                    if s.get("name", "").startswith("Claude Code"))
        run = step["run"]
        for var in ("finding_change", "finding_summary", "finding_files"):
            assert "inputs." + var not in run, f"{var} reaches a shell word"
        assert set(step["env"]) >= {"FINDING_ID", "FINDING_SUMMARY",
                                    "FINDING_CHANGE", "FINDING_FILES"}

    @pytest.mark.parametrize("finding", [True, False])
    def test_both_prompt_branches_render(self, finding, tmp_path):
        import os
        import subprocess
        py = self._py().replace("/tmp/prompt.txt", str(tmp_path / "p.txt"))
        env = dict(os.environ)
        for k in ("FINDING_ID", "FINDING_SUMMARY", "FINDING_CHANGE", "FINDING_FILES"):
            env.pop(k, None)
        if finding:
            env.update({"FINDING_ID": "integrity/abc",
                        "FINDING_SUMMARY": "A `bad` pick — target ≤ entry",
                        "FINDING_CHANGE": "Drop it in `_validate_and_clean_picks`",
                        "FINDING_FILES": "ai_analyzer.py"})
        r = subprocess.run([sys.executable, "-c", py], capture_output=True, text=True, env=env)
        assert r.returncode == 0, r.stderr
        text = (tmp_path / "p.txt").read_text()
        if finding:
            assert "integrity/abc" in text and "target ≤ entry" in text
            assert "monitoring workflow just failed" not in text
        else:
            assert text.startswith("A monitoring workflow just failed")
            assert "APPROVED" not in text


class TestAnApprovedFindingReachesAPullRequest:
    """🔴 The loop must not end one step short. A finding-triggered run pushes
    a branch; without this it then reported "🧪 TEST run ... no PR (manual
    run)" and created nothing to review, because the flag conflated "manual
    dispatch" with "do not propose". That is the gate-that-could-never-pass
    class this workflow has already hit twice — a missing test runner, then a
    missing test dependency, each leaving correct diagnoses undelivered."""

    def _wf(self):
        import yaml
        return yaml.safe_load((ROOT / ".github/workflows/self_heal.yml").read_text())

    def test_a_dispatched_finding_takes_the_PR_path(self):
        env = self._wf()["jobs"]["self-heal"]["env"]["AUTO_MERGE"]
        assert "finding_id" in env, "an approved finding must not be treated as a test run"
        assert "workflow_run" in env, "a monitor failure must still propose"

    def test_a_bare_manual_dispatch_still_proposes_nothing(self):
        """The safety this flag was protecting: exercising the healer by hand
        must not open a PR."""
        env = self._wf()["jobs"]["self-heal"]["env"]["AUTO_MERGE"]
        assert "!=" in env and "''" in env, "a dispatch WITHOUT a finding must stay a test"

    def test_a_dispatched_finding_reaches_the_build_job_at_all(self):
        """triage gates the build. A manual dispatch is declared actionable
        unconditionally, which is what lets an approved finding through."""
        wf = self._wf()
        assert wf["jobs"]["self-heal"]["needs"] == "triage"
        triage = "\n".join(str(s.get("run", "")) for s in wf["jobs"]["triage"]["steps"])
        assert "workflow_dispatch" in triage and "actionable=true" in triage

    def test_nothing_in_this_path_merges_or_deploys_on_its_own(self):
        """Approval buys a PR, never a deploy. The second decision stays with
        the owner on the self-heal card."""
        body = (ROOT / ".github/workflows/self_heal.yml").read_text()
        assert "git push origin HEAD:main" not in body
        i = body.index("gh pr create")
        assert "--base main" in body[i:i + 200]
