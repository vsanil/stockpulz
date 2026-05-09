"""
webhook.py — Flask app to receive Telegram bot updates via webhook.
Deploy to Render.com free tier. After deploying, register the webhook URL once:

    python webhook.py --set-webhook https://your-render-url.onrender.com/webhook

Or call the /register endpoint manually.
"""

import os
import sys
import time
import threading
import requests
from flask import Flask, request, jsonify

from config_manager import get_config, get_allowed_users
from telegram_notifier import handle_incoming_command, handle_callback_query, set_webhook, send_typing_action, typing_until_done, send_message

app = Flask(__name__)


# ── Keep-alive (prevents Render free tier cold starts) ────────────────────────

def _keep_alive_loop():
    """
    Ping /health every 14 minutes so Render doesn't spin down the service.
    Render free tier idles after 15 minutes of inactivity — first request after
    idle takes 15-20s. This keeps the process warm at zero extra cost.
    Requires RENDER_EXTERNAL_URL env var (set automatically by Render).
    """
    url = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        print("[webhook] RENDER_EXTERNAL_URL not set — keep-alive disabled.")
        return
    ping_url = f"{url}/health"
    print(f"[webhook] Keep-alive started — pinging {ping_url} every 14 min.")
    while True:
        time.sleep(14 * 60)
        try:
            resp = requests.get(ping_url, timeout=10)
            print(f"[webhook] Keep-alive ping → {resp.status_code}")
        except Exception as exc:
            print(f"[webhook] Keep-alive ping failed: {exc}")

threading.Thread(target=_keep_alive_loop, daemon=True).start()


# ── Telegram webhook receiver ─────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """Receive Telegram update (message from user to bot)."""
    data = request.get_json(silent=True) or {}

    # Extract message text and chat_id from Telegram update format
    # ── Inline keyboard button tap ────────────────────────────────────────────
    callback_query = data.get("callback_query")
    if callback_query:
        cq_chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))
        if cq_chat_id and cq_chat_id not in get_allowed_users():
            return jsonify({"status": "ok", "access": "denied"}), 200
        with typing_until_done(cq_chat_id or None):
            handle_callback_query(callback_query)
        return jsonify({"status": "ok", "type": "callback_query"}), 200

    # ── Regular message ───────────────────────────────────────────────────────
    message = data.get("message") or data.get("edited_message", {})
    if not message:
        return jsonify({"status": "ignored", "reason": "no message"}), 200

    text    = message.get("text", "").strip()
    chat_id = str(message.get("chat", {}).get("id", ""))

    if not text or not chat_id:
        return jsonify({"status": "ignored", "reason": "empty text or chat_id"}), 200

    print(f"[webhook] Received from {chat_id}: {text!r}")

    # ── Access control ────────────────────────────────────────────────────────
    owner   = os.environ.get("TELEGRAM_CHAT_ID", "")
    allowed = get_allowed_users()   # always includes owner

    # Any /start message (plain or with deep-link param like /start adminref_xxx)
    # must reach handle_incoming_command — it contains the HMAC verification logic
    # for admin invite links. Matching on the prefix covers all variants.
    text_lower = text.strip().lower()
    is_start = text_lower.startswith("/start") or text_lower == "start"

    if chat_id not in allowed:
        if is_start:
            # Let handle_incoming_command deal with it — it handles pending flow,
            # admin invite auto-approval, and welcome messages.
            try:
                with typing_until_done(chat_id):
                    handle_incoming_command(text, chat_id=chat_id)
            except Exception as exc:
                print(f"[webhook] Error handling /start for {chat_id}: {exc}")
                send_message("⚠️ Something went wrong — please try again.", chat_id=chat_id)
        else:
            send_message(
                "🔒 You don't have access yet. Send /start to request access.",
                chat_id=chat_id,
            )
        return jsonify({"status": "ok", "access": "denied"}), 200

    try:
        with typing_until_done(chat_id):
            reply = handle_incoming_command(text, chat_id=chat_id)
    except Exception as exc:
        print(f"[webhook] Error handling {text!r}: {exc}")
        send_message("⚠️ Something went wrong — please try again.", chat_id=chat_id)
        return jsonify({"status": "error", "detail": str(exc)}), 200

    if reply:
        pass   # handle_incoming_command already sent via send_message for inline flows
    return jsonify({"status": "ok", "reply": reply}), 200


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    """Health check — returns current config."""
    try:
        config = get_config()
        return jsonify({"status": "ok", "config": config}), 200
    except Exception as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 500


# ── One-time webhook registration ─────────────────────────────────────────────

@app.route("/register", methods=["GET"])
def register():
    """
    Call this once after deploying to Render to register the Telegram webhook.
    e.g. https://your-app.onrender.com/register?url=https://your-app.onrender.com/webhook
    """
    webhook_url = request.args.get("url", "")
    if not webhook_url:
        host = request.host_url.rstrip("/")
        webhook_url = f"{host}/webhook"
    ok = set_webhook(webhook_url)
    return jsonify({"registered": ok, "webhook_url": webhook_url}), 200 if ok else 500


@app.route("/", methods=["GET"])
def index():
    return jsonify({"service": "Stock Agent Telegram Webhook", "status": "running"}), 200


# ── CLI webhook registration ──────────────────────────────────────────────────

if __name__ == "__main__":
    if "--set-webhook" in sys.argv:
        idx = sys.argv.index("--set-webhook")
        url = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        if not url:
            print("Usage: python webhook.py --set-webhook https://your-app.onrender.com/webhook")
            sys.exit(1)
        success = set_webhook(url)
        sys.exit(0 if success else 1)

    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
