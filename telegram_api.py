"""
telegram_api.py — Pure Telegram Bot HTTP layer.

All functions here are stateless wrappers around the Telegram Bot API.
No application or business logic belongs in this file.
Imported by bot_commands.py and re-exported through telegram_notifier.py.
"""

import os
import re
import time
import threading
import requests


TELEGRAM_API       = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4096   # Telegram hard limit
MAX_RETRIES        = 3
RETRY_DELAY        = 5      # seconds between retries


def _strip_html(text: str) -> str:
    """Strip all HTML tags — used as fallback when Telegram rejects HTML."""
    return re.sub(r"<[^>]+>", "", text)


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
        sent = False
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = requests.post(url, json=payload, timeout=15)
                if resp.status_code == 200:
                    print(f"[telegram] Message chunk sent (attempt {attempt}).")
                    sent = True
                    break
                elif resp.status_code == 400 and "parse entities" in resp.text:
                    # Malformed HTML — strip tags and retry once as plain text
                    print(f"[telegram] HTML parse error — retrying as plain text.")
                    plain_payload = {
                        "chat_id": chat_id,
                        "text":    _strip_html(chunk),
                    }
                    resp2 = requests.post(url, json=plain_payload, timeout=15)
                    if resp2.status_code == 200:
                        print(f"[telegram] Message chunk sent as plain text (fallback).")
                        sent = True
                    else:
                        print(f"[telegram] Plain text fallback also failed: {resp2.status_code}")
                    break   # don't retry after HTML fallback attempt
                else:
                    print(f"[telegram] Attempt {attempt} failed: HTTP {resp.status_code} — {resp.text}")
            except Exception as exc:
                print(f"[telegram] Attempt {attempt} exception: {exc}")

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

        if not sent:
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
            elif resp.status_code == 400 and "parse entities" in resp.text:
                print(f"[telegram] send_inline_keyboard HTML error — retrying as plain text.")
                payload["text"] = _strip_html(payload["text"])
                payload.pop("parse_mode", None)
                resp2 = requests.post(url, json=payload, timeout=15)
                return resp2.status_code == 200
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


def send_photo(photo_bytes: bytes, caption: str = "", chat_id: str | None = None) -> bool:
    """Send a photo (PNG bytes) to a Telegram chat with an optional HTML caption."""
    token   = _bot_token()
    chat_id = chat_id or _chat_id()
    url     = TELEGRAM_API.format(token=token, method="sendPhoto")
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(
                url,
                data  = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"},
                files = {"photo": ("chart.png", photo_bytes, "image/png")},
                timeout = 30,
            )
            if resp.status_code == 200:
                print(f"[telegram] Photo sent ({len(photo_bytes)//1024}KB).")
                return True
            print(f"[telegram] send_photo attempt {attempt} failed: HTTP {resp.status_code} — {resp.text[:120]}")
        except Exception as exc:
            print(f"[telegram] send_photo attempt {attempt} exception: {exc}")
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)
    return False


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


# ── Async broadcast (1000 users in ~3-5 seconds) ──────────────────────────────

