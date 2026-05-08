"""
telegram_api.py — Pure Telegram Bot HTTP layer.

All functions here are stateless wrappers around the Telegram Bot API.
No application or business logic belongs in this file.
Imported by bot_commands.py and re-exported through telegram_notifier.py.
"""

import os
import time
import threading
import requests


TELEGRAM_API       = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4096   # Telegram hard limit
MAX_RETRIES        = 3
RETRY_DELAY        = 5      # seconds between retries


# ── Credentials ───────────────────────────────────────────────────────────────

def _bot_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise EnvironmentError("TELEGRAM_BOT_TOKEN environment variable is not set.")
    return token


def _chat_id() -> str:
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not chat_id:
        raise EnvironmentError("TELEGRAM_CHAT_ID environment variable is not set.")
    return chat_id


# ── Send helpers ──────────────────────────────────────────────────────────────

def send_message(text: str, chat_id: str | None = None) -> bool:
    """
    Send a Telegram message. Splits messages > 4096 chars automatically.
    Retries up to 3 times on failure. Returns True on success.
    """
    token   = _bot_token()
    chat_id = chat_id or _chat_id()
    url     = TELEGRAM_API.format(token=token, method="sendMessage")

    # Split long messages — always break at newlines to avoid splitting inside HTML tags
    def _safe_split(txt: str, limit: int) -> list[str]:
        if len(txt) <= limit:
            return [txt]
        parts = []
        while txt:
            if len(txt) <= limit:
                parts.append(txt)
                break
            split_at = txt.rfind("\n", 0, limit)
            if split_at == -1:
                split_at = limit        # no newline found — hard cut as last resort
            parts.append(txt[:split_at])
            txt = txt[split_at:].lstrip("\n")
        return parts

    chunks = _safe_split(text, MAX_MESSAGE_LENGTH)

    for chunk in chunks:
        payload = {
            "chat_id":    chat_id,
            "text":       chunk,
            "parse_mode": "HTML",   # supports <b>, <i>, <code> tags
        }
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    print(f"[telegram] Message chunk sent (attempt {attempt}).")
                    break
                else:
                    print(f"[telegram] Attempt {attempt} failed: HTTP {resp.status_code} — {resp.text}")
            except Exception as exc:
                print(f"[telegram] Attempt {attempt} exception: {exc}")

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
        else:
            print("[telegram] All send attempts failed for a chunk.")
            return False

    return True


def send_inline_keyboard(text: str, buttons: list[list[dict]],
                         chat_id: str | None = None) -> bool:
    """Send a message with an inline keyboard for user selection. Retries up to 3×."""
    token   = _bot_token()
    chat_id = chat_id or _chat_id()
    url     = TELEGRAM_API.format(token=token, method="sendMessage")
    payload = {
        "chat_id":      chat_id,
        "text":         text,
        "parse_mode":   "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return True
            print(f"[telegram] send_inline_keyboard attempt {attempt} failed: HTTP {resp.status_code}")
        except Exception as exc:
            print(f"[telegram] send_inline_keyboard attempt {attempt} exception: {exc}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return False


def send_typing_action(chat_id: str | None = None) -> None:
    """Send a single 'typing...' action (lasts ~5 s in Telegram UI). Fire-and-forget."""
    token   = _bot_token()
    chat_id = chat_id or _chat_id()
    url     = TELEGRAM_API.format(token=token, method="sendChatAction")
    try:
        requests.post(url, json={"chat_id": chat_id, "action": "typing"}, timeout=5)
    except Exception:
        pass


def typing_until_done(chat_id: str | None = None):
    """
    Context manager that keeps the 'typing...' indicator alive for the duration of a block.

    Telegram's typing action only lasts ~5 s, so we re-fire it every 4 s in a background
    thread. The indicator disappears automatically once the context exits and the reply lands.

    Usage:
        with typing_until_done(chat_id):
            reply = slow_operation()
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        resolved_chat_id = chat_id or _chat_id()
        stop = threading.Event()

        def _keep_typing():
            while not stop.is_set():
                send_typing_action(resolved_chat_id)
                stop.wait(4)   # re-fire every 4 s (Telegram clears it after 5 s)

        t = threading.Thread(target=_keep_typing, daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()

    return _ctx()


def answer_callback_query(callback_query_id: str, text: str = "") -> None:
    """Acknowledge a Telegram callback query (dismisses the loading spinner)."""
    token = _bot_token()
    url   = TELEGRAM_API.format(token=token, method="answerCallbackQuery")
    try:
        requests.post(url, json={"callback_query_id": callback_query_id, "text": text}, timeout=10)
    except Exception:
        pass


# Bot username never changes — cache after first fetch
_bot_username_cache: str = ""


def _get_bot_username() -> str:
    """Return the bot's Telegram username, fetching once and caching for the process lifetime."""
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    try:
        resp = requests.get(
            TELEGRAM_API.format(token=_bot_token(), method="getMe"),
            timeout=5,
        ).json()
        _bot_username_cache = resp.get("result", {}).get("username", "") or ""
    except Exception:
        pass
    return _bot_username_cache


def set_webhook(webhook_url: str) -> bool:
    """Register a Telegram webhook URL (call once after deploying to Render)."""
    token = _bot_token()
    url   = TELEGRAM_API.format(token=token, method="setWebhook")
    resp  = requests.post(url, json={"url": webhook_url}, timeout=10)
    data  = resp.json()
    if data.get("ok"):
        print(f"[telegram] Webhook set to {webhook_url}")
        return True
    print(f"[telegram] Failed to set webhook: {data}")
    return False
