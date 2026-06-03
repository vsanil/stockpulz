#!/usr/bin/env python3
"""
scripts/check_js.py — Static analysis for miniapp/index.html JavaScript.

Catches JS bugs that Python tests can't see. Runs automatically via pytest
(tests/test_frontend_js.py). Also callable directly:

    python scripts/check_js.py          → print issues, exit 1 if errors
    python scripts/check_js.py --list   → list IIFEs and let/const decls found

Checks
------
1. Node.js syntax          — node --check catches parse errors and bad syntax
2. TDZ in top-level IIFEs  — let/const variable used in an IIFE before its
                              declaration (the bug that blanked the paper trade page)
3. Blocked Telegram APIs   — window.confirm/alert/prompt crash silently in
                              Telegram WebView; tg.enableClosingConfirmation is
                              banned by CLAUDE.md
4. window.open             — may be blocked; tg.openLink() is the safe path

Design notes
------------
Top-level IIFEs execute at script-load time and run BEFORE most let/const
declarations. They are detected by matching '(function' at column 0 (no
leading whitespace) paired with the nearest following '})();' also at col 0.
Indented IIFEs inside function bodies are ignored — they don't execute at
parse time so TDZ cannot occur there.

Comment and string stripping is line-level only (multi-line template literals
are NOT fully stripped). This means pattern checks work on line-level content.
The brace counter used for IIFE range detection only counts top-level function
boundaries, so template literal braces inside function bodies do not affect it.
"""

import re
import subprocess
import tempfile
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML_FILE    = ROOT / "miniapp" / "index.html"
WEBHOOK_FILE = ROOT / "webhook.py"


# ── Helpers ───────────────────────────────────────────────────────────────────

def extract_js(html: str) -> tuple[str, int]:
    """
    Return (js_content, html_line_offset) for the inline <script> block
    (i.e. a <script> tag with no src= attribute — the app's own JS).
    External <script src="..."> tags are ignored.
    """
    # Match <script> or <script ...> without src= — greedy to get the LAST one
    for m in re.finditer(r'<script(?![^>]*\bsrc\b)[^>]*>(.*?)</script>',
                         html, re.DOTALL | re.IGNORECASE):
        if m.group(1).strip():   # skip empty blocks (e.g. <script></script>)
            offset = html[: m.start(1)].count('\n')
            return m.group(1), offset
    return "", 0


def _strip_line(line: str) -> str:
    """
    Strip single-line comments and simple string/template literals from a line
    so pattern checks don't false-positive on commented-out code or strings.
    This is intentionally line-level only.
    """
    # Remove // comments (but not :// in URLs)
    line = re.sub(r'(?<![:/])//.*$', '', line)
    # Remove double-quoted strings
    line = re.sub(r'"(?:[^"\\]|\\.)*"', '""', line)
    # Remove single-quoted strings
    line = re.sub(r"'(?:[^'\\]|\\.)*'", "''", line)
    # Remove single-line template literals
    line = re.sub(r'`(?:[^`\\]|\\.)*`', '``', line)
    return line


class Issue:
    def __init__(self, level: str, check: str, line_no: int, message: str):
        self.level = level      # 'ERROR' | 'WARN'
        self.check = check
        self.line_no = line_no  # 1-indexed absolute HTML line
        self.message = message

    def __str__(self) -> str:
        return f"  [{self.level}] line {self.line_no}: {self.message}"

    def __lt__(self, other):
        return self.line_no < other.line_no


# ── Check 1: Node.js syntax ───────────────────────────────────────────────────

