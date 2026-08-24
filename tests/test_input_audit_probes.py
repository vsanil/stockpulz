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
