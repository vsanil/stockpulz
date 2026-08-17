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


class TestPickCardActionRows:
    """🔴 The pick card's real-estate layout (Aug 15 2026).

    Measured against the deployed CSS at a 375px viewport: the card was 430px
    with THREE action rows, because `.btn-buy`'s `min-width:150px` could not fit
    beside Chart + Backtest + Alert and wrapped to a full-width row of its own.

    Moving Chart and Backtest into the Analysis disclosure frees that wrap so
    Alert and Buy share ONE line: 430px -> 383px, three rows -> two. 47px per
    card, ~235px across a 5-pick day.

    The saving comes from ELIMINATING A ROW, not from hiding buttons — pulling
    items out of a horizontal row without collapsing the row saves nothing.
    """

    @property
    def _src(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "miniapp", "index.html")) as f:
            return f.read()

    def _card_fn(self):
        """The pick-card template literal only."""
        s = self._src
        i = s.index('<div class="pick-actions">', s.index("pick-thesis-toggle"))
        # from the Analysis toggle through the Paper Trade row
        start = s.rindex("pick-thesis-toggle", 0, i)
        end = s.index("btn-paper", i)
        return s[start - 400:end + 200]

    def test_chart_and_backtest_are_inside_the_analysis_disclosure(self):
        """Discriminating form: both must appear BEFORE the action row that holds
        btn-buy. In the old markup they sat INSIDE it, so their index was higher.
        (Comparing against `pick-thesis-body` alone passed either way — the
        disclosure precedes the action row in both layouts.)"""
        s = self._src
        # `id="buy-` appears only in the card template; "btn-buy" also matches
        # the CSS rule defined far earlier in the file.
        buy = s.index('id="buy-')
        row = s.rindex('<div class="pick-actions"', 0, buy)
        # `onclick="openChart(` is markup-only; a bare `openChart(` also matches
        # the function DECLARATION far earlier in the file, which made this pass
        # against both layouts.
        assert s.index('onclick="openChart(') < row, "Chart is in the action row — Buy will wrap"
        assert s.index('onclick="openBacktestSheet(') < row, "Backtest is in the action row"

    def test_the_action_row_holds_only_alert_and_buy(self):
        """A third button here re-creates the forced wrap and gives the row back."""
        block = self._card_fn()
        row = block[block.index('<div class="pick-actions">', block.index("/div>")):]
        row = row[:row.index("</div>")]
        assert "btn-buy" in row
        assert "openChart(" not in row and "openBacktestSheet(" not in row, \
            "a research button is back in the action row — Buy will wrap again"

    def test_the_min_width_that_makes_them_share_a_row_is_unchanged(self):
        """150px + Alert (~110px) + 8px gap fits 375px. Raising it re-wraps."""
        assert "min-width: 150px;" in self._src

    def test_the_disclosure_renders_unconditionally(self):
        """It now always holds Chart + Backtest, so gating it on a thesis would
        make them vanish on a pick that has none."""
        assert "(p.thesis || p.reason || p.theme || p.edge) ? `" not in self._src, \
            "the toggle is gated on a thesis again — Chart/Backtest vanish without one"

    def test_the_alert_button_is_still_on_the_card(self):
        """It displays STATE (amber when an alert exists), so hiding it behind a
        disclosure would conceal whether the pick is already alerted."""
        assert "_pickAlertBtnHtml(sym" in self._src


class TestApiIsCalledPositionally:
    """🔴 The usage counter never sent a single request (2026-08-08 → 08-15).

    `api()` is POSITIONAL — api(path, method, body). One call site was written
    fetch-style, `api(path, {method:'POST', body:'…'})`, so `method` became an
    object that stringifies to "[object Object]" — an invalid HTTP method — and
    fetch threw directly into the site's own `.catch(() => {})`.

    Nothing surfaced: telemetry is designed to fail silently, so the endpoint,
    the auth, the mutator and the deployed page were all correct and the file it
    writes simply never appeared. The feature it existed to inform (which
    tabs/sheets are worth keeping) then had to be decided without evidence.
    """

    _RE = r"""api\(\s*(?:'[^']*'|"[^"]*"|`[^`]*`)\s*,\s*\{"""

    @property
    def _src(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "miniapp", "index.html")) as f:
            return f.read()

    def test_no_call_site_passes_an_options_object_as_the_method(self):
        import re
        src = self._src
        bad = []
        for m in re.finditer(self._RE, src, re.S):
            line = src.count("\n", 0, m.start()) + 1
            bad.append(f"index.html:{line}: {m.group(0).strip()}")
        assert not bad, (
            "api() takes (path, method, body) positionally. These pass an object "
            "as `method`, which fetch rejects and the caller's .catch() hides:\n  "
            + "\n  ".join(bad))

    def test_the_scan_can_actually_detect_an_offender(self):
        """A guard that cannot fail is not a guard — this reintroduces the exact
        broken shape and asserts the regex catches it."""
        import re
        offender = """api('/api/miniapp/usage', {method: 'POST', body: '{}'})"""
        assert re.search(self._RE, offender, re.S), "the scan would miss the real bug"

    def test_the_usage_flush_now_posts_positionally(self):
        assert "api('/api/miniapp/usage', 'POST', {counts: u})" in self._src

    def test_every_post_call_site_uses_a_string_method(self):
        import re
        src = self._src
        calls = re.findall(r"api\(\s*(?:'[^']*'|\"[^\"]*\")\s*,\s*([^,)\s][^,)]*)", src)
        for arg in calls:
            arg = arg.strip()
            assert arg.startswith(("'", '"', "`")), \
                f"api() second argument is not a method string: {arg[:60]}"