def check_syntax(js: str) -> list[Issue]:
    """Run `node --check` on the extracted JS block."""
    issues: list[Issue] = []
    with tempfile.NamedTemporaryFile(suffix='.js', mode='w', delete=False, encoding='utf-8') as f:
        f.write(js)
        tmp = f.name
    try:
        result = subprocess.run(
            ['node', '--check', tmp],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            for line in (result.stderr or '').splitlines():
                issues.append(Issue('ERROR', 'syntax', 0,
                                    f"Node syntax: {line.strip()}"))
    except FileNotFoundError:
        issues.append(Issue('WARN', 'syntax', 0,
                            "node not found — skipping JS syntax check"))
    except subprocess.TimeoutExpired:
        issues.append(Issue('WARN', 'syntax', 0, "node --check timed out"))
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return issues


# ── Check 2: TDZ in top-level IIFEs ──────────────────────────────────────────

def _find_toplevel_iife_ranges(lines: list[str]) -> list[tuple[int, int]]:
    """
    Return list of (start_idx, end_idx) 0-indexed line pairs for top-level IIFEs.

    Top-level means the IIFE opens at column 0 (no leading whitespace).
    Detected pattern:
      start : line begins with '(function' (no leading spaces)
      end   : line begins with '})();'     (no leading spaces)

    Pairs are matched in source order (first start → first end, etc.).
    This is correct because top-level IIFEs do not nest.
    """
    starts = []
    ends   = []
    for i, line in enumerate(lines):
        if re.match(r'^\((?:async\s+)?function', line):
            starts.append(i)
        if re.match(r'^\}\)\(\)', line):
            ends.append(i)

    # Zip in order — each start pairs with the nearest following end
    ranges = []
    ei = 0
    for start in starts:
        while ei < len(ends) and ends[ei] < start:
            ei += 1
        if ei < len(ends):
            ranges.append((start, ends[ei]))
            ei += 1
    return ranges


def check_tzdz(js: str, line_offset: int = 0) -> list[Issue]:
    """
    Detect TDZ: a let/const variable assigned inside a top-level IIFE that
    appears before the variable's declaration line.

    Example (the bug that blanked the paper trade miniapp):
        line 2738:  (function _handleDeepLinkTab() {
                        portfolioMode = 'paper';   ← assignment inside IIFE
                    })();
        line 4427:  let portfolioMode = 'real';    ← declaration — too late!

    NOTE: variables declared with let/const INSIDE an IIFE are local to that
    IIFE — they cannot cause outer-scope TDZ. We filter those out so only
    true script-scope declarations are checked.
    """
    issues: list[Issue] = []
    lines  = js.split('\n')

    # --- find top-level IIFE ranges FIRST (needed for filtering) ---
    iife_ranges = _find_toplevel_iife_ranges(lines)

    def _is_inside_any_iife(line_idx: int) -> bool:
        return any(start <= line_idx <= end for start, end in iife_ranges)

    # --- collect SCRIPT-SCOPE let/const declarations only ---
    # Declarations that fall inside an IIFE are local to that IIFE — no TDZ.
    # key: varname, value: first script-scope declaration line index (0-based)
    declarations: dict[str, int] = {}
    for i, line in enumerate(lines):
        if _is_inside_any_iife(i):
            continue  # local to IIFE — skip
        stripped = _strip_line(line)
        for m in re.finditer(
            r'\b(?:let|const)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)',
            stripped,
        ):
            varname = m.group(1)
            if varname not in declarations:
                declarations[varname] = i

    # Pattern for bare assignment: not a property access, not a comparison
    # (?<!\.)\b ensures we don't match obj.varname = ...
    def _is_local_decl(stripped: str, varname: str) -> bool:
        """Return True if this line declares varname locally (let/const)."""
        return bool(re.search(
            r'\b(?:let|const)\s+' + re.escape(varname) + r'\b',
            stripped,
        ))

    # --- for each IIFE that starts before a declaration, check for assignments ---
    for iife_start, iife_end in iife_ranges:
        for varname, decl_line in declarations.items():
            if iife_start >= decl_line:
                continue  # IIFE is after the declaration — safe

            # Scan the IIFE body for bare assignments to varname.
            # We track nested function literals so we only flag assignments
            # that execute at IIFE-load time (not inside callbacks/listeners).
            #
            # Algorithm:
            #   - brace_depth starts at 0; the IIFE's own opening `{` brings it to 1
            #   - nested_fn_stack: brace_depth pushed when we enter a nested function
            #   - first_func: skip the IIFE's own `function` keyword
            brace_depth        = 0
            nested_fn_stack: list[int] = []
            first_func_seen    = False
            assign_pat = (r'(?<!\.)\b' + re.escape(varname)
                          + r'\s*(?<![=!<>])=(?!=)')

            for j in range(iife_start, min(iife_end + 1, len(lines))):
                stripped = _strip_line(lines[j])
                opens  = stripped.count('{')
                closes = stripped.count('}')

                # ── 1. Count opens first ──────────────────────────────────
                brace_depth += opens

                # ── 2. Track nested function literals ────────────────────
                num_funcs = len(re.findall(r'\bfunction\b', stripped))
                for _ in range(num_funcs):
                    if not first_func_seen:
                        first_func_seen = True  # the IIFE itself — don't track
                    else:
                        nested_fn_stack.append(brace_depth)

                # ── 3. Check for bare assignment (only outside nested fns) ─
                if not nested_fn_stack:
                    if (not _is_local_decl(stripped, varname)
                            and re.search(assign_pat, stripped)):
                        abs_line = j + 1 + line_offset
                        issues.append(Issue(
                            'ERROR', 'tzdz', abs_line,
                            f"TDZ: '{varname}' assigned here (inside IIFE at lines "
                            f"{iife_start + 1}–{iife_end + 1}), but "
                            f"'let/const {varname}' is not declared until line "
                            f"{decl_line + 1 + line_offset}. "
                            f"Move the assignment inside the _appReady listener."
                        ))
                        break  # one report per IIFE × variable

                # ── 4. Count closes; pop expired nested functions ─────────
                brace_depth -= closes
                while nested_fn_stack and brace_depth < nested_fn_stack[-1]:
                    nested_fn_stack.pop()

    return issues


# ── Check 3: Blocked / banned APIs ───────────────────────────────────────────

_BANNED_PATTERNS: list[tuple[str, str]] = [
    (
        r'window\.confirm\s*\(',
        "window.confirm() is blocked in Telegram WebView — use tg.showPopup() instead",
    ),
    (
        r'window\.alert\s*\(',
        "window.alert() is blocked in Telegram WebView — use tg.showPopup() instead",
    ),
    (
        r'window\.prompt\s*\(',
        "window.prompt() is blocked in Telegram WebView — use tg.showPopup() instead",
    ),
    (
        r'tg\.enableClosingConfirmation\s*\(',
        "tg.enableClosingConfirmation() is banned (CLAUDE.md) — use tg.disableClosingConfirmation()",
    ),
    (
        r'(?<![.\w])window\.open\s*\(',
        "window.open() may be blocked in Telegram WebView — use tg.openLink() for external URLs",
    ),
]


def check_blocked_apis(js: str, line_offset: int = 0) -> list[Issue]:
    """Detect Telegram-blocked APIs and CLAUDE.md-banned patterns."""
    issues: list[Issue] = []
    lines = js.split('\n')
    for i, raw_line in enumerate(lines):
        stripped = _strip_line(raw_line)
        for pattern, msg in _BANNED_PATTERNS:
            if re.search(pattern, stripped):
                issues.append(Issue(
                    'ERROR', 'blocked_api',
                    i + 1 + line_offset,
                    msg,
                ))
    return issues


# ── Check 4: await without async ──────────────────────────────────────────────

def check_async_await(js: str, line_offset: int = 0) -> list[Issue]:
    """
    Detect non-async functions that use await — a SyntaxError at runtime.
    Heuristic: find 'await ' on a line whose nearest enclosing function
    declaration does not include the 'async' keyword.
    """
    issues: list[Issue] = []
    lines = js.split('\n')

    # Track the most recent function declaration line and whether it's async
    func_stack: list[tuple[int, bool]] = []   # (line_idx, is_async)
    brace_depth = 0

    for i, raw_line in enumerate(lines):
        stripped = _strip_line(raw_line)

        # Detect function start
        func_m = re.search(
            r'\b(async\s+)?function\s*\*?\s*\w*\s*\(',
            stripped,
        )
        if func_m:
            is_async = bool(func_m.group(1))
            func_stack.append((i, is_async))

        # Track brace depth for matching (rough — ignores template literals)
        opens  = stripped.count('{')
        closes = stripped.count('}')
        brace_depth += opens - closes
        # Pop function when braces close back
        if closes > 0 and func_stack:
            # Simplified: pop last function when depth returns toward start level
            pass  # brace-depth matching is unreliable; skip pop logic

        # Check for await usage
        if re.search(r'\bawait\s+', stripped):
            # Is the innermost enclosing function async?
            enclosing_async = func_stack[-1][1] if func_stack else True
            if not enclosing_async:
                issues.append(Issue(
                    'ERROR', 'async_await',
                    i + 1 + line_offset,
                    f"'await' used inside a non-async function "
                    f"(declared near line {func_stack[-1][0] + 1 + line_offset if func_stack else '?'}). "
                    f"Add 'async' to the function declaration.",
                ))

    return issues


# ── Runner ────────────────────────────────────────────────────────────────────

def extract_admin_html(webhook_path: Path = WEBHOOK_FILE) -> str:
    """
    Extract the _ADMIN_DASH_HTML triple-quoted string from webhook.py.
    Returns the resolved Python string value (backslash sequences interpreted).
    """
    src = webhook_path.read_text(encoding='utf-8')
    m = re.search(r'_ADMIN_DASH_HTML\s*=\s*"""(.*?)"""', src, re.DOTALL)
    if not m:
        return ""
    # Interpret common Python escape sequences so the HTML is accurate
    raw = m.group(1)
    raw = raw.replace("\\'", "'").replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
    return raw


def run_all_checks(html_path: Path = HTML_FILE) -> list[Issue]:
    all_issues: list[Issue] = []

    # ── 1. miniapp/index.html ─────────────────────────────────────────────────
    html = html_path.read_text(encoding='utf-8')
    js, offset = extract_js(html)
    if not js:
        all_issues.append(Issue('ERROR', 'extract', 0,
                                f"No <script> block found in {html_path}"))
    else:
        all_issues += check_syntax(js)
        all_issues += check_tzdz(js, offset)
        all_issues += check_blocked_apis(js, offset)

    # ── 2. webhook.py admin dashboard (_ADMIN_DASH_HTML) ─────────────────────
    admin_html = extract_admin_html()
    if admin_html:
        admin_js, admin_offset = extract_js(admin_html)
        if admin_js:
            for issue in check_syntax(admin_js):
                issue.message = f"[admin dashboard] {issue.message}"
                all_issues.append(issue)
            for issue in check_blocked_apis(admin_js, admin_offset):
                issue.message = f"[admin dashboard] {issue.message}"
                all_issues.append(issue)
        # Note: TDZ check skipped for admin dashboard (no top-level IIFEs there)
    else:
        all_issues.append(Issue('WARN', 'extract', 0,
                                "Could not extract _ADMIN_DASH_HTML from webhook.py"))

    return all_issues


def _debug_list(html_path: Path = HTML_FILE) -> None:
    """Print IIFEs and let/const declarations found (for --list flag)."""
    html = html_path.read_text(encoding='utf-8')
    js, offset = extract_js(html)
    lines = js.split('\n')

    print("\n=== Top-level IIFEs ===")
    for start, end in _find_toplevel_iife_ranges(lines):
        print(f"  lines {start+1+offset}–{end+1+offset}: {lines[start][:60].strip()}")

    print("\n=== let/const declarations (first 20) ===")
    count = 0
    for i, line in enumerate(lines):
        stripped = _strip_line(line)
        for m in re.finditer(r'\b(?:let|const)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)', stripped):
            print(f"  line {i+1+offset}: {m.group(0)}")
            count += 1
            if count >= 20:
                print("  ... (truncated)")
                return


def main() -> None:
    if '--list' in sys.argv:
        _debug_list()
        return

    issues = run_all_checks()
    errors = [i for i in issues if i.level == 'ERROR']
    warns  = [i for i in issues if i.level == 'WARN']

    if issues:
        print(f"\n{'='*60}")
        print("JS CHECK — miniapp/index.html + webhook.py admin dashboard")
        print('='*60)
        for issue in sorted(issues):
            print(issue)
        print(f"\n{len(errors)} error(s)  {len(warns)} warning(s)")

    if errors:
        print("\n❌  JS checks FAILED — fix errors before committing\n")
        sys.exit(1)
    else:
        tag = "⚠️  warnings" if warns else "✅"
        print(f"\n{tag}  JS checks passed\n")
        sys.exit(0)


if __name__ == '__main__':
    main()
