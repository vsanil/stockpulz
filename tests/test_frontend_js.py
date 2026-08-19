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


class TestAnalysisToggleLooksTappable:
    """🔴 Owner feedback, Aug 18: "it doesn't even look clickable."

    The disclosure was styled as a section LABEL — `background:none; border:none`,
    `--hint` grey at 11px, ~22px tall, and its only feedback was `opacity` on
    `:active`, i.e. after you had already guessed it was a control. That was
    survivable while it hid optional prose. It became a real defect when Chart
    and Backtest were moved inside it, because the affordance became
    load-bearing — the pixels were optimised without checking discoverability.
    """

    @property
    def _css(self):
        import os, re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "miniapp", "index.html")) as f:
            src = f.read()
        m = re.search(r"\.pick-thesis-toggle \{(.*?)\}", src, re.S)
        assert m, ".pick-thesis-toggle rule not found"
        # Strip CSS comments: the rule's own comment MENTIONS the banned
        # rgba(255,255,255,.04) while explaining why it was removed, and a naive
        # scan flags that as an offence. Same docstring-scan trap as before.
        return re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)

    @property
    def _src(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "miniapp", "index.html")) as f:
            return f.read()

    def test_it_has_a_surface_and_an_edge(self):
        css = self._css
        assert "background: none" not in css, "no surface — reads as a text label"
        assert "border: none" not in css, "no edge — reads as a text label"
        assert "border-radius" in css

    def test_the_surface_is_a_THEME_TOKEN_not_a_white_overlay(self):
        """`rgba(255,255,255,.04)` is invisible on a white card, and light mode
        is the theme actually in use. Caught by reading the COMPUTED colour
        against the card, not by looking at the CSS."""
        css = self._css
        assert "var(--card-hover)" in css, "surface must come from a theme token"
        assert "rgba(255,255,255" not in css, \
            "hardcoded white overlay disappears in light mode"

    def test_it_meets_the_44px_touch_target(self):
        """The project's own mobile rule, DECLARED rather than emergent.

        Asserting on padding was the wrong guard: it passes or fails for the
        wrong reason the moment the font size or line-height changes. min-height
        states the constraint directly.
        """
        import re
        m = re.search(r"min-height:\s*(\d+)px", self._css)
        assert m and int(m.group(1)) >= 44, \
            f"min-height is {m and m.group(1)} — the touch target is not guaranteed"

    def test_the_label_names_what_is_hidden(self):
        """"Analysis" alone gave no clue Chart and Backtest were behind it."""
        src = self._src
        assert "pick-thesis-sub" in src
        assert "chart, backtest" in src, "the label must name the payload"

    def test_open_state_is_exposed_and_styled(self):
        src, css_all = self._src, self._src
        assert 'aria-expanded="false"' in src, "initial state must be declared"
        assert "setAttribute('aria-expanded'" in src, "state must track the toggle"
        assert '.pick-thesis-toggle[aria-expanded="true"]' in css_all, \
            "open state needs a visual difference, not just an attribute"

    def test_pressed_feedback_is_more_than_opacity(self):
        assert "transform: scale(.98)" in self._src, \
            "a pressed state should match the app's other buttons"


