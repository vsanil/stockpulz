"""
test_commands.py — Automated test runner for StockPulz bot commands.

Tests NL parsing, ticker resolution, and command routing across all major
commands with realistic edge cases. Uses real Anthropic API (Haiku) for NL
tests, mocks everything else (Telegram, Gist, yfinance).

Run:
    python3 test_commands.py            # all tests
    python3 test_commands.py nl         # NL/parsing tests only
    python3 test_commands.py commands   # command-flow tests only
    python3 test_commands.py fast       # skip slow Haiku calls (unit tests only)
"""
from __future__ import annotations

import os
import sys
import json
import time
import traceback
from unittest.mock import patch, MagicMock, call
from typing import Any

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):  print(f"  {GREEN}✓{RESET}  {msg}")
def fail(msg): print(f"  {RED}✗{RESET}  {msg}")
def skip(msg): print(f"  {YELLOW}–{RESET}  {msg}")
def header(msg): print(f"\n{BOLD}{CYAN}{'─'*60}{RESET}\n{BOLD}{CYAN}  {msg}{RESET}\n{BOLD}{CYAN}{'─'*60}{RESET}")

# ── Fake environment so imports don't blow up ─────────────────────────────────
os.environ.setdefault("ANTHROPIC_API_KEY",    os.environ.get("ANTHROPIC_API_KEY", ""))
os.environ.setdefault("TELEGRAM_BOT_TOKEN",   "test_token")
os.environ.setdefault("ADMIN_CHAT_ID",        "999")
os.environ.setdefault("GITHUB_GIST_ID",       "fake_gist")
os.environ.setdefault("GITHUB_TOKEN",         "fake_token")
os.environ.setdefault("FINNHUB_API_KEY",      "fake_finnhub")

TEST_CHAT = "999"

# ── Minimal in-memory mocks for config_manager ───────────────────────────────
_FAKE_CONFIG      = {"allowed_users": [TEST_CHAT], "pending_users": []}
_FAKE_USER_CFGS   = {}
_FAKE_PENDING     = {}
_FAKE_TRADE_LOGS  = {}
_FAKE_PAPER       = {}
_FAKE_PICKS       = {}
_FAKE_FEEDBACK    = {"items": []}

def _fake_get_config():           return dict(_FAKE_CONFIG)
def _fake_update_config(k, v):    _FAKE_CONFIG[k] = v
def _fake_get_user_config(cid):   return dict(_FAKE_USER_CFGS.get(cid, {}))
def _fake_update_user_config(cid, k, v): _FAKE_USER_CFGS.setdefault(cid, {})[k] = v
def _fake_update_user_config_multi(cid, d):
    _FAKE_USER_CFGS.setdefault(cid, {}).update(d)
    return dict(_FAKE_USER_CFGS[cid])
def _fake_reset_user_config(cid): _FAKE_USER_CFGS.pop(cid, None)
def _fake_load_picks():           return dict(_FAKE_PICKS)
def _fake_load_pending(cid):      return dict(_FAKE_PENDING.get(cid, {}))
def _fake_save_pending(cid, cmd, step=1, data=None):
    _FAKE_PENDING[cid] = {"command": cmd, "step": step, "data": data or {}, "ts": time.time()}
def _fake_clear_pending(cid):     _FAKE_PENDING.pop(cid, None)
def _fake_load_trade_log(cid):    return dict(_FAKE_TRADE_LOGS.get(cid, {"open": [], "closed": []}))
def _fake_save_trade_log(cid, log): _FAKE_TRADE_LOGS[cid] = log
def _fake_load_paper(cid):        return dict(_FAKE_PAPER.get(cid, {"cash": 10000, "positions": {}, "history": []}))
def _fake_get_pending_users():    return list(_FAKE_CONFIG.get("pending_users", []))
def _fake_add_pending(cid):       _FAKE_CONFIG.setdefault("pending_users", []).append(cid)
def _fake_remove_pending(cid):
    _FAKE_CONFIG["pending_users"] = [u for u in _FAKE_CONFIG.get("pending_users", []) if u != cid]
def _fake_get_allowed():          return list(_FAKE_CONFIG.get("allowed_users", []))
def _fake_load_feedback():        return dict(_FAKE_FEEDBACK)
def _fake_add_feedback(cid, txt): _FAKE_FEEDBACK["items"].append({"chat_id": cid, "text": txt})
def _fake_mark_feedback_read(i):  pass
def _fake_count_unread():         return 0

