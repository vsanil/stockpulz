#!/usr/bin/env python3
"""
full_sweep.py — exhaustive user-surface exerciser (3-day hardening window).

Drives EVERY user-reachable path as the admin and asserts nothing 500s / crashes:
  • every GET  /api/miniapp/*  (read-only data endpoints)
  • a representative, snapshot-safe pass over every MUTATING /api/miniapp/* POST
  • every informational bot command  (via handle_incoming_command)
  • the inline-button callback dispatcher (via handle_callback_query)
  • the LLM endpoints (/define) — exercised every run per the owner's choice

Complements:
  • canary.py        — verifies the MATH + delivery (this verifies the SURFACE)
  • synthetic_user.py — real accumulating lifecycle

Safety:
  • Telegram sends are MUTED during the exercise (api.telegram.org calls are
    stubbed) so driving dozens of commands never DMs the admin — only the final
    report is sent, for real.
  • All mutating Gist stores are snapshotted up front and ALWAYS restored in a
    finally block, so the admin's data is byte-identical after a run.
  • Mini-app auth runs in param mode (MINIAPP_AUTH_DISABLED=1) so the in-process
    Flask test client can authenticate as the admin via a chat_id param.

Self-retires after _SWEEP_END (the 3-day window) — the workflow can then be
deleted. Reports loudly on any failure; on green it stays quiet except for one
end-of-day summary.

Usage: python3 scripts/full_sweep.py [--dry-run]
"""
from __future__ import annotations

import os
import sys
import math
import argparse
import datetime as _dt
import traceback

# MUST be set before importing webhook so mini-app auth accepts a chat_id param
# from the in-process test client (production is unaffected — separate process).
os.environ.setdefault("MINIAPP_AUTH_DISABLED", "1")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

# The hardening window. After this ET date the sweep no-ops (delete the workflow).
_SWEEP_END = "2026-07-06"