def broadcast_all(payloads: list[dict]) -> dict[str, bool]:
    """
    Send personalised Telegram messages to many users concurrently.

    Each payload dict:
        {"chat_id": str, "text": str, "keyboard": list[list[dict]] | None}

    Returns {chat_id: success_bool}.

    Implementation: asyncio + aiohttp with a semaphore cap of 30 concurrent
    requests (Telegram allows ~30 msg/s per bot).  Falls back to the
    synchronous send_message() loop if aiohttp is not installed.

    Typical throughput:
        Sequential (old):  1000 users × ~0.5s/msg = ~500 seconds
        Async (new):       1000 users ÷ 30 parallel × ~0.5s/batch = ~17 seconds
    """
    import asyncio

    if not payloads:
        return {}

    try:
        import aiohttp
    except ImportError:
        print("[telegram] aiohttp not installed — falling back to synchronous broadcast.")
        results = {}
        for p in payloads:
            cid = p["chat_id"]
            kb  = p.get("keyboard")
            if kb:
                results[cid] = send_inline_keyboard(p["text"], kb, chat_id=cid)
            else:
                results[cid] = send_message(p["text"], chat_id=cid)
        return results

    token = _bot_token()
    send_url = TELEGRAM_API.format(token=token, method="sendMessage")

    def _split(txt: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
        if len(txt) <= limit:
            return [txt]
        parts = []
        while txt:
            if len(txt) <= limit:
                parts.append(txt)
                break
            at = txt.rfind("\n", 0, limit)
            if at == -1:
                at = limit
            parts.append(txt[:at])
            txt = txt[at:].lstrip("\n")
        return parts

    async def _send_one(session: "aiohttp.ClientSession",
                        sem: asyncio.Semaphore,
                        chat_id: str,
                        text: str,
                        keyboard: list | None) -> bool:
        chunks = _split(text)
        success = True
        async with sem:
            for i, chunk in enumerate(chunks):
                payload: dict = {
                    "chat_id":    chat_id,
                    "text":       chunk,
                    "parse_mode": "HTML",
                }
                # Only attach keyboard to the last chunk
                if keyboard and i == len(chunks) - 1:
                    payload["reply_markup"] = {"inline_keyboard": keyboard}

                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        async with session.post(send_url, json=payload, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status == 200:
                                break
                            body = await resp.text()
                            print(f"[telegram/async] {chat_id} attempt {attempt}: HTTP {resp.status} — {body[:120]}")
                            if resp.status == 400 and "parse entities" in body:
                                # HTML rejected — fall back to plain text immediately
                                plain = dict(payload)
                                plain["text"] = _strip_html(chunk)
                                plain.pop("parse_mode", None)
                                async with session.post(send_url, json=plain, timeout=aiohttp.ClientTimeout(total=15)) as r2:
                                    if r2.status == 200:
                                        print(f"[telegram/async] {chat_id}: plain text fallback succeeded.")
                                    else:
                                        print(f"[telegram/async] {chat_id}: plain text fallback failed: {r2.status}")
                                        success = False
                                break   # don't retry after fallback
                    except Exception as exc:
                        print(f"[telegram/async] {chat_id} attempt {attempt}: {exc}")
                    if attempt < MAX_RETRIES:
                        await asyncio.sleep(RETRY_DELAY)
                else:
                    success = False
        return success

    async def _run_all() -> dict[str, bool]:
        sem = asyncio.Semaphore(30)   # Telegram rate limit: ~30 msg/s per bot
        connector = aiohttp.TCPConnector(limit=50)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [
                _send_one(session, sem, p["chat_id"], p["text"], p.get("keyboard"))
                for p in payloads
            ]
            results_list = await asyncio.gather(*tasks, return_exceptions=True)

        out = {}
        for p, result in zip(payloads, results_list):
            cid = p["chat_id"]
            if isinstance(result, Exception):
                print(f"[telegram/async] {cid} raised exception: {result}")
                out[cid] = False
            else:
                out[cid] = bool(result)
        return out

    print(f"[telegram] async broadcast → {len(payloads)} users...")
    try:
        # Use a new event loop if we're in a non-async context (e.g. GitHub Actions)
        try:
            loop = asyncio.get_running_loop()
            # Already in an async context — schedule on the existing loop
            import concurrent.futures
            future = asyncio.ensure_future(_run_all())
            # This path is rare; callers are usually synchronous agent.py runs
            results = loop.run_until_complete(future)
        except RuntimeError:
            results = asyncio.run(_run_all())
    except Exception as exc:
        print(f"[telegram] async broadcast failed ({exc}) — falling back to sync.")
        results = {}
        for p in payloads:
            cid = p["chat_id"]
            kb  = p.get("keyboard")
            results[cid] = (send_inline_keyboard(p["text"], kb, chat_id=cid)
                            if kb else send_message(p["text"], chat_id=cid))

    failed = [cid for cid, ok in results.items() if not ok]
    if failed:
        print(f"[telegram] async broadcast: {len(failed)} failed — {failed[:5]}")
    print(f"[telegram] async broadcast done: {len(results) - len(failed)}/{len(results)} succeeded.")
    return results
