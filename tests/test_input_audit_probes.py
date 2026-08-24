"""Guards for the input inventory's probes.

🔴 The rule this file already states in prose, now enforced: A PROBE MUST CALL
THE THING IT CLAIMS TO TEST. `p_gist` was inventoried as "GitHub Gist — CORE
all storage" but read through `config_manager._load_gist_file`, which resolves
via `get_storage_backend()`. On production and CI that meant it probed
SUPABASE while the row said Gist — so a green line said nothing about the Gist.

Same class as probing Polygon's free reference endpoint (HTTP 200) and calling
the paid options snapshot healthy while it 403s on every call.
"""
import ast
import pathlib

SRC = pathlib.Path("scripts/input_audit.py")


def _fn(name):
    tree = ast.parse(SRC.read_text())
    return next(f for f in ast.walk(tree)
                if isinstance(f, ast.FunctionDef) and f.name == name)


def _body(name):
    src = SRC.read_text()
    f = _fn(name)
    return "\n".join(src.splitlines()[f.lineno - 1:f.body[-1].end_lineno])


class TestTheGistProbeActuallyProbesTheGist:
    def test_it_does_not_go_through_the_storage_backend(self):
        """Scans the AST, not the source text: the docstring explaining this
        fix names the very function it bans, so a text scan flags itself. That
        trap has appeared repeatedly — tokenise, or anchor on real code."""
        fn = _fn("p_gist")
        banned = {"_load_gist_file", "get_storage_backend", "_store_read"}
        used = {n.attr for n in ast.walk(fn) if isinstance(n, ast.Attribute)}
        used |= {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
        assert not (used & banned), (
            f"p_gist calls {sorted(used & banned)} — those resolve via the "
            f"active backend, so it would probe Supabase, not the Gist"
        )

    def test_it_calls_the_gist_api_directly(self):
        assert "api.github.com/gists" in _body("p_gist")

    def test_it_reads_the_store_the_WRITER_uses(self):
        """picks.json's writer, config_manager.save_picks, hits the Gist API
        with a hardcoded URL — so the Gist is its live store and Supabase holds
        only a frozen copy from the Aug-19 migration."""
        cm = pathlib.Path("config_manager.py").read_text()
        i = cm.index("def save_picks")
        assert "api.github.com/gists" in cm[i:cm.index("def load_picks", i)], \
            "save_picks moved — p_gist must follow it"

    def test_missing_credentials_are_reported_not_swallowed(self):
        """A probe that returns True with no credentials is a false pass."""
        body = _body("p_gist")
        assert "not set" in body and "return False" in body

    def test_it_explains_why_it_bypasses_the_backend(self):
        """An exception to the read-the-live-store rule must justify itself, or
        someone will 'fix' it back."""
        assert len(ast.get_docstring(_fn("p_gist")) or "") > 200


class TestTheInventoryLabelIsHonest:
    def test_the_gist_is_no_longer_called_all_storage(self):
        """Supabase is the live store for everything except picks.json. The old
        label invited exactly the mistake the probe was making."""
        src = SRC.read_text()
        i = src.index("p_gist,")
        row = src[i:src.index("\n", i)]
        assert "all storage" not in row, f"stale label: {row.strip()}"
        assert "picks.json" in row

    def test_every_input_row_points_at_a_real_probe(self):
        """A row naming a function that does not exist is an input nobody
        counts — the thing this inventory exists to prevent."""
        src = SRC.read_text()
        tree = ast.parse(src)
        defined = {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
        i = src.index("INPUTS")
        for name in set(__import__("re").findall(r"\bp_[a-z_]+\b", src[i:])):
            assert name in defined, f"INPUTS references {name}, which does not exist"


class TestTheSupabaseRow:
    """🔴 The primary store had NO row until 2026-08-24, while this file's own
    rule says every external source gets one. The Gist row was covering for it
    with the label "all storage" — which is how a probe ended up testing
    Supabase while claiming to test the Gist."""

    def test_the_row_exists(self):
        """Imports the module and inspects INPUTS for real — a source scan
        passes against a COMMENTED-OUT row, which is how this guard first
        failed to catch its own mutation."""
        import importlib.util
        spec = importlib.util.spec_from_file_location("ia_t", str(SRC.resolve()))
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        names = [row[0] for row in m.INPUTS]
        assert "Supabase" in names, \
            f"the primary store is not inventoried; rows: {names}"

    def test_it_says_what_it_covers(self):
        src = SRC.read_text()
        i = src.index('"Supabase"')
        row = src[i:src.index("\n", i)]
        assert "picks.json" in row, \
            "the row must record that picks.json is the exception"

    def test_an_unconfigured_surface_is_not_reported_as_ok(self):
        """A local shell has no SUPABASE_* by design. Saying 'ok' there would
        claim the primary store was verified when it was never contacted."""
        body = _body("p_supabase")
        assert 'return False, "SUPABASE_* unset' in body

    def test_a_silent_fallback_is_reported_as_DEAD(self):
        """Configured but resolved to the Gist means construction failed and
        fell back — the split-brain that ran undetected for four days. It must
        never read as ok."""
        # Anchor on THAT branch's return, not on "return False" anywhere in
        # the function — the unset branch also returns False, so a loose scan
        # passes even when this one is flipped to True.
        fn = _fn("p_supabase")
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            if "!= 'supabase'" in ast.unparse(node.test).replace('"', "'"):
                rets = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
                assert rets, "the fallback branch returns nothing"
                first = rets[0].value
                ok = first.elts[0] if isinstance(first, ast.Tuple) else first
                assert isinstance(ok, ast.Constant) and ok.value is False, \
                    "a silent fallback to the Gist must never report ok"
                return
        raise AssertionError("no branch detects a fallback away from supabase")

    def test_it_states_that_a_READ_probe_cannot_see_RLS_write_denial(self):
        """RLS makes SELECT return fewer rows, not an error — which is exactly
        why the 2026-08-21 outage stayed invisible. Claiming more than reach is
        the false pass this file exists to prevent."""
        doc = ast.get_docstring(_fn("p_supabase")) or ""
        assert "RLS" in doc and "verify_storage" in doc

    def test_it_actually_reads_both_table_shapes(self):
        """Documents and per-user ROWS are different tables; reading only one
        would miss the half that was denied."""
        fn = _fn("p_supabase")
        calls = {n.func.attr for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert {"read", "read_all_users"} <= calls
