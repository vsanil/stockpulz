"""
tests/test_frontend_js.py — Static analysis for miniapp/index.html JavaScript.

These tests catch JS bugs that Python tests cannot:
  - Syntax errors (via node --check)
  - TDZ violations — let/const used in IIFEs before their declaration
    (this exact bug blanked the paper trade page when ?mode=paper was opened)
  - Blocked Telegram APIs — window.confirm/alert/prompt crash silently
  - Banned patterns from CLAUDE.md

Run as part of the normal pytest suite:
    python -m pytest tests/ -q
Or target just these:
    python -m pytest tests/test_frontend_js.py -v
"""

import os
import re
import sys
import pytest
from pathlib import Path

# Make scripts/ importable without installing as a package
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from check_js import (
    HTML_FILE,
    run_all_checks,
    extract_js,
    check_syntax,
    check_tzdz,
    check_blocked_apis,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def html_content():
    assert HTML_FILE.exists(), f"miniapp/index.html not found at {HTML_FILE}"
    return HTML_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js_content(html_content):
    js, offset = extract_js(html_content)
    assert js, "No <script> block found in index.html"
    return js, offset


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestJSSyntax:
    def test_node_syntax_check(self, js_content):
        """
        node --check must pass.
        Catches: missing brackets, bad tokens, reserved keyword misuse.
        """
        js, offset = js_content
        issues = check_syntax(js)
        errors = [i for i in issues if i.level == "ERROR"]
        if errors:
            detail = "\n".join(str(e) for e in errors)
            pytest.fail(f"JS syntax errors in miniapp/index.html:\n{detail}")


class TestTDZ:
    def test_no_tzdz_in_iifes(self, js_content):
        """
        No TDZ violation: let/const variables must not be assigned inside a
        top-level IIFE that appears before the variable's declaration line.

        Regression for: paper trade miniapp showed blank page when opened
        via ?mode=paper because _handleDeepLinkTab IIFE (line ~2731) assigned
        `portfolioMode = 'paper'` but `let portfolioMode` wasn't declared
        until line ~4427 — ReferenceError crashed the entire script.
        """
        js, offset = js_content
        issues = check_tzdz(js, offset)
        errors = [i for i in issues if i.level == "ERROR"]
        if errors:
            detail = "\n".join(str(e) for e in errors)
            pytest.fail(
                f"TDZ violation(s) in miniapp/index.html:\n{detail}\n\n"
                f"Fix: move the assignment inside the _appReady listener "
                f"where the variable is guaranteed to be declared."
            )

    def test_iife_detection_finds_known_iifes(self, js_content):
        """
        Sanity check: IIFE detector finds at least the known top-level IIFEs.
        If this fails, the detector is broken, not the app code.
        """
        from check_js import _find_toplevel_iife_ranges
        js, offset = js_content
        lines = js.split("\n")
        ranges = _find_toplevel_iife_ranges(lines)
        # We know there are at least 3 early IIFEs + several at end of script
        assert len(ranges) >= 3, (
            f"Expected ≥3 top-level IIFEs, found {len(ranges)}. "
            f"IIFE detection may be broken."
        )

    def test_deep_link_handler_no_pre_declaration_assign(self, html_content):
        """
        Regression test: the _handleDeepLinkTab IIFE must NOT directly assign
        to `portfolioMode` — it must use the _appReady listener instead.
        This is the exact bug that caused the blank page on ?mode=paper.
        """
        # Find the IIFE section
        iife_start = html_content.find("_handleDeepLinkTab")
        iife_end   = html_content.find("})();", iife_start)
        assert iife_start != -1, "_handleDeepLinkTab IIFE not found"
        assert iife_end   != -1, "_handleDeepLinkTab IIFE closing not found"

        iife_body = html_content[iife_start:iife_end]

        # portfolioMode must NOT appear before the _appReady listener inside the IIFE
        appready_pos = iife_body.find("_appReady")
        assert appready_pos != -1, "_appReady listener not found in _handleDeepLinkTab"

        # Check no direct portfolioMode ASSIGNMENT before _appReady
        # (comments mentioning portfolioMode are fine; actual assignments are not)
        before_appready = iife_body[:appready_pos]
        # Strip comment lines before scanning for assignments
        non_comment_lines = [
            ln for ln in before_appready.splitlines()
            if not ln.strip().startswith('//')
        ]
        before_appready_code = '\n'.join(non_comment_lines)
        assert not re.search(r'\bportfolioMode\s*=(?!=)', before_appready_code), (
            "portfolioMode is assigned before the _appReady listener "
            "inside _handleDeepLinkTab — this causes a TDZ crash when "
            "?mode=paper is in the URL. Move the assignment inside _appReady."
        )


class TestBlockedAPIs:
    def test_no_window_confirm(self, js_content):
        """window.confirm() is blocked in Telegram WebView."""
        js, offset = js_content
        issues = check_blocked_apis(js, offset)
        errors = [i for i in issues
                  if i.level == "ERROR" and "confirm" in i.message]
        if errors:
            detail = "\n".join(str(e) for e in errors)
            pytest.fail(
                f"window.confirm() found in miniapp/index.html:\n{detail}\n"
                f"Use tg.showPopup() instead."
            )

    def test_no_window_alert(self, js_content):
        """window.alert() is blocked in Telegram WebView."""
        js, offset = js_content
        issues = check_blocked_apis(js, offset)
        errors = [i for i in issues
                  if i.level == "ERROR" and "alert" in i.message]
        if errors:
            detail = "\n".join(str(e) for e in errors)
            pytest.fail(
                f"window.alert() found in miniapp/index.html:\n{detail}\n"
                f"Use tg.showPopup() instead."
            )

    def test_no_enable_closing_confirmation(self, js_content):
        """tg.enableClosingConfirmation() is banned (CLAUDE.md)."""
        js, offset = js_content
        issues = check_blocked_apis(js, offset)
        errors = [i for i in issues
                  if i.level == "ERROR" and "enableClosingConfirmation" in i.message]
        if errors:
            detail = "\n".join(str(e) for e in errors)
            pytest.fail(
                f"tg.enableClosingConfirmation() found:\n{detail}\n"
                f"Use tg.disableClosingConfirmation() per CLAUDE.md."
            )


class TestIntegration:
    def test_full_check_passes(self):
        """
        Full run_all_checks() must return zero errors.
        This is the gate that blocks a commit if any JS issue is present.
        """
        issues = run_all_checks(HTML_FILE)
        errors = [i for i in issues if i.level == "ERROR"]
        if errors:
            detail = "\n".join(str(e) for e in sorted(errors))
            pytest.fail(
                f"JS check failures in miniapp/index.html "
                f"({len(errors)} error(s)):\n{detail}"
            )
