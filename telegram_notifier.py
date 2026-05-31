"""
telegram_notifier.py — Thin entry point and re-export facade.

Business logic lives in bot_commands.py.
Telegram HTTP layer lives in telegram_api.py.

This file exists so that agent.py and webhook.py keep their existing imports
without modification.
"""
from __future__ import annotations

# ── Telegram API re-exports (webhook.py imports these) ───────────────────────
from telegram_api import (                          # noqa: F401
    send_message,
    send_inline_keyboard,
    send_typing_action,
    typing_until_done,
    answer_callback_query,
    set_webhook,
    _chat_id,
)

# ── Formatter re-exports (agent.py imports these) ────────────────────────────
from formatters import (                            # noqa: F401
    format_daily_message,
    format_confirmation_message,
    format_weekly_recap_message,
)

# ── Command handler re-export (webhook.py imports this) ──────────────────────
from bot_commands import handle_callback_query      # noqa: F401

# ── Internal imports needed by handle_incoming_command ───────────────────────
from config_manager import (
    load_pending_state, clear_pending_state,
    get_pending_users, get_allowed_users,
)
from bot_commands import (
    _parse_and_execute,
    _handle_pending_reply,
    _is_admin,
)


# ── Entry point ───────────────────────────────────────────────────────────────

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

    reply = _parse_and_execute(text.upper(), original=text, chat_id=chat_id)
    if reply:
        # Append /help hint to every command response except /help itself and daily picks
        cmd = text.lstrip("/").split()[0].lower() if text else ""
        if cmd not in ("help", "start", "today", "share") and not reply.startswith("📋"):
            reply = reply + "\n\n<i>📋 /help  ·  📲 /share</i>"
        send_message(reply, chat_id=chat_id)
    return reply
