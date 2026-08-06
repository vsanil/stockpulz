"""
cmd_admin.py — Admin commands extracted from bot_commands.py.
"""
from __future__ import annotations

import os
import threading

from telegram_api import send_message, send_inline_keyboard, TELEGRAM_API
from config_manager import (
    get_config, update_config, get_user_config, update_user_config, update_user_config_multi,
    reset_user_config, load_user_trade_log, get_allowed_users, get_pending_users,
    remove_pending_user, load_feedback, mark_feedback_read, count_unread_feedback,
)
from formatters import _esc, _p
from cmd_helpers import (
    _is_admin, _make_admin_invite_token, _verify_admin_invite_token,
    _send_release_broadcast, _get_client, _fetch_live_price,
)
from cmd_settings import _send_settings_panel, _start_onboarding_wizard, _send_onboarding_complete, _prompt_for_param


def _cmd_admin(text: str, original: str, chat_id: str) -> "str | None":
    """Admin user-management commands."""
    # ── Admin: user management ────────────────────────────────────────────────
    if text.startswith("ADDUSER ") or text == "ADDUSER":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        parts = text.split()
        if len(parts) < 2:
            return "Usage: /adduser <chat_id>"
        from config_manager import add_allowed_user
        new_id = parts[1].strip()
        add_allowed_user(new_id)
        remove_pending_user(new_id)
        # Start the interactive onboarding wizard for the new user
        _start_onboarding_wizard(new_id)
        return f"✅ <code>{new_id}</code> approved — onboarding wizard sent."

    if text.startswith("REMOVEUSER ") or text == "REMOVEUSER":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        parts = text.split()
        if len(parts) < 2:
            return "Usage: /removeuser <chat_id>"
        from config_manager import remove_allowed_user
        rem_id = parts[1].strip()
        try:
            remove_allowed_user(rem_id)
            return f"✅ Removed <code>{rem_id}</code> from allowlist."
        except ValueError as e:
            return f"❌ {e}"

    # ── /feedback — submit feedback (everyone, including admin) ─────────────
    if text == "FEEDBACK":
        _prompt_for_param("feedback", chat_id)
        return ""

    # ── /feedbacks — admin view of all submitted feedback ────────────────────
    if text == "FEEDBACKS":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        entries = load_feedback()
        if not entries:
            return "💬 <b>No feedback yet.</b>"
        mark_feedback_read()
        lines = [f"<b>💬 Feedback ({len(entries)} total)</b>\n"]
        for e in entries[:20]:
            name     = _esc(e.get("first_name") or e.get("chat_id", "?"))
            uname    = f"  @{_esc(e['username'])}" if e.get("username") else ""
            date_str = e.get("submitted_at", "")[:10]
            lines.append(f"<b>{name}</b>{uname}  <i>{date_str}</i>")
            lines.append(f"{_esc(e['text'])}\n")
        if len(entries) > 20:
            lines.append(f"<i>…and {len(entries) - 20} more</i>")
        return "\n".join(lines)

    if text.startswith("FEEDBACK "):
        import requests as _req
        from config_manager import add_feedback
        raw_feedback = original.split(" ", 1)[1].strip() if " " in original else ""
        if not raw_feedback:
            return "⚠️ Please include your feedback text. e.g. <code>/feedback I love this bot!</code>"
        # Get user profile for context
        try:
            r = _req.get(
                f"{TELEGRAM_API}/getChat",
                params={"chat_id": chat_id}, timeout=5,
            )
            result    = r.json().get("result", {})
            first_name = result.get("first_name", "")
            username   = result.get("username", "")
        except Exception:
            first_name, username = "", ""
        add_feedback(chat_id, raw_feedback, username=username, first_name=first_name)
        # Notify admin
        try:
            admin_id  = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
            name_str  = _esc(first_name) if first_name else f"<code>{chat_id}</code>"
            uname_str = f"  @{_esc(username)}" if username else ""
            send_message(
                f"💬 <b>New feedback</b> from {name_str}{uname_str}\n\n{_esc(raw_feedback)}",
                chat_id=admin_id,
            )
        except Exception:
            pass
        return "✅ <b>Thanks for your feedback!</b> It's been sent to the team."

    # ── /dashboard — admin overview ───────────────────────────────────────────
    if text == "DASHBOARD":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        from datetime import datetime as _dt, timezone as _tz
        from trade_logger import get_performance_stats

        users   = get_allowed_users()
        owner   = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
        pending = get_pending_users()
        now_utc = _dt.now(_tz.utc)

        # ── Active users (seen in last 7 days) ────────────────────────────────
        active_7d = 0
        total_open = 0
        best_uid, best_win_rate, best_trades = None, 0, 0

        for uid in users:
            ucfg = get_user_config(uid)
            last_seen = ucfg.get("last_seen")
            if last_seen:
                try:
                    delta = (now_utc - _dt.fromisoformat(last_seen)).days
                    if delta <= 7:
                        active_7d += 1
                except Exception:
                    pass

            log = load_user_trade_log(uid)
            total_open += len(log.get("open", []))

            stats = get_performance_stats(uid)
            if stats and stats["count"] >= 3 and stats["win_rate"] > best_win_rate:
                best_win_rate = stats["win_rate"]
                best_trades   = stats["count"]
                best_uid      = uid

        # ── Last morning run ──────────────────────────────────────────────────
        cfg = get_config()
        last_run = cfg.get("last_morning_run")
        if last_run:
            try:
                run_dt  = _dt.fromisoformat(last_run)
                run_ago = (now_utc - run_dt).seconds // 60
                if run_ago < 60:
                    run_str = f"{run_ago}m ago"
                else:
                    run_str = run_dt.strftime("%b %d  %H:%M UTC")
            except Exception:
                run_str = last_run[:16]
        else:
            run_str = "unknown"

        # ── Build message ─────────────────────────────────────────────────────
        lines = ["<b>📊 StockPulz Dashboard</b>\n"]

        lines.append(f"👥 <b>Users</b>  {len(users)} total  ·  {active_7d} active last 7d")
        if pending:
            lines.append(f"⏳ <b>Pending</b>  {len(pending)} request(s) — /pending to action")
        else:
            lines.append("✅ <b>Pending</b>  No requests")

        lines.append(f"\n📈 <b>Open positions</b>  {total_open} across all users")

        if best_uid:
            tag = " (you)" if best_uid == owner else f"  <code>{best_uid}</code>"
            lines.append(f"🏆 <b>Top user</b>{tag}  {best_win_rate}% win rate  ·  {best_trades} trades")
        else:
            lines.append("🏆 <b>Top user</b>  Not enough data yet (need ≥3 trades)")

        lines.append(f"\n🤖 <b>Last morning run</b>  {run_str}")

        unread = count_unread_feedback()
        if unread:
            lines.append(f"💬 <b>Feedback</b>  {unread} unread — /feedback to view")
        else:
            lines.append("💬 <b>Feedback</b>  No new feedback")

        return "\n".join(lines)

    # ── /test — live NL + routing smoke test (admin only) ────────────────────
    if text == "TEST":
        if not _is_admin(chat_id):
            return "🔒 Admin only."

        from cmd_helpers import _resolve_ticker_candidates, _nl_param
        from cmd_nlp import _nl_extract_tickers_list, _nl_parse_trade

        send_message("🧪 <i>Running bot self-test… (uses Haiku, takes ~20s)</i>", chat_id=chat_id)

        lines   = ["<b>🧪 Bot Self-Test Results</b>\n"]
        passed  = 0
        failed  = 0

        def _chk(label: str, actual, expected):
            nonlocal passed, failed
            ok = str(actual).upper() == str(expected).upper()
            if ok:
                lines.append(f"  ✅ {label}")
                passed += 1
            else:
                lines.append(f"  ❌ {label}  <i>got {actual}, want {expected}</i>")
                failed += 1

        def _chk_in(label: str, actual_list, must_include: list):
            nonlocal passed, failed
            upper = [str(x).upper() for x in actual_list]
            missing = [m for m in must_include if m.upper() not in upper]
            if not missing:
                lines.append(f"  ✅ {label}")
                passed += 1
            else:
                lines.append(f"  ❌ {label}  <i>missing: {missing} in {actual_list}</i>")
                failed += 1

        # ── 1. Ticker resolution ───────────────────────────────────────────────
        lines.append("\n<b>Ticker Resolution</b>")
        try:
            r = _resolve_ticker_candidates("apple")
            _chk("apple → AAPL", r[0]["ticker"] if r else "?", "AAPL")
        except Exception as e:
            lines.append(f"  ❌ apple → AAPL  <i>{e}</i>"); failed += 1

        try:
            r = _resolve_ticker_candidates("avery dennison")
            _chk("avery dennison → AVY", r[0]["ticker"] if r else "?", "AVY")
        except Exception as e:
            lines.append(f"  ❌ avery dennison → AVY  <i>{e}</i>"); failed += 1

        try:
            r = _resolve_ticker_candidates("costco")
            _chk("costco → COST", r[0]["ticker"] if r else "?", "COST")
        except Exception as e:
            lines.append(f"  ❌ costco → COST  <i>{e}</i>"); failed += 1

        try:
            r = _resolve_ticker_candidates("nvidea")   # intentional misspelling
            _chk("nvidea → NVDA", r[0]["ticker"] if r else "?", "NVDA")
        except Exception as e:
            lines.append(f"  ❌ nvidea → NVDA  <i>{e}</i>"); failed += 1

        # ── 2. Multi-ticker extraction ─────────────────────────────────────────
        lines.append("\n<b>Multi-Ticker NL Extraction</b>")
        try:
            r = _nl_extract_tickers_list("avery dennison, microsoft, CRM, solana and EEM")
            _chk_in("5-item mixed list", r, ["avery dennison", "microsoft", "CRM", "solana", "EEM"])
            _chk("correct count (5)", len(r), 5)
        except Exception as e:
            lines.append(f"  ❌ 5-item extraction  <i>{e}</i>"); failed += 1; failed += 1

        try:
            r = _nl_extract_tickers_list("I picked up some apple and a bit of tesla today")
            _chk("sentence noise stripped (count=2)", len(r), 2)
        except Exception as e:
            lines.append(f"  ❌ sentence extraction  <i>{e}</i>"); failed += 1

        # ── 3. NL trade parse ─────────────────────────────────────────────────
        lines.append("\n<b>NL Trade Parsing</b>")
        try:
            r = _nl_parse_trade("bought", "I bought 10 apple stocks for $182.50")
            _chk("bought: ticker (AAPL)", (r.get("ticker") or "").upper(), "AAPL")
            _chk("bought: shares (10)", int(r.get("shares") or 0), 10)
            _chk("bought: price (182.5)", round(float(r.get("price") or 0), 1), 182.5)
        except Exception as e:
            lines.append(f"  ❌ bought parse  <i>{e}</i>"); failed += 3

        try:
            r = _nl_parse_trade("sold", "sold my avery dennison position")
            _chk("sold: company name → AVY", (r.get("ticker") or "").upper(), "AVY")
        except Exception as e:
            lines.append(f"  ❌ sold parse  <i>{e}</i>"); failed += 1

        try:
            r = _nl_parse_trade("alert", "when nvidia drops below 800")
            _chk("alert: ticker (NVDA)", (r.get("ticker") or "").upper(), "NVDA")
            _chk("alert: direction (below)", r.get("direction"), "below")
            _chk("alert: price (800)", int(float(r.get("price") or 0)), 800)
        except Exception as e:
            lines.append(f"  ❌ alert parse  <i>{e}</i>"); failed += 3

        try:
            r = _nl_parse_trade("paper_buy", "5 shares of tesla")
            _chk("paper_buy: ticker (TSLA)", (r.get("ticker") or "").upper(), "TSLA")
            _chk("paper_buy: shares (5)", int(float(r.get("shares") or 0)), 5)
        except Exception as e:
            lines.append(f"  ❌ paper_buy parse  <i>{e}</i>"); failed += 2

        # ── Summary ───────────────────────────────────────────────────────────
        total = passed + failed
        emoji = "🟢" if failed == 0 else ("🟡" if failed <= 2 else "🔴")
        lines.append(f"\n{emoji} <b>{passed}/{total} passed</b>")
        if failed:
            lines.append("⚠️ <i>Check logs on Render for details.</i>")
        else:
            lines.append("✅ <i>All systems operational.</i>")

        return "\n".join(lines)

    if text == "USERS":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        users = get_allowed_users()
        owner = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
        lines = ["<b>👥 Allowed Users</b>\n"]
        for u in users:
            tag = "  <i>(you)</i>" if u == owner else ""
            lines.append(f"• <code>{u}</code>{tag}")
        lines.append(f"\n<i>{len(users)} user(s) total</i>")
        return "\n".join(lines)

    # ── /pending — show pending access requests with approve/reject buttons ───
    if text == "PENDING":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        pending = get_pending_users()
        if not pending:
            return "✅ <b>No pending access requests.</b>"
        lines = [f"<b>⏳ Pending Requests ({len(pending)})</b>\n"]
        for uid, info in pending.items():
            name     = info.get("first_name", "")
            uname    = info.get("username", "")
            req_at   = info.get("requested_at", "")[:10]   # date only
            name_str = _esc(name) if name else ""
            uname_str = f"  @{_esc(uname)}" if uname else ""
            lines.append(f"• <code>{uid}</code>  {name_str}{uname_str}  <i>{req_at}</i>")
        lines.append("\nUse the buttons below to action each request, or tap the name to copy the ID.")
        # Send one inline keyboard per pending user so each has its own buttons
        header = "\n".join(lines)
        send_message(header, chat_id=chat_id)
        for uid, info in pending.items():
            name = info.get("first_name", "") or uid
            send_inline_keyboard(
                f"<code>{uid}</code>  {_esc(info.get('first_name', '') or '')}",
                [[
                    {"text": f"✅ Approve {_esc(name)}",
                     "callback_data": f"approve_user|{uid}"},
                    {"text": "❌ Reject",
                     "callback_data": f"reject_user|{uid}"},
                ]],
                chat_id=chat_id,
            )
        return ""   # already sent above

    # ── /admin_perf — aggregate performance across all users (admin-only) ─────
    if text == "ADMIN PERF":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        from trade_logger import get_performance_stats
        users = get_allowed_users()
        lines = ["<b>👥 All-User Performance</b>\n"]
        for uid in users:
            s = get_performance_stats(uid)
            tag = " <i>(you)</i>" if uid == str(os.environ.get("TELEGRAM_CHAT_ID", "")) else ""
            if not s:
                lines.append(f"<code>{uid}</code>{tag}: no closed trades")
            else:
                sign = "+" if s["avg_return"] >= 0 else ""
                lines.append(
                    f"<code>{uid}</code>{tag}\n"
                    f"  {s['count']} trades · {s['win_rate']}% wins · "
                    f"avg {sign}{s['avg_return']}% · P&L ${s['total_gain_usd']:+.2f}"
                )
        return "\n".join(lines)

    # ── /fixticker OLD NEW — rename a stored ticker in your trade log ─────────
    if text.startswith("FIXTICKER "):
        from config_manager import mutate_user_trade_log, NO_WRITE
        parts_ft = text[len("FIXTICKER "):].strip().upper().split()
        if len(parts_ft) != 2:
            return "⚠️ Usage: <code>/fixticker OLDTICKER NEWTICKER</code>  e.g. <code>/fixticker COSTCO COST</code>"
        old_tk, new_tk = parts_ft

        def _mut(log):
            log = log or {"open": [], "closed": [], "watchlist": []}
            changed = 0
            for t in log.get("open", []) + log.get("closed", []):
                if t["ticker"] == old_tk:
                    t["ticker"] = new_tk
                    changed += 1
            if changed == 0:
                return NO_WRITE, 0
            return log, changed

        changed = mutate_user_trade_log(chat_id, _mut)
        if changed == 0:
            return f"⚠️ <b>{old_tk}</b> not found in your portfolio."
        return f"✅ Renamed <b>{old_tk}</b> → <b>{new_tk}</b> ({changed} entr{'y' if changed == 1 else 'ies'} updated)."

    # ── /pause /resume (per-user) ─────────────────────────────────────────────
    if text == "PAUSE":
        send_inline_keyboard(
            "⏸ <b>Pause your daily picks?</b>\n"
            "<i>You won't receive morning briefings until you send /resume. Other users are unaffected.</i>",
            [[{"text": "✅ Yes, pause picks", "callback_data": "pause_confirm"},
              {"text": "❌ Cancel",           "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return ""

    if text == "RESUME":
        update_user_config(chat_id, "paused", False)
        return "▶️ <b>Picks resumed.</b> You'll receive tomorrow's morning briefing as normal."

    # ── /bot_pause /bot_resume (admin-only global kill switch) ───────────────
    if text == "BOT PAUSE":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        update_config("enabled", False)
        return "⏸ <b>Bot paused globally.</b> No picks will be sent to anyone. Use /bot_resume to restart."

    if text == "BOT RESUME":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        update_config("enabled", True)
        return "▶️ <b>Bot resumed globally.</b> Daily picks will run tomorrow morning."

    # ── /bot_crypto_on / /bot_crypto_off (admin-only) ─────────────────────────
    if text == "BOT CRYPTO ON":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        update_config("crypto_enabled", True)
        return "✅ <b>Crypto picks enabled.</b> Takes effect tomorrow morning."

    if text == "BOT CRYPTO OFF":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        update_config("crypto_enabled", False)
        return "⏸ <b>Crypto picks disabled.</b> No crypto analysis will run tomorrow morning."

    # ── /bot_showcounts on|off (admin-only social buy-count feature) ─────────
    if text in ("BOT SHOWCOUNTS ON", "BOT SHOWCOUNTS OFF"):
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        enabled = text.endswith("ON")
        update_config("show_buy_counts", enabled)
        if enabled:
            return (
                "👥 <b>Buy counts enabled.</b>\n"
                "Members will see <i>'👥 N bought'</i> badges on picks once 2+ people tap ✅ Bought.\n"
                "<i>Takes effect immediately.</i>"
            )
        return (
            "🔕 <b>Buy counts disabled.</b>\n"
            "Badges will no longer appear on picks.\n"
            "<i>Takes effect immediately.</i>"
        )

    # ── /crypto on|off (per-user crypto visibility toggle) ───────────────────
    if text in ("CRYPTO ON", "CRYPTO OFF", "CRYPTO"):
        if text == "CRYPTO ON":
            update_user_config(chat_id, "show_crypto", True)
            return "✅ <b>Crypto picks enabled</b> for your account. You'll see them in tomorrow's briefing."
        if text == "CRYPTO OFF":
            update_user_config(chat_id, "show_crypto", False)
            return "⏸ <b>Crypto picks hidden</b> for your account. Stock picks are unaffected.\n<i>To re-enable: /crypto on</i>"
        # /crypto alone → show current state
        user_cfg = get_user_config(chat_id)
        state    = "✅ on" if user_cfg.get("show_crypto", True) else "⏸ off"
        return f"🪙 <b>Crypto picks:</b> {state}\n\n/crypto on  ·  /crypto off"

    # ── /broadcast (admin — send a message to all users) ─────────────────────
    if text == "BROADCAST" or text.startswith("BROADCAST "):
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        if text == "BROADCAST":
            _prompt_for_param("broadcast", chat_id)
            return ""
        body = text[len("BROADCAST "):].strip()
        if not body:
            return "Usage: /broadcast Your message here"
        recipients = [u for u in get_allowed_users() if u != chat_id]
        msg = f"📢 <b>StockPulz Update</b>\n\n{_esc(body)}"
        sent = 0
        for uid in recipients:
            if send_message(msg, chat_id=uid):
                sent += 1
        return f"✅ Broadcast sent to {sent} user(s)."

    # ── /release (admin — versioned release note to all users) ───────────────
    if text == "RELEASE" or text.startswith("RELEASE "):
        if not _is_admin(chat_id):
            return "🔒 Admin only."

        # /release with custom text → broadcast directly (override flow)
        if text.startswith("RELEASE "):
            notes = text[len("RELEASE "):].strip()
            if notes:
                return _send_release_broadcast(notes, chat_id)

        # /release alone → show pending AI-generated note(s)
        from release_tracker import get_pending_notes
        pending = get_pending_notes()
        if not pending:
            send_inline_keyboard(
                "📢 <b>No pending release notes.</b>\n\n"
                "A note is auto-generated after each <code>git push</code>.\n"
                "Or type your own: <code>/release Your message here</code>",
                [[{"text": "❌ Close", "callback_data": "cancel_abort"}]],
                chat_id=chat_id,
            )
            return ""

        # Show the latest pending note
        note = pending[-1]
        note_id = note["id"]
        summary = note["summary"]
        send_inline_keyboard(
            f"📢 <b>Pending release note</b>\n\n"
            f"{_esc(summary)}\n\n"
            f"<i>Send this to all users, edit it, or skip.</i>",
            [[
                {"text": "📤 Send to all",  "callback_data": f"release_send|{note_id}"},
                {"text": "✏️ Edit",         "callback_data": f"release_edit|{note_id}"},
                {"text": "⏭ Skip",          "callback_data": f"release_skip|{note_id}"},
            ]],
            chat_id=chat_id,
        )
        return ""

    # ── /set_thresholds (per-user stop loss & target gain) ────────────────────
    if text == "SET THRESHOLDS":
        import json as _json
        user_cfg   = get_user_config(chat_id)
        global_cfg = get_config()
        sl  = user_cfg.get("stop_loss_pct")   or global_cfg.get("stop_loss_pct",   7)
        tg  = user_cfg.get("target_gain_pct") or global_cfg.get("target_gain_pct", 15)
        sl_src = "" if user_cfg.get("stop_loss_pct")   else " (global default)"
        tg_src = "" if user_cfg.get("target_gain_pct") else " (global default)"
        return (
            f"⚙️ <b>Your Thresholds</b>\n"
            f"Stop loss:   <b>{sl}%</b>{sl_src}\n"
            f"Target gain: <b>{tg}%</b>{tg_src}\n\n"
            f"<i>To change:</i>\n"
            f"/set_thresholds stop 7 target 15\n"
            f"/set_thresholds stop 5\n"
            f"/set_thresholds target 12\n"
            f"/set_thresholds reset  — restore global defaults\n\n"
            f"<i>Note: applies to trades you log via /bought. Morning pick stops are set by Claude based on technical levels.</i>"
        )

    if text.startswith("SET THRESHOLDS "):
        import re, json as _json
        raw = text[len("SET THRESHOLDS "):].strip().lower()

        if raw in ("reset", "off", "default", "clear", "none"):
            update_user_config_multi(chat_id, {"stop_loss_pct": None, "target_gain_pct": None})
            global_cfg = get_config()
            sl = global_cfg.get("stop_loss_pct", 7)
            tg = global_cfg.get("target_gain_pct", 15)
            return f"✅ Thresholds reset to global defaults — stop <b>{sl}%</b>, target <b>{tg}%</b>."

        updates = {}
        for match in re.finditer(r"(stop(?:\s+loss)?|target(?:\s+gain)?)\s+([\d.]+)%?", raw):
            key = "stop_loss_pct" if match.group(1).startswith("stop") else "target_gain_pct"
            updates[key] = max(0.5, round(float(match.group(2)), 1))

        if not updates:
            # NL fallback via Haiku
            try:
                _client = _get_client()
                _sys = (
                    'Parse threshold values. Return JSON only.\n'
                    '{"stop_loss_pct": <number or null>, "target_gain_pct": <number or null>}\n'
                    'Examples: "7% stop 12% target" → {"stop_loss_pct": 7, "target_gain_pct": 12}\n'
                    '"tighten stop to 4" → {"stop_loss_pct": 4, "target_gain_pct": null}'
                )
                _msg    = _client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=80,
                    messages=[{"role": "user", "content": f"{_sys}\n\nInput: {raw}"}],
                )
                parsed = _json.loads(_msg.content[0].text.strip())
                if parsed.get("stop_loss_pct")   is not None:
                    updates["stop_loss_pct"]   = max(0.5, round(float(parsed["stop_loss_pct"]),   1))
                if parsed.get("target_gain_pct") is not None:
                    updates["target_gain_pct"] = max(0.5, round(float(parsed["target_gain_pct"]), 1))
            except Exception:
                pass

        if not updates:
            return "❌ Couldn't parse. Try: /set_thresholds stop 7 target 12"

        user_cfg   = update_user_config_multi(chat_id, updates)
        global_cfg = get_config()
        sl = user_cfg.get("stop_loss_pct")   or global_cfg.get("stop_loss_pct",   5)
        tg = user_cfg.get("target_gain_pct") or global_cfg.get("target_gain_pct", 8)
        return (
            f"✅ <b>Thresholds updated.</b>\n"
            f"Stop loss:   <b>{sl}%</b>\n"
            f"Target gain: <b>{tg}%</b>\n"
            f"<i>New trades logged with /bought will use these values.</i>"
        )

    if text == "RESET":
        send_inline_keyboard(
            "⚠️ <b>Reset all your settings?</b>\n"
            "<i>Risk, mode, budgets, watchlist, stop loss, target gain — all wiped back to defaults.</i>",
            [[
                {"text": "✅ Yes, reset",  "callback_data": "reset_confirm"},
                {"text": "❌ Cancel",      "callback_data": "cancel_abort"},
            ]],
            chat_id=chat_id,
        )
        return ""

    if text == "RESET CONFIRM":
        reset_user_config(chat_id)
        global_cfg = get_config()
        sl = global_cfg.get("stop_loss_pct", 7)
        tg = global_cfg.get("target_gain_pct", 15)
        return (
            f"🔄 Your settings reset to defaults.\n"
            f"Risk: moderate  ·  Pick mode: both\n"
            f"Budgets: unset  ·  Watchlist: cleared\n"
            f"Stop loss: {sl}%  ·  Target gain: {tg}%  (global defaults)"
        )

    if text == "GUIDE":
        return (
            "📖 <b>StockPulz — Quick Reference</b>\n\n"
            "<b>📅 What you receive each day:</b>\n"
            "📬 <b>7:00 AM ET</b> — Morning picks: entry, target &amp; stop for each\n"
            "🕙 <b>10:30 AM ET</b> — Confirmation: enter, wait, or exit signal\n"
            "🔔 <b>Every 30 min</b> — Alert if a stop or target is hit\n"
            "📊 <b>4:15 PM ET</b> — EOD wrap: how today's picks moved\n"
            "📅 <b>Saturday</b> — Crypto picks + weekly P&amp;L recap\n"
            "🗓 <b>Sunday</b> — Week-ahead: earnings, macro events\n\n"
            "<b>💼 Tracking your trades:</b>\n"
            "When you place a real trade, send <code>/bought TICKER</code>.\n"
            "When you exit, send <code>/sold TICKER</code>.\n"
            "That's all it takes — /stats then shows your win rate, avg gain, expectancy, and total P&amp;L.\n\n"
            "<b>🔑 Essential commands:</b>\n"
            "/today — today's picks on demand\n"
            "/positions — open trades with live P&amp;L\n"
            "/stats — your personal performance dashboard\n"
            "/settings — budget, risk, assets, alerts\n"
            "/guide — this card\n"
            "/help — full command list\n\n"
            "<b>💬 Natural language works too:</b>\n"
            "<i>\"Why was NVDA picked?\"</i>  ·  <i>\"Set my risk to aggressive\"</i>  ·  <i>\"Alert me when BTC hits 100k\"</i>\n\n"
            "Questions? Just ask."
        )

    if text == "SETUP":
        # Re-run the onboarding wizard so the user can reconfigure from scratch
        if _is_admin(chat_id) or chat_id in get_allowed_users():
            _start_onboarding_wizard(chat_id)
            return ""
        return "🔒 You need to be an approved member to use this command."

    if text == "STATUS":
        global_cfg    = get_config()
        user_cfg      = get_user_config(chat_id)
        bot_status    = "✅ Active" if global_cfg.get("enabled") else "⏸ Paused (admin)"
        pick_status   = "⏸ Paused" if user_cfg.get("paused") else "✅ Active"
        crypto_status = "✅ On" if global_cfg.get("crypto_enabled", True) else "⏸ Off (admin)"
        return (
            f"<b>⚙️ Status</b>\n"
            f"Your picks:      {pick_status}\n"
            f"Bot:             {bot_status}\n"
            f"Crypto analysis: {crypto_status}\n"
            f"Risk profile:    {user_cfg.get('risk_profile', 'moderate')}\n"
            f"Pick mode:       {user_cfg.get('pick_mode', 'both')}\n\n"
            f"<i>For full settings: /settings</i>"
        )

    if text == "NEXT":
        from datetime import datetime, timedelta
        import pytz
        ET = pytz.timezone("America/New_York")
        now = datetime.now(ET)
        wd  = now.weekday()   # 0=Mon … 6=Sun
        h, m = now.hour, now.minute

        # Scheduled user-facing events (ET wall-clock), weekdays only unless noted
        # (prescreener is silent — not shown)
        schedule = [
            # (name, emoji, hour, minute, weekdays_only)
            ("Morning picks",        "📬", 8,  30, True),
            ("10:30 AM confirmation","🕙", 10, 30, True),
            ("3:30 PM close check",  "📊", 15, 30, True),
        ]

        def _minutes_until(target_h, target_m, weekdays_only):
            """Return (minutes_until, delivery_datetime_ET)."""
            candidate = now.replace(hour=target_h, minute=target_m, second=0, microsecond=0)
            days_ahead = 0
            while True:
                t = candidate + timedelta(days=days_ahead)
                is_weekday = t.weekday() < 5
                if t > now and (not weekdays_only or is_weekday):
                    return int((t - now).total_seconds() / 60), t
                days_ahead += 1
                if days_ahead > 14:
                    break
            return None, None

        lines = ["<b>⏰ Next Scheduled Messages</b>\n"]
        upcoming = []
        for name, emoji, eh, em, wdonly in schedule:
            mins, dt = _minutes_until(eh, em, wdonly)
            if mins is not None:
                upcoming.append((mins, name, emoji, dt))

        upcoming.sort(key=lambda x: x[0])

        for i, (mins, name, emoji, dt) in enumerate(upcoming[:4]):
            day_str = dt.strftime("%a") if dt.date() != now.date() else "Today"
            time_str = dt.strftime("%-I:%M %p ET")
            if mins < 60:
                eta = f"{mins}m"
            elif mins < 120:
                eta = f"1h {mins % 60}m"
            else:
                eta = f"{mins // 60}h {mins % 60}m"
            prefix = "→ " if i == 0 else "   "
            lines.append(f"{prefix}{emoji} <b>{name}</b>  {day_str} {time_str}  <i>(in {eta})</i>")

        # Weekend note
        if wd >= 4 and h >= 15:   # Friday afternoon or weekend
            lines.append("\n<i>Weekend: crypto picks arrive Saturday ~7 AM ET.</i>")

        return "\n".join(lines)

    if text == "SETTINGS":
        _send_settings_panel(chat_id)
        return ""

    # Bare budget commands — prompt for the value
    # ── /set_budget ───────────────────────────────────────────────────────────
    if text == "SET BUDGET":
        config = get_user_config(chat_id)
        sb = config.get("stock_budget")
        cb = config.get("crypto_budget")
        sb_str = f"${sb}" if sb else "not set"
        cb_str = f"${cb}" if cb else "not set"
        return (
            f"💰 <b>Current budgets</b>\n"
            f"Stocks: <b>{sb_str}</b>\n"
            f"Crypto: <b>{cb_str}</b>\n\n"
            f"<i>To update:</i>\n"
            f"/set_budget stocks 200 crypto 50\n"
            f"/set_budget stocks 150\n"
            f"/set_budget off  — clears both"
        )

    if text.startswith("SET BUDGET "):
        import re, json as _json
        from cmd_nlp import _nl_parse_trade
        raw = text[len("SET BUDGET "):].strip().lower()

        # "off" or "0" → clear both
        if raw in ("off", "0", "none", "clear"):
            update_user_config_multi(chat_id, {"stock_budget": None, "crypto_budget": None})
            return "✅ Budgets cleared — picks will show no allocation amounts."

        # Parse "stocks <n> crypto <n>" in any order, or just one bucket
        updates = {}
        for match in re.finditer(r"(stocks?|crypto)\s+([\d,.]+k?)", raw):
            bucket = "stock_budget" if match.group(1).startswith("stock") else "crypto_budget"
            val_str = match.group(2).replace(",", "")
            val = float(val_str[:-1]) * 1000 if val_str.endswith("k") else float(val_str)
            updates[bucket] = val if val > 0 else None

        if not updates:
            # NL fallback: "200 for stocks, 50 crypto"
            parsed = _nl_parse_trade("paper_reset", raw)   # reuse schema (price = amount)
            amount = parsed.get("price")
            if amount:
                # Amount given but no bucket — ask via buttons
                a = int(amount)
                send_inline_keyboard(
                    f"💰 Apply <b>${a}</b> budget to which picks?",
                    [[
                        {"text": f"📈 Stocks ${a}",      "callback_data": f"set_budget|stocks|{a}"},
                        {"text": f"🪙 Crypto ${a}",      "callback_data": f"set_budget|crypto|{a}"},
                        {"text": f"Both ${a}",           "callback_data": f"set_budget|both|{a}"},
                    ]],
                    chat_id=chat_id,
                )
                return ""
            # Nothing parseable — prompt from scratch
            from config_manager import save_pending_state
            save_pending_state(chat_id, "set_budget")
            send_inline_keyboard(
                "💰 <b>Set your per-trade budget</b>\n"
                "<i>e.g. <code>stocks 200 crypto 50</code> or <code>stocks 150</code></i>",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""

        config = update_user_config_multi(chat_id, updates)
        global_cfg = get_config()
        lines = ["✅ <b>Budget updated:</b>"]
        if "stock_budget" in updates:
            v = config.get("stock_budget")
            lines.append(f"Stocks → {f'${v}' if v else 'cleared'}")
        if "crypto_budget" in updates:
            v = config.get("crypto_budget")
            lines.append(f"Crypto → {f'${v}' if v else 'cleared'}")
        # Show resulting per-pick amounts
        sb = config.get("stock_budget")
        cb = config.get("crypto_budget")
        max_s = global_cfg.get("max_short_picks", 2) + global_cfg.get("max_long_picks", 3)
        max_c = global_cfg.get("max_crypto_short_picks", 2) + global_cfg.get("max_crypto_long_picks", 2)
        if sb:
            lines.append(f"<i>→ ${round(sb/max_s,2)}/pick across {max_s} stock slots</i>")
        if cb:
            lines.append(f"<i>→ ${round(cb/max_c,2)}/pick across {max_c} crypto slots</i>")
        return "\n".join(lines)

    # ── /set_picks ────────────────────────────────────────────────────────────
    if text == "SET PICKS":
        config = get_user_config(chat_id)
        ms = config.get("max_stock_picks")
        mc = config.get("max_crypto_picks")
        ms_str = str(ms) if ms else "all (default)"
        mc_str = str(mc) if mc else "all (default)"
        return (
            f"📊 <b>Pick limits</b>\n"
            f"Stocks: <b>{ms_str}</b>\n"
            f"Crypto: <b>{mc_str}</b>\n\n"
            f"<i>To update:</i>\n"
            f"/set_picks stocks 3 crypto 1\n"
            f"/set_picks stocks 5\n"
            f"/set_picks off  — show all picks (default)"
        )

    if text.startswith("SET PICKS "):
        import re, json as _json
        raw = text[len("SET PICKS "):].strip().lower()

        # "off" / "all" / "reset" → clear both caps
        if raw in ("off", "all", "reset", "none", "clear"):
            update_user_config_multi(chat_id, {"max_stock_picks": None, "max_crypto_picks": None})
            return "✅ Pick limits cleared — you'll see all picks."

        # Strict parse: "stocks N", "crypto N", or both
        updates = {}
        for match in re.finditer(r"(stocks?|crypto)\s+(\d+)", raw):
            key = "max_stock_picks" if match.group(1).startswith("stock") else "max_crypto_picks"
            updates[key] = max(1, int(match.group(2)))

        if not updates:
            # NL fallback via Haiku
            try:
                prompt = (
                    f'Parse "{raw}" into pick limits for a stock bot. '
                    'Return ONLY JSON with optional keys "max_stock_picks" and "max_crypto_picks" as integers. '
                    'Examples: "3 stocks 2 crypto"→{"max_stock_picks":3,"max_crypto_picks":2}, '
                    '"show me 4 stocks"→{"max_stock_picks":4}, "just 1 crypto"→{"max_crypto_picks":1}. '
                    'If unclear return {}.'
                )
                client  = _get_client()
                msg     = client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=60,
                    messages=[{"role": "user", "content": prompt}],
                )
                updates = _json.loads(msg.content[0].text.strip())
                updates = {k: max(1, int(v)) for k, v in updates.items()
                           if k in ("max_stock_picks", "max_crypto_picks")}
            except Exception:
                pass

        if not updates:
            return (
                "🤔 Try:\n"
                "/set_picks stocks 3 crypto 1\n"
                "/set_picks stocks 5\n"
                "/set_picks off"
            )

        update_user_config_multi(chat_id, updates)
        user_cfg = get_user_config(chat_id)
        pick_mode = user_cfg.get("pick_mode", "both")
        lines = ["✅ <b>Pick limits updated:</b>"]
        if "max_stock_picks" in updates:
            n = updates["max_stock_picks"]
            lines.append(f"Stocks → max <b>{n}</b> picks")
            # Warn if the 40/60 split would drop LT entirely
            if n == 1 and pick_mode == "both":
                lines.append("<i>⚠️ With stocks=1 and mode=both, long-term stock picks will be hidden (40/60 split rounds to 1 ST + 0 LT). Use /mode st to show only short-term, or set stocks ≥ 2.</i>")
        if "max_crypto_picks" in updates:
            n = updates["max_crypto_picks"]
            lines.append(f"Crypto → max <b>{n}</b> picks")
            if n == 1 and pick_mode == "both":
                lines.append("<i>⚠️ With crypto=1 and mode=both, long-term crypto picks will be hidden (50/50 split rounds to 1 ST + 0 LT).</i>")
        lines.append("<i>Takes effect on tomorrow's briefing.</i>")
        return "\n".join(lines)

    return None