class TestNarrowViewportOverflow:
    """🔴 Two controls were unreachable or unreadable at 375px (Aug 18).

    Both are the same root cause — a flex item defaults to `min-width: auto` and
    will not shrink below its content's min-content width, so the overflow lands
    on whatever sits last in the row.

      * Watchlist: the left block (ticker + name + "🔔 fired $X · Nd ago")
        measured 204px and the button group 161px = 365px inside a 347px row,
        pushing the ✕ to 379-391px on a 375px viewport. OFF SCREEN — the remove
        control could not be tapped. It only bit rows carrying the fired
        annotation, which is why it survived.
      * Portfolio: five tiles at `flex: 1` split 347px into 61px each, leaving a
        43px label area for "Positions" (67px) and "Portfolio $" (69px). Single
        words, so they could not wrap, and the row did not scroll.
    """

    @property
    def _src(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "miniapp", "index.html")) as f:
            return f.read()

    # ── watchlist row ────────────────────────────────────────────────────────
    def _row(self):
        s = self._src
        i = s.index('html += `<div id="wrow-${t}"')
        return s[i:s.index("</div>`", i)]

    def test_the_button_group_never_shrinks_or_is_pushed_out(self):
        row = self._row()
        i = row.index('<div style="display:flex;gap:8px')
        assert "flex:0 0 auto" in row[i:i + 120], \
            "the ✕/alert/add/chart group must not shrink — it gets pushed off-screen"

    def test_the_ticker_has_a_width_floor(self):
        """Over-correcting with min-width:0 alone let the text block collapse to
        ~9px and truncated every ticker to ONE character."""
        row = self._row()
        assert "min-width:64px" in row, "without a floor the ticker itself is clipped"

    def test_the_fired_annotation_ellipsizes_rather_than_widening_the_row(self):
        s = self._src
        i = s.index("🔔 fired $")
        assert "text-overflow:ellipsis" in s[max(0, i - 260):i], \
            "the fired line is the widest element; it must truncate, not push"

    # ── portfolio summary tiles ──────────────────────────────────────────────
    def _rule(self, name):
        import re
        m = re.search(rf"\.{name} \{{(.*?)\}}", self._src, re.S)
        assert m, f".{name} rule not found"
        return re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)   # strip comments

    def test_the_summary_row_can_wrap(self):
        assert "flex-wrap: wrap" in self._rule("summary-row"), \
            "five tiles cannot fit one 375px row without clipping their labels"

    def test_each_tile_is_wide_enough_for_its_longest_label(self):
        import re
        css = self._rule("summary-box")
        m = re.search(r"min-width:\s*(\d+)px", css)
        assert m and int(m.group(1)) >= 96, \
            f"min-width {m and m.group(1)} is under the 69px label + 16px padding + border"

    # NOTE: a guard here once asserted the sparkline was HIDDEN. That was true
    # only while four icon buttons took 47% of the row. Cutting to two inline
    # controls freed ~86px and the sparkline fits again, so the constraint no
    # longer holds — TestWatchlistRowDensity now asserts the opposite. Removed
    # rather than relaxed: a test that contradicts current intent is worse than
    # no test.


class TestWatchlistRowDensity:
    """Four unlabelled icon buttons per row took 164px — 47% of a 347px row.

    That density is what forced the ✕ off-screen and squeezed out the sparkline.
    Two stay inline; the rest move into a ⋯ sheet.

      * 🔔/🔕 stays because it is a STATE indicator (amber when an alert is
        live) — hiding it would conceal whether a ticker is already alerted,
        the same reasoning that kept Set Alert on the pick card.
      * 📊 was redundant: tapping the ticker already opens the chart.
      * 🗑 Remove is destructive, so a sheet is the safer home for it.
    """

    @property
    def _src(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "miniapp", "index.html")) as f:
            return f.read()

    def _group(self):
        s = self._src
        i = s.index('<div style="display:flex;gap:8px;flex:0 0 auto">')
        return s[i:s.index("</div>\n      </div>`;", i)]

    def test_only_two_controls_stay_inline(self):
        """Measured live: 78px instead of 164px."""
        grp = self._group()
        assert grp.count("<button") == 2, \
            f"{grp.count('<button')} inline buttons — the row cannot carry more at 375px"

    def test_the_alert_bell_is_one_of_them(self):
        grp = self._group()
        assert "openWatchAlert(" in grp, \
            "the bell shows alert STATE; behind a sheet that state is invisible"
        assert "var(--amber)" in grp, "the active-alert colour must stay on the row"

    def test_the_overflow_menu_is_the_other(self):
        assert "openWatchMore(" in self._group()

    def test_the_sheet_offers_every_action_that_left_the_row(self):
        s = self._src
        i = s.index('id="watchmore-overlay"')
        sheet = s[i:s.index("</div>\n</div>", i)]
        for needed in ("View chart", "Add to portfolio", "price alert", "Remove from watchlist"):
            assert needed in sheet, f"{needed!r} is unreachable — it left the row with no home"

    def test_the_alert_label_reflects_existing_state(self):
        assert "'Edit price alert' : 'Set price alert'" in self._src

    def test_the_sparkline_is_no_longer_suppressed(self):
        """It was hidden only because four buttons took 47% of the row. Two give
        ~86px back, so [ticker][price][sparkline] fits in ~205px of ~237px."""
        s = self._src
        assert ".watch-spark { display: none; }" not in s, \
            "the sparkline should fit again now the row is not button-bound"


