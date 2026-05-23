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
from cmd_trade_exec import _send_chart, _execute_bought, _execute_sold, _execute_update_level, _offer_setstop_if_needed
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

    # Record last-seen + log the command (background — non-blocking)
    def _record_seen():
        try:
            from datetime import datetime as _dt
            update_user_config(chat_id, "last_seen", _dt.utcnow().isoformat())
        except Exception:
            pass
        try:
            from config_manager import log_user_event
            cmd_label = text.split()[0][:40] if text else "(empty)"
            log_user_event(chat_id, "command", cmd_label)
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
        "onboard_goal":       "✅ Got it!",
        "onboard_horizon":    "✅ Got it!",
        "onboard_portfolio":  "✅ Got it!",
        "onboard_assets":     "✅ Setting up your account…",
        "quickbuy":           "📝 Confirm below…",
        "quickbuy_confirm":   "⏳ Logging position…",
        "chart":              "⏳ Generating chart…",
        "confirm_sell":       "⏳ Removing position…",
        "sold_pick":          "⏳ Loading…",
        "sold_review":        "⏳ Loading…",
        "sold_confirm":       "⏳ Closing position…",
        "bought_confirm":     "⏳ Logging position…",
        "buy_pick":           "🛒 Loading pick details…",
        "watch_pick":         "👁 Adding to watchlist…",
        "confirm_buy":        "⏳ Logging position…",
        "skip_buy":           "👍 Skipped.",
        "change_buy_amount":  "✏️ Enter your amount…",
        "updatelevel_pick":   "⏳ Loading…",
        "note_pick":          "📝 Loading…",
        "unalert_confirm":    "⏳ Removing alert…",
        "paper_reset_confirm":"⏳ Resetting…",
        "pause_confirm":      "⏳ Pausing picks…",
        "reset_confirm":      "⏳ Resetting settings…",
        "psell":              "⏳ Loading…",
        "psell_confirm":      "⏳ Closing paper position…",
        "sell":               "⏳ Closing position…",
        "alert":              "🔔 Setting alert…",
        "noop":               "",
        "setstop_skip":       "👍",
        "setstop_prompt":     "📉 Enter stop price…",
        "be_stop":            "⏳ Updating stop…",
        "be_stop_dismiss":    "👍 Got it.",
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
        send_typing_action(chat_id)
        threading.Thread(
            target=_send_chart, args=(ticker, asset_type, chat_id), daemon=True
        ).start()
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

    if action == "note_pick":
        # User tapped a position from the /note picker — prompt for note text
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            return
        log       = load_user_trade_log(chat_id)
        cur_notes = next((t.get("notes", "") for t in log.get("open", []) if t["ticker"] == ticker), "")
        hint      = f"\n<i>Current note: {cur_notes[:80]}</i>" if cur_notes else ""
        save_pending_state(chat_id, "note", step=2, data={"ticker": ticker})
        send_inline_keyboard(
            f"📝 <b>Note for {ticker}</b>{hint}\n<i>Type your note below:</i>",
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
        # Proceed to total portfolio size step
        send_inline_keyboard(
            (
                "💼 <b>Optional: Total Portfolio Size</b>\n\n"
                "How much total money do you have invested across all accounts? "
                "This helps me warn you if you're over-concentrated.\n\n"
                "<i>This is private and only used for guidance. Skip if you prefer.</i>"
            ),
            [
                [
                    {"text": "Under $5k",   "callback_data": "onboard_portfolio|5000"},
                    {"text": "Under $25k",  "callback_data": "onboard_portfolio|25000"},
                ],
                [
                    {"text": "Under $100k", "callback_data": "onboard_portfolio|100000"},
                    {"text": "Over $100k",  "callback_data": "onboard_portfolio|500000"},
                ],
                [
                    {"text": "Skip",        "callback_data": "onboard_portfolio|skip"},
                ],
            ],
            chat_id=chat_id,
        )
        return

    if action == "onboard_risk":
        key = parts[1] if len(parts) > 1 else ""
        if key not in _RISK_OPTIONS:
            return
        _, profile = _RISK_OPTIONS[key]
        update_user_config(chat_id, "risk_profile", profile)
        send_message(
            "⚖️ Risk profile saved.\n\n"
            "🛡 <b>Stop-Loss &amp; Target</b>\n\n"
            "A <b>stop-loss</b> is your safety net — if a stock drops this % from your entry, sell automatically to prevent bigger losses. (Default: 7%)\n\n"
            "A <b>target</b> is your profit goal — if a stock rises this %, consider taking your gains. (Default: 15%)\n\n"
            "<i>These defaults work well for most people. You can always adjust with /set_thresholds</i>",
            chat_id=chat_id,
        )
        # Proceed to investment goal step (new wizard step)
        send_inline_keyboard(
            "🎯 <b>What are you investing for?</b>\n\n"
            "This helps me tailor picks to your situation. There's no wrong answer.",
            [
                [
                    {"text": "🏠 House / Big Purchase", "callback_data": "onboard_goal|house"},
                    {"text": "🏖 Retirement",           "callback_data": "onboard_goal|retirement"},
                ],
                [
                    {"text": "📈 General Wealth",       "callback_data": "onboard_goal|wealth"},
                    {"text": "🧪 Learning / Practice",  "callback_data": "onboard_goal|learning"},
                ],
            ],
            chat_id=chat_id,
        )
        return

    if action == "onboard_goal":
        goal_value = parts[1] if len(parts) > 1 else ""
        if not goal_value:
            return
        log_g = load_user_trade_log(chat_id)
        log_g.setdefault("settings", {})["investment_goal"] = goal_value
        save_user_trade_log(chat_id, log_g)
        # Proceed to time horizon step
        send_inline_keyboard(
            (
                "\u23f3 <b>What's your time horizon?</b>\n\n"
                "How long are you planning to keep money in the market?\n\n"
                "<i>This affects how aggressively I pick \u2014 longer horizons can handle more volatility.</i>"
            ),
            [
                [
                    {"text": "\u26a1 Under 1 year",  "callback_data": "onboard_horizon|short"},
                    {"text": "\U0001f4c5 1\u20133 years",     "callback_data": "onboard_horizon|medium"},
                ],
                [
                    {"text": "\U0001f331 3\u201310 years",    "callback_data": "onboard_horizon|long"},
                    {"text": "\U0001f3d4 10+ years",     "callback_data": "onboard_horizon|verylong"},
                ],
            ],
            chat_id=chat_id,
        )
        return

    if action == "onboard_horizon":
        horizon_value = parts[1] if len(parts) > 1 else ""
        if not horizon_value:
            return
        log_h = load_user_trade_log(chat_id)
        log_h.setdefault("settings", {})["time_horizon"] = horizon_value
        save_user_trade_log(chat_id, log_h)
        # Proceed to asset class step (final question)
        send_inline_keyboard(
            "<b>Last question \u2014 Which asset classes do you want picks for?</b>\n"
            "<i>You can change this anytime in /settings.</i>",
            [[
                {"text": label, "callback_data": f"onboard_assets_{key2}"}
                for key2, (label, _, __) in _ASSET_OPTIONS.items()
            ]],
            chat_id=chat_id,
        )
        return

    if action in ("onboard_assets_stocks", "onboard_assets_stockscrypto", "onboard_assets_all") or action.startswith("onboard_assets_"):
        key = action[len("onboard_assets_"):]
        opt = _ASSET_OPTIONS.get(key)
        if opt:
            _, show_crypto, show_etfs = opt
            update_user_config_multi(chat_id, {"show_crypto": show_crypto, "show_etfs": show_etfs, "onboarded": True})
        else:
            update_user_config(chat_id, "onboarded", True)
        _send_onboarding_complete(chat_id)
        orientation = (
            "\U0001f4c5 <b>Here's what to expect every day:</b>\n\n"
            "\U0001f305 <b>Morning (market days)</b>\n"
            "Your daily picks arrive with entry price, position size, stop-loss and target already calculated. "
            "Just tap <b>[\u2705 Buy]</b> on anything you like.\n\n"
            "\u2600\ufe0f <b>During the day</b>\n"
            "I'll message you if:\n"
            "\u2022 News breaks on one of your positions\n"
            "\u2022 Markets get unusually volatile\n"
            "\u2022 A position is drifting toward its stop\n"
            "\u2022 A major economic event is happening tomorrow\n\n"
            "\U0001f514 <b>When a target or stop hits</b>\n"
            "You'll get an alert with a one-tap <b>[\u2705 Log as Sold]</b> button. "
            "No need to watch charts.\n\n"
            "\U0001f4ca <b>Friday evening</b>\n"
            "Your weekly wrap arrives automatically \u2014 P&amp;L, wins, losses, how you did vs the S&amp;P 500.\n\n"
            "\U0001f5d3 <b>Sunday morning</b>\n"
            "A personalised week-ahead briefing with upcoming earnings on your positions and key market events.\n\n"
            "<b>That's genuinely all you need to do.</b> The research, monitoring and alerts run automatically.\n\n"
            "Start with /today to see today's picks, or /help for all commands. Good luck! \U0001f3af"
        )
        send_message(orientation, chat_id=chat_id)
        return

    if action == "onboard_portfolio":
        portfolio_value = parts[1] if len(parts) > 1 else ""
        if not portfolio_value:
            return
        log_p = load_user_trade_log(chat_id)
        log_p.setdefault("settings", {})
        if portfolio_value != "skip":
            try:
                log_p["settings"]["total_portfolio_size"] = int(portfolio_value)
            except ValueError:
                pass
        save_user_trade_log(chat_id, log_p)
        # Proceed to risk profile step
        send_message(
            "⚖️ <b>Risk Profile</b>\n\n"
            "This tells me how cautious to be with your picks.\n\n"
            "\u2022 <b>Conservative</b> \u2014 Safer, slower-moving stocks. Smaller gains but fewer surprises. Good if you're new or don't like stress.\n"
            "\u2022 <b>Moderate</b> \u2014 A balance of growth and safety. Most people start here.\n"
            "\u2022 <b>Aggressive</b> \u2014 Higher-reward picks that can also drop fast. Better for experienced traders who can handle volatility.\n\n"
            "<i>You can change this anytime with /set_risk</i>",
            chat_id=chat_id,
        )
        send_inline_keyboard(
            "<b>What's your risk appetite?</b>",
            [[
                {"text": label, "callback_data": f"onboard_risk|{key2}"}
                for key2, (label, _) in _RISK_OPTIONS.items()
            ]],
            chat_id=chat_id,
        )
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

    elif action == "sell":
        # Ticker disambiguation for /sold — user picked from a multi-match list.
        # callback_data: sell|TICKER|PRICE|SHARES
        ticker     = parts[1].upper() if len(parts) > 1 else ""
        price_raw  = parts[2]         if len(parts) > 2 else None
        shares_raw = parts[3]         if len(parts) > 3 else None
        if not ticker:
            return
        try:
            reply = _execute_sold(ticker, chat_id, price=price_raw or None, shares_sold=shares_raw or None)
            send_message(reply, chat_id=chat_id)
        except Exception as exc:
            print(f"[bot] sell callback failed for {ticker}: {exc}")
            send_message(f"⚠️ Couldn't close <b>{ticker}</b> — try <code>/sold {ticker}</code> instead.", chat_id=chat_id)

    elif action == "alert":
        # Quick alert shortcut from pick/exit alert messages.
        # callback_data: alert|TICKER
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            return
        try:
            live = _fetch_live_price(ticker)
            price_hint = f"  (live: <code>${_p(live)}</code>)" if live else ""
            save_pending_state(chat_id, "alert", step=2, data={"ticker": ticker})
            send_inline_keyboard(
                f"🔔 <b>Set alert for {ticker}</b>{price_hint}\n"
                f"Type the target price (e.g. <code>500</code>), or type <code>{ticker} above 500</code>.",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
        except Exception as exc:
            print(f"[bot] alert callback failed for {ticker}: {exc}")
            send_message(f"⚠️ Use <code>/alert {ticker} above PRICE</code> to set an alert.", chat_id=chat_id)

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
            _offer_setstop_if_needed(ticker.upper(), chat_id)
        except Exception as exc:
            print(f"[bot] bought_confirm failed for {ticker}: {exc}")
            send_message(f"⚠️ Couldn't log <b>{ticker}</b> — try <code>/bought {ticker}</code> instead.", chat_id=chat_id)

    elif action == "setstop_prompt":
        # User tapped "Set Stop Loss" after logging a buy — ask for the stop price
        ticker = parts[1] if len(parts) > 1 else ""
        if not ticker:
            return
        save_pending_state(chat_id, "setstop", step=1, data={"ticker": ticker})
        send_inline_keyboard(
            f"📉 Stop loss price for <b>{ticker}</b>? (e.g. <code>170.00</code>)",
            [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )

    elif action == "setstop_use":
        # User tapped "Set Stop @ $X (ATR)" — apply the suggested stop directly
        ticker     = parts[1].upper() if len(parts) > 1 else ""
        stop_price = parts[2]         if len(parts) > 2 else ""
        if not ticker or not stop_price:
            return
        try:
            stop_val = float(stop_price)
        except ValueError:
            return
        result = _execute_update_level(ticker, "stop_loss", stop_val, chat_id)
        # Also auto-create a price alert at this stop level
        try:
            from price_alert_manager import add_alert
            add_alert(chat_id, ticker, stop_val, direction="below", auto=True)
        except Exception:
            pass
        send_message(result, chat_id=chat_id)

    elif action == "journal_prompt":
        # User tapped "Add a note" on the post-sell journal prompt
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            return
        save_pending_state(chat_id, "journal_note", step=1, data={"ticker": ticker})
        send_inline_keyboard(
            f"📓 What did you learn from your <b>{ticker}</b> trade?\n"
            f"<i>Type your note below — a sentence or two is plenty.</i>",
            [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )

    elif action == "journal_skip":
        # User tapped Skip on journal prompt — nothing to do
        return

    elif action == "setstop_skip":
        # User tapped Skip — toast already sent via _TOASTS above
        return

    elif action == "be_stop":
        # User tapped "✅ Set stop to break-even" from the +8% nudge.
        # callback_data: be_stop|TICKER|ENTRY_PRICE
        ticker     = parts[1].upper() if len(parts) > 1 else ""
        entry_str  = parts[2] if len(parts) > 2 else ""
        if not (ticker and entry_str):
            return
        try:
            from cmd_trade_exec import _execute_update_level
            result = _execute_update_level(ticker, "stop", entry_str, chat_id)
            send_message(
                result or f"✅ Stop for <b>{ticker}</b> moved to break-even (<code>${entry_str}</code>). "
                f"You can't lose on this trade now.",
                chat_id=chat_id,
            )
        except Exception as exc:
            send_message(f"❌ Couldn't update stop: {exc}", chat_id=chat_id)

    elif action == "be_stop_dismiss":
        # User tapped "Not yet" — acknowledge silently
        return

    elif action == "watch_pick":
        # User tapped [👁 Watch] on a morning pick.
        # callback_data: watch_pick|TICKER
        ticker = parts[1].upper() if len(parts) > 1 else ""
        if not ticker:
            return
        try:
            from config_manager import get_user_config, update_user_config
            cfg       = get_user_config(chat_id)
            watchlist = list(cfg.get("watchlist") or [])
            if ticker.upper() not in [w.upper() for w in watchlist]:
                watchlist.append(ticker.upper())
                update_user_config(chat_id, "watchlist", watchlist)
                send_message(
                    f"👁 <b>{ticker}</b> added to your watchlist.\n"
                    f"<i>You'll see it in /watchlist and the Mini App dashboard.</i>",
                    chat_id=chat_id,
                )
            else:
                send_message(
                    f"👁 <b>{ticker}</b> is already on your watchlist.",
                    chat_id=chat_id,
                )
        except Exception as exc:
            send_message(f"⚠️ Could not add to watchlist: {exc}", chat_id=chat_id)
        return

    elif action == "buy_pick":
        # User tapped [✅ Buy TICKER] on the morning picks message.
        # callback_data: buy_pick|TICKER|ENTRY|SHARES|ASSET_TYPE|STOP_PCT|TARGET_PCT
        ticker     = parts[1].upper() if len(parts) > 1 else ""
        entry_raw  = parts[2]         if len(parts) > 2 else ""
        shares_raw = parts[3]         if len(parts) > 3 else "1"
        asset_type = parts[4]         if len(parts) > 4 else "stock"
        stop_pct   = parts[5]         if len(parts) > 5 else "7"
        target_pct = parts[6]         if len(parts) > 6 else "15"
        if not ticker or not entry_raw:
            return
        try:
            entry      = float(entry_raw)
            shares     = int(shares_raw) if shares_raw.isdigit() else 1
            sp         = float(stop_pct)
            tp         = float(target_pct)
            stop_price  = round(entry * (1 - sp / 100), 2)
            target_price = round(entry * (1 + tp / 100), 2)
            # Look up company name from today's picks (best-effort)
            try:
                _picks = load_picks() or {}
                _all_picks = (
                    _picks.get("stocks", {}).get("short_term", []) +
                    _picks.get("stocks", {}).get("long_term",  []) +
                    _picks.get("crypto", {}).get("short_term", []) +
                    _picks.get("etfs",   {}).get("short_term", []) +
                    _picks.get("etfs",   {}).get("long_term",  []) +
                    _picks.get("commodities", {}).get("short_term", []) +
                    _picks.get("commodities", {}).get("long_term",  [])
                )
                company = next(
                    (
                        p.get("company") or p.get("name") or ""
                        for p in _all_picks
                        if (p.get("ticker") or p.get("symbol") or "").upper() == ticker
                    ),
                    "",
                )
            except Exception:
                company = ""
            company_suffix = (" — " + _esc(company)) if company else ""
            confirm_msg = (
                "🛒 <b>Ready to log your buy?</b>\n"
                + f"📌 <b>{ticker}</b>{company_suffix}\n"
                + f"💰 Entry: <code>${_p(entry)}</code>\n"
                + f"📦 Shares: {shares} (based on your budget)\n"
                + f"🛡 Stop-loss: <code>${_p(stop_price)}</code> ({sp}% below entry)\n"
                + f"🎯 Target: <code>${_p(target_price)}</code> ({tp}% above entry)\n\n"
                + "Tap Confirm to log this position."
            )
            confirm_cb     = f"confirm_buy|{ticker}|{entry_raw}|{shares}|{asset_type}|{stop_price}|{target_price}"
            skip_cb        = f"skip_buy|{ticker}"
            change_amt_cb  = f"change_buy_amount|{ticker}|{entry_raw}|{asset_type}|{stop_pct}|{target_pct}"
            send_inline_keyboard(
                confirm_msg,
                [
                    [
                        {"text": "✅ Confirm Buy", "callback_data": confirm_cb},
                        {"text": "❌ Skip",        "callback_data": skip_cb},
                    ],
                    [
                        {"text": f"✏️ Edit entry price  ·  ${_p(entry)}/share", "callback_data": change_amt_cb},
                    ],
                ],
                chat_id=chat_id,
            )
        except Exception as exc:
            print(f"[bot] buy_pick failed for {ticker}: {exc}")
            send_message(f"⚠️ Something went wrong — try <code>/bought {ticker}</code> instead.", chat_id=chat_id)
        return

    elif action == "change_buy_amount":
        # User tapped [✏️ Change Amount] — ask for a dollar amount then re-show confirmation.
        # callback_data: change_buy_amount|TICKER|ENTRY|ASSET_TYPE|STOP_PCT|TARGET_PCT
        ticker     = parts[1].upper() if len(parts) > 1 else ""
        entry_raw  = parts[2]         if len(parts) > 2 else ""
        asset_type = parts[3]         if len(parts) > 3 else "stock"
        stop_pct   = parts[4]         if len(parts) > 4 else "7"
        target_pct = parts[5]         if len(parts) > 5 else "15"
        if not ticker or not entry_raw:
            send_message("⚠️ Couldn't load pick details. Try <code>/bought {}</code> instead.".format(ticker), chat_id=chat_id)
            return
        try:
            entry = float(entry_raw)
        except ValueError:
            send_message("⚠️ Invalid entry price.", chat_id=chat_id)
            return
        save_pending_state(chat_id, "change_buy_amount", step=1, data={
            "ticker":     ticker,
            "entry":      entry_raw,
            "asset_type": asset_type,
            "stop_pct":   stop_pct,
            "target_pct": target_pct,
        })
        send_inline_keyboard(
            f"✏️ <b>What price did you pay per share of {ticker}?</b>\n"
            f"<i>Suggested entry: <code>${_p(entry)}</code> — type your actual fill price</i>",
            [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return

    elif action == "confirm_buy":
        # User confirmed the buy from the pick confirmation message.
        # callback_data: confirm_buy|TICKER|ENTRY|SHARES|ASSET_TYPE|STOP_PRICE|TARGET_PRICE
        ticker       = parts[1].upper() if len(parts) > 1 else ""
        entry_raw    = parts[2]         if len(parts) > 2 else ""
        shares_raw   = parts[3]         if len(parts) > 3 else None
        stop_raw     = parts[5]         if len(parts) > 5 else None
        target_raw   = parts[6]         if len(parts) > 6 else None
        if not ticker:
            return
        try:
            result = _execute_bought(ticker, chat_id, price=entry_raw or None, shares=shares_raw or None)
            # Override stop/target in trade log if we have them
            if stop_raw or target_raw:
                try:
                    log = load_user_trade_log(chat_id)
                    for t in log.get("open", []):
                        if t["ticker"] == ticker:
                            if stop_raw:
                                t["stop_loss"] = float(stop_raw)
                            if target_raw:
                                t["target_price"] = float(target_raw)
                            break
                    save_user_trade_log(chat_id, log)
                except Exception as exc2:
                    print(f"[bot] confirm_buy stop/target override failed (non-critical): {exc2}")
            entry_str  = f"<code>${_p(float(entry_raw))}</code>" if entry_raw else ""
            shares_str = f" — {shares_raw} shares" if shares_raw else ""
            stop_str   = f"<code>${_p(float(stop_raw))}</code>"   if stop_raw   else "—"
            target_str = f"<code>${_p(float(target_raw))}</code>" if target_raw else "—"
            success_msg = (
                "✅ <b>Position logged!</b>\n"
                + f"<b>{ticker}</b>{shares_str} at {entry_str}\n"
                + f"Stop: {stop_str}  |  Target: {target_str}\n"
                + "Track it anytime with /positions"
            )
            send_message(success_msg, chat_id=chat_id, parse_mode="HTML")
            try:
                from config_manager import increment_buy_count
                cfg2 = get_config()
                if cfg2.get("show_buy_counts"):
                    increment_buy_count(ticker)
            except Exception as exc3:
                print(f"[bot] buy count increment failed (non-critical): {exc3}")
        except Exception as exc:
            print(f"[bot] confirm_buy failed for {ticker}: {exc}")
            send_message(f"⚠️ Couldn't log <b>{ticker}</b> — try <code>/bought {ticker}</code> instead.", chat_id=chat_id, parse_mode="HTML")
        return

    elif action == "skip_buy":
        # User tapped Skip on the buy confirmation.
        # callback_data: skip_buy|TICKER
        ticker = parts[1].upper() if len(parts) > 1 else ""
        msg    = f"No problem — you can log it later with <code>/bought {ticker}</code>" if ticker else "No problem."
        send_message(msg, chat_id=chat_id)
        return

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

            # Journal prompt — ask what the user learned (non-blocking)
            if result.startswith("✅"):
                try:
                    send_inline_keyboard(
                        f"📓 <b>Trade Journal</b>\nWhat did you learn from this <b>{ticker}</b> trade? "
                        f"(Optional — helps you improve over time)",
                        [[{"text": "✍️ Add a note", "callback_data": f"journal_prompt|{ticker.upper()}"},
                          {"text": "Skip",           "callback_data": f"journal_skip|{ticker.upper()}"}]],
                        chat_id=chat_id,
                    )
                except Exception:
                    pass
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
            resolved_ticker = candidates[0]["ticker"]
            result = _execute_bought(resolved_ticker, chat_id)
            _offer_setstop_if_needed(resolved_ticker, chat_id)
            return result

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
    if command == "change_buy_amount":
        # User typed their actual fill price per share — recalculate and re-show confirmation.
        raw_price  = text.strip().replace(",", "").replace("$", "")
        if not _is_number(raw_price):
            return "⚠️ Please send just the price per share, e.g. <code>61.50</code>"
        new_entry  = float(raw_price)
        ticker     = data.get("ticker", "")
        entry_raw  = data.get("entry", "")
        asset_type = data.get("asset_type", "stock")
        stop_pct   = data.get("stop_pct", "7")
        target_pct = data.get("target_pct", "15")
        try:
            orig_entry   = float(entry_raw)
            sp           = float(stop_pct)
            tp           = float(target_pct)
            stop_price   = round(new_entry * (1 - sp / 100), 2)
            target_price = round(new_entry * (1 + tp / 100), 2)
        except (ValueError, ZeroDivisionError):
            return "⚠️ Couldn't calculate levels. Try <code>/bought {}</code> instead.".format(ticker)
        # ── Sanity check: flag if price looks like a typo ─────────────────────
        warning_line = ""
        if orig_entry > 0:
            ratio = new_entry / orig_entry
            if ratio >= 10:
                warning_line = (
                    f"\n⚠️ <b>Heads up:</b> <code>${_p(new_entry)}</code> is "
                    f"{ratio:.0f}× the suggested price of <code>${_p(orig_entry)}</code> — typo?"
                )
            elif ratio <= 0.1:
                warning_line = (
                    f"\n⚠️ <b>Heads up:</b> <code>${_p(new_entry)}</code> is far below "
                    f"the suggested <code>${_p(orig_entry)}</code> — typo?"
                )
        new_entry_raw = str(new_entry)
        confirm_msg = (
            "🛒 <b>Ready to log your buy?</b>\n"
            + f"📌 <b>{ticker}</b>\n"
            + f"💰 Entry: <code>${_p(new_entry)}</code>\n"
            + f"🛡 Stop-loss: <code>${_p(stop_price)}</code> ({sp}% below entry)\n"
            + f"🎯 Target: <code>${_p(target_price)}</code> ({tp}% above entry)"
            + warning_line + "\n\n"
            + "Tap Confirm to log this position."
        )
        confirm_cb    = f"confirm_buy|{ticker}|{new_entry_raw}||{asset_type}|{stop_price}|{target_price}"
        skip_cb       = f"skip_buy|{ticker}"
        change_amt_cb = f"change_buy_amount|{ticker}|{new_entry_raw}|{asset_type}|{stop_pct}|{target_pct}"
        send_inline_keyboard(
            confirm_msg,
            [
                [
                    {"text": "✅ Confirm Buy", "callback_data": confirm_cb},
                    {"text": "❌ Skip",        "callback_data": skip_cb},
                ],
                [
                    {"text": f"✏️ Bought at ${_p(new_entry)}/share", "callback_data": change_amt_cb},
                ],
            ],
            chat_id=chat_id,
        )
        return ""

    if command == "explain":
        return _explain_pick(text)

    if command == "ask":
        # /ask <question> — portfolio-aware AI Q&A in background thread
        query = text.strip()
        if not query:
            return (
                "🤖 <b>Ask me anything</b>\n\n"
                "Examples:\n"
                "  • <code>/ask Should I hold my NVDA position?</code>\n"
                "  • <code>/ask What's a good stop-loss strategy?</code>\n"
                "  • <code>/ask Why is tech selling off today?</code>"
            )
        from cmd_nlp import _ask_ai
        send_message("🤔 <i>Thinking…</i>", chat_id=chat_id)
        def _run_ask():
            answer = _ask_ai(query, chat_id)
            send_message(answer, chat_id=chat_id)
        threading.Thread(target=_run_ask, daemon=True).start()
        return None

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

    if command == "setstop":
        # User typed a stop loss price after the post-buy prompt
        ticker = data.get("ticker", "")
        raw_stop = text.strip().replace(",", "").replace("$", "")
        if not ticker:
            return "⚠️ Couldn't find the ticker. Please use <code>/updatestop TICKER PRICE</code> instead."
        if not _is_number(raw_stop):
            return "⚠️ Please send just the price, e.g. <code>170.00</code>"
        stop_price = float(raw_stop)
        try:
            _log = load_user_trade_log(chat_id)
            for t in _log.get("open", []):
                if t["ticker"] == ticker:
                    t["stop_loss"] = stop_price
                    break
            save_user_trade_log(chat_id, _log)
            return f"🛑 Stop loss set for <b>{ticker}</b> at <code>${_p(stop_price)}</code>\n<i>I'll alert you if the price drops to this level.</i>"
        except Exception as exc:
            return f"⚠️ Couldn't set stop loss: {exc}"

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

    if command == "note":
        # step 2: received note text for a ticker
        ticker = data.get("ticker", "")
        if not ticker:
            return "⚠️ Couldn't find the ticker. Please try /note TICKER again."
        note_text = text.strip()[:500]
        if not note_text:
            return "⚠️ Note can't be empty."
        log = load_user_trade_log(chat_id)
        saved = False
        for t in log.get("open", []):
            if t["ticker"] == ticker:
                t["notes"] = note_text
                saved = True
                break
        if not saved:
            return f"⚠️ <b>{ticker}</b> not found in your open positions."
        save_user_trade_log(chat_id, log)
        return f"📝 Note saved for <b>{ticker}</b>:\n<i>{note_text[:200]}</i>"

    if command == "journal_note":
        # User typed a lesson/reflection after closing a trade
        ticker    = data.get("ticker", "")
        note_text = text.strip()[:600]
        if not note_text:
            return "⚠️ Note can't be empty — skipping journal entry."
        log = load_user_trade_log(chat_id)
        # Store on the most-recently-closed trade for this ticker
        saved = False
        for t in reversed(log.get("closed", [])):
            if t.get("ticker") == ticker:
                t["journal_note"] = note_text
                saved = True
                break
        if saved:
            save_user_trade_log(chat_id, log)
            return (
                f"📓 <b>Journal saved</b> for {ticker}:\n"
                f"<i>{_esc(note_text[:300])}</i>\n\n"
                f"<i>View all your notes with /journal</i>"
            )
        return f"⚠️ Couldn't find a closed trade for <b>{ticker}</b> to attach the note to."

    if command == "accuracy":
        return _parse_and_execute("ACCURACY", original="/accuracy", chat_id=chat_id)

    if command == "define":
        return _parse_and_execute(f"DEFINE {text}".strip(), original=f"/define {text}".strip(), chat_id=chat_id)

    if command == "watch":
        return _parse_and_execute(f"WATCH {text}", original=f"/watch {text}", chat_id=chat_id)

    if command in ("track", "untrack"):
        ticker = text.strip().upper()
        if not ticker:
            return ("📌 <b>Usage:</b> <code>/track NVDA</code> — track a price in your watchlist\n"
                    "<code>/untrack NVDA</code> — remove it")
        from config_manager import load_user_trade_log, save_user_trade_log
        log = load_user_trade_log(chat_id)
        watchlist = log.get("watchlist", [])
        if command == "track":
            if ticker in watchlist:
                return f"👁 <b>{ticker}</b> is already on your watchlist."
            watchlist.append(ticker)
            log["watchlist"] = watchlist
            save_user_trade_log(chat_id, log)
            return (f"✅ <b>{ticker}</b> added to your price watchlist.\n"
                    f"You now have {len(watchlist)} ticker(s) tracked.\n"
                    f"<i>Open the dashboard to view live prices.</i>")
        else:  # untrack
            if ticker not in watchlist:
                return f"⚠️ <b>{ticker}</b> is not on your watchlist."
            watchlist = [t for t in watchlist if t != ticker]
            log["watchlist"] = watchlist
            save_user_trade_log(chat_id, log)
            return f"🗑 <b>{ticker}</b> removed from your watchlist."

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

    if command == "invite":
        return _parse_and_execute("SHARE", original="/invite", chat_id=chat_id)

    if command in ("tradeshare", "share"):
        arg      = text.strip().upper() if text.strip() else ""
        cmd_text = f"TRADESHARE {arg}" if arg else "TRADESHARE"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command == "trim":
        arg      = text.strip() if text.strip() else ""
        cmd_text = f"TRIM {arg.upper()}" if arg else "TRIM"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command == "size":
        arg      = text.strip().upper() if text.strip() else ""
        cmd_text = f"SIZE {arg}" if arg else "SIZE"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command == "pause":
        arg      = text.strip().upper() if text.strip() else ""
        cmd_text = f"PAUSE {arg}" if arg else "PAUSE"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command == "import":
        # text may be empty (shows help) or contain CSV lines
        body     = text.strip()
        cmd_text = f"IMPORT\n{body}" if body else "IMPORT"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command in ("bestsetup", "best_setup", "setup"):
        return _parse_and_execute("BESTSETUP", original=original, chat_id=chat_id)

    if command == "playbook":
        body     = text.strip()
        cmd_text = f"PLAYBOOK {body}" if body else "PLAYBOOK"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command == "remind":
        body     = text.strip()
        cmd_text = f"REMIND {body}" if body else "REMIND"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command in ("quiethours", "quiet", "dnd"):
        body     = text.strip()
        cmd_text = f"QUIETHOURS {body}" if body else "QUIETHOURS"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command == "add":
        arg      = text.strip() if text.strip() else ""
        cmd_text = f"ADD {arg.upper()}" if arg else "ADD"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command == "goal":
        arg      = text.strip() if text.strip() else ""
        cmd_text = f"GOAL {arg}" if arg else "GOAL"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

    if command == "missed":
        return _parse_and_execute("MISSED", original="/missed", chat_id=chat_id)

    if command == "review":
        return _parse_and_execute("REVIEW", original="/review", chat_id=chat_id)

    if command == "since":
        arg = text.strip() if text.strip() else ""
        return _parse_and_execute(f"SINCE {arg}" if arg else "SINCE", original=original, chat_id=chat_id)

    if command == "adduser":
        return _parse_and_execute(f"ADDUSER {text}", original=f"/adduser {text}", chat_id=chat_id)

    if command == "removeuser":
        return _parse_and_execute(f"REMOVEUSER {text}", original=f"/removeuser {text}", chat_id=chat_id)

    if command == "dividends":
        return _parse_and_execute("DIVIDENDS", original="/dividends", chat_id=chat_id)

    if command == "size":
        arg = text.strip().upper() if text.strip() else ""
        cmd_text = f"SIZE {arg}" if arg else "SIZE"
        return _parse_and_execute(cmd_text, original=original, chat_id=chat_id)

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


    try:
        for _handler in (_cmd_market, _cmd_alerts, _cmd_paper, _cmd_misc,
                         _cmd_settings, _cmd_admin, _cmd_trades):
            _result = _handler(text, original, chat_id)
            if _result is not None:
                return _result

        # ── Natural language fallback ─────────────────────────────────────────
        return _handle_natural_language(original or text, chat_id=chat_id)
    except Exception as _top_exc:
        import traceback
        _tb = traceback.format_exc()
        print(f"[bot] Unhandled error for {chat_id} cmd={original!r}: {_top_exc}\n{_tb}")
        try:
            from config_manager import log_user_event
            log_user_event(chat_id, "error", f"{original!r}: {type(_top_exc).__name__}: {_top_exc}", level="error")
        except Exception:
            pass
        return "⚠️ Something went wrong. Please try again in a moment."


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
