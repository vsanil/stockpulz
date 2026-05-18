"""
bot_commands.py — Bot business logic: entry point, callbacks, pending-reply handler, router.

Imported by telegram_notifier.py (entry point).
Telegram API calls (send_message, etc.) come from telegram_api.py.

Command handlers live in cmd_*.py modules:
  cmd_helpers.py   — shared utilities and AI helpers
  cmd_trade_exec.py — trade execution helpers
  cmd_nlp.py       — NLP/natural-language handlers
  cmd_settings.py  — settings panel + settings commands
  cmd_market.py    — market data commands
  cmd_alerts.py    — alert commands
  cmd_paper.py     — paper-trading commands
  cmd_misc.py      — help / start
  cmd_admin.py     — admin commands
  cmd_trades.py    — real-money trade commands
"""

import os
import time
import threading
import hmac
import hashlib

import json
import anthropic
import requests
from telegram_api import (
    TELEGRAM_API,
    send_message, send_inline_keyboard, send_typing_action, send_photo,
    typing_until_done, answer_callback_query, _bot_token, _chat_id,
    _get_bot_username,
)
from config_manager import (
    get_config, update_config,
    get_user_config, update_user_config, update_user_config_multi, reset_user_config,
    load_picks,
    load_pending_state, save_pending_state, clear_pending_state,
    load_user_trade_log, save_user_trade_log,
    load_user_paper,
    get_pending_users, add_pending_user, remove_pending_user,
    get_allowed_users,
    load_feedback, add_feedback, mark_feedback_read, count_unread_feedback,
)
from formatters import (
    _esc, _p,
    format_daily_message, format_confirmation_message,
)

# ── Import all extracted helpers and command handlers ─────────────────────────
from cmd_helpers import (
    _is_number, _CRYPTO_SYMBOLS, _is_admin,
    _get_client,
    ADMIN_INVITE_TTL_HOURS, _make_admin_invite_token, _verify_admin_invite_token,
    _fetch_live_price, _resolve_ticker_candidates, _resolve_ticker_and_price,
    _nl_param, _send_release_broadcast,
)
from cmd_trade_exec import _send_chart, _execute_bought, _execute_sold, _execute_update_level
from cmd_nlp import _explain_pick, _handle_natural_language, _nl_extract_tickers_list, _nl_parse_trade
from cmd_settings import (
    _BUDGET_BUCKETS, _RISK_OPTIONS, _ASSET_OPTIONS,
    _start_onboarding_wizard, _send_onboarding_complete,
    _PARAM_PROMPTS, _prompt_for_param, _send_settings_panel,
    _cmd_settings,
)
from cmd_market import _cmd_market
from cmd_alerts import _cmd_alerts
from cmd_paper import _cmd_paper
from cmd_misc import _cmd_misc
from cmd_admin import _cmd_admin
from cmd_trades import _cmd_trades


# ── Command handler ───────────────────────────────────────────────────────────

def handle_incoming_command(message_text: str, chat_id: str | None = None) -> str:
    """Parse and execute a Telegram command. Sends reply and returns reply text."""
    chat_id = chat_id or _chat_id()
    text    = message_text.strip()

    # Non-slash message while a command is waiting for a param → handle as reply
    if not text.startswith("/"):
        state = load_pending_state(chat_id)
        if state:
            reply = _handle_pending_reply(state, text, chat_id)
            if reply:
                send_message(reply, chat_id=chat_id)
            return reply
    else:
        # Any new slash command cancels pending state
        clear_pending_state(chat_id)

    # ── Unknown user guard ────────────────────────────────────────────────────
    # Allow /start through (it handles its own pending logic)
    # Block everything else until admin approves
    cmd_lower = text.lstrip("/").split()[0].lower() if text else ""
    if not _is_admin(chat_id) and chat_id not in get_allowed_users():
        if cmd_lower != "start":
            pending = get_pending_users()
            if chat_id in pending:
                send_message(
                    "⏳ <b>Your access request is pending.</b>\n"
                    "You'll receive a notification as soon as you're approved.",
                    chat_id=chat_id,
                )
            else:
                send_message(
                    "👋 <b>Welcome to StockPulz!</b>\n\n"
                    "Send /start to request access.",
                    chat_id=chat_id,
                )
            return ""

    # Record last-seen timestamp for dashboard activity tracking (background — non-blocking)
    def _record_seen():
        try:
            from datetime import datetime as _dt
            update_user_config(chat_id, "last_seen", _dt.utcnow().isoformat())
        except Exception:
            pass
    threading.Thread(target=_record_seen, daemon=True).start()

    reply = _parse_and_execute(text.upper(), original=text, chat_id=chat_id)
    if reply:
        # Append /help hint to every command response except /help itself and daily picks
        cmd = text.lstrip("/").split()[0].lower() if text else ""
        if cmd not in ("help", "start", "today", "prices", "share", "feedback") and not reply.startswith("📋") and "/help" not in reply:
            reply = reply + "\n\n<i>📋 /help  ·  📲 /share  ·  💬 /feedback</i>"
        send_message(reply, chat_id=chat_id)
    return reply


