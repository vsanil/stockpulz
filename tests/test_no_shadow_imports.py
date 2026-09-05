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


def _scan_conditional(path):
    """The OTHER half of the class: a local import nested inside a branch.

    🔴 MEASURED 2026-09-05. `_scan_file` above only catches a use whose LINE
    NUMBER precedes the local import. That is a subset, and the rest of the
    class is just as live: `cmd_market.py` imported `send_inline_keyboard`
    locally at line 635 inside an `if`, and used it at line 1385. 1385 > 635, so
    the line-order check said "clean" — but 635 never executed on the `/digest`
    path, so line 1385 raised
    `UnboundLocalError: cannot access local variable 'send_inline_keyboard'`.
    The daily digest caught it, printed "handler errored — suppressed", reported
    "sent to 0 user(s)" and exited 0. Green job, zero delivery, for weeks.

    🔑 The lesson is about THIS FILE, not that bug. Its docstring said it
    "catches the whole class ... keep it at zero", and it had reported zero ever
    since finding 8 bugs in July — while an instance of the same class sat in
    the tree. A guard that covers a subset while claiming the class is worse
    than no guard, because the zero is believed.

    Predicate here is REACHABILITY, not line order: an import nested inside a
    branch binds the name for the whole function, so ANY use outside that
    branch's own block can hit the unbound local.
    """
    with open(path) as fh:
        tree = ast.parse(fh.read(), path)
    module_names = _module_imported_names(tree)
    bugs = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        own = list(_own_scope_nodes(fn))
        params = {a.arg for a in list(fn.args.args) + list(fn.args.kwonlyargs)}
        top_level = {id(s) for s in fn.body}
        for stmt in fn.body:                      # each top-level block of fn
            lo, hi = stmt.lineno, getattr(stmt, "end_lineno", stmt.lineno)
            for n in ast.walk(stmt):
                if not isinstance(n, (ast.Import, ast.ImportFrom)):
                    continue
                if id(n) in top_level:            # unconditional — always runs
                    continue
                for a in n.names:
                    nm = a.asname or a.name.split(".")[0]
                    if nm not in module_names or nm in params:
                        continue
                    outside = sorted({u.lineno for u in own
                                      if isinstance(u, ast.Name)
                                      and isinstance(u.ctx, ast.Load)
                                      and u.id == nm
                                      and not (lo <= u.lineno <= hi)})
                    if outside:
                        bugs.append((fn.name, nm, n.lineno, outside[:4]))
    return bugs


def test_no_conditional_shadow_imports():
    """Companion to test_no_shadow_before_use_imports — see _scan_conditional."""
    offenders = []
    for path in glob.glob(os.path.join(_ROOT, "*.py")) + \
            glob.glob(os.path.join(_ROOT, "scripts", "*.py")):
        for fn, name, imp_ln, uses in _scan_conditional(path):
            offenders.append(
                f"{os.path.basename(path)}:{fn}() imports '{name}' locally at line "
                f"{imp_ln} inside a BRANCH, but uses it outside that branch at "
                f"{uses} → UnboundLocalError when the branch does not run")
    assert not offenders, "conditional shadow-import bugs:\n  " + "\n  ".join(offenders)


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