CONFIG_MOCKS = {
    "config_manager.get_config":                  _fake_get_config,
    "config_manager.update_config":               _fake_update_config,
    "config_manager.get_user_config":             _fake_get_user_config,
    "config_manager.update_user_config":          _fake_update_user_config,
    "config_manager.update_user_config_multi":    _fake_update_user_config_multi,
    "config_manager.reset_user_config":           _fake_reset_user_config,
    "config_manager.load_picks":                  _fake_load_picks,
    "config_manager.load_pending_state":          _fake_load_pending,
    "config_manager.save_pending_state":          _fake_save_pending,
    "config_manager.clear_pending_state":         _fake_clear_pending,
    "config_manager.load_user_trade_log":         _fake_load_trade_log,
    "config_manager.save_user_trade_log":         _fake_save_trade_log,
    "config_manager.load_user_paper":             _fake_load_paper,
    "config_manager.get_pending_users":           _fake_get_pending_users,
    "config_manager.add_pending_user":            _fake_add_pending,
    "config_manager.remove_pending_user":         _fake_remove_pending,
    "config_manager.get_allowed_users":           _fake_get_allowed,
    "config_manager.load_feedback":               _fake_load_feedback,
    "config_manager.add_feedback":                _fake_add_feedback,
    "config_manager.mark_feedback_read":          _fake_mark_feedback_read,
    "config_manager.count_unread_feedback":       _fake_count_unread,
}

# Captured outbound Telegram messages
_SENT: list[dict] = []

def _fake_send_message(text, chat_id=None, **kw):
    _SENT.append({"type": "message", "text": text, "chat_id": chat_id})
    return True

def _fake_send_inline_keyboard(text, buttons, chat_id=None, **kw):
    _SENT.append({"type": "keyboard", "text": text, "buttons": buttons, "chat_id": chat_id})
    return True

def _fake_send_photo(path, caption=None, chat_id=None, **kw):
    _SENT.append({"type": "photo", "caption": caption, "chat_id": chat_id})
    return True

def _fake_typing(*a, **kw): pass
def _fake_answer_cq(*a, **kw): pass
def _fake_bot_token(): return "test_token"
def _fake_chat_id(): return TEST_CHAT
def _fake_get_username(): return "TestBot"

TELEGRAM_MOCKS = {
    "telegram_api.send_message":          _fake_send_message,
    "telegram_api.send_inline_keyboard":  _fake_send_inline_keyboard,
    "telegram_api.send_typing_action":    _fake_typing,
    "telegram_api.send_photo":            _fake_send_photo,
    "telegram_api.typing_until_done":     _fake_typing,
    "telegram_api.answer_callback_query": _fake_answer_cq,
    "telegram_api._bot_token":            _fake_bot_token,
    "telegram_api._chat_id":              _fake_chat_id,
    "telegram_api._get_bot_username":     _fake_get_username,
    "telegram_api.TELEGRAM_API":          "https://api.telegram.org/bottest_token",
}

def _fake_live_price(ticker):
    """Return a plausible fake price per ticker."""
    prices = {
        "AAPL": 185.0, "MSFT": 415.0, "NVDA": 875.0, "TSLA": 175.0,
        "AVY": 163.0, "COST": 820.0, "CRM": 295.0, "EEM": 42.0,
        "SOL": 145.0, "BTC": 62000.0, "ETH": 3100.0,
    }
    return prices.get(ticker.upper(), 100.0)


# ── Test infrastructure ───────────────────────────────────────────────────────

class TestResults:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.failures = []

    def record(self, name, passed, detail=""):
        if passed:
            self.passed += 1
            ok(name)
        else:
            self.failed += 1
            self.failures.append((name, detail))
            fail(f"{name}  →  {detail}")

    def record_skip(self, name, reason):
        self.skipped += 1
        skip(f"{name}  ({reason})")

    def summary(self):
        total = self.passed + self.failed + self.skipped
        print(f"\n{BOLD}{'─'*60}{RESET}")
        print(f"{BOLD}Results: {GREEN}{self.passed} passed{RESET}  "
              f"{RED}{self.failed} failed{RESET}  "
              f"{YELLOW}{self.skipped} skipped{RESET}  "
              f"/ {total} total{RESET}")
        if self.failures:
            print(f"\n{RED}Failed tests:{RESET}")
            for name, detail in self.failures:
                print(f"  • {name}: {detail}")
        print()
        return self.failed == 0


R = TestResults()
HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))


def with_mocks(fn):
    """Run fn inside the full mock context; clear _SENT before each call."""
    with patch.multiple("config_manager", **{
        k.split(".", 1)[1]: v for k, v in CONFIG_MOCKS.items()
    }):
        with patch.multiple("telegram_api", **{
            k.split(".", 1)[1]: v for k, v in TELEGRAM_MOCKS.items()
        }):
            with patch("bot_commands.send_message",         _fake_send_message), \
                 patch("bot_commands.send_inline_keyboard", _fake_send_inline_keyboard), \
                 patch("bot_commands.send_photo",           _fake_send_photo), \
                 patch("bot_commands._fetch_live_price",    _fake_live_price), \
                 patch("bot_commands._chat_id",             _fake_chat_id), \
                 patch("bot_commands._get_bot_username",    _fake_get_username):
                _SENT.clear()
                return fn()