def handle_callback_query(callback_query: dict) -> None:
    """
    Handle inline keyboard button taps.
    callback_data format:
      buy|TICKER|price|shares   (price/shares may be empty string)
      sell|TICKER|price
    """
    cq_id   = callback_query.get("id", "")
    data    = callback_query.get("data", "")
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))

    parts = data.split("|")
    action = parts[0] if parts else ""

    # Show an instant toast so users know the tap registered (prevents double-tapping)
    _TOASTS = {
        "onboard_budget":     "✅ Got it!",
        "onboard_risk":       "✅ Got it!",
        "onboard_assets":     "✅ Setting up your account…",
        "quickbuy":           "📝 Confirm below…",
        "quickbuy_confirm":   "⏳ Logging position…",
        "chart":              "⏳ Generating chart…",
        "confirm_sell":       "⏳ Removing position…",
        "sold_pick":          "⏳ Loading…",
        "sold_review":        "⏳ Loading…",
        "sold_confirm":       "⏳ Closing position…",
        "bought_confirm":     "⏳ Logging position…",
        "updatelevel_pick":   "⏳ Loading…",
        "unalert_confirm":    "⏳ Removing alert…",
        "paper_reset_confirm":"⏳ Resetting…",
        "pause_confirm":      "⏳ Pausing picks…",
        "reset_confirm":      "⏳ Resetting settings…",
        "psell":              "⏳ Loading…",
        "psell_confirm":      "⏳ Closing paper position…",
        "noop":               "",
    }
    toast = _TOASTS.get(action, "⏳ Working…") if action not in ("noop", "cancel_pending", "cancel_abort") else ""
    answer_callback_query(cq_id, text=toast)

    if action == "cancel_pending":
        target_chat = parts[1] if len(parts) > 1 else chat_id
        clear_pending_state(target_chat)
        send_message("👍 Cancelled.", chat_id=chat_id)
        return

    # Generic command shortcut — cmd|ACCURACY, cmd|HISTORY, etc.
    if action == "cmd":
        cmd_text = parts[1].upper() if len(parts) > 1 else ""
        if cmd_text:
            reply = _parse_and_execute(cmd_text, original=f"/{cmd_text.lower()}", chat_id=chat_id)
            if reply:
                send_message(reply, chat_id=chat_id)
        return

    if action == "quickbuy":
        # Show confirmation before logging — prevents accidental taps
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            return
        live = _fetch_live_price(ticker)
        price_hint = f"  <i>(live: <code>${_p(live)}</code>)</i>" if live else ""
        send_inline_keyboard(
            f"📝 <b>Log {ticker} as bought?</b>{price_hint}\n"
            f"<i>This will add it to your portfolio and start tracking P&amp;L.</i>",
            [[
                {"text": f"✅ Yes, log {ticker}", "callback_data": f"quickbuy_confirm|{ticker}"},
                {"text": "❌ Cancel",             "callback_data": "cancel_abort"},
            ]],
            chat_id=chat_id,
        )
        return

    if action == "quickbuy_confirm":
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            return
        try:
            reply = _execute_bought(ticker, chat_id)
            send_message(reply, chat_id=chat_id)
            try:
                from config_manager import increment_buy_count
                cfg = get_config()
                if cfg.get("show_buy_counts"):
                    increment_buy_count(ticker)
            except Exception as exc:
                print(f"[bot] buy count increment failed (non-critical): {exc}")
        except Exception as exc:
            print(f"[bot] quickbuy_confirm failed for {ticker}: {exc}")
            send_message(f"⚠️ Couldn't log <b>{ticker}</b> — try <code>/bought {ticker}</code> instead.", chat_id=chat_id)
        return

    if action == "chart":
        ticker     = parts[1].upper() if len(parts) > 1 else ""
        asset_type = parts[2] if len(parts) > 2 else "stock"
        if not ticker:
            return
        try:
            send_message("📊 <i>Generating chart…</i>", chat_id=chat_id)
            threading.Thread(
                target=_send_chart, args=(ticker, asset_type, chat_id), daemon=True
            ).start()
        except Exception as exc:
            print(f"[bot] chart failed for {ticker}: {exc}")
            send_message(f"⚠️ Chart unavailable for <b>{ticker}</b> right now.", chat_id=chat_id)
        return

    if action == "noop":
        return   # section header tap — do nothing

    if action == "sold_pick":
        # User tapped a position from the /sold picker — ask for exit price
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            return
        try:
            live = _fetch_live_price(ticker)
            live_hint = f"  <i>(live: <code>${_p(live)}</code>)</i>" if live else ""
            save_pending_state(chat_id, "sold", step=2, data={"ticker": ticker})
            kb = [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]]
            if live:
                kb.insert(0, [{"text": f"Use live price ${_p(live)}", "callback_data": f"sold_review|{ticker}|{live}"}])
            send_inline_keyboard(
                f"💸 <b>{ticker}</b> — what price did you sell at?{live_hint}\n"
                f"<i>Type the price, or tap the button to use live price.</i>",
                kb,
                chat_id=chat_id,
            )
        except Exception as exc:
            print(f"[bot] sold_pick failed for {ticker}: {exc}")
            send_message(f"⚠️ Something went wrong — try <code>/sold {ticker}</code> instead.", chat_id=chat_id)
        return

    if action == "sold_review":
        # Confirmation step — show summary before executing
        ticker     = parts[1].upper() if len(parts) > 1 else ""
        price_raw  = parts[2]         if len(parts) > 2 else ""
        if not ticker or not price_raw:
            return
        try:
            price = float(price_raw)
        except ValueError:
            send_message("⚠️ Invalid price.", chat_id=chat_id)
            return
        # Look up entry for P&L preview
        log   = load_user_trade_log(chat_id)
        entry = None
        for t in log.get("open", []):
            if t["ticker"] == ticker:
                entry = t.get("entry_price")
                break
        pnl_preview = ""
        if entry:
            try:
                ret = (price - float(entry)) / float(entry) * 100
                sign = "+" if ret >= 0 else ""
                pnl_preview = f"\n<i>Return: {sign}{ret:.1f}% vs entry <code>${_p(entry)}</code></i>"
            except Exception:
                pass
        send_inline_keyboard(
            f"💸 Close <b>{ticker}</b> at <code>${_p(price)}</code>?{pnl_preview}",
            [[{"text": f"✅ Yes, close {ticker}", "callback_data": f"sold_confirm|{ticker}|{price}|"},
              {"text": "❌ Cancel",               "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return

    if action == "sold_manual":
        # User wants to type a ticker manually
        _prompt_for_param("sold", chat_id)
        return

    if action == "updatelevel_pick":
        # User tapped a position from the /updatestop or /updatetarget picker
        sub_cmd   = parts[1] if len(parts) > 1 else ""   # "updatestop" or "updatetarget"
        ticker    = parts[2].upper() if len(parts) > 2 else ""
        if not sub_cmd or not ticker:
            return
        _field    = "stop_loss" if sub_cmd == "updatestop" else "target_price"
        label     = "stop-loss" if _field == "stop_loss" else "target"
        emoji     = "🛑" if _field == "stop_loss" else "🎯"
        log       = load_user_trade_log(chat_id)
        current   = next((t.get(_field) for t in log.get("open", []) if t["ticker"] == ticker), None)
        hint      = f"  <i>(current: {emoji} <code>${_p(current)}</code>)</i>" if current else ""
        save_pending_state(chat_id, sub_cmd, step=2, data={"ticker": ticker})
        send_inline_keyboard(
            f"📝 New {label} for <b>{ticker}</b>?{hint}\n"
            f"<i>Type the price below.</i>",
            [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return

    if action == "approve_user":
        new_id = parts[1] if len(parts) > 1 else ""
        if not new_id:
            send_message("⚠️ Could not read user ID from button.", chat_id=chat_id)
            return
        # Reuse the /adduser logic
        reply = _parse_and_execute(f"ADDUSER {new_id}", original=f"/adduser {new_id}", chat_id=chat_id)
        send_message(reply or f"✅ {new_id} approved.", chat_id=chat_id)
        return

    if action == "reject_user":
        rej_id = parts[1] if len(parts) > 1 else ""
        if not rej_id:
            send_message("⚠️ Could not read user ID from button.", chat_id=chat_id)
            return
        from config_manager import remove_pending_user
        remove_pending_user(rej_id)
        send_message(
            f"❌ <b>Access denied.</b> Your request to join StockPulz was not approved.",
            chat_id=rej_id,
        )
        send_message(f"🚫 <code>{rej_id}</code> rejected and removed from pending.", chat_id=chat_id)
        return

    # ── Onboarding wizard callbacks ───────────────────────────────────────────
    if action == "onboard_budget":
        key = parts[1] if len(parts) > 1 else ""
        if key not in _BUDGET_BUCKETS:
            return
        _, stock_b, crypto_b = _BUDGET_BUCKETS[key]
        update_user_config_multi(chat_id, {"stock_budget": stock_b, "crypto_budget": crypto_b})
        send_inline_keyboard(
            "💰 Budget saved.\n\n"
            "<b>Question 2 of 3 — What's your risk appetite?</b>\n"
            "<i>Conservative = tighter stops, fewer trades. Aggressive = wider targets, higher volatility picks.</i>",
            [[
                {"text": label, "callback_data": f"onboard_risk_{key2}"}
                for key2, (label, _) in _RISK_OPTIONS.items()
            ]],
            chat_id=chat_id,
        )
        return

    if action == "onboard_risk":
        key = parts[1] if len(parts) > 1 else ""
        if key not in _RISK_OPTIONS:
            return
        _, profile = _RISK_OPTIONS[key]
        update_user_config(chat_id, "risk_profile", profile)
        send_inline_keyboard(
            "⚖️ Risk profile saved.\n\n"
            "<b>Question 3 of 3 — Which asset classes do you want picks for?</b>\n"
            "<i>You can change this anytime in /settings.</i>",
            [[
                {"text": label, "callback_data": f"onboard_assets_{key2}"}
                for key2, (label, _, __) in _ASSET_OPTIONS.items()
            ]],
            chat_id=chat_id,
        )
        return

    if action == "onboard_assets":
        key = parts[1] if len(parts) > 1 else ""
        if key not in _ASSET_OPTIONS:
            return
        _, show_crypto, show_etfs = _ASSET_OPTIONS[key]
        update_user_config_multi(chat_id, {
            "show_crypto": show_crypto,
            "show_etfs":   show_etfs,
            "onboarded":   True,
        })
        _send_onboarding_complete(chat_id)
        return

    if action == "set_budget":
        bucket = parts[1] if len(parts) > 1 else ""
        amount_str = parts[2] if len(parts) > 2 else ""
        try:
            amount = float(amount_str)
        except ValueError:
            send_message("⚠️ Could not read amount.", chat_id=chat_id)
            return
        updates = {}
        if bucket in ("stocks", "both"):
            updates["stock_budget"] = amount
        if bucket in ("crypto", "both"):
            updates["crypto_budget"] = amount
        if updates:
            update_user_config_multi(chat_id, updates)
            parts_str = "  ·  ".join(
                f"{'Stocks' if k == 'stock_budget' else 'Crypto'} → <b>${int(amount)}</b>"
                for k in updates
            )
            send_message(f"✅ Budget updated: {parts_str}", chat_id=chat_id)
        return

    if action == "set_risk":
        profile = parts[1] if len(parts) > 1 else ""
        if profile not in ("conservative", "moderate", "aggressive"):
            send_message("⚠️ Invalid risk profile.", chat_id=chat_id)
            return
        update_user_config(chat_id, "risk_profile", profile)
        descriptions = {
            "conservative": "Fewer picks, tighter stops, low-volatility sectors, reduced crypto.",
            "moderate":     "Balanced approach — default settings.",
            "aggressive":   "More picks, wider stops, all sectors, full crypto allocation.",
        }
        send_message(
            f"✅ Risk profile → <b>{profile}</b>\n<i>{descriptions[profile]}</i>\nTakes effect tomorrow.",
            chat_id=chat_id,
        )
        return

    if action == "buy":
        ticker = parts[1] if len(parts) > 1 else ""
        if not ticker:
            return
        reply = _execute_bought(ticker, chat_id)
        send_message(reply, chat_id=chat_id)

    elif action == "confirm_sell":
        ticker = parts[1] if len(parts) > 1 else ""
        if not ticker:
            return
        try:
            reply = _execute_sold(ticker, chat_id)
            send_message(reply, chat_id=chat_id)
        except Exception as exc:
            print(f"[bot] confirm_sell failed for {ticker}: {exc}")
            send_message(f"⚠️ Couldn't remove <b>{ticker}</b> — try <code>/sold {ticker}</code> instead.", chat_id=chat_id)

    # ── /history remove buttons ───────────────────────────────────────────────
    elif action == "cancel_auto":
        # Show confirmation before deleting any trade record from history.
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            return
        log       = load_user_trade_log(chat_id)
        in_open   = any(t["ticker"] == ticker for t in log.get("open",   []))
        in_closed = any(t["ticker"] == ticker for t in log.get("closed", []))
        if not in_open and not in_closed:
            send_message(f"⚠️ <b>{ticker}</b> not found in your history.", chat_id=chat_id)
            return
        label = "open position" if in_open else "closed trade record"
        send_inline_keyboard(
            f"🗑 <b>Remove {ticker} from history?</b>\n"
            f"<i>This deletes the {label} from your records permanently.</i>",
            [[{"text": f"✅ Yes, remove {ticker}", "callback_data": f"cancel_auto_do|{ticker}"},
              {"text": "❌ Cancel",                "callback_data": "cancel_abort"}]],
            chat_id=chat_id,
        )

    elif action == "cancel_auto_do":
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            return
        log             = load_user_trade_log(chat_id)
        open_before     = len(log.get("open",   []))
        closed_before   = len(log.get("closed", []))
        log["open"]     = [t for t in log.get("open",   []) if t["ticker"] != ticker]
        log["closed"]   = [t for t in log.get("closed", []) if t["ticker"] != ticker]
        removed = (len(log["open"]) < open_before) or (len(log["closed"]) < closed_before)
        if removed:
            save_user_trade_log(chat_id, log)
            send_message(f"✅ <b>{ticker}</b> removed from your trade history.", chat_id=chat_id)
        else:
            send_message(f"⚠️ <b>{ticker}</b> not found in your history.", chat_id=chat_id)

    elif action == "sold_bulk":
        payload = "|".join(parts[1:])
        entries = payload.split(",")
        results = []
        for entry in entries:
            ep = entry.split("|")
            if len(ep) == 2:
                ticker, price = ep[0].strip(), ep[1].strip()
                try:
                    r = _execute_sold(ticker, chat_id, price=price)
                    results.append(r)
                except Exception as exc:
                    print(f"[bot] sold_bulk failed for {ticker}: {exc}")
                    results.append(f"⚠️ Couldn't close <b>{ticker}</b>.")
        clear_pending_state(chat_id)
        send_message("\n\n".join(results), chat_id=chat_id)

    elif action == "bought_bulk":
        # Log multiple positions at live price — format: "bought_bulk|MSFT|416.0,BNB|652.0,BTC|80000"
        payload = "|".join(parts[1:])   # rejoin since ticker/price pairs use |
        entries = payload.split(",")
        results = []
        for entry in entries:
            ep = entry.split("|")
            if len(ep) == 2:
                ticker, price = ep[0].strip(), ep[1].strip()
                try:
                    r = _execute_bought(ticker, chat_id, price=price)
                    results.append(r)
                except Exception as exc:
                    print(f"[bot] bought_bulk failed for {ticker}: {exc}")
                    results.append(f"⚠️ Couldn't log <b>{ticker}</b>.")
        clear_pending_state(chat_id)
        send_message("\n\n".join(results), chat_id=chat_id)

    elif action == "bought_confirm":
        ticker     = parts[1] if len(parts) > 1 else ""
        price_raw  = parts[2] if len(parts) > 2 else ""
        shares_raw = parts[3] if len(parts) > 3 else None
        clear_pending_state(chat_id)
        try:
            result = _execute_bought(ticker, chat_id, price=price_raw or None, shares=shares_raw or None)
            send_message(result, chat_id=chat_id)
        except Exception as exc:
            print(f"[bot] bought_confirm failed for {ticker}: {exc}")
            send_message(f"⚠️ Couldn't log <b>{ticker}</b> — try <code>/bought {ticker}</code> instead.", chat_id=chat_id)

    elif action == "sold_confirm":
        ticker     = parts[1] if len(parts) > 1 else ""
        price_raw  = parts[2] if len(parts) > 2 else ""
        shares_raw = parts[3] if len(parts) > 3 else None
        clear_pending_state(chat_id)
        try:
            # Grab open trade BEFORE removing (debrief needs it)
            _pre_log   = load_user_trade_log(chat_id)
            _open_trade = next(
                (t for t in _pre_log.get("open", []) if t["ticker"] == ticker.upper()), {}
            )
            result = _execute_sold(ticker, chat_id, price=price_raw or None, shares_sold=shares_raw or None)
            send_message(result, chat_id=chat_id)

            # Fire Haiku debrief in background (non-blocking, best-effort)
            if result.startswith("✅") and price_raw and _open_trade:
                try:
                    _exit   = float(price_raw)
                    _entry  = float(_open_trade.get("entry_price") or 0)
                    if _entry > 0:
                        _ret  = (_exit - _entry) / _entry * 100
                        _alloc = float(_open_trade.get("allocation") or 0)
                        _gain  = round(_alloc * _ret / 100, 2)
                        _tgt   = float(_open_trade.get("target_price") or 0)
                        _stp   = float(_open_trade.get("stop_loss")   or 0)
                        if _tgt > 0 and _exit >= _tgt * 0.95:
                            _outcome = "target"
                        elif _stp > 0 and _exit <= _stp * 1.05:
                            _outcome = "stop"
                        else:
                            _outcome = "expired"
                        _debrief_trade = {
                            **_open_trade,
                            "closed_price": round(_exit, 2),
                            "return_pct":   round(_ret, 2),
                            "gain_usd":     _gain,
                            "outcome":      _outcome,
                        }
                        def _send_debrief(trade=_debrief_trade, cid=chat_id):
                            from ai_analyzer import generate_trade_debrief
                            msg = generate_trade_debrief(trade)
                            if msg:
                                send_message(f"💡 <i>{_esc(msg)}</i>", chat_id=cid)
                        threading.Thread(target=_send_debrief, daemon=True).start()
                except Exception as _de:
                    print(f"[bot] debrief setup failed (non-critical): {_de}")
        except Exception as exc:
            print(f"[bot] sold_confirm failed for {ticker}: {exc}")
            send_message(f"⚠️ Couldn't close <b>{ticker}</b> — try <code>/sold {ticker}</code> instead.", chat_id=chat_id)

    elif action == "pbuy_confirm":
        # Confirm paper buy — format: pbuy_confirm|TICKER|price|shares
        ticker     = parts[1] if len(parts) > 1 else ""
        price_raw  = parts[2] if len(parts) > 2 else ""
        shares_raw = parts[3] if len(parts) > 3 else ""
        price  = float(price_raw)  if price_raw  else None
        shares = float(shares_raw) if shares_raw else 1.0
        clear_pending_state(chat_id)
        from paper_trader import paper_buy as _pb
        result = _pb(ticker, shares, chat_id, price)
        send_message(result, chat_id=chat_id)

    elif action == "pbuy":
        # Ticker disambiguation for paper_buy — format: pbuy|TICKER|shares_or_empty
        ticker     = parts[1] if len(parts) > 1 else ""
        shares_raw = parts[2] if len(parts) > 2 else ""
        if ticker:
            if shares_raw and _is_number(shares_raw):
                shares = float(shares_raw)
                live   = _fetch_live_price(ticker)
                price  = live
                shares_str = f"  ·  <b>{shares} shares</b>"
                total_str  = f"  ·  total <code>${float(price or 0) * shares:,.2f}</code>" if price else ""
                price_str  = f"<code>${_p(price)}</code>" if price else "<i>live price</i>"
                send_inline_keyboard(
                    f"📄 <b>Confirm paper buy?</b>\n"
                    f"<b>{ticker}</b>{shares_str}  @  {price_str}{total_str}\n"
                    f"<i>Tap ✅ to simulate, or type correct details to adjust.</i>",
                    [[{"text": "✅ Confirm", "callback_data": f"pbuy_confirm|{ticker}|{price or ''}|{shares}"},
                      {"text": "❌ Cancel",  "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
            else:
                live = _fetch_live_price(ticker)
                live_hint = f"  <i>(live: <code>${_p(live)}</code>)</i>" if live else ""
                save_pending_state(chat_id, "paper_buy", step=1, data={"ticker": ticker})
                send_inline_keyboard(
                    f"📄 How many shares of <b>{ticker}</b> to simulate buying?{live_hint}",
                    [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )

    elif action == "bought_skip_qty":
        # User tapped "Skip" on the quantity step — log without shares
        ticker    = parts[1] if len(parts) > 1 else ""
        price_raw = parts[2] if len(parts) > 2 else ""
        clear_pending_state(chat_id)
        result = _execute_bought(ticker, chat_id, price=price_raw or None)
        send_message(result, chat_id=chat_id)

    elif action == "sold_all":
        # User tapped "All shares" — full position close
        ticker    = parts[1] if len(parts) > 1 else ""
        price_raw = parts[2] if len(parts) > 2 else ""
        clear_pending_state(chat_id)
        result = _execute_sold(ticker, chat_id, price=price_raw or None)
        send_message(result, chat_id=chat_id)

    elif action == "paper_cancel_pos":
        ticker = parts[1] if len(parts) > 1 else ""
        from paper_trader import paper_cancel
        result = paper_cancel(ticker, chat_id)
        send_message(result, chat_id=chat_id)

    elif action == "settings_toggle":
        # Instant toggle for pause / show_crypto / notification opt-outs
        key = parts[1] if len(parts) > 1 else ""
        cfg = get_user_config(chat_id)
        if key == "paused":
            new_val = not bool(cfg.get("paused", False))
            update_user_config(chat_id, "paused", new_val)
            send_message("⏸ Picks paused." if new_val else "▶️ Picks resumed.", chat_id=chat_id)
        elif key == "show_crypto":
            new_val = not bool(cfg.get("show_crypto", True))
            update_user_config(chat_id, "show_crypto", new_val)
            send_message("🔕 Crypto hidden from picks." if not new_val else "🔔 Crypto shown in picks.", chat_id=chat_id)
        elif key == "skip_confirmation":
            new_val = not bool(cfg.get("skip_confirmation", False))
            update_user_config(chat_id, "skip_confirmation", new_val)
            send_message("🔕 10:30 AM confirmation off — you won't get the mid-morning price check." if new_val
                         else "📨 10:30 AM confirmation on.", chat_id=chat_id)
        elif key == "skip_eod":
            new_val = not bool(cfg.get("skip_eod", False))
            update_user_config(chat_id, "skip_eod", new_val)
            send_message("🔕 EOD summary off — no end-of-day snapshot." if new_val
                         else "🌅 EOD summary on.", chat_id=chat_id)
        elif key == "skip_watchlist_alerts":
            new_val = not bool(cfg.get("skip_watchlist_alerts", False))
            update_user_config(chat_id, "skip_watchlist_alerts", new_val)
            send_message("🔕 Watchlist alerts off — RSI/MACD signals won't be sent." if new_val
                         else "👁 Watchlist alerts on.", chat_id=chat_id)
        _send_settings_panel(chat_id)

    elif action == "settings_open":
        # Show a choice picker for risk, mode, or pick counts
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "risk":
            send_inline_keyboard(
                "⚖️ <b>Choose risk level:</b>",
                [[
                    {"text": "🛡 Conservative", "callback_data": "settings_risk|conservative"},
                    {"text": "⚖️ Moderate",     "callback_data": "settings_risk|moderate"},
                    {"text": "🔥 Aggressive",   "callback_data": "settings_risk|aggressive"},
                ]],
                chat_id=chat_id,
            )
        elif sub == "mode":
            send_inline_keyboard(
                "📊 <b>Which picks to receive?</b>",
                [[
                    {"text": "📈 ST only",  "callback_data": "settings_mode|st"},
                    {"text": "📊 LT only",  "callback_data": "settings_mode|lt"},
                    {"text": "✅ Both",     "callback_data": "settings_mode|both"},
                ]],
                chat_id=chat_id,
            )
        elif sub == "picks_stock":
            send_inline_keyboard(
                "📈 <b>Max stock picks per day?</b>",
                [[
                    {"text": "2", "callback_data": "settings_picks|stock|2"},
                    {"text": "3", "callback_data": "settings_picks|stock|3"},
                    {"text": "4", "callback_data": "settings_picks|stock|4"},
                    {"text": "5", "callback_data": "settings_picks|stock|5"},
                    {"text": "All", "callback_data": "settings_picks|stock|0"},
                ]],
                chat_id=chat_id,
            )
        elif sub == "picks_crypto":
            send_inline_keyboard(
                "🪙 <b>Max crypto picks per day?</b>",
                [[
                    {"text": "1", "callback_data": "settings_picks|crypto|1"},
                    {"text": "2", "callback_data": "settings_picks|crypto|2"},
                    {"text": "3", "callback_data": "settings_picks|crypto|3"},
                    {"text": "All", "callback_data": "settings_picks|crypto|0"},
                ]],
                chat_id=chat_id,
            )

    elif action == "settings_risk":
        profile = parts[1] if len(parts) > 1 else ""
        if profile in ("conservative", "moderate", "aggressive"):
            update_user_config(chat_id, "risk_profile", profile)
            descs = {
                "conservative": "Fewer picks, tighter stops, low-volatility sectors.",
                "moderate":     "Balanced approach — default settings.",
                "aggressive":   "More picks, wider stops, all sectors.",
            }
            send_message(f"✅ Risk → <b>{profile}</b>  <i>{descs[profile]}</i>", chat_id=chat_id)
        _send_settings_panel(chat_id)

    elif action == "settings_mode":
        mode = parts[1] if len(parts) > 1 else ""
        labels = {"st": "Short term only", "lt": "Long term only", "both": "Both"}
        if mode in labels:
            update_user_config(chat_id, "pick_mode", mode)
            send_message(f"✅ Pick mode → <b>{labels[mode]}</b>", chat_id=chat_id)
        _send_settings_panel(chat_id)

    elif action == "settings_picks":
        bucket = parts[1] if len(parts) > 1 else ""
        val_str = parts[2] if len(parts) > 2 else "0"
        try:
            val = int(val_str)
        except ValueError:
            val = 0
        key = "max_stock_picks" if bucket == "stock" else "max_crypto_picks"
        update_user_config(chat_id, key, val if val > 0 else None)
        label = f"{val}" if val > 0 else "all"
        kind  = "stock" if bucket == "stock" else "crypto"
        send_message(f"✅ Max {kind} picks → <b>{label}</b>", chat_id=chat_id)
        _send_settings_panel(chat_id)

    elif action == "settings_prompt":
        # Start a pending state and ask for typed input
        sub = parts[1] if len(parts) > 1 else ""
        prompts = {
            "budget_stock":     ("settings_budget_stock",     "💰 <b>Stock budget per trade?</b>\n<i>e.g. <code>200</code> or <code>off</code> to clear</i>"),
            "budget_crypto":    ("settings_budget_crypto",    "₿ <b>Crypto budget per trade?</b>\n<i>e.g. <code>50</code> or <code>off</code> to clear</i>"),
            "stop":             ("settings_stop",             "🛑 <b>Stop loss %?</b>\n<i>e.g. <code>7</code> for 7%</i>"),
            "target":           ("settings_target",           "🎯 <b>Target gain %?</b>\n<i>e.g. <code>15</code> for 15%</i>"),
            "watchlist":        ("watch",                     "👀 <b>Watchlist tickers?</b>\n<i>e.g. <code>TSLA MSFT NVDA</code>  ·  blank to clear</i>"),
            "exclude":          ("exclude",                   "🚫 <b>Sectors to exclude?</b>\n<i>e.g. <code>Energy Utilities</code>  ·  blank to clear</i>"),
            "portfolio_size":       ("settings_portfolio_size",       "💼 <b>Total portfolio capital?</b>\n<i>Enables position sizing on every pick.\ne.g. <code>25000</code> or <code>25k</code>  ·  <code>off</code> to disable</i>"),
            "portfolio_risk":       ("settings_portfolio_risk",       "⚖️ <b>Risk per trade (% of capital)?</b>\n<i>1% is standard; 0.5%–2% is the typical range.\ne.g. <code>1</code> for 1% risk per trade</i>"),
            "portfolio_max_pos":    ("settings_portfolio_max_pos",    "🎯 <b>Max single position size (% of capital)?</b>\n<i>Default 10% — prevents any one pick from dominating.\ne.g. <code>10</code> for 10%</i>"),
            "portfolio_max_sector": ("settings_portfolio_max_sector", "🏭 <b>Max sector concentration (% of capital)?</b>\n<i>Default 35% — warns when one sector dominates.\ne.g. <code>35</code> for 35%</i>"),
            "min_conviction":       ("settings_min_conviction",       "🏆 <b>Minimum conviction to receive a pick?</b>\n<i>4 = high conviction only (recommended)\n3 = include moderate setups too\nEnter <code>3</code> or <code>4</code></i>"),
        }
        if sub in prompts:
            cmd, prompt_text = prompts[sub]
            save_pending_state(chat_id, cmd)
            send_inline_keyboard(
                prompt_text,
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )

    elif action == "settings_reset_ask":
        send_inline_keyboard(
            "⚠️ <b>Reset all your settings?</b>\n"
            "<i>Risk, mode, budgets, thresholds, watchlist, excluded sectors — all wiped.</i>",
            [[
                {"text": "✅ Yes, reset",  "callback_data": "settings_reset_do"},
                {"text": "❌ No, cancel", "callback_data": "cancel_abort"},
            ]],
            chat_id=chat_id,
        )

    elif action == "settings_reset_do":
        reset_user_config(chat_id)
        send_message("🔄 Settings reset to defaults.", chat_id=chat_id)
        _send_settings_panel(chat_id)

    elif action == "reset_confirm":
        return _parse_and_execute("RESET CONFIRM", original="/reset", chat_id=chat_id)

    elif action == "release_send":
        note_id = parts[1] if len(parts) > 1 else ""
        from release_tracker import get_pending_notes, mark_note_sent
        note = next((n for n in get_pending_notes() if n["id"] == note_id), None)
        if not note:
            send_message("⚠️ Release note not found — may have already been sent.", chat_id=chat_id)
            return
        result = _send_release_broadcast(note["summary"], chat_id)
        mark_note_sent(note_id, note["summary"])
        send_message(result, chat_id=chat_id)

    elif action == "release_edit":
        note_id = parts[1] if len(parts) > 1 else ""
        save_pending_state(chat_id, "release", step=1, data={"note_id": note_id})
        send_inline_keyboard(
            "✏️ <b>Edit the release note</b>\n\nType your updated message — it will be sent to all users:",
            [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )

    elif action == "release_skip":
        note_id = parts[1] if len(parts) > 1 else ""
        from release_tracker import mark_note_skipped
        mark_note_skipped(note_id)
        send_message("⏭ Release note skipped.", chat_id=chat_id)

    elif action == "rearm_alert":
        # Re-set a fired alert at the same price + direction
        # callback_data: rearm_alert|TICKER|TARGET|DIRECTION
        ticker    = parts[1].upper() if len(parts) > 1 else ""
        target    = float(parts[2])  if len(parts) > 2 else None
        direction = parts[3]         if len(parts) > 3 else "auto"
        if not ticker or target is None:
            send_message("⚠️ Could not re-arm alert — missing parameters.", chat_id=chat_id)
        else:
            try:
                from price_alert_manager import add_alert as _aa
                confirmation = _aa(chat_id, ticker, target, direction)
                send_message(confirmation, chat_id=chat_id)
            except ValueError as e:
                send_message(f"⚠️ {e}", chat_id=chat_id)
            except Exception as e:
                send_message(f"⚠️ Could not re-arm alert: {e}", chat_id=chat_id)

    elif action == "cancel_abort":
        send_message("👍 No changes made.", chat_id=chat_id)

    elif action == "paper_hist_rm_confirm":
        from paper_trader import paper_history as get_paper_history
        idx_str = parts[1] if len(parts) > 1 else ""
        try:
            idx = int(idx_str)
        except ValueError:
            send_message("⚠️ Invalid trade index.", chat_id=chat_id)
            return
        history = get_paper_history(chat_id)
        if idx < 0 or idx >= len(history):
            send_message("⚠️ Trade not found — the list may have changed.", chat_id=chat_id)
            return
        t        = history[idx]
        gain_pct = t.get("gain_pct", 0)
        gain     = t.get("gain", 0)
        sign     = "+" if gain >= 0 else ""
        proceeds = round(float(t.get("sell_price", 0)) * float(t.get("shares", 0)), 2)
        send_inline_keyboard(
            f"⚠️ <b>Reverse this paper trade?</b>\n\n"
            f"<b>{t['ticker']}</b>  {t.get('shares')} shares  "
            f"sold @ <code>${_p(t.get('sell_price'))}</code>  {sign}{gain_pct:.1f}%\n"
            f"Closed: {t.get('closed_date', '')}\n\n"
            f"<i>Proceeds (${proceeds:,.2f}) will be deducted and shares restored to your position.</i>",
            [[
                {"text": "✅ Yes, reverse it", "callback_data": f"paper_hist_rm_do|{idx}"},
                {"text": "❌ No, keep it",     "callback_data": "cancel_abort"},
            ]],
            chat_id=chat_id,
        )

    elif action == "paper_hist_rm_do":
        from paper_trader import paper_remove_history
        idx_str = parts[1] if len(parts) > 1 else ""
        try:
            idx = int(idx_str)
        except ValueError:
            send_message("⚠️ Invalid trade index.", chat_id=chat_id)
            return
        result = paper_remove_history(chat_id, idx)
        send_message(result, chat_id=chat_id)

    elif action == "pause_confirm":
        update_user_config(chat_id, "paused", True)
        send_message("⏸ <b>Your picks paused.</b> You won't receive daily briefings until you send /resume.\n<i>Other users are unaffected.</i>", chat_id=chat_id)

    elif action == "reset_confirm":
        try:
            reset_user_config(chat_id)
            global_cfg = get_config()
            sl = global_cfg.get("stop_loss_pct", 7)
            tg = global_cfg.get("target_gain_pct", 15)
            send_message(
                f"🔄 Your settings reset to defaults.\n"
                f"Risk: moderate  ·  Pick mode: both\n"
                f"Budgets: unset  ·  Watchlist: cleared\n"
                f"Stop loss: {sl}%  ·  Target gain: {tg}%  (global defaults)",
                chat_id=chat_id,
            )
        except Exception as exc:
            print(f"[bot] reset_confirm failed: {exc}")
            send_message("⚠️ Reset failed — try again.", chat_id=chat_id)

    elif action == "cancel_abort":
        send_message("👍 Cancelled.", chat_id=chat_id)

    elif action == "unalert_confirm":
        # Execute alert removal after user confirmed
        ticker    = parts[1].upper() if len(parts) > 1 else ""
        price_enc = parts[2]         if len(parts) > 2 else ""
        if not ticker:
            send_message("⚠️ Missing ticker.", chat_id=chat_id)
            return
        import urllib.parse as _urlparse
        price = None
        if price_enc:
            try:
                price = float(_urlparse.unquote(price_enc))
            except Exception:
                pass
        from price_alert_manager import remove_alert as _ra
        result = _ra(chat_id, ticker, price)
        send_message(result, chat_id=chat_id)

    elif action == "paper_reset_confirm":
        amount_raw = parts[1] if len(parts) > 1 else ""
        amount = float(amount_raw) if amount_raw and _is_number(amount_raw) else None
        try:
            from paper_trader import paper_reset as _pr
            result = _pr(chat_id, amount)
            send_message(result, chat_id=chat_id)
        except Exception as exc:
            print(f"[bot] paper_reset_confirm failed: {exc}")
            send_message("⚠️ Reset failed — try again.", chat_id=chat_id)

    elif action == "psell_confirm":
        # Confirm paper sell — format: psell_confirm|TICKER|price_or_empty|shares_or_empty
        ticker     = parts[1] if len(parts) > 1 else ""
        price_raw  = parts[2] if len(parts) > 2 else ""
        shares_raw = parts[3] if len(parts) > 3 else ""
        price  = float(price_raw)  if price_raw  else None
        shares = float(shares_raw) if shares_raw else None
        clear_pending_state(chat_id)
        from paper_trader import paper_sell as _ps
        result = _ps(ticker, chat_id, shares, price)
        send_message(result, chat_id=chat_id)

    elif action == "psell_bulk":
        # Sell multiple paper positions at live price — format: psell_bulk|TICKER,TICKER,...
        payload = "|".join(parts[1:])
        tickers = [t.strip() for t in payload.split(",") if t.strip()]
        from paper_trader import paper_sell as _ps
        results = []
        for t in tickers:
            r = _ps(t, chat_id, None, None)
            results.append(r)
        clear_pending_state(chat_id)
        send_message("\n\n".join(results), chat_id=chat_id)

    elif action == "psell":
        # Disambiguation button for paper_sell — ticker chosen, show confirmation
        ticker     = parts[1] if len(parts) > 1 else ""
        shares_raw = parts[2] if len(parts) > 2 else ""
        if ticker:
            shares = float(shares_raw) if shares_raw and _is_number(shares_raw) else None
            live   = _fetch_live_price(ticker)
            price  = live
            shares_str = f"  ·  <b>{shares} shares</b>" if shares else "  ·  full position"
            price_str  = f"<code>${_p(price)}</code>" if price else "<i>live price</i>"
            send_inline_keyboard(
                f"📄 <b>Confirm paper sell?</b>\n"
                f"<b>{ticker}</b>{shares_str}  @  {price_str}\n"
                f"<i>Tap ✅ to simulate, or type correct details to adjust.</i>",
                [[{"text": "✅ Confirm", "callback_data": f"psell_confirm|{ticker}|{price or ''}|{shares_raw}"},
                  {"text": "❌ Cancel",  "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )


# ── Pending reply handler ─────────────────────────────────────────────────────

def _handle_pending_reply(state: dict, text: str, chat_id: str) -> str:
    """
    Called when the user sends a plain message while a pending command state exists.
    Routes to the appropriate handler based on the saved command + step.
    """
    command = state["command"]
    step    = state.get("step", 1)
    data    = state.get("data", {})

    # State already consumed — always clear it first
    clear_pending_state(chat_id)

    # ── /bought ───────────────────────────────────────────────────────────────
    if command == "bought":
        raw = text.strip()
        if not raw:
            return "⚠️ Please tell me which stock or crypto you bought."

        # Use Haiku NLP to extract a list of asset names/tickers from natural language.
        # This handles "avery dennison, microsoft, CRM, solana and EEM" as well as
        # casual sentences like "I picked up some apple and a bit of tesla today".
        names_list = _nl_extract_tickers_list(raw)

        if len(names_list) == 1:
            # Single item — keep original behaviour with disambiguation prompt
            name_raw = names_list[0]
            candidates = _resolve_ticker_candidates(name_raw)
            if not candidates:
                return f"⚠️ Couldn't find a ticker for <b>{_esc(name_raw)}</b>. Try using the ticker symbol directly, e.g. <code>AVY</code>"
            if len(candidates) > 1:
                buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                             "callback_data": f"buy|{c['ticker']}"}]
                           for c in candidates]
                send_inline_keyboard(f"🔍 Which one did you mean by <b>{_esc(name_raw)}</b>?",
                                     buttons, chat_id=chat_id)
                return ""
            return _execute_bought(candidates[0]["ticker"], chat_id)

        # Multiple items — resolve and add each, report results
        results = []
        for name_raw in names_list:
            try:
                candidates = _resolve_ticker_candidates(name_raw)
                if not candidates:
                    results.append(f"⚠️ <b>{_esc(name_raw)}</b> — couldn't resolve ticker")
                    continue
                ticker = candidates[0]["ticker"]
                reply  = _execute_bought(ticker, chat_id)
                results.append(reply)
            except Exception as exc:
                results.append(f"⚠️ <b>{_esc(name_raw)}</b> — error: {exc}")
        return "\n\n".join(results)

    # ── /sold ─────────────────────────────────────────────────────────────────
    if command == "sold":
        if step == 2:
            # User typed an exit price after tapping a position from the picker
            ticker = data.get("ticker", "")
            if not ticker:
                return "⚠️ Couldn't find the ticker. Please try /sold again."
            if not _is_number(text.strip()):
                return "⚠️ Please send just the price, e.g. <code>197.50</code>"
            price = float(text.strip().replace(",", ""))
            # Show P&L preview
            log   = load_user_trade_log(chat_id)
            entry = None
            for t in log.get("open", []):
                if t["ticker"] == ticker:
                    entry = t.get("entry_price")
                    break
            pnl_preview = ""
            if entry:
                try:
                    ret  = (price - float(entry)) / float(entry) * 100
                    sign = "+" if ret >= 0 else ""
                    pnl_preview = f"\n<i>Return: {sign}{ret:.1f}% vs entry <code>${_p(entry)}</code></i>"
                except Exception:
                    pass
            send_inline_keyboard(
                f"💸 Close <b>{ticker}</b> at <code>${_p(price)}</code>?{pnl_preview}",
                [[{"text": f"✅ Yes, close {ticker}", "callback_data": f"sold_confirm|{ticker}|{price}|"},
                  {"text": "❌ Cancel",               "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""

        # step 1 — user typed a ticker name (natural language supported)
        raw_sold = text.strip()
        if not raw_sold:
            return "⚠️ Please tell me which stock or crypto you sold."

        # Use NL parse first so "sold my avery dennison" resolves correctly
        parsed_sold = _nl_parse_trade("sold", raw_sold)
        name_raw    = (parsed_sold.get("ticker") or "").strip() or None
        if not name_raw:
            # Fallback: resolve full phrase as-is
            name_raw = raw_sold

        candidates = _resolve_ticker_candidates(name_raw)
        if len(candidates) > 1:
            buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                         "callback_data": f"confirm_sell|{c['ticker']}"}]
                       for c in candidates]
            send_inline_keyboard(f"🔍 Which one did you sell?", buttons, chat_id=chat_id)
            return ""

        ticker = candidates[0]["ticker"]
        send_inline_keyboard(
            f"Remove <b>{ticker}</b> from your portfolio?",
            [[{"text": f"✅ Yes, I sold {ticker}", "callback_data": f"confirm_sell|{ticker}"},
              {"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return ""

    # ── Single-step param commands ────────────────────────────────────────────
    if command == "explain":
        return _explain_pick(text)

    if command == "feedback":
        feedback_text = text.strip()
        if not feedback_text:
            return "⚠️ Please include some feedback text."
        try:
            import requests as _req
            r = _req.get(
                f"{TELEGRAM_API}/getChat",
                params={"chat_id": chat_id}, timeout=5,
            )
            result     = r.json().get("result", {})
            first_name = result.get("first_name", "")
            username   = result.get("username", "")
        except Exception:
            first_name, username = "", ""
        add_feedback(chat_id, feedback_text, username=username, first_name=first_name)
        try:
            admin_id  = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
            name_str  = _esc(first_name) if first_name else f"<code>{chat_id}</code>"
            uname_str = f"  @{_esc(username)}" if username else ""
            send_message(
                f"💬 <b>New feedback</b> from {name_str}{uname_str}\n\n{_esc(feedback_text)}",
                chat_id=admin_id,
            )
        except Exception:
            pass
        return "✅ <b>Thanks for your feedback!</b> It's been sent to the team."

    if command == "dividends":
        log     = load_user_trade_log(chat_id)
        open_t  = log.get("open", [])
        tickers = [t["ticker"] for t in open_t if t.get("asset_type", "stock") == "stock"]
        if not tickers:
            return "📭 You have no open stock positions to check dividends for.\n\nLog a position with /bought first."
        send_message("💰 <i>Fetching dividend info…</i>", chat_id=chat_id)
        try:
            from dividends_checker import get_dividend_info, format_dividends_message
            info = get_dividend_info(tickers)
            return format_dividends_message(info)
        except Exception as exc:
            return f"⚠️ Could not fetch dividend data: {exc}"

    if command == "chart":
        raw_chart = text.strip()
        if not raw_chart:
            return "⚠️ Please provide a ticker, e.g. <code>AAPL</code>"
        # Resolve full phrase — handles "apple", "avery dennison", "NVDA", etc.
        chart_candidates = _resolve_ticker_candidates(raw_chart)
        ticker = chart_candidates[0]["ticker"].upper() if chart_candidates else raw_chart.upper()
        from chart_generator import is_crypto
        asset_type = "crypto" if is_crypto(ticker) else "stock"
        send_message("📊 <i>Generating chart…</i>", chat_id=chat_id)
        threading.Thread(
            target=_send_chart, args=(ticker, asset_type, chat_id), daemon=True
        ).start()
        return None

    if command in ("updatestop", "updatetarget"):
        field = "stop_loss" if command == "updatestop" else "target_price"
        if step == 1:
            # Received ticker — resolve full phrase then ask for price
            raw_upd = text.strip()
            if not raw_upd:
                return "⚠️ Please send the ticker, e.g. <code>NVDA</code>"
            upd_candidates = _resolve_ticker_candidates(raw_upd)
            ticker = upd_candidates[0]["ticker"].upper() if upd_candidates else raw_upd.upper()
            label = "stop-loss" if field == "stop_loss" else "target"
            save_pending_state(chat_id, command, step=2, data={"ticker": ticker})
            send_inline_keyboard(
                f"📝 New {label} price for <b>{ticker}</b>?",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""
        else:
            # step 2: received price
            ticker = data.get("ticker", "")
            if not ticker or not _is_number(text.strip()):
                return "⚠️ Please send just the new price, e.g. <code>118.50</code>"
            return _execute_update_level(ticker, field, float(text.strip().replace(",", "")), chat_id)

    if command == "accuracy":
        return _parse_and_execute("ACCURACY", original="/accuracy", chat_id=chat_id)

    if command == "define":
        return _parse_and_execute(f"DEFINE {text}".strip(), original=f"/define {text}".strip(), chat_id=chat_id)

    if command == "watch":
        return _parse_and_execute(f"WATCH {text}", original=f"/watch {text}", chat_id=chat_id)

    if command == "exclude":
        return _parse_and_execute(f"EXCLUDE {text}", original=f"/exclude {text}", chat_id=chat_id)

    if command == "set_risk":
        return _parse_and_execute(f"SET RISK {text}", original=text, chat_id=chat_id)

    if command == "set_budget":
        return _parse_and_execute(f"SET BUDGET {text}".strip(), original=f"/set_budget {text}", chat_id=chat_id)

    if command == "settings_budget_stock":
        raw = text.strip().lower()
        if raw in ("off", "0", "none", "clear", ""):
            update_user_config(chat_id, "stock_budget", None)
            send_message("✅ Stock budget cleared.", chat_id=chat_id)
        else:
            try:
                val = float(raw.replace(",", "").rstrip("k")) * (1000 if raw.endswith("k") else 1)
                update_user_config(chat_id, "stock_budget", val)
                send_message(f"✅ Stock budget → <b>${int(val)}/trade</b>", chat_id=chat_id)
            except ValueError:
                send_message("⚠️ Couldn't parse that. Try a number like <code>200</code>.", chat_id=chat_id)
        _send_settings_panel(chat_id)
        return ""

    if command == "settings_budget_crypto":
        raw = text.strip().lower()
        if raw in ("off", "0", "none", "clear", ""):
            update_user_config(chat_id, "crypto_budget", None)
            send_message("✅ Crypto budget cleared.", chat_id=chat_id)
        else:
            try:
                val = float(raw.replace(",", "").rstrip("k")) * (1000 if raw.endswith("k") else 1)
                update_user_config(chat_id, "crypto_budget", val)
                send_message(f"✅ Crypto budget → <b>${int(val)}/trade</b>", chat_id=chat_id)
            except ValueError:
                send_message("⚠️ Couldn't parse that. Try a number like <code>50</code>.", chat_id=chat_id)
        _send_settings_panel(chat_id)
        return ""

    if command == "settings_stop":
        raw = text.strip().replace("%", "")
        try:
            val = float(raw)
            update_user_config(chat_id, "stop_loss_pct", val)
            send_message(f"✅ Stop loss → <b>{val}%</b>", chat_id=chat_id)
        except ValueError:
            send_message("⚠️ Couldn't parse that. Try a number like <code>7</code>.", chat_id=chat_id)
        _send_settings_panel(chat_id)
        return ""

    if command == "settings_target":
        raw = text.strip().replace("%", "")
        try:
            val = float(raw)
            update_user_config(chat_id, "target_gain_pct", val)
            send_message(f"✅ Target gain → <b>{val}%</b>", chat_id=chat_id)
        except ValueError:
            send_message("⚠️ Couldn't parse that. Try a number like <code>15</code>.", chat_id=chat_id)
        _send_settings_panel(chat_id)
        return ""

    if command == "settings_portfolio_size":
        raw = text.strip().lower().replace(",", "")
        if raw in ("off", "0", "none", "clear", ""):
            existing = cfg.get("portfolio", {}) if isinstance(cfg.get("portfolio"), dict) else {}
            existing.pop("portfolio_size", None)
            update_user_config(chat_id, "portfolio", existing)
            send_message("✅ Portfolio capital cleared — position sizing disabled.", chat_id=chat_id)
        else:
            try:
                val = float(raw.rstrip("k")) * (1000 if raw.endswith("k") else 1)
                existing = cfg.get("portfolio", {}) if isinstance(cfg.get("portfolio"), dict) else {}
                update_user_config(chat_id, "portfolio", {**existing, "portfolio_size": val})
                send_message(
                    f"✅ Portfolio capital → <b>${int(val):,}</b>\n"
                    f"Position sizing is now active — each pick will include share counts and risk $.",
                    chat_id=chat_id,
                )
            except ValueError:
                send_message("⚠️ Couldn't parse that. Try a number like <code>25000</code> or <code>25k</code>.", chat_id=chat_id)
        _send_settings_panel(chat_id)
        return ""

    if command == "settings_portfolio_risk":
        raw = text.strip().replace("%", "")
        try:
            val = float(raw)
            if not (0.1 <= val <= 5.0):
                raise ValueError("out of range")
            existing = cfg.get("portfolio", {}) if isinstance(cfg.get("portfolio"), dict) else {}
            update_user_config(chat_id, "portfolio", {**existing, "risk_per_trade_pct": val})
            send_message(f"✅ Risk per trade → <b>{val}%</b> of portfolio capital per pick.", chat_id=chat_id)
        except ValueError:
            send_message("⚠️ Enter a number between 0.1 and 5 (e.g. <code>1</code> = 1% risk per trade).", chat_id=chat_id)
        _send_settings_panel(chat_id)
        return ""

    if command == "settings_portfolio_max_pos":
        raw = text.strip().replace("%", "")
        try:
            val = float(raw)
            if not (1.0 <= val <= 50.0):
                raise ValueError("out of range")
            existing = cfg.get("portfolio", {}) if isinstance(cfg.get("portfolio"), dict) else {}
            update_user_config(chat_id, "portfolio", {**existing, "max_position_pct": val})
            send_message(f"✅ Max position size → <b>{val}%</b> of portfolio per pick.", chat_id=chat_id)
        except ValueError:
            send_message("⚠️ Enter a number between 1 and 50 (e.g. <code>10</code> = 10% max per position).", chat_id=chat_id)
        _send_settings_panel(chat_id)
        return ""

    if command == "settings_portfolio_max_sector":
        raw = text.strip().replace("%", "")
        try:
            val = float(raw)
            if not (5.0 <= val <= 100.0):
                raise ValueError("out of range")
            existing = cfg.get("portfolio", {}) if isinstance(cfg.get("portfolio"), dict) else {}
            update_user_config(chat_id, "portfolio", {**existing, "max_sector_pct": val})
            send_message(f"✅ Max sector concentration → <b>{val}%</b>.", chat_id=chat_id)
        except ValueError:
            send_message("⚠️ Enter a number between 5 and 100 (e.g. <code>35</code> = 35% max per sector).", chat_id=chat_id)
        _send_settings_panel(chat_id)
        return ""

    if command == "settings_min_conviction":
        raw = text.strip()
        try:
            val = int(raw)
            if val not in (3, 4):
                raise ValueError("out of range")
            update_user_config(chat_id, "min_conviction", val)
            label = "★★★★ (high conviction only)" if val == 4 else "★★★ (includes moderate setups)"
            send_message(
                f"✅ Minimum conviction → <b>{label}</b>\n"
                f"<i>Takes effect from tomorrow's morning run.</i>",
                chat_id=chat_id,
            )
        except ValueError:
            send_message("⚠️ Enter <code>3</code> or <code>4</code>.", chat_id=chat_id)
        _send_settings_panel(chat_id)
        return ""

    if command == "alert":
        if step == 2:
            # User replied with price (and optional direction)
            ticker    = data.get("ticker", "")
            direction = data.get("direction", "auto")
            # Parse "above 900" / "below 800" / "900"
            parts_p = text.strip().split()
            if len(parts_p) >= 2 and parts_p[0].lower() in ("above", "below"):
                direction = parts_p[0].lower()
                price_str = parts_p[1]
            else:
                price_str = parts_p[0] if parts_p else ""
            try:
                from price_alert_manager import add_alert
                return add_alert(chat_id, ticker, float(price_str.replace(",", "")), direction)
            except (ValueError, IndexError):
                save_pending_state(chat_id, "alert", step=2,
                                   data={"ticker": ticker, "direction": direction})
                return f"🤔 Didn't catch that — enter a price like <code>850</code> or <code>above 900</code>"
        # Step 1: user replied with ticker
        return _parse_and_execute(f"ALERT {text}", original=f"/alert {text}", chat_id=chat_id)

    if command == "unalert":
        if step == 1:
            return _parse_and_execute(f"UNALERT {text}", original=f"/unalert {text}", chat_id=chat_id)
        return _parse_and_execute(f"UNALERT {text}", original=f"/unalert {text}", chat_id=chat_id)

    if command == "paper_buy":
        if step == 2:
            from paper_trader import paper_buy
            ticker = data.get("ticker", "")
            shares = data.get("shares")
            price_raw = text.strip() or None
            price = None
            if price_raw:
                try:
                    price = float(price_raw.replace(",", ""))
                except ValueError:
                    # NL reply like "87.5 each" or "at 83 dollars" — extract price
                    parsed_p = _nl_parse_trade("paper_buy", price_raw)
                    price = parsed_p.get("price")
                    # Also grab ticker/shares if user gave a full sentence like "Evrg 5 at 83.5"
                    if not ticker and parsed_p.get("ticker"):
                        ticker = parsed_p["ticker"]
                    if not shares and parsed_p.get("shares"):
                        shares = parsed_p["shares"]
            if not ticker:
                # Stale state — restart cleanly
                return _parse_and_execute("PAPER BUY", original="/paper_buy", chat_id=chat_id)
            return paper_buy(ticker, shares, chat_id, price)
        if step == 1:
            import re as _re
            ticker  = data.get("ticker", "")
            tickers = data.get("tickers", [])  # multi-ticker list

            if tickers:
                # Multi-ticker: parse share counts from "2 and 5" or "2 5"
                nums = [float(n) for n in _re.findall(r'\d+(?:\.\d+)?', text.replace(",", ""))]
                if len(nums) < len(tickers):
                    save_pending_state(chat_id, "paper_buy", step=1, data={"tickers": tickers})
                    names_str = ", ".join(f"<b>{t}</b>" for t in tickers)
                    return (f"🤔 Please enter {len(tickers)} share counts (one per ticker).\n"
                            f"e.g. <code>{' '.join(['1'] * len(tickers))}</code>")
                # Execute all at live prices
                from paper_trader import paper_buy as _pb
                results = []
                for i, t in enumerate(tickers):
                    r = _pb(t, nums[i], chat_id, None)
                    results.append(r)
                clear_pending_state(chat_id)
                send_message("\n\n".join(results), chat_id=chat_id)
                return ""

            # Single ticker — extract first number from reply
            nums = [float(n) for n in _re.findall(r'\d+(?:\.\d+)?', text.replace(",", ""))]

            # No numbers in reply → user probably typed a ticker/company name, restart flow
            if not nums:
                return _parse_and_execute(f"PAPER BUY {text}", original=f"/paper_buy {text}", chat_id=chat_id)

            shares = nums[0]
            # Also refresh ticker if empty (stale state) by re-routing
            if not ticker:
                return _parse_and_execute(f"PAPER BUY {text}", original=f"/paper_buy {text}", chat_id=chat_id)

            # Ask for price
            live = _fetch_live_price(ticker)
            live_hint = f"  <i>(live: <code>${_p(live)}</code>)</i>" if live else ""
            save_pending_state(chat_id, "paper_buy", step=2,
                               data={"ticker": ticker, "shares": shares})
            send_inline_keyboard(
                f"💰 At what price to simulate the buy for <b>{ticker}</b>?{live_hint}\n"
                f"<i>Send blank to use live price</i>",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""
        return _parse_and_execute(f"PAPER BUY {text}", original=f"/paper_buy {text}", chat_id=chat_id)

    if command == "paper_sell":
        if step == 2:
            # User replied with share count (optional — blank = sell all)
            from paper_trader import paper_sell
            ticker    = data.get("ticker", "")
            price_raw = data.get("price")

            # Stale state with no ticker — restart cleanly
            if not ticker:
                return _parse_and_execute("PAPER SELL", original="/paper_sell", chat_id=chat_id)

            shares = None
            raw    = text.strip()
            if raw:
                import re as _re
                nums = [float(n) for n in _re.findall(r'\d+(?:\.\d+)?', raw.replace(",", ""))]
                if nums:
                    shares = nums[0]
                else:
                    # No numbers — user may have typed a company name by mistake, restart
                    return _parse_and_execute(f"PAPER SELL {raw}", original=f"/paper_sell {raw}", chat_id=chat_id)
            return paper_sell(ticker, chat_id, shares, price_raw)

        if step == 1:
            # User replied with ticker (and optionally shares) — use NL parse
            raw_ps = text.strip()
            if not raw_ps:
                return "⚠️ Please tell me which stock to sell."

            parsed_ps  = _nl_parse_trade("paper_sell", raw_ps)
            name_raw   = (parsed_ps.get("ticker") or "").strip() or raw_ps
            shares_raw = str(parsed_ps["shares"]) if parsed_ps.get("shares") is not None else None

            # Shares might also appear as a bare number after the name ("apple 5")
            if shares_raw is None:
                parts = raw_ps.split()
                if len(parts) >= 2 and _is_number(parts[-1]):
                    shares_raw = parts[-1]

            candidates = _resolve_ticker_candidates(name_raw)
            if len(candidates) > 1:
                shares_enc = shares_raw or ""
                buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                             "callback_data": f"psell|{c['ticker']}|{shares_enc}"}]
                           for c in candidates]
                send_inline_keyboard(
                    f"🔍 Which stock did you mean by <b>{_esc(name_raw)}</b>?",
                    buttons, chat_id=chat_id)
                return ""

            ticker = candidates[0]["ticker"]
            if shares_raw is None:
                save_pending_state(chat_id, "paper_sell", step=2, data={"ticker": ticker})
                send_inline_keyboard(
                    f"📄 How many shares of <b>{ticker}</b> to simulate selling?\n"
                    f"<i>Send blank to sell your full position</i>",
                    [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""

            from paper_trader import paper_sell
            return paper_sell(ticker, chat_id, float(shares_raw), None)

        # Fallback (step=0 / unexpected)
        return _parse_and_execute(f"PAPER SELL {text}", original=f"/paper_sell {text}", chat_id=chat_id)

    if command == "paper_add_cash":
        from paper_trader import paper_add_cash
        raw = text.strip().replace(",", "")
        amount = None
        try:
            amount = float(raw[:-1]) * 1000 if raw.lower().endswith("k") else float(raw)
        except ValueError:
            parsed = _nl_parse_trade("paper_reset", raw)
            amount = parsed.get("price")
        if not amount:
            save_pending_state(chat_id, "paper_add_cash")
            return "🤔 How much? e.g. <code>5000</code> or <code>10k</code>"
        return paper_add_cash(amount, chat_id)

    if command == "start":
        return _parse_and_execute("START", original="/start", chat_id=chat_id)

    if command in ("share", "invite"):
        return _parse_and_execute("SHARE", original="/share", chat_id=chat_id)

    if command == "adduser":
        return _parse_and_execute(f"ADDUSER {text}", original=f"/adduser {text}", chat_id=chat_id)

    if command == "removeuser":
        return _parse_and_execute(f"REMOVEUSER {text}", original=f"/removeuser {text}", chat_id=chat_id)

    if command == "dividends":
        return _parse_and_execute("DIVIDENDS", original="/dividends", chat_id=chat_id)

    if command == "performance":
        return _parse_and_execute("PERFORMANCE", original="/performance", chat_id=chat_id)

    if command == "feedback":
        return _parse_and_execute(f"FEEDBACK {text}" if text else "FEEDBACK", original=original, chat_id=chat_id)

    if command == "test":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        send_message("🧪 <i>Running self-test… back in ~20s</i>", chat_id=chat_id)
        def _run_test():
            result = _parse_and_execute("TEST", original="/test", chat_id=chat_id)
            if result:
                send_message(result, chat_id=chat_id)
        threading.Thread(target=_run_test, daemon=True).start()
        return None

    if command == "dashboard":
        return _parse_and_execute("DASHBOARD", original="/dashboard", chat_id=chat_id)

    if command == "users":
        return _parse_and_execute("USERS", original="/users", chat_id=chat_id)

    if command == "pending":
        return _parse_and_execute("PENDING", original="/pending", chat_id=chat_id)

    if command == "broadcast":
        return _parse_and_execute(f"BROADCAST {text}", original=f"/broadcast {text}", chat_id=chat_id)

    if command == "release":
        # step=1: admin typed the edited release note text
        if step == 1:
            note_id = data.get("note_id", "")
            from release_tracker import mark_note_sent
            result = _send_release_broadcast(text, chat_id)
            if note_id:
                mark_note_sent(note_id, text)
            return result
        return _parse_and_execute(f"RELEASE {text}", original=f"/release {text}", chat_id=chat_id)

    return _handle_natural_language(text)


def _parse_and_execute(text: str, original: str = "", chat_id: str | None = None) -> str:
    """Parse command string and return reply."""
    chat_id = chat_id or _chat_id()

    # Telegram slash-commands (/help) or plain text (HELP) — normalise both
    text = text.lstrip("/").replace("_", " ")   # /set_st 30 → SET ST 30


    for _handler in (_cmd_market, _cmd_alerts, _cmd_paper, _cmd_misc,
                     _cmd_settings, _cmd_admin, _cmd_trades):
        _result = _handler(text, original, chat_id)
        if _result is not None:
            return _result

    # ── Natural language fallback ─────────────────────────────────────────────
    return _handle_natural_language(original or text, chat_id=chat_id)


# ── CLI test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    mock_picks = {
        "daily_summary": "Markets cautiously optimistic; crypto momentum building.",
        "stocks": {
            "short_term": [{
                "ticker": "AAPL", "company": "Apple Inc", "action": "BUY",
                "entry_price": 182.50, "target_price": 197.10, "stop_loss": 173.38,
                "allocation": 12.50, "conviction": 4,
                "thesis": "Breakout with volume confirms momentum.",
                "risk": "Macro headwinds could reverse quickly.",
            }],
            "long_term": [{
                "ticker": "MSFT", "company": "Microsoft Corp", "action": "BUY",
                "entry_price": 415.00, "target_price": 500.00,
                "allocation": 16.67, "conviction": 5,
                "thesis": "Cloud + AI growth drives long-term value.",
                "horizon": "2-3 years",
            }],
        },
        "crypto": {
            "short_term": [{
                "symbol": "BTC", "name": "Bitcoin", "action": "BUY",
                "entry_price": 65000, "target_price": 72000, "stop_loss": 61750,
                "allocation": 10.00, "conviction": 3,
                "thesis": "Momentum breakout above key resistance.",
                "risk": "High volatility; macro risk.",
            }],
            "long_term": [{
                "symbol": "ETH", "name": "Ethereum", "action": "BUY",
                "entry_price": 3200, "target_price": 5000,
                "allocation": 15.00, "conviction": 4,
                "thesis": "ETF inflows and staking yield drive demand.",
                "horizon": "12-18 months",
            }],
        },
        "disclaimer": "For informational purposes only. Not financial advice.",
    }
    mock_config = {
        "stock_budget": 200, "crypto_budget": 50,
    }
    msg = format_daily_message(mock_picks, mock_config)
    print(msg)
    print(f"\nLength: {len(msg)} chars")
