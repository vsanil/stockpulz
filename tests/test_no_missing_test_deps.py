"""Regression for the missing-pyyaml incident (2026-09-06).

`tests/test_selfheal_triage.py` added a module-level `import yaml` without
adding pyyaml to requirements.txt. `test.yml` runs plain
`python -m pytest tests/ -q --tb=short` — no `--continue-on-collection-errors`
— so ONE unresolvable top-level import in ANY test file aborts COLLECTION OF
THE WHOLE SUITE, not just that file. That broke the "Tests" GitHub Actions
workflow on every push since the commit landed, and starved self_heal.yml's
merge gate (same `pytest tests/ -q` command) of ever passing — a repeat of
the `TestTheGateCanActuallyRun` class (Aug 24), this time caused by a missing
test dependency instead of a missing test runner.

This scans every test module's TOP-LEVEL imports via AST (a function-local
import is fine — it only fails the one test that runs it, never collection)
and checks each is actually IMPORTABLE in this process — the same process
shape CI runs (`pip install -r requirements.txt pytest` then `pytest`).
Checking importability directly (rather than hand-parsing requirements.txt
and maintaining a pip-name alias table) also correctly passes transitive
deps like `numpy` (pulled in by `pandas`) without a hand-maintained list —
the exact kind of tally this project's own notes warn goes stale.
"""
import ast
import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS_DIR = ROOT / "tests"

# Installed separately from requirements.txt in every CI workflow that runs
# the suite (test.yml, self_heal.yml both do `pip install -r requirements.txt
# pytest`).
_ALLOWED_EXTRA = {"pytest"}


def _project_local_modules() -> set[str]:
    mods = {p.stem for p in ROOT.glob("*.py")}
    mods |= {p.stem for p in (ROOT / "scripts").glob("*.py")}
    mods.add("tests")
    return mods


def _top_level_imports(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    names = set()
    for node in tree.body:  # module level ONLY -- a function-local import is safe
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _is_importable(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def test_every_top_level_test_import_is_installable():
    local = _project_local_modules()

    missing = {}
    for path in sorted(TESTS_DIR.glob("*.py")):
        for name in _top_level_imports(path):
            if name in local or name in _ALLOWED_EXTRA:
                continue
            if _is_importable(name):
                continue
            missing.setdefault(path.name, set()).add(name)

    assert not missing, (
        "test module(s) import a package at TOP LEVEL that is not installed by "
        "`pip install -r requirements.txt pytest` -- one such import aborts "
        f"collection of the WHOLE suite, not just this file: {missing}"
    )


def test_the_scan_can_detect_an_offender(tmp_path, monkeypatch):
    """A guard that cannot fail is not a guard."""
    bogus = tmp_path / "test_bogus_dep.py"
    bogus.write_text("import this_package_does_not_exist_anywhere\n")
    monkeypatch.setattr("tests.test_no_missing_test_deps.TESTS_DIR", tmp_path)
    try:
        test_every_top_level_test_import_is_installable()
    except AssertionError as exc:
        assert "this_package_does_not_exist_anywhere" in str(exc)
    else:
        raise AssertionError("the scan failed to catch an unresolvable top-level import")