class TestTabHelp:
    """Per-tab explanation strips (Aug 18).

    Owner asked for a tooltip per tab, having found figures like "At Risk ·
    6/100" and "STREAK ↑3W" unexplained anywhere in the app.
    """

    @property
    def _src(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "miniapp", "index.html")) as f:
            return f.read()

    def test_every_tab_has_an_entry(self):
        s = self._src
        block = s[s.index("const _TAB_HELP = {"):s.index("const _TABHELP", s.index("const _TAB_HELP = {")) if "const _TABHELP" in s else s.index("function toggleTabHelp")]
        for tab in ("picks", "portfolio", "performance", "watchlist", "settings"):
            assert f"{tab}: {{" in block, f"{tab} has no help entry"

    def test_it_does_NOT_auto_expand(self):
        """The first build opened it on first visit and produced a wall of text
        above the fold — adding to the overwhelm it was meant to relieve."""
        s = self._src
        assert "DELIBERATELY does not auto-expand" in s
        assert "sp_tabhelp_seen" not in s, "dead first-run state should be gone"

    def test_the_strip_reads_as_a_control(self):
        import re
        m = re.search(r"\.tabhelp \{(.*?)\}", self._src, re.S)
        css = re.sub(r"/\*.*?\*/", "", m.group(1), flags=re.S)
        assert "var(--card-hover)" in css, "a hardcoded white overlay is invisible in light mode"
        assert "min-height" in css, "the tap target must be declared, not emergent"
        assert "cursor: pointer" in css

    def test_state_is_exposed(self):
        s = self._src
        assert 'aria-expanded="false"' in s
        assert "btn.setAttribute('aria-expanded'" in s

    # ── the guard that actually matters ──────────────────────────────────────
    def test_the_health_score_explanation_matches_the_CODE(self):
        """🔴 An explanation that misstates a number is worse than none.

        The penalties are computed in one place and described in another; if the
        computation changes, the help text silently becomes a lie. Pin them
        together.
        """
        s = self._src
        # Slice through the LABEL logic too — it sits after _hColor, and ending
        # the window early made this assertion fail for the wrong reason.
        calc = s[s.index("let _health = 100;"):s.index("${_hLabel} ·")]
        help_txt = s[s.index("portfolio: { line:"):s.index("performance: { line:")]

        # (penalty per position, cap) as written in the calculation
        for per, cap in (("12", "40"), ("8", "24"), ("5", "15")):
            assert f"_noStop * 12" in calc or per in calc
            assert f"<b>{per}</b>" in help_txt, f"penalty {per} is not explained"
            assert f"max {cap}" in help_txt, f"cap {cap} is not explained"
        assert "Math.min(40, _noStop * 12)" in calc
        assert "Math.min(24, _nearStop * 8)" in calc
        assert "Math.min(15, _stale * 5)" in calc
        assert "_health -= 15" in calc and "60%+" in help_txt, \
            "the concentration penalty must stay described"
        assert "_health >= 50 ? 'Fair'" in calc and 'Under 50 reads "At Risk"' in help_txt

    def test_the_streak_explanation_matches_the_CODE(self):
        s = self._src
        calc = s[s.index("const s = d.current_streak || 0;"):s.index("streakEl.style.color")]
        assert "'↑' + s + 'W'" in calc and "'↓' + Math.abs(s) + 'L'" in calc
        help_txt = s[s.index("portfolio: { line:"):s.index("performance: { line:")]
        assert "↑3W" in help_txt and "↓2L" in help_txt

    def test_the_long_term_stop_explanation_matches_agent(self):
        """LT_INVALIDATION_PCT is 15 in agent.py; the help text says 15%."""
        import os, re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "agent.py")) as f:
            agent = f.read()
        m = re.search(r"LT_INVALIDATION_PCT\s*=\s*([\d.]+)", agent)
        assert m, "LT_INVALIDATION_PCT not found"
        pct = str(int(float(m.group(1))))
        assert f"invalidation alert {pct}% below entry" in self._src, \
            f"help says a different number than agent's {pct}%"
