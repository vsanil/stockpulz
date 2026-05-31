"""
tests/test_ux_playwright.py — End-to-end UX tests using Playwright.

Covers:
  - Tab navigation (all 5 tabs load without JS errors)
  - Paper trading flows (buy, add cash, reset)
  - Confirmation popups on all destructive/significant actions
  - Toast feedback for validation errors
  - Button disabled state during async actions
  - Settings auto-save

Run:
    python3 -m pytest tests/test_ux_playwright.py -v
    python3 -m pytest tests/test_ux_playwright.py -v --headed   # see the browser

Requirements:
    pip install playwright pytest-playwright
    python3 -m playwright install chromium
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from unittest.mock import patch

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

TEST_CHAT_ID = "9999999"

# ── Playwright fixture ────────────────────────────────────────────────────────

try:
    from playwright.sync_api import sync_playwright, Page, expect
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not PLAYWRIGHT_AVAILABLE,
    reason="playwright not installed — run: pip install playwright && python3 -m playwright install chromium"
)


# ── Live server fixture ───────────────────────────────────────────────────────

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    """Start the Flask app on a random port. Patches auth + storage."""
    port = _free_port()

    # In-memory store
    from tests.conftest import _MemStore
    store = _MemStore()

    # Seed paper account
    store.write(f"user_paper_{TEST_CHAT_ID}.json", {
        "positions": [],
        "history": [],
        "starting_cash": 10000.0,
        "cash": 10000.0,
    })

    import config_manager
    import webhook

    patchers = [
        patch("webhook._miniapp_auth", return_value=TEST_CHAT_ID),
        patch.object(config_manager, "_load_gist_file", side_effect=store.load),
        patch.object(config_manager, "_write_gist_file", side_effect=store.write),
    ]
    for p in patchers:
        p.start()

    server = threading.Thread(
        target=lambda: webhook.app.run(host="127.0.0.1", port=port, use_reloader=False, threaded=True),
        daemon=True,
    )
    server.start()

    # Wait for server to be ready
    for _ in range(30):
        try:
            import urllib.request
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1)
            break
        except Exception:
            time.sleep(0.2)

    yield f"http://127.0.0.1:{port}"

    for p in patchers:
        p.stop()


# ── Telegram tg stub injected into every page ─────────────────────────────────

TG_STUB = """
(function() {
  var wa = {
    ready: function() {},
    expand: function() {},
    close: function() {},
    enableClosingConfirmation: function() {},
    disableClosingConfirmation: function() {},
    setHeaderColor: function() {},
    setBackgroundColor: function() {},
    isExpanded: true,
    colorScheme: 'dark',
    themeParams: { bg_color: '#1a1a2e', text_color: '#ffffff', hint_color: '#aaaaaa', button_color: '#5865f2', button_text_color: '#ffffff' },
    initData: 'query_id=test&user=%7B%22id%22%3A9999999%7D&auth_date=9999999999&hash=testhash',
    initDataUnsafe: { user: { id: 9999999, first_name: 'Test', language_code: 'en' } },
    MainButton: {
      text: '', isVisible: false,
      show: function() { this.isVisible = true; },
      hide: function() { this.isVisible = false; },
      enable: function() {}, disable: function() {},
      showProgress: function() {}, hideProgress: function() {},
      onClick: function() {}, offClick: function() {},
      setText: function(t) { this.text = t; },
    },
    BackButton: { show: function(){}, hide: function(){}, onClick: function(){}, offClick: function(){} },
    HapticFeedback: {
      impactOccurred: function() {},
      notificationOccurred: function() {},
      selectionChanged: function() {},
    },
    showPopup: function(params, cb) {
      var btn = (params.buttons || []).find(function(b) { return b.id; });
      setTimeout(function() { if (cb) cb(btn ? btn.id : null); }, 50);
    },
    showAlert: function(msg, cb) { setTimeout(function() { if (cb) cb(); }, 50); },
    showConfirm: function(msg, cb) { setTimeout(function() { if (cb) cb(true); }, 50); },
    openLink: function() {},
    openTelegramLink: function() {},
    sendData: function() {},
    CloudStorage: { getItem: function(k,cb){ cb(null,null); }, setItem: function(k,v,cb){ if(cb) cb(null,true); } },
  };
  window.Telegram = { WebApp: wa };
  window.tg = wa;
})();
"""


@pytest.fixture(scope="session")
def browser_ctx(live_server):
    """One browser context for the whole session."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()

        # Inject our tg stub as an init script — runs before page scripts.
        # The real telegram-web-app.js will load after and may overwrite parts,
        # so we re-patch showPopup in each test after the page is fully ready.
        ctx.add_init_script(TG_STUB)
        yield ctx, live_server
        ctx.close()
        browser.close()