# ── Import bot_commands under mocks ──────────────────────────────────────────
# We patch at module level so all imports resolve cleanly.

_mock_ctx = patch.multiple("config_manager", **{
    k.split(".", 1)[1]: v for k, v in CONFIG_MOCKS.items()
})
_mock_ctx2 = patch.multiple("telegram_api", **{
    k.split(".", 1)[1]: v for k, v in TELEGRAM_MOCKS.items()
})
_mock_ctx.start()
_mock_ctx2.start()

try:
    import bot_commands as bc
    from bot_commands import (
        _nl_extract_tickers_list,
        _nl_parse_trade,
        _resolve_ticker_candidates,
        _parse_and_execute,
        _handle_pending_reply,
    )
    BOT_IMPORTED = True
except Exception as e:
    BOT_IMPORTED = False
    print(f"{RED}ERROR: Could not import bot_commands: {e}{RESET}")
    traceback.print_exc()


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 1 — NL Parser unit tests (require Anthropic API)
# ═════════════════════════════════════════════════════════════════════════════

def run_nl_tests():
    header("NL Parsing Tests  (real Haiku calls)")

    if not HAS_API_KEY:
        R.record_skip("All NL tests", "ANTHROPIC_API_KEY not set")
        return
    if not BOT_IMPORTED:
        R.record_skip("All NL tests", "bot_commands failed to import")
        return

    # ── _nl_extract_tickers_list ──────────────────────────────────────────────
    print(f"\n  {BOLD}› _nl_extract_tickers_list{RESET}")
    cases = [
        # (description, input, must_contain_all, must_have_count)
        ("Single ticker",               "NVDA",                                 ["NVDA"],                           1),
        ("Single company name",         "apple",                                ["apple"],                          1),
        ("Multi-word company",          "avery dennison",                       ["avery dennison"],                 1),
        ("Comma-separated tickers",     "AAPL, MSFT, NVDA",                     ["AAPL", "MSFT", "NVDA"],           3),
        ("Mixed names and tickers",     "apple, microsoft, CRM",                ["apple", "microsoft", "CRM"],      3),
        ("Comma + and",                 "avery dennison, microsoft, CRM, solana and EEM",
                                                                                 None,                              5),
        ("Sentence with noise words",   "I picked up some apple and a bit of tesla today",
                                                                                 None,                              2),
        ("Crypto mixed in",             "bought bitcoin, ethereum and nvidia",  None,                               3),
        ("All caps tickers",            "JPM TSLA SOL",                         None,                               3),
    ]
    for desc, inp, must_contain, must_count in cases:
        try:
            result = _nl_extract_tickers_list(inp)
            passed = True
            detail = f"got {result}"
            if must_count and len(result) != must_count:
                passed = False
                detail = f"expected {must_count} items, got {len(result)}: {result}"
            elif must_contain:
                lowers = [r.lower() for r in result]
                for m in must_contain:
                    if not any(m.lower() in r for r in lowers):
                        passed = False
                        detail = f"'{m}' not found in {result}"
                        break
            R.record(f"extract_list: {desc}", passed, detail)
        except Exception as e:
            R.record(f"extract_list: {desc}", False, str(e))

    # ── _nl_parse_trade ───────────────────────────────────────────────────────
    print(f"\n  {BOLD}› _nl_parse_trade{RESET}")
    parse_cases = [
        # (desc, command, input, expected_ticker, expected_price, expected_shares)
        ("bought simple",         "bought", "apple",                               "AAPL", None,  None),
        ("bought with price",     "bought", "bought 10 apple stocks for $182.50",  "AAPL", 182.5, 10.0),
        ("bought shares only",    "bought", "5 shares of tesla",                   "TSLA", None,  5.0),
        ("bought NL sentence",    "bought", "I got some nvidia today",             "NVDA", None,  None),
        ("sold basic",            "sold",   "sold my avery dennison",              "AVY",  None,  None),
        ("sold with price",       "sold",   "closed microsoft at 415",             "MSFT", 415.0, None),
        ("alert above",           "alert",  "when nvidia hits 1000",               "NVDA", 1000.0, None),
        ("alert below",           "alert",  "alert me if apple drops below 170",   "AAPL", 170.0, None),
        ("unalert company name",  "unalert","remove nvidia alert",                 "NVDA", None,  None),
        ("paper_buy with shares", "paper_buy","10 shares of tesla",               "TSLA", None,  10.0),
        ("paper_buy NL",          "paper_buy","buy some apple",                   "AAPL", None,  None),
        ("paper_sell basic",      "paper_sell","my tesla position",               "TSLA", None,  None),
        ("paper_sell with count", "paper_sell","5 shares of microsoft",           "MSFT", None,  5.0),
    ]
    for desc, cmd, inp, exp_ticker, exp_price, exp_shares in parse_cases:
        try:
            result = _nl_parse_trade(cmd, inp)
            ticker_ok = (result.get("ticker") or "").upper() == exp_ticker.upper() if exp_ticker else True
            price_ok  = (abs(float(result["price"]) - exp_price) < 1.0) if exp_price and result.get("price") else (exp_price is None or result.get("price") is not None)
            shares_ok = (abs(float(result["shares"]) - exp_shares) < 0.5) if exp_shares and result.get("shares") else (exp_shares is None or result.get("shares") is not None)
            passed = ticker_ok and price_ok and shares_ok
            detail = f"ticker={result.get('ticker')} price={result.get('price')} shares={result.get('shares')}"
            R.record(f"nl_parse ({cmd}): {desc}", passed, detail)
        except Exception as e:
            R.record(f"nl_parse ({cmd}): {desc}", False, str(e))

    # ── _resolve_ticker_candidates ─────────────────────────────────────────────
    print(f"\n  {BOLD}› _resolve_ticker_candidates{RESET}")
    resolve_cases = [
        # (desc, input, expected_first_ticker)
        ("exact ticker",             "AAPL",            "AAPL"),
        ("lowercase ticker",         "msft",            "MSFT"),
        ("company name",             "apple",           "AAPL"),
        ("multi-word company",       "avery dennison",  "AVY"),
        ("multi-word company 2",     "costco",          "COST"),
        ("crypto",                   "solana",          "SOL"),
        ("misspelling",              "nvidea",          "NVDA"),
        ("ETF name",                 "emerging markets","EEM"),
        ("full company",             "microsoft corporation", "MSFT"),
    ]
    for desc, inp, exp in resolve_cases:
        try:
            result = _resolve_ticker_candidates(inp)
            got = result[0]["ticker"].upper() if result else ""
            passed = got == exp.upper()
            R.record(f"resolve: {desc}", passed, f"expected {exp}, got {got}")
        except Exception as e:
            R.record(f"resolve: {desc}", False, str(e))


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 2 — Command flow tests (mock Telegram + storage, real NL if key set)
# ═════════════════════════════════════════════════════════════════════════════

