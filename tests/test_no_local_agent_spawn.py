"""Render must never run agent.py — it relays to GitHub Actions.

🔴 The OOM class this closes. Importing agent.py costs ~121 MB before it does a
single unit of work: it pulls screener / ai_analyzer / etf_screener /
chart_generator at module level, and with them pandas, numpy, yfinance and
matplotlib — on top of the copy of pandas gunicorn already holds. Render's
instance OOM-kills the run mid-flight, which sets `cron_last_<mode>` while
never delivering, and the per-day guard then blocks the retry. The failure is a
SILENT MISS, not an error.

morning was converted in Jul 2026 and weekly in Aug 2026, each after exactly
this. The other 16 of 19 trigger modes, AND the /admin "run agent" button, were
left spawning locally until 2026-09-03.
"""
import ast
import pathlib

WEB = pathlib.Path("webhook.py")
DAILY = pathlib.Path(".github/workflows/daily_run.yml")


def _spawn_calls():
    """Every subprocess.run/Popen call in webhook.py, via AST.

    ⚠️ AST, not grep. The comments explaining this fix NAME `subprocess.Popen`
    and `agent.py`, so a text scan flags itself — that trap has now appeared
    nine times in this repo. Scan code, never prose.
    """
    tree = ast.parse(WEB.read_text())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = ""
        if isinstance(f, ast.Attribute):
            name = f.attr
            if isinstance(f.value, ast.Name):
                name = f"{f.value.id}.{f.attr}"
        if name in ("subprocess.run", "subprocess.Popen", "Popen"):
            out.append((getattr(node, "lineno", 0), ast.unparse(node)))
    return out


class TestNoRouteRunsAgentLocally:
    def test_webhook_never_spawns_agent_py(self):
        offenders = [(ln, src) for ln, src in _spawn_calls() if "agent.py" in src]
        assert not offenders, (
            "webhook.py spawns agent.py on Render — that is the ~121 MB OOM "
            f"path: {offenders}")

    def test_the_generic_trigger_route_relays(self):
        src = WEB.read_text()
        i = src.index("def trigger_mode")
        body = src[i:i + 4000]
        assert "_relay_to_github(" in body

    def test_the_admin_run_button_relays(self):
        """⚠️ Checked STRUCTURALLY. An earlier version asserted `"agent.py" not
        in body`, which failed on the docstring EXPLAINING the fix — the tenth
        time that trap has appeared here, written into the same file that warns
        about it. Walk the function's AST instead of reading its prose."""
        tree = ast.parse(WEB.read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "admin_run_agent")
        src = ast.unparse(fn)
        assert "_relay_to_github(" in src
        spawns = [ast.unparse(c) for c in ast.walk(fn)
                  if isinstance(c, ast.Call)
                  and isinstance(c.func, ast.Attribute)
                  and c.func.attr in ("run", "Popen")
                  and isinstance(c.func.value, ast.Name)
                  and c.func.value.id == "subprocess"]
        assert not spawns, f"the admin button still spawns a subprocess: {spawns}"


class TestOwnerOnlySurvivesTheRelay:
    """🔴 /trigger/<mode>?owner_only=1 restricts delivery to the OWNER (manual
    test triggers). The local path set it via subprocess env. Dropping it in the
    relay would BROADCAST A TEST RUN TO EVERY USER — a silent, user-visible
    regression from a change that is otherwise invisible."""

    def test_the_helper_accepts_and_forwards_it(self):
        src = WEB.read_text()
        i = src.index("def _relay_to_github")
        body = src[i:src.index("@app.route", i)]
        assert "owner_only" in body
        assert '"owner_only": "1" if owner_only else ""' in body

    def test_the_generic_route_still_reads_the_query_param(self):
        src = WEB.read_text()
        assert 'request.args.get("owner_only"' in src

    def test_the_workflow_declares_the_input_and_sets_the_env(self):
        import yaml
        d = yaml.safe_load(DAILY.read_text())
        inputs = d[True]["workflow_dispatch"]["inputs"]
        assert "owner_only" in inputs, \
            "the relay sends owner_only but the workflow would silently drop it"
        assert "OWNER_ONLY:" in DAILY.read_text(), \
            "declared as an input but never passed to agent.py — a no-op"

    def test_mock_survives_too(self):
        """The /admin button offers a mock run; losing it would fire a REAL
        run — screeners, Claude, and live sends — from a button labelled test."""
        src = WEB.read_text()
        assert '"mock_data": "true" if mock else "false"' in src
        d = __import__("yaml").safe_load(DAILY.read_text())
        assert "mock_data" in d[True]["workflow_dispatch"]["inputs"]