RESULTS: list[tuple[str, bool, str]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {('· ' + detail) if detail else ''}")


def _fin(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def _pos(x) -> bool:
    return _fin(x) and float(x) > 0


# ── raw Gist snapshot / restore (guarantees zero residue) ─────────────────────
_GID = os.environ.get("GIST_ID")
_TOK = os.environ.get("GH_GIST_TOKEN") or os.environ.get("GITHUB_TOKEN")
_SNAP_FILES = ("user_paper.json", "price_alerts.json", "trade_log.json",
               "user_trades.json", "user_configs.json", "feedback.json",
               "backtest_trades.json", "buy_counts.json")


def _gist_all() -> dict:
    r = requests.get(f"https://api.github.com/gists/{_GID}",
                     headers={"Authorization": f"token {_TOK}"}, timeout=20)
    r.raise_for_status()
    return r.json().get("files", {})


def _gist_restore(snapshot: dict) -> None:
    """Write snapshotted contents back verbatim, with retries (GitHub 409s on
    rapid successive PATCHes to the same gist — the sweep does many writes)."""
    import time
    files = {fn: {"content": c} for fn, c in snapshot.items() if c is not None}
    if not files:
        return
    time.sleep(1.5)
    last = ""
    for attempt in range(1, 7):
        r = requests.patch(f"https://api.github.com/gists/{_GID}",
                           headers={"Authorization": f"token {_TOK}"},
                           json={"files": files}, timeout=25)
        if r.status_code < 400:
            return
        last = f"{r.status_code} {r.text[:80]}"
        time.sleep(2 * attempt)
    raise RuntimeError(f"gist restore failed after retries: {last}")


# ── Telegram muter: stub api.telegram.org so the exercise never DMs the admin ──
# Patches at the requests layer so it catches EVERY send path regardless of how
# the module imported send_message. Gist (github.com) + price APIs pass through.
_orig_post = requests.post
_orig_get = requests.get
_orig_req = requests.Session.request


def _is_tg(url) -> bool:
    return isinstance(url, str) and "api.telegram.org" in url


class _FakeResp:
    status_code = 200
    text = '{"ok":true,"result":{}}'

    def json(self):
        return {"ok": True, "result": {"message_id": 1}}

    def raise_for_status(self):
        return None


def _mute_telegram() -> None:
    def post(url, *a, **k):
        return _FakeResp() if _is_tg(url) else _orig_post(url, *a, **k)

    def get(url, *a, **k):
        return _FakeResp() if _is_tg(url) else _orig_get(url, *a, **k)

    def req(self, method, url, *a, **k):
        return _FakeResp() if _is_tg(url) else _orig_req(self, method, url, *a, **k)

    requests.post, requests.get, requests.Session.request = post, get, req


def _unmute_telegram() -> None:
    requests.post, requests.get, requests.Session.request = _orig_post, _orig_get, _orig_req


def _recent_weekday(days_back: int = 5) -> str:
    d = _dt.date.today() - _dt.timedelta(days=days_back)
    while d.weekday() >= 5:
        d -= _dt.timedelta(days=1)
    return d.isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# 1. GET endpoints — every read-only /api/miniapp/* (must return non-500 + JSON)
# ══════════════════════════════════════════════════════════════════════════════

def sweep_get_endpoints(client, admin: str) -> None:
    date = _recent_weekday()
    paths = [
        "/api/miniapp/picks", "/api/miniapp/picks/history", "/api/miniapp/positions",
        "/api/miniapp/settings", "/api/miniapp/history", "/api/miniapp/pnl_history",
        "/api/miniapp/alerts", "/api/miniapp/paper", "/api/miniapp/performance",
        "/api/miniapp/my_performance", "/api/miniapp/community", "/api/miniapp/watchlist",
        "/api/miniapp/status", "/api/miniapp/markets", "/api/miniapp/regime",
        "/api/miniapp/live_prices", "/api/miniapp/earnings", "/api/miniapp/earnings_surprise",
        "/api/miniapp/economic_calendar", "/api/miniapp/analyst_ratings",
        "/api/miniapp/volume_spikes", "/api/miniapp/dividends", "/api/miniapp/tax_lots",
        "/api/miniapp/screener_data", "/api/miniapp/share_link",
        "/api/miniapp/chart/AAPL?period=1mo", "/api/miniapp/names?tickers=AAPL,MSFT",
        "/api/miniapp/quote?ticker=AAPL", "/api/miniapp/news/AAPL",
        "/api/miniapp/sparkline/AAPL", f"/api/miniapp/close_on_date?ticker=AAPL&date={date}",
        "/api/miniapp/backtest_pick?ticker=AAPL&entry=200&stop=188&target=220",
    ]
    for p in paths:
        sep = "&" if "?" in p else "?"
        full = f"{p}{sep}chat_id={admin}"
        name = "GET " + p.split("?")[0]
        try:
            r = client.get(full)
            j = r.get_json(silent=True)
            ok = r.status_code < 500 and j is not None
            _check(name, ok, f"{r.status_code}" + ("" if ok else f" · body={r.data[:60]}"))
        except Exception as e:
            _check(name, False, f"exc: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 2. Bot commands — informational (no mutation); must not crash / "went wrong"
# ══════════════════════════════════════════════════════════════════════════════

def sweep_bot_commands(admin: str) -> None:
    from bot_commands import handle_incoming_command
    cmds = ["/help", "/today", "/positions", "/paper", "/watchlist", "/performance",
            "/accuracy", "/review", "/settings", "/size AAPL", "/missed", "/dashboard",
            "/bestsetup", "/history", "/dividends", "/chart AAPL"]
    for c in cmds:
        name = "cmd " + c.split()[0]
        try:
            reply = handle_incoming_command(c, chat_id=admin)
            ok = (reply is None) or (isinstance(reply, str) and "Something went wrong" not in reply)
            _check(name, ok, "" if ok else f"reply={str(reply)[:60]}")
        except Exception as e:
            _check(name, False, f"exc: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 3. Inline-button callbacks — prompt-only (dispatcher + handler must not crash)
# ══════════════════════════════════════════════════════════════════════════════

def sweep_callbacks(admin: str) -> None:
    from bot_commands import handle_callback_query
    datas = [
        "chart|AAPL", "buy_pick|AAPL|200|1|stock|5|10", "settings_open|risk",
        "settings|noop", "alert|AAPL", "sold_pick|AAPL", "note_pick|AAPL",
        "setstop_prompt|AAPL", "pbuy|AAPL", "psell|AAPL",
        "change_buy_amount|AAPL|200|stock|5|10",
    ]
    for d in datas:
        name = "cb " + d.split("|")[0]
        cbq = {
            "id": "sweeptest",
            "from": {"id": int(admin), "first_name": "Sweep"},
            "message": {"message_id": 1, "chat": {"id": int(admin)}, "text": "x"},
            "data": d,
        }
        try:
            handle_callback_query(cbq)
            _check(name, True, "dispatched")
        except Exception as e:
            _check(name, False, f"exc: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# 4. Mutating POST endpoints — every write path, inside snapshot/restore
# ══════════════════════════════════════════════════════════════════════════════

def _post(client, path, body, admin) -> None:
    name = "POST " + path
    try:
        r = client.post(path, json={**body, "chat_id": admin})
        j = r.get_json(silent=True)
        ok = r.status_code < 500 and j is not None
        _check(name, ok, f"{r.status_code}" + ("" if ok else f" · body={r.data[:60]}"))
    except Exception as e:
        _check(name, False, f"exc: {e}")


def sweep_mutations(client, admin: str) -> None:
    from market_data import get_live_price
    from config_manager import get_user_config
    cfg = get_user_config(admin) or {}
    px = get_live_price("V")
    entry = float(px) if _pos(px) else 300.0

    # ── positions: log → update → close/remove/unlog ──────────────────────────
    _post(client, "/api/miniapp/log_bought",
          {"ticker": "V", "entry_price": entry, "stop_loss": round(entry * .93, 2),
           "target_price": round(entry * 1.15, 2), "shares": 2, "asset_type": "stock"}, admin)
    _post(client, "/api/miniapp/update_position",
          {"ticker": "V", "stop_loss": round(entry * .95, 2)}, admin)
    _post(client, "/api/miniapp/close_position", {"ticker": "V", "price": round(entry * 1.05, 2)}, admin)
    _post(client, "/api/miniapp/log_bought",
          {"ticker": "MA", "entry_price": 400.0, "shares": 1, "asset_type": "stock"}, admin)
    _post(client, "/api/miniapp/remove_position", {"ticker": "MA"}, admin)
    _post(client, "/api/miniapp/log_bought",
          {"ticker": "UNH", "entry_price": 500.0, "shares": 1, "asset_type": "stock"}, admin)
    _post(client, "/api/miniapp/unlog_bought", {"ticker": "UNH"}, admin)

    # ── alerts: add → alerts(POST) → remove → clear ───────────────────────────
    _post(client, "/api/miniapp/add_alert", {"ticker": "MSFT", "target": 300.0}, admin)
    _post(client, "/api/miniapp/alerts",
          {"ticker": "MSFT", "target": 290.0, "action": "add", "replace": True}, admin)
    _post(client, "/api/miniapp/remove_alert", {"ticker": "MSFT"}, admin)
    _post(client, "/api/miniapp/clear_alerts", {}, admin)

    # ── watchlist: add → remove → replace(with current) ───────────────────────
    _post(client, "/api/miniapp/watchlist/add", {"ticker": "SPY"}, admin)
    _post(client, "/api/miniapp/watchlist/remove", {"ticker": "SPY"}, admin)
    _post(client, "/api/miniapp/update_watchlist", {"watchlist": ["AAPL", "MSFT"]}, admin)

    # ── settings: set fields back to their CURRENT values (no net change) ──────
    _post(client, "/api/miniapp/toggle_paused", {"paused": bool(cfg.get("paused", False))}, admin)
    _post(client, "/api/miniapp/settings/update",
          {"key": "target_gain_pct", "value": cfg.get("target_gain_pct", 15)}, admin)
    _post(client, "/api/miniapp/update_settings",
          {"risk_profile": cfg.get("risk_profile", "moderate")}, admin)
    _post(client, "/api/miniapp/update_exclusions",
          {"excluded_sectors": cfg.get("excluded_sectors", [])}, admin)

    # ── paper: buy → sell(partial) → cancel → add_cash → reset ─────────────────
    _post(client, "/api/miniapp/paper_buy", {"ticker": "AAPL", "shares": 2}, admin)
    _post(client, "/api/miniapp/paper_sell", {"ticker": "AAPL", "shares": 1}, admin)
    _post(client, "/api/miniapp/paper_buy", {"ticker": "NVDA", "shares": 1}, admin)
    _post(client, "/api/miniapp/paper_cancel", {"ticker": "NVDA"}, admin)
    _post(client, "/api/miniapp/paper_add_cash", {"amount": 100}, admin)
    _post(client, "/api/miniapp/paper_reset", {"amount": 10000}, admin)

    # ── feedback (snapshotted so it's restored, not accumulated) ──────────────
    _post(client, "/api/miniapp/feedback",
          {"text": "[full_sweep] automated test — ignore", "page": "sweep"}, admin)


# ══════════════════════════════════════════════════════════════════════════════
# 5. LLM endpoints — exercised every run (owner's choice)
# ══════════════════════════════════════════════════════════════════════════════

def sweep_llm(client, admin: str) -> None:
    try:
        r = client.get(f"/api/miniapp/define?term=RSI&chat_id={admin}")
        j = r.get_json(silent=True) or {}
        ok = r.status_code == 200 and bool(j.get("explanation"))
        _check("LLM define", ok, f"{r.status_code} · {str(j.get('explanation'))[:50]}")
    except Exception as e:
        _check("LLM define", False, f"exc: {e}")


# ══════════════════════════════════════════════════════════════════════════════

def build_report() -> str:
    import pytz
    now = _dt.datetime.now(pytz.timezone("America/New_York")).strftime("%a %b %d · %I:%M %p ET")
    fails = [r for r in RESULTS if not r[1]]
    passes = [r for r in RESULTS if r[1]]
    head = "✅" if not fails else "🔴"
    lines = [f"{head} <b>Full Sweep — every surface</b>", f"<i>{now}</i>"]
    if fails:
        lines += ["", "🚨🚨🚨 <b>ACTION NEEDED</b> 🚨🚨🚨",
                  "<b>Forward this whole message to Claude to fix.</b>", ""]
    lines.append(f"<b>{len(passes)}/{len(RESULTS)} paths OK</b>")
    if fails:
        lines.append(f"\n🔴 <b>{len(fails)} FAILED:</b>")
        for name, _ok, detail in fails:
            lines.append(f"  • <b>{name}</b> — {detail}")
    else:
        lines.append("<i>Every endpoint, command, callback + LLM path responded. 👍</i>")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print only, don't send")
    args = ap.parse_args()

    admin = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not admin:
        print("TELEGRAM_CHAT_ID not set — cannot run as admin.")
        return 2

    # Self-retire after the hardening window.
    try:
        from config_manager import et_today
        today = et_today()
    except Exception:
        today = _dt.date.today().isoformat()
    if today > _SWEEP_END:
        print(f"[full_sweep] window ended ({today} > {_SWEEP_END}) — no-op. Delete the workflow.")
        return 0

    from webhook import app
    client = app.test_client()

    snapshot = {}
    _mute_telegram()
    try:
        snap_files = _gist_all()
        snapshot = {fn: snap_files.get(fn, {}).get("content")
                    for fn in _SNAP_FILES if fn in snap_files}
        _check("snapshot", bool(snapshot), f"{len(snapshot)} files snapshotted")

        for fn in (sweep_get_endpoints, sweep_bot_commands, sweep_callbacks,
                   sweep_mutations, sweep_llm):
            try:
                # get/mutation/llm sweeps need the client; command/callback don't
                if fn in (sweep_bot_commands, sweep_callbacks):
                    fn(admin)
                else:
                    fn(client, admin)
            except Exception as e:
                _check(fn.__name__, False, f"crashed: {e}")
                traceback.print_exc()
    finally:
        try:
            _gist_restore(snapshot)
            _check("restore", True, "all mutated stores restored to snapshot")
        except Exception as e:
            _check("restore", False, f"RESTORE FAILED: {e}")
        _unmute_telegram()

    report = build_report()
    print("\n" + "=" * 60 + "\n" + report.replace("<b>", "").replace("</b>", "")
          .replace("<i>", "").replace("</i>", ""))

    # Loud on any failure; on green stay quiet except one end-of-day summary
    # (UTC hour >= 20 ≈ the 4:30 PM ET run) so ~3 green runs/day → 1 DM.
    has_fail = any(not r[1] for r in RESULTS)
    should_send = (not args.dry_run) and (has_fail or _dt.datetime.utcnow().hour >= 20)
    if should_send:
        try:
            from telegram_api import send_message
            send_message(report, chat_id=admin)
            print("\n[full_sweep] report sent to admin.")
        except Exception as e:
            print(f"[full_sweep] failed to send report: {e}")

    return 1 if has_fail else 0


if __name__ == "__main__":
    sys.exit(main())