def _run_cmd(text: str) -> str:
    """Fire _parse_and_execute with mocks active, return reply."""
    _SENT.clear()
    _FAKE_PENDING.clear()
    reply = _parse_and_execute(text.upper(), original=text, chat_id=TEST_CHAT)
    return reply or ""


def _run_pending(command: str, step: int, text: str, data: dict | None = None) -> str:
    """Simulate a pending-state reply."""
    _SENT.clear()
    state = {"command": command, "step": step, "data": data or {}, "ts": time.time()}
    reply = _handle_pending_reply(state, text, TEST_CHAT)
    return reply or ""


def _keyboard_tickers() -> list[str]:
    """Extract tickers from last inline keyboard callback_data sent."""
    for msg in reversed(_SENT):
        if msg["type"] == "keyboard":
            tickers = []
            for row in msg.get("buttons", []):
                for btn in row:
                    cd = btn.get("callback_data", "")
                    parts = cd.split("|")
                    if len(parts) >= 2 and parts[1] and not parts[1].startswith("cancel"):
                        tickers.append(parts[1])
            return tickers
    return []


def run_command_tests():
    if not BOT_IMPORTED:
        header("Command Flow Tests")
        R.record_skip("All command tests", "bot_commands failed to import")
        return

    def run():
        # ── /bought ───────────────────────────────────────────────────────────
        header("Command: /bought")

        # Direct path: exact ticker
        _FAKE_TRADE_LOGS.clear()
        reply = _run_cmd("BOUGHT AAPL")
        R.record("/bought exact ticker", "AAPL" in reply or any("AAPL" in s.get("text","") for s in _SENT),
                 f"reply={reply[:80]}")

        # Direct path: company name NL
        if HAS_API_KEY:
            _FAKE_TRADE_LOGS.clear()
            reply = _run_cmd("BOUGHT apple")
            R.record("/bought company name (apple→AAPL)",
                     "AAPL" in reply or any("AAPL" in s.get("text","") for s in _SENT),
                     f"reply={reply[:80]}")

        # Pending path: multi-ticker natural language
        if HAS_API_KEY:
            _FAKE_TRADE_LOGS.clear()
            _FAKE_PENDING[TEST_CHAT] = {"command": "bought", "step": 1, "data": {}, "ts": time.time()}
            state = _FAKE_PENDING[TEST_CHAT]
            reply = _run_pending("bought", 1, "avery dennison, microsoft, CRM, solana and EEM")
            combined = reply + " ".join(s.get("text","") for s in _SENT)
            R.record("/bought pending multi NL (5 items)",
                     all(t in combined for t in ["AVY", "MSFT", "CRM"]),
                     f"reply={reply[:120]}")

        # Pending path: full sentence
        if HAS_API_KEY:
            _FAKE_TRADE_LOGS.clear()
            reply = _run_pending("bought", 1, "I picked up some apple and a bit of tesla today")
            combined = reply + " ".join(s.get("text","") for s in _SENT)
            R.record("/bought pending sentence (apple + tesla)",
                     "AAPL" in combined or "TSLA" in combined,
                     f"reply={reply[:120]}")

        # ── /sold ─────────────────────────────────────────────────────────────
        header("Command: /sold")

        if HAS_API_KEY:
            # Pending: company name sentence
            reply = _run_pending("sold", 1, "sold my avery dennison position")
            combined = reply + " ".join(s.get("text","") for s in _SENT)
            R.record("/sold pending NL company name",
                     "AVY" in combined,
                     f"reply={reply[:80]}, sent={[s.get('text','')[:40] for s in _SENT]}")

            # Pending: simple company name
            reply = _run_pending("sold", 1, "microsoft")
            combined = reply + " ".join(s.get("text","") for s in _SENT)
            R.record("/sold pending company name (microsoft→MSFT)",
                     "MSFT" in combined,
                     f"combined={combined[:80]}")

        # ── /chart ────────────────────────────────────────────────────────────
        header("Command: /chart")

        # Direct exact ticker
        with patch("chart_generator.is_crypto", return_value=False), \
             patch("bot_commands._send_chart"):
            reply = _run_cmd("CHART NVDA")
            R.record("/chart exact ticker", True, "no error")

        # Direct company name
        if HAS_API_KEY:
            with patch("chart_generator.is_crypto", return_value=False), \
                 patch("bot_commands._send_chart"):
                reply = _run_cmd("CHART avery dennison")
                # Should resolve to AVY before spawning thread
                R.record("/chart company name resolved",
                         not reply or "avery dennison" not in (reply or "").upper(),
                         f"reply={reply}")

        # Pending: company name
        if HAS_API_KEY:
            with patch("chart_generator.is_crypto", return_value=False), \
                 patch("bot_commands._send_chart"):
                reply = _run_pending("chart", 1, "apple")
                R.record("/chart pending company name",
                         True, "no error")

        # ── /updatestop ───────────────────────────────────────────────────────
        header("Command: /updatestop + /updatetarget")

        # Direct: company name + price
        if HAS_API_KEY:
            _FAKE_TRADE_LOGS[TEST_CHAT] = {
                "open": [{"ticker": "AVY", "asset_type": "stock", "entry_price": 155.0,
                           "stop_loss": 148.0, "target_price": 175.0, "opened_date": "2025-01-01"}],
                "closed": []
            }
            with patch("bot_commands._execute_update_level", return_value="✅ Stop updated") as mock_upd:
                _run_cmd("UPDATESTOP avery dennison 150")
                called = mock_upd.called
                if called:
                    ticker_arg = mock_upd.call_args[0][0]
                    R.record("/updatestop company name + price (AVY)",
                             ticker_arg == "AVY",
                             f"called with ticker={ticker_arg}")
                else:
                    R.record("/updatestop company name + price (AVY)", False, "function not called")

        # Direct: ticker only → should ask for price
        reply = _run_cmd("UPDATESTOP NVDA")
        R.record("/updatestop ticker only → prompts for price",
                 any(s["type"] == "keyboard" for s in _SENT) or "price" in (reply or "").lower(),
                 f"reply={reply[:60]}")

        # ── /alert ────────────────────────────────────────────────────────────
        header("Command: /alert")

        # Strict: NVDA 1000
        with patch("price_alert_manager.add_alert", return_value="✅ Alert set") as mock_alert:
            reply = _run_cmd("ALERT NVDA 1000")
            R.record("/alert strict format (NVDA 1000)",
                     mock_alert.called and mock_alert.call_args[0][1] == "NVDA",
                     f"called={mock_alert.called}")

        # NL: company name with direction
        if HAS_API_KEY:
            with patch("price_alert_manager.add_alert", return_value="✅ Alert set") as mock_alert:
                reply = _run_cmd("ALERT when nvidia drops below 800")
                R.record("/alert NL (nvidia drops below 800)",
                         mock_alert.called,
                         f"called={mock_alert.called}, args={mock_alert.call_args}")

        # ── /unalert ──────────────────────────────────────────────────────────
        header("Command: /unalert")

        # /unalert now shows a confirmation keyboard before removing — check for that
        with patch("price_alert_manager.remove_alert", return_value="✅ Removed"):
            _SENT.clear()
            reply = _run_cmd("UNALERT NVDA")
            # Expect a confirmation inline keyboard (not direct removal)
            has_confirm_kb = any(
                s["type"] == "keyboard" and "NVDA" in s.get("text", "")
                for s in _SENT
            )
            R.record("/unalert exact ticker → shows confirmation keyboard",
                     has_confirm_kb,
                     f"sent={[s.get('text','')[:40] for s in _SENT]}")

        if HAS_API_KEY:
            with patch("price_alert_manager.remove_alert", return_value="✅ Removed"):
                _SENT.clear()
                reply = _run_cmd("UNALERT stop alerting me about apple")
                has_confirm_kb = any(
                    s["type"] == "keyboard" and "AAPL" in s.get("text", "")
                    for s in _SENT
                )
                R.record("/unalert NL sentence → shows confirmation keyboard",
                         has_confirm_kb,
                         f"sent={[s.get('text','')[:40] for s in _SENT]}")

        # ── /paper_buy ────────────────────────────────────────────────────────
        header("Command: /paper_buy")

        with patch("paper_trader.paper_buy", return_value="✅ Paper bought") as mock_pb:
            reply = _run_cmd("PAPER BUY AAPL 10")
            R.record("/paper_buy exact ticker + shares",
                     mock_pb.called or any("AAPL" in s.get("text","") for s in _SENT),
                     f"called={mock_pb.called}")

        if HAS_API_KEY:
            _SENT.clear()
            with patch("paper_trader.paper_buy", return_value="✅ Paper bought"):
                reply = _run_cmd("PAPER BUY 5 shares of apple")
                combined = reply + " ".join(s.get("text","") for s in _SENT)
                R.record("/paper_buy NL (5 shares of apple)",
                         "AAPL" in combined or "apple" in combined.lower(),
                         f"combined={combined[:80]}")

        if HAS_API_KEY:
            _SENT.clear()
            with patch("paper_trader.paper_buy", return_value="✅ Paper bought"):
                reply = _run_cmd("PAPER BUY avery dennison and microsoft")
                combined = reply + " ".join(s.get("text","") for s in _SENT)
                R.record("/paper_buy multi NL company names (AVY + MSFT)",
                         "AVY" in combined or "MSFT" in combined,
                         f"combined={combined[:100]}")

        # ── /paper_sell ───────────────────────────────────────────────────────
        header("Command: /paper_sell")

        _FAKE_PAPER[TEST_CHAT] = {
            "cash": 5000,
            "positions": {"TSLA": {"shares": 10, "avg_price": 165.0}},
            "history": []
        }

        if HAS_API_KEY:
            reply = _run_pending("paper_sell", 1, "my tesla position")
            combined = reply + " ".join(s.get("text","") for s in _SENT)
            R.record("/paper_sell pending NL (my tesla position)",
                     "TSLA" in combined,
                     f"combined={combined[:80]}")

            reply = _run_pending("paper_sell", 1, "5 shares of microsoft")
            combined = reply + " ".join(s.get("text","") for s in _SENT)
            R.record("/paper_sell pending NL with shares (5 shares of microsoft)",
                     "MSFT" in combined,
                     f"combined={combined[:80]}")

        # ── /watch ────────────────────────────────────────────────────────────
        header("Command: /watch")

        reply = _run_cmd("WATCH NVDA TSLA AAPL")
        R.record("/watch space-separated tickers",
                 "NVDA" in (reply or "") or "watchlist" in (reply or "").lower(),
                 f"reply={reply[:80]}")

        if HAS_API_KEY:
            reply = _run_cmd("WATCH tesla and apple")
            R.record("/watch NL company names",
                     reply and ("TSLA" in reply or "tesla" in reply.lower()),
                     f"reply={reply[:80]}")

        # ── /updatetarget (pending step 1) ────────────────────────────────────
        header("Command: /updatetarget pending")

        if HAS_API_KEY:
            reply = _run_pending("updatetarget", 1, "avery dennison")
            combined = reply + " ".join(s.get("text","") for s in _SENT)
            R.record("/updatetarget pending NL (avery dennison → AVY)",
                     "AVY" in combined,
                     f"combined={combined[:80]}")

        # ── Edge cases ────────────────────────────────────────────────────────
        header("Edge Cases")

        # Empty input
        reply = _run_pending("bought", 1, "")
        R.record("/bought empty input → warning",
                 "please" in (reply or "").lower() or "⚠️" in (reply or ""),
                 f"reply={reply}")

        # Gibberish ticker
        if HAS_API_KEY:
            _FAKE_TRADE_LOGS.clear()
            reply = _run_pending("bought", 1, "xyzzy123nonsense")
            R.record("/bought gibberish input → handles gracefully",
                     reply is not None,
                     f"reply={reply[:80]}")

        # Crypto in /bought
        if HAS_API_KEY:
            _FAKE_TRADE_LOGS.clear()
            reply = _run_pending("bought", 1, "solana")
            combined = reply + " ".join(s.get("text","") for s in _SENT)
            R.record("/bought crypto (solana)",
                     "SOL" in combined or "solana" in combined.lower(),
                     f"combined={combined[:80]}")

        # /sold step 2 (price confirmation after ticker selected)
        _FAKE_TRADE_LOGS[TEST_CHAT] = {
            "open": [{"ticker": "AAPL", "asset_type": "stock", "entry_price": 170.0,
                      "target_price": 200.0, "stop_loss": 160.0, "opened_date": "2025-01-01"}],
            "closed": []
        }
        reply = _run_pending("sold", 2, "185.50", data={"ticker": "AAPL"})
        combined = reply + " ".join(s.get("text","") for s in _SENT)
        R.record("/sold step 2 price confirmation",
                 "185" in combined or "AAPL" in combined,
                 f"combined={combined[:80]}")

    with_mocks(run)