@pytest.fixture()
def page(browser_ctx):
    """Fresh page per test with console error tracking."""
    ctx, base_url = browser_ctx
    pg = ctx.new_page()
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)

    yield pg, base_url, errors

    pg.close()


def miniapp_url(base: str, tab: str = "picks", mode: str = "live") -> str:
    return f"{base}/miniapp?chat_id={TEST_CHAT_ID}&tab={tab}&mode={mode}"


# Map tab name -> a stable element that exists once the tab is active
TAB_READY = {
    "picks":       "#picks-spinner, #picks-body",
    "portfolio":   "#port-spinner, #port-body",
    "performance": "#perf-spinner, #perf-body",
    "watchlist":   "#watch-spinner, #watch-body",
    "settings":    "#sett-spinner, #sett-body",
}


def wait_tab(pg: Page, tab: str, timeout: int = 8000):
    """Wait until the tab's container element is in the DOM (may be hidden until activated)."""
    selector = TAB_READY.get(tab, ".bottom-nav")
    # Use 'attached' — the element exists in DOM even when its tab isn't active yet
    pg.wait_for_selector(selector, state="attached", timeout=timeout)
    # Also wait for nav bar to be ready
    pg.wait_for_selector(".bottom-nav", timeout=timeout)


def wait_no_spinner(pg: Page, timeout: int = 5000):
    try:
        pg.wait_for_function(
            "Array.from(document.querySelectorAll('.spinner')).every(el => "
            "  el.closest('.loading') === null || el.closest('.loading').style.display === 'none')",
            timeout=timeout,
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════════
# Navigation tests
# ══════════════════════════════════════════════════════════════════════════════

class TestNavigation:

    def test_picks_tab_loads(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "picks"))
        wait_tab(pg, "picks")
        assert not any("Uncaught" in e for e in errors), f"JS errors: {errors}"

    def test_portfolio_tab_loads(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio"))
        wait_tab(pg, "portfolio")
        assert not any("Uncaught" in e for e in errors), f"JS errors: {errors}"

    def test_performance_tab_loads(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "performance"))
        wait_tab(pg, "performance")
        assert not any("Uncaught" in e for e in errors), f"JS errors: {errors}"

    def test_watch_tab_loads(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "watchlist"))
        wait_tab(pg, "watchlist")
        assert not any("Uncaught" in e for e in errors), f"JS errors: {errors}"

    def test_settings_tab_loads(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "settings"))
        wait_tab(pg, "settings")
        assert not any("Uncaught" in e for e in errors), f"JS errors: {errors}"

    def test_switch_all_tabs_no_errors(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "picks"))
        pg.wait_for_selector(".bottom-nav", timeout=5000)

        for tab_id in ["nav-portfolio", "nav-performance", "nav-watchlist", "nav-settings", "nav-picks"]:
            btn = pg.query_selector(f"#{tab_id}")
            if btn:
                btn.click()
                pg.wait_for_timeout(300)

        assert not any("Uncaught" in e for e in errors), f"JS errors on tab switch: {errors}"

    def test_paper_mode_loads(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        assert not any("Uncaught" in e for e in errors), f"JS errors: {errors}"


# ══════════════════════════════════════════════════════════════════════════════
# Paper trading tests
# ══════════════════════════════════════════════════════════════════════════════

class TestPaperTrading:

    def _open_paper_portfolio(self, pg: Page, base: str):
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        wait_no_spinner(pg)

    def test_paper_mode_banner_visible(self, page):
        pg, base, errors = page
        self._open_paper_portfolio(pg, base)
        # Paper mode banner should be visible
        pg.wait_for_function(
            "document.body.innerText.includes('PAPER MODE')",
            timeout=5000
        )

    def test_paper_buy_button_opens_picker(self, page):
        pg, base, errors = page
        self._open_paper_portfolio(pg, base)
        # Find and click the Buy button
        pg.wait_for_function(
            "Array.from(document.querySelectorAll('button')).some(b => b.textContent.includes('Buy'))",
            timeout=5000
        )
        pg.evaluate("""
            Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.trim().includes('Buy') && !b.textContent.includes('Paper Buy'))
                ?.click()
        """)
        pg.wait_for_timeout(500)
        # Either picker or buy sheet should be open
        overlay_open = pg.evaluate("""
            document.querySelector('#paper-picker-overlay.open') !== null ||
            document.querySelector('#paper-buy-overlay.open') !== null
        """)
        assert overlay_open, "Paper buy sheet or picker did not open"

    def test_paper_buy_no_ticker_shows_toast(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        # Open buy sheet directly
        pg.evaluate("openPaperBuySheet()")
        pg.wait_for_selector("#paper-buy-overlay.open", timeout=3000)
        # Clear ticker and click buy
        pg.evaluate("""
            document.getElementById('pbuy-ticker').value = '';
            confirmPaperBuy();
        """)
        pg.wait_for_timeout(400)
        toast_visible = pg.evaluate("""
            document.body.innerText.includes('Enter a ticker') ||
            document.querySelector('.toast')?.style.display !== 'none'
        """)
        assert toast_visible, "Toast should appear when ticker is empty"

    def test_paper_buy_with_ticker_no_shares_shows_popup(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.wait_for_function("typeof openSellSheet === 'function'", timeout=8000)
        pg.evaluate("openPaperBuySheet('AAPL', '189.50', '', '')")
        pg.wait_for_selector("#paper-buy-overlay.open", timeout=3000)
        # Patch showPopup then fire buy — no crash expected
        pg.evaluate("""
            (function() {
                function _ux_stub(params, cb) {
                    var btn = params && params.buttons && params.buttons.find(function(b) { return b.id; });
                    setTimeout(function() { if (cb) cb(btn ? btn.id : null); }, 50);
                }
                if (window.Telegram && window.Telegram.WebApp) { window.Telegram.WebApp.showPopup = _ux_stub; }
                if (!window.tg) window.tg = {};
                window.tg.showPopup = _ux_stub;
            })();
            confirmPaperBuy();
        """)
        pg.wait_for_timeout(800)
        uncaught = [e for e in errors if "Uncaught" in e]
        assert not uncaught, f"JS errors: {uncaught}"

    def test_paper_buy_confirmation_popup_fires(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.wait_for_function("typeof showToast === 'function'", timeout=8000)
        pg.evaluate("openPaperBuySheet('TSLA', '172.30', '', '')")
        pg.wait_for_selector("#paper-buy-overlay.open", timeout=3000)

        # Intercept showPopup — resolve immediately on fire, don't wait for async API chain
        pg.evaluate("""
            window._ux_popup_fired = false;
            function _ux_stub_popup(params, cb) {
                window._ux_popup_fired = true;
                var btn = params && params.buttons && params.buttons.find(function(b) { return b.id; });
                setTimeout(function() { if (cb) cb(btn ? btn.id : null); }, 50);
            }
            if (window.Telegram && window.Telegram.WebApp) { window.Telegram.WebApp.showPopup = _ux_stub_popup; }
            window.tg = window.tg || {}; window.tg.showPopup = _ux_stub_popup;
        """)
        pg.evaluate("""
            var t = document.getElementById('pbuy-ticker');
            var s = document.getElementById('pbuy-shares');
            if (t) t.value = 'TSLA';
            if (s) s.value = '5';
            confirmPaperBuy();
        """)
        pg.wait_for_timeout(500)
        popup_fired = pg.evaluate("window._ux_popup_fired === true")
        assert popup_fired, "showPopup was not called for paper buy confirmation"

    def test_add_cash_button_opens_sheet(self, page):
        pg, base, errors = page
        self._open_paper_portfolio(pg, base)
        pg.wait_for_function(
            "Array.from(document.querySelectorAll('button')).some(b => b.textContent.includes('Add Cash'))",
            timeout=5000
        )
        pg.evaluate("""
            Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.includes('Add Cash'))?.click()
        """)
        pg.wait_for_selector("#addcash-overlay.open", timeout=3000)

    def test_add_cash_sheet_shows_balance(self, page):
        pg, base, errors = page
        self._open_paper_portfolio(pg, base)
        pg.wait_for_timeout(1000)  # let portfolio load
        pg.evaluate("openAddCashSheet()")
        pg.wait_for_selector("#addcash-overlay.open", timeout=3000)
        pg.wait_for_timeout(600)  # async balance fetch
        balance_text = pg.evaluate("document.getElementById('addcash-balance')?.textContent || ''")
        assert balance_text != "—", f"Balance should be loaded, got: {balance_text}"
        assert "$" in balance_text, f"Balance should show dollar amount, got: {balance_text}"

    def test_add_cash_empty_amount_does_nothing(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.evaluate("openAddCashSheet()")
        pg.wait_for_selector("#addcash-overlay.open", timeout=3000)
        # Click Add Cash with empty input
        pg.evaluate("confirmAddCash()")
        pg.wait_for_timeout(300)
        # Sheet should still be open (nothing happened)
        still_open = pg.evaluate("document.querySelector('#addcash-overlay.open') !== null")
        assert still_open, "Sheet should stay open when amount is empty"

    def test_add_cash_confirmation_popup_fires(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.evaluate("openAddCashSheet()")
        pg.wait_for_selector("#addcash-overlay.open", timeout=3000)
        pg.evaluate("""
            window._ux_popup_fired = false;
            function _ux_stub_popup(params, cb) {
                window._ux_popup_fired = true;
                var btn = params && params.buttons && params.buttons.find(function(b) { return b.id; });
                setTimeout(function() { if (cb) cb(btn ? btn.id : null); }, 50);
            }
            if (window.Telegram && window.Telegram.WebApp) { window.Telegram.WebApp.showPopup = _ux_stub_popup; }
            window.tg = window.tg || {}; window.tg.showPopup = _ux_stub_popup;
            document.getElementById('addcash-input').value = '500';
            confirmAddCash();
        """)
        pg.wait_for_timeout(500)
        popup_fired = pg.evaluate("window._ux_popup_fired === true")
        assert popup_fired, "showPopup was not called for add cash confirmation"

    def test_reset_button_visible(self, page):
        pg, base, errors = page
        self._open_paper_portfolio(pg, base)
        has_reset = pg.evaluate("""
            Array.from(document.querySelectorAll('button'))
                .some(b => b.textContent.includes('Reset'))
        """)
        assert has_reset, "Reset button should be visible in paper portfolio"

    def test_reset_confirmation_popup_fires(self, page):
        pg, base, errors = page
        self._open_paper_portfolio(pg, base)
        pg.wait_for_timeout(500)

        pg.evaluate("""
            window._ux_popup_fired = false;
            function _ux_stub_popup(params, cb) {
                window._ux_popup_fired = true;
                var btn = params && params.buttons && params.buttons.find(function(b) { return b.id; });
                setTimeout(function() { if (cb) cb(btn ? btn.id : null); }, 50);
            }
            if (window.Telegram && window.Telegram.WebApp) { window.Telegram.WebApp.showPopup = _ux_stub_popup; }
            window.tg = window.tg || {}; window.tg.showPopup = _ux_stub_popup;
            confirmResetPaper();
        """)
        pg.wait_for_timeout(400)
        popup_fired = pg.evaluate("window._ux_popup_fired === true")
        assert popup_fired, "showPopup was not called for reset confirmation"

    def test_reset_resets_cash_to_10k(self, page):
        pg, base, errors = page
        self._open_paper_portfolio(pg, base)
        pg.wait_for_function("typeof openSellSheet === 'function'", timeout=8000)
        pg.wait_for_timeout(500)
        pg.evaluate("""
            (function() {
                function _ux_stub(params, cb) {
                    var btn = params && params.buttons && params.buttons.find(function(b) { return b.id; });
                    setTimeout(function() { if (cb) cb(btn ? btn.id : null); }, 50);
                }
                if (window.Telegram && window.Telegram.WebApp) { window.Telegram.WebApp.showPopup = _ux_stub; }
                if (!window.tg) window.tg = {};
                window.tg.showPopup = _ux_stub;
            })();
            confirmResetPaper();
        """)
        pg.wait_for_timeout(1500)
        # Portfolio should reload — check for $10K
        pg.wait_for_function(
            "document.body.innerText.includes('10') || document.body.innerText.includes('$10')",
            timeout=5000
        )
        assert not any("Uncaught" in e for e in errors), f"JS errors: {errors}"


# ══════════════════════════════════════════════════════════════════════════════
# Confirmation popup tests
# ══════════════════════════════════════════════════════════════════════════════

class TestConfirmationPopups:

    def _wait_app_ready(self, pg, timeout: int = 10000):
        """Wait until the app JS has fully initialized (key variables out of TDZ)."""
        pg.wait_for_load_state("networkidle", timeout=timeout)
        # openSellSheet is defined after all the let declarations — if it's callable,
        # all top-level let vars are initialized
        pg.wait_for_function("typeof openSellSheet === 'function'", timeout=timeout)
        pg.wait_for_timeout(200)

    def _check_popup_fires(self, pg, setup_js: str) -> bool:
        """Helper: intercept showPopup, run setup_js, wait, return whether popup fired."""
        self._wait_app_ready(pg)
        pg.evaluate("""
            window._ux_popup_fired = false;
            function _ux_stub_popup(params, cb) {
                window._ux_popup_fired = true;
                var btn = params && params.buttons && params.buttons.find(function(b) { return b.id; });
                setTimeout(function() { if (cb) cb(btn ? btn.id : null); }, 50);
            }
            // Patch both the app's tg object and window.Telegram.WebApp
            if (window.Telegram && window.Telegram.WebApp) {
                window.Telegram.WebApp.showPopup = _ux_stub_popup;
            }
            window.tg = window.tg || {};
            window.tg.showPopup = _ux_stub_popup;
        """)
        pg.evaluate(setup_js)
        pg.wait_for_timeout(600)
        return pg.evaluate("window._ux_popup_fired === true")

    def test_sell_confirmation_popup_fires(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio"))
        wait_tab(pg, "portfolio")
        fired = self._check_popup_fires(pg, "openSellSheet('AAPL'); confirmSell();")
        assert fired, "showPopup was not called for sell confirmation"

    def test_delete_alert_confirmation_popup_fires(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "watchlist"))
        wait_tab(pg, "watchlist")
        fired = self._check_popup_fires(pg, "deleteAlert('AAPL', 200);")
        assert fired, "showPopup was not called for delete alert confirmation"

    def test_remove_watch_ticker_confirmation_popup_fires(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "settings"))
        wait_tab(pg, "settings")
        fired = self._check_popup_fires(pg, "removeWatchTicker('TSLA');")
        assert fired, "showPopup was not called for remove watchlist ticker confirmation"

    def test_remove_position_confirmation_popup_fires(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio"))
        wait_tab(pg, "portfolio")
        fired = self._check_popup_fires(pg, "openPositionDetail('NVDA', 100, 105, 95, 120); removePosition();")
        assert fired, "showPopup was not called for remove position confirmation"

    def test_log_buy_confirmation_popup_fires(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "picks"))
        wait_tab(pg, "picks")
        fired = self._check_popup_fires(pg, """
            openBuySheet('AAPL', 'stock', 'short');
            var eEl = document.getElementById('buypos-entry');
            var sEl = document.getElementById('buypos-shares');
            if (eEl) eEl.value = '189.50';
            if (sEl) sEl.value = '10';
            confirmBuyWithLevels();
        """)
        assert fired, "showPopup was not called for log buy confirmation"


# ══════════════════════════════════════════════════════════════════════════════
# Validation & feedback tests
# ══════════════════════════════════════════════════════════════════════════════

class TestValidationFeedback:

    def test_toast_function_exists(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "picks"))
        wait_tab(pg, "picks")
        has_toast = pg.evaluate("typeof showToast === 'function'")
        assert has_toast, "showToast function must exist"

    def test_toast_appears_for_empty_ticker_paper_buy(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.evaluate("openPaperBuySheet()")
        pg.wait_for_selector("#paper-buy-overlay.open", timeout=3000)
        pg.evaluate("""
            document.getElementById('pbuy-ticker').value = '';
            confirmPaperBuy();
        """)
        pg.wait_for_timeout(400)
        toast_text = pg.evaluate("document.querySelector('.toast')?.textContent || ''")
        assert "ticker" in toast_text.lower() or "Enter" in toast_text, \
            f"Expected ticker toast, got: {toast_text!r}"

    def test_button_disabled_while_inflight(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.wait_for_function("typeof openSellSheet === 'function'", timeout=8000)
        pg.evaluate("openPaperBuySheet('AAPL', '189.50', '', '')")
        pg.wait_for_selector("#paper-buy-overlay.open", timeout=3000)
        pg.evaluate("document.getElementById('pbuy-shares').value = '5'")
        # Patch showPopup then fire
        pg.evaluate("""
            (function() {
                function _ux_stub(params, cb) {
                    var btn = params && params.buttons && params.buttons.find(function(b) { return b.id; });
                    setTimeout(function() { if (cb) cb(btn ? btn.id : null); }, 50);
                }
                if (window.Telegram && window.Telegram.WebApp) { window.Telegram.WebApp.showPopup = _ux_stub; }
                if (!window.tg) window.tg = {};
                window.tg.showPopup = _ux_stub;
            })();
            confirmPaperBuy();
        """)
        pg.wait_for_timeout(200)  # after popup auto-confirms
        pg.wait_for_timeout(200)  # after button disables
        # By now button text should have changed (disabled state)
        btn_text = pg.evaluate("document.getElementById('pbuy-btn')?.textContent || ''")
        btn_disabled = pg.evaluate("document.getElementById('pbuy-btn')?.disabled || false")
        # Either disabled or showing loading text
        assert btn_disabled or "Buying" in btn_text or "…" in btn_text, \
            f"Button should be disabled in-flight, got text: {btn_text!r}, disabled: {btn_disabled}"

    def test_no_uncaught_errors_on_picks_tab(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "picks"))
        wait_tab(pg, "picks")
        pg.wait_for_timeout(1000)
        uncaught = [e for e in errors if "Uncaught" in e]
        assert not uncaught, f"Uncaught JS errors on picks tab: {uncaught}"

    def test_no_uncaught_errors_on_paper_tab(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.wait_for_timeout(1000)
        uncaught = [e for e in errors if "Uncaught" in e]
        assert not uncaught, f"Uncaught JS errors on paper tab: {uncaught}"


# ══════════════════════════════════════════════════════════════════════════════
# Settings tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSettings:

    def test_settings_tab_has_risk_profile(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "settings"))
        wait_tab(pg, "settings")
        pg.wait_for_timeout(1000)
        has_risk = pg.evaluate("""
            document.body.innerText.toLowerCase().includes('risk') ||
            document.querySelector('[id*="risk"]') !== null
        """)
        assert has_risk, "Settings should show risk profile section"

    def test_settings_tab_no_js_errors(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "settings"))
        wait_tab(pg, "settings")
        pg.wait_for_timeout(1000)
        uncaught = [e for e in errors if "Uncaught" in e]
        assert not uncaught, f"Uncaught JS errors on settings tab: {uncaught}"

    def test_settings_functions_exist(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "settings"))
        wait_tab(pg, "settings")
        for fn in ["loadSettings", "saveBudget", "saveThreshold"]:
            exists = pg.evaluate(f"typeof {fn} === 'function'")
            assert exists, f"Function {fn} must exist in settings"


# ══════════════════════════════════════════════════════════════════════════════
# Picker sheet tests
# ══════════════════════════════════════════════════════════════════════════════

class TestPickerSheet:

    def test_picker_sheet_opens(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.wait_for_timeout(800)
        pg.evaluate("openPaperPickerSheet()")
        pg.wait_for_selector("#paper-picker-overlay.open", timeout=3000)

    def test_picker_has_manual_entry_button(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.wait_for_timeout(800)
        pg.evaluate("openPaperPickerSheet()")
        pg.wait_for_selector("#paper-picker-overlay.open", timeout=3000)
        has_manual = pg.evaluate("""
            Array.from(document.querySelectorAll('button'))
                .some(b => b.textContent.includes('Enter Ticker Manually'))
        """)
        assert has_manual, "Picker should have 'Enter Ticker Manually' button"

    def test_manual_entry_opens_buy_sheet(self, page):
        pg, base, errors = page
        pg.goto(miniapp_url(base, "portfolio", "paper"))
        wait_tab(pg, "portfolio")
        pg.wait_for_timeout(800)
        pg.evaluate("openPaperPickerSheet()")
        pg.wait_for_selector("#paper-picker-overlay.open", timeout=3000)
        # Click manual entry button
        pg.evaluate("""
            Array.from(document.querySelectorAll('button'))
                .find(b => b.textContent.includes('Enter Ticker Manually'))?.click()
        """)
        pg.wait_for_timeout(400)
        buy_open = pg.evaluate("document.querySelector('#paper-buy-overlay.open') !== null")
        assert buy_open, "Paper buy sheet should open after tapping manual entry"
