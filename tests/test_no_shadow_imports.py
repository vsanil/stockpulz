"""Guard against the function-local-import shadow bug class (CLAUDE.md).

A module-level name (e.g. `os`, `get_user_config`, `_fetch_live_price`) that is
ALSO re-imported *inside* a function becomes local to that whole function. If any
code path uses the name BEFORE the local import line, Python raises
`UnboundLocalError` at runtime — which the app's catch-alls swallow into
"Something went wrong", silently breaking /positions, /size, /history, the
no-picks morning broadcast, etc.

`py_compile` does NOT catch this (it's a runtime error), and it only fires on the
specific branch, so unit tests miss it too. This AST scan catches the whole class
statically. It found 8 real production bugs on 2026-07-03; keep it at zero.
"""
import ast
import glob
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _module_imported_names(tree):
    names = set()
    for node in tree.body:                       # top-level only
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
    return names


def _own_scope_nodes(fn):
    """Nodes in fn's OWN scope — never descend into nested function/lambda bodies
    (those are separate scopes and would produce false positives)."""
    stack = list(fn.body)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        yield n
        stack.extend(ast.iter_child_nodes(n))


def _scan_file(path):
    """Return list of (func, name, use_line, import_line) shadow-before-use bugs."""
    with open(path) as fh:
        tree = ast.parse(fh.read(), path)
    module_names = _module_imported_names(tree)
    bugs = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(_own_scope_nodes(fn))
        local_imp = {}
        for n in nodes:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    nm = a.asname or a.name.split(".")[0]
                    if nm in module_names:
                        local_imp[nm] = min(local_imp.get(nm, 10 ** 9), n.lineno)
        params = {a.arg for a in list(fn.args.args) + list(fn.args.kwonlyargs)}
        for n in nodes:
            if (isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                    and n.id in local_imp and n.id not in params
                    and n.lineno < local_imp[n.id]):
                bugs.append((fn.name, n.id, n.lineno, local_imp[n.id]))
    return bugs


def test_no_shadow_before_use_imports():
    offenders = []
    for path in glob.glob(os.path.join(_ROOT, "*.py")) + \
            glob.glob(os.path.join(_ROOT, "scripts", "*.py")):
        for fn, name, use_ln, imp_ln in _scan_file(path):
            offenders.append(
                f"{os.path.basename(path)}:{fn}() uses '{name}' at line {use_ln} "
                f"but re-imports it locally at line {imp_ln} "
                f"(module-level import shadowed → UnboundLocalError)")
    assert not offenders, "shadow-before-use import bugs:\n  " + "\n  ".join(offenders)