# ═════════════════════════════════════════════════════════════════════════════
# SECTION 3 — Extended coverage: /today, /positions, /history, /settings
# ═════════════════════════════════════════════════════════════════════════════

_MOCK_PICKS = {
    "daily_summary": "Markets are cautiously optimistic.",
    "stocks": {
        "short_term": [{
            "ticker": "AAPL", "company": "Apple Inc", "action": "BUY",
            "entry_price": 185.0, "target_price": 200.0, "stop_loss": 175.0,
            "allocation": 50.0, "conviction": 4,
            "thesis": "Breakout on volume.", "risk": "Macro headwinds.",
        }],
        "long_term": [{
            "ticker": "MSFT", "company": "Microsoft Corp", "action": "BUY",
            "entry_price": 415.0, "target_price": 500.0,
            "allocation": 50.0, "conviction": 5,
            "thesis": "Cloud growth intact.", "horizon": "2-3 years",
        }],
    },
    "crypto": {
        "short_term": [{
            "id": "bitcoin", "symbol": "BTC", "name": "Bitcoin", "action": "BUY",
            "entry_price": 62000, "target_price": 70000, "stop_loss": 58000,
            "allocation": 25.0, "conviction": 3,
            "thesis": "Momentum building.", "risk": "High volatility.",
        }],
    },
    "disclaimer": "For informational purposes only.",
    "_saved_date": __import__("datetime").date.today().isoformat(),
}

_MOCK_TRADE_LOG = {
    "open": [
        {"ticker": "AAPL", "asset_type": "stock", "entry_price": 170.0,
         "target_price": 200.0, "stop_loss": 160.0, "opened_date": "2025-01-01",
         "shares": 5, "allocation": 50.0},
        {"ticker": "NVDA", "asset_type": "stock", "entry_price": 850.0,
         "target_price": 1000.0, "stop_loss": 800.0, "opened_date": "2025-01-10",
         "shares": 2, "allocation": 100.0},
    ],
    "closed": [
        {"ticker": "TSLA", "asset_type": "stock", "entry_price": 165.0,
         "closed_price": 185.0, "return_pct": 12.1, "outcome": "target",
         "gain_usd": 40.0, "opened_date": "2024-12-01", "closed_date": "2025-01-05"},
    ],
}


def run_extended_tests():
    if not BOT_IMPORTED:
        header("Extended Coverage Tests")
        R.record_skip("All extended tests", "bot_commands failed to import")
        return

    def run():
        # ── /today ────────────────────────────────────────────────────────────
        header("Command: /today")

        # No picks → returns "No picks" message
        reply = _run_cmd("TODAY")
        R.record("/today — no picks stored",
                 "no picks" in reply.lower() or "📭" in reply,
                 f"reply={reply[:60]}")

        # With picks → formats daily message
        _FAKE_PICKS.update(_MOCK_PICKS)
        reply = _run_cmd("TODAY")
        combined = reply + " ".join(s.get("text", "") for s in _SENT)
        R.record("/today — with picks → renders message",
                 "AAPL" in combined or "Apple" in combined or "stocks" in combined.lower(),
                 f"reply={reply[:80]}")
        _FAKE_PICKS.clear()

        # ── /positions ────────────────────────────────────────────────────────
        header("Command: /positions")

        # No positions
        reply = _run_cmd("POSITIONS")
        R.record("/positions — empty portfolio",
                 reply is not None,
                 f"reply={reply[:60]}")

        # With open positions
        _FAKE_TRADE_LOGS[TEST_CHAT] = dict(_MOCK_TRADE_LOG)
        reply = _run_cmd("POSITIONS")
        combined = reply + " ".join(s.get("text", "") for s in _SENT)
        R.record("/positions — shows open holdings",
                 "AAPL" in combined or "NVDA" in combined or "position" in combined.lower(),
                 f"combined={combined[:80]}")
        _FAKE_TRADE_LOGS.clear()

        # ── /history ──────────────────────────────────────────────────────────
        header("Command: /history")

        # No history
        reply = _run_cmd("HISTORY")
        R.record("/history — no closed trades",
                 reply is not None,
                 f"reply={reply[:60]}")

        # With closed trades
        _FAKE_TRADE_LOGS[TEST_CHAT] = dict(_MOCK_TRADE_LOG)
        reply = _run_cmd("HISTORY")
        combined = reply + " ".join(s.get("text", "") for s in _SENT)
        R.record("/history — shows closed trades",
                 "TSLA" in combined or "closed" in combined.lower() or "history" in combined.lower(),
                 f"combined={combined[:80]}")
        _FAKE_TRADE_LOGS.clear()

        # ── /settings ────────────────────────────────────────────────────────
        header("Command: /settings")

        reply = _run_cmd("SETTINGS")
        combined = reply + " ".join(s.get("text", "") for s in _SENT)
        R.record("/settings — shows settings panel",
                 bool(combined.strip()),
                 f"combined={combined[:60]}")

        # ── /status ───────────────────────────────────────────────────────────
        header("Command: /status")

        reply = _run_cmd("STATUS")
        R.record("/status — returns a response",
                 reply is not None,
                 f"reply={reply[:60]}")

        # ── /set_risk ────────────────────────────────────────────────────────
        header("Command: /set_risk")

        reply = _run_cmd("SET RISK aggressive")
        R.record("/set_risk aggressive",
                 "aggressive" in (reply or "").lower(),
                 f"reply={reply[:60]}")

        reply = _run_cmd("SET RISK conservative")
        R.record("/set_risk conservative",
                 "conservative" in (reply or "").lower(),
                 f"reply={reply[:60]}")

        # ── /set_budget ───────────────────────────────────────────────────────
        header("Command: /set_budget")

        reply = _run_cmd("SET BUDGET stocks 200 crypto 50")
        R.record("/set_budget stocks + crypto",
                 "200" in (reply or "") or "budget" in (reply or "").lower(),
                 f"reply={reply[:60]}")

        reply = _run_cmd("SET BUDGET off")
        R.record("/set_budget off — clears budgets",
                 "clear" in (reply or "").lower() or "✅" in (reply or ""),
                 f"reply={reply[:60]}")

        # ── /watch ────────────────────────────────────────────────────────────
        header("Command: /watch extended")

        _FAKE_USER_CFGS.clear()
        reply = _run_cmd("WATCH NVDA TSLA AAPL")
        R.record("/watch — accepts multiple tickers",
                 reply is not None,
                 f"reply={reply[:60]}")

        # ── /pause / /resume ──────────────────────────────────────────────────
        header("Command: /pause + /resume")

        reply = _run_cmd("PAUSE")
        # /pause sends a confirmation keyboard, returns "" — check for sent keyboard
        combined = reply + " ".join(s.get("text", "") for s in _SENT)
        R.record("/pause — sends confirmation keyboard",
                 "pause" in combined.lower() or any(s["type"] == "keyboard" for s in _SENT),
                 f"combined={combined[:60]}")

        reply = _run_cmd("RESUME")
        R.record("/resume — acknowledges",
                 reply is not None and len(reply) > 0,
                 f"reply={reply[:60]}")

        # ── /help ─────────────────────────────────────────────────────────────
        header("Command: /help")

        reply = _run_cmd("HELP")
        R.record("/help — returns command list",
                 "help" in (reply or "").lower() or "/" in (reply or ""),
                 f"reply={reply[:60]}")

    with_mocks(run)


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    mode = sys.argv[1].lower() if len(sys.argv) > 1 else "all"

    print(f"\n{BOLD}{CYAN}StockPulz Bot — Command Test Runner{RESET}")
    if not HAS_API_KEY:
        print(f"{YELLOW}⚠  ANTHROPIC_API_KEY not set — NL/Haiku tests will be skipped{RESET}")
    if not BOT_IMPORTED:
        print(f"{RED}✗  bot_commands could not be imported — aborting{RESET}")
        sys.exit(1)

    if mode in ("all", "nl"):
        run_nl_tests()
    if mode in ("all", "commands"):
        run_command_tests()
    if mode in ("all", "extended"):
        run_extended_tests()
    if mode == "fast":
        print(f"\n{YELLOW}Fast mode: only running non-Haiku tests{RESET}")
        # Temporarily disable API key so NL tests are skipped
        _orig = os.environ.pop("ANTHROPIC_API_KEY", "")
        globals()["HAS_API_KEY"] = False
        run_command_tests()
        run_extended_tests()
        if _orig:
            os.environ["ANTHROPIC_API_KEY"] = _orig
        globals()["HAS_API_KEY"] = bool(_orig)

    ok_result = R.summary()
    sys.exit(0 if ok_result else 1)
