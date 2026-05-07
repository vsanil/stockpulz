"""
telegram_notifier.py — Telegram Bot send/receive helpers + command parser.
Replaces whatsapp.py. Uses Telegram Bot API via plain requests (no heavy SDK).
"""

import os
import time
import threading
import requests

from config_manager import (
    get_config, update_config, update_config_multi,
    get_user_config, update_user_config, update_user_config_multi, reset_user_config,
    load_picks,
    load_pending_state, save_pending_state, clear_pending_state,
    load_user_trade_log,
    get_pending_users, add_pending_user, remove_pending_user,
    get_allowed_users,
)
from formatters import (
    _esc, _stars, _p, _upside, _short_company,
    format_daily_message, format_confirmation_message, format_weekly_recap_message,
)


def _is_number(s: str) -> bool:
    """Return True if s looks like a numeric value (int or float, optional commas)."""
    try:
        float(s.replace(",", ""))
        return True
    except (ValueError, AttributeError):
        return False

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
MAX_MESSAGE_LENGTH = 4096   # Telegram limit (much larger than WhatsApp)
MAX_RETRIES = 3
RETRY_DELAY = 5

# Crypto tickers recognised by the bot (used to set asset_type on manual trades)
_CRYPTO_SYMBOLS = {
    "BTC","ETH","SOL","BNB","XRP","ADA","DOGE","AVAX","DOT","MATIC",
    "LINK","UNI","ATOM","LTC","BCH","ALGO","XLM","VET","ICP","FIL",
}


def _is_admin(chat_id: str | None = None) -> bool:
    """Return True if the given chat_id (or env TELEGRAM_CHAT_ID) is the bot owner."""
    resolved = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    return str(resolved) == str(os.environ.get("TELEGRAM_CHAT_ID", ""))


# ── Core send ─────────────────────────────────────────────────────────────────

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
            "chat_id": chat_id,
            "text": chunk,
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
    """Send a message with an inline keyboard for user selection."""
    token   = _bot_token()
    chat_id = chat_id or _chat_id()
    url     = TELEGRAM_API.format(token=token, method="sendMessage")
    payload = {
        "chat_id":      chat_id,
        "text":         text,
        "parse_mode":   "HTML",
        "reply_markup": {"inline_keyboard": buttons},
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        return resp.status_code == 200
    except Exception as exc:
        print(f"[telegram] send_inline_keyboard error: {exc}")
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

    reply = _parse_and_execute(text.upper(), original=text, chat_id=chat_id)
    if reply:
        # Append /help hint to every command response except /help itself and daily picks
        cmd = text.lstrip("/").split()[0].lower() if text else ""
        if cmd not in ("help", "start", "today", "share") and not reply.startswith("📋"):
            reply = reply + "\n\n<i>📋 /help  ·  📲 /share</i>"
        send_message(reply, chat_id=chat_id)
    return reply


def _explain_pick(query: str) -> str:
    """
    Use Claude Haiku to answer a plain-English question about today's picks.
    Fuzzy-matches the query to a specific pick when possible.
    """
    import anthropic
    import json

    picks = load_picks()
    if not picks:
        return "📭 No picks for today yet. Check back after 8 AM ET."

    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})
    all_picks = (
        [("Short-term stock", p) for p in stocks.get("short_term", [])] +
        [("Long-term stock",  p) for p in stocks.get("long_term",  [])] +
        [("Short-term crypto", p) for p in crypto.get("short_term", [])] +
        [("Long-term crypto",  p) for p in crypto.get("long_term",  [])]
    )

    if not all_picks:
        return "📭 No picks found in today's message."

    # Fuzzy-match query against ticker / company / name
    q = query.lower()
    matched_label, matched_pick = None, None
    for label, p in all_picks:
        ticker  = (p.get("ticker") or p.get("symbol") or "").lower()
        company = (p.get("company") or p.get("name") or "").lower()
        if ticker in q or q in ticker or q in company or any(w in company for w in q.split()):
            matched_label, matched_pick = label, p
            break

    if matched_pick:
        context = f"Category: {matched_label}\nPick data: {json.dumps(matched_pick, indent=2)}"
        system  = (
            "You are a friendly financial analyst explaining a stock or crypto pick to a "
            "retail investor. Answer in plain English — no jargon, 3-5 sentences max. "
            "Focus on: why this pick was chosen today, key risk to watch, and one thing "
            "to monitor. Do NOT give general financial advice disclaimers."
        )
    else:
        context = f"Today's picks:\n{json.dumps([p for _, p in all_picks], indent=2)}"
        system  = (
            "You are a friendly financial analyst. Answer questions about today's stock and "
            "crypto picks in plain English — no jargon, 3-5 sentences max. "
            "If the question is about something not in today's picks, say so briefly."
        )

    user_msg = f"{context}\n\nUser question: {query}"

    try:
        client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            system=system,
            messages=[{"role": "user", "content": user_msg}],
        )
        return f"💬 {message.content[0].text.strip()}"
    except Exception as exc:
        return f"⚠️ Could not generate explanation: {exc}"


def _nl_param(command: str, raw: str) -> str:
    """
    Use Claude Haiku to normalize a natural-language parameter for a slash command.
    Only called when the parameter isn't an obvious exact match.

    command="exclude" → returns JSON array of sector names  e.g. '["Energy","Utilities"]'
    command="watch"   → returns JSON array of ticker symbols e.g. '["TSLA","MSFT","BRK-B"]'
    command="risk"    → returns one word: conservative | moderate | aggressive
    """
    import anthropic
    prompts = {
        "exclude": (
            f'Map "{raw}" to a JSON array of standard US stock sector names. '
            'Valid values: Technology, Financials, Health Care, Energy, Utilities, '
            'Consumer Discretionary, Consumer Staples, Industrials, Materials, '
            'Real Estate, Communication Services. '
            'Return ONLY a JSON array, e.g. ["Energy"] or ["Financials","Utilities"].'
        ),
        "watch": (
            f'Map "{raw}" to a JSON array of US stock ticker symbols. '
            'Use official NYSE/NASDAQ tickers. Examples: Tesla→TSLA, Microsoft→MSFT, '
            'Berkshire→BRK-B, Google/Alphabet→GOOGL, Meta→META, Amazon→AMZN, '
            'Nvidia→NVDA, Apple→AAPL, JPMorgan→JPM, Pepsi→PEP. '
            'Return ONLY a JSON array, e.g. ["TSLA","MSFT"].'
        ),
        "risk": (
            f'Map "{raw}" to exactly one of: conservative, moderate, aggressive. '
            'Examples: "safe/careful/low risk" → conservative, '
            '"bold/risky/go big" → aggressive, "normal/balanced" → moderate. '
            'Return ONLY the single word.'
        ),
    }
    try:
        client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompts[command]}],
        )
        return message.content[0].text.strip()
    except Exception as exc:
        print(f"[telegram] _nl_param failed for {command!r}: {exc}")
        return raw


def _resolve_ticker_candidates(name_or_ticker: str) -> list[dict]:
    """
    Resolve a company name or ticker to a list of candidate dicts:
      [{"ticker": "AAPL", "name": "Apple Inc"}, ...]
    Returns a single-item list for unambiguous matches, multiple for ambiguous ones.
    """
    import re as _re
    import json as _j
    import anthropic as _ant

    raw = name_or_ticker.strip()

    # Already looks like a ticker — return directly, no Haiku needed
    if _re.match(r"^[A-Za-z.\-]{1,6}$", raw):
        return [{"ticker": raw.upper(), "name": raw.upper()}]

    # Ask Haiku for up to 4 candidates with full names
    prompt = (
        f'The user typed "{raw}" as a stock or crypto to trade. '
        'Return up to 4 matching US stock/crypto candidates as a JSON array of objects. '
        'Each object must have "ticker" (official symbol) and "name" (short company name). '
        'Order by most likely match first. '
        'Examples: "apple" → [{"ticker":"AAPL","name":"Apple Inc"}], '
        '"bank" → [{"ticker":"JPM","name":"JPMorgan"},{"ticker":"BAC","name":"Bank of America"},'
        '{"ticker":"WFC","name":"Wells Fargo"},{"ticker":"C","name":"Citigroup"}]. '
        'Return ONLY the JSON array.'
    )
    try:
        client  = _ant.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        candidates = _j.loads(message.content[0].text.strip())
        if isinstance(candidates, list) and candidates:
            return candidates
    except Exception as exc:
        print(f"[telegram] _resolve_ticker_candidates failed: {exc}")

    return [{"ticker": raw.upper(), "name": raw.upper()}]


def _fetch_live_price(ticker: str) -> float | None:
    """Fetch the latest price for a ticker via yfinance."""
    import yfinance as _yf
    try:
        data = _yf.download(ticker, period="1d", interval="1m",
                            progress=False, auto_adjust=True)
        return float(data["Close"].dropna().iloc[-1])
    except Exception:
        return None


def _resolve_ticker_and_price(name_or_ticker: str, price_str: str | None) -> tuple[str, float | None]:
    """Single-result convenience wrapper used when caller doesn't need multi-select."""
    candidates = _resolve_ticker_candidates(name_or_ticker)
    ticker = candidates[0]["ticker"]
    price: float | None = None
    if price_str:
        try:
            price = float(price_str)
        except ValueError:
            pass
    if price is None:
        price = _fetch_live_price(ticker)
    return ticker, price


def handle_callback_query(callback_query: dict) -> None:
    """
    Handle inline keyboard button taps.
    callback_data format:
      buy|TICKER|price|shares   (price/shares may be empty string)
      sell|TICKER|price
    """
    from trade_logger import manual_open_trade, manual_close_trade

    cq_id   = callback_query.get("id", "")
    data    = callback_query.get("data", "")
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))

    answer_callback_query(cq_id)   # dismiss spinner immediately

    parts = data.split("|")
    action = parts[0] if parts else ""

    if action == "cancel_pending":
        target_chat = parts[1] if len(parts) > 1 else chat_id
        clear_pending_state(target_chat)
        send_message("👍 Cancelled.", chat_id=chat_id)
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

    if action == "buy":
        ticker     = parts[1] if len(parts) > 1 else ""
        price_raw  = parts[2] if len(parts) > 2 else ""
        shares_raw = parts[3] if len(parts) > 3 else ""

        price = float(price_raw) if price_raw else _fetch_live_price(ticker)
        if not price:
            send_message(f"⚠️ Could not fetch price for <b>{ticker}</b>. Try: <code>/bought {ticker} 182.50</code>", chat_id=chat_id)
            return

        shares = float(shares_raw) if shares_raw else None

        # Pull target/stop from today's picks
        target = stop = None
        picks  = load_picks()
        if picks:
            all_st = (picks.get("stocks", {}).get("short_term", []) +
                      picks.get("crypto", {}).get("short_term", []))
            for p in all_st:
                sym = p.get("ticker") or p.get("symbol", "")
                if sym.upper() == ticker:
                    target = p.get("target_price")
                    stop   = p.get("stop_loss")
                    break

        asset_type = "crypto" if ticker in _CRYPTO_SYMBOLS else "stock"

        # Resolve per-user thresholds for callback-path buys
        _ucfg   = get_user_config(chat_id)
        _gcfg   = get_config()
        _sl_pct = float(_ucfg.get("stop_loss_pct")   or _gcfg.get("stop_loss_pct",   7.0))
        _tg_pct = float(_ucfg.get("target_gain_pct") or _gcfg.get("target_gain_pct", 15.0))

        trade = manual_open_trade(ticker, price, chat_id, asset_type=asset_type,
                                  shares=shares, target_price=target, stop_loss=stop,
                                  stop_loss_pct=_sl_pct, target_gain_pct=_tg_pct)

        alloc_str  = f"  · <code>${trade['allocation']:.2f}</code> deployed" if trade.get("allocation") else ""
        shares_str = f"  · {shares} shares" if shares else ""
        send_message(
            f"✅ <b>Logged: bought {ticker}</b>\n"
            f"Entry:  <code>${trade['entry_price']}</code>{shares_str}{alloc_str}\n"
            f"Target: <code>${trade['target_price']}</code>  "
            f"<i>(+{((trade['target_price']/trade['entry_price'])-1)*100:.1f}%)</i>\n"
            f"Stop:   <code>${trade['stop_loss']}</code>  "
            f"<i>({((trade['stop_loss']/trade['entry_price'])-1)*100:.1f}%)</i>\n"
            f"<i>I'll track this and alert you at target/stop.</i>",
            chat_id=chat_id,
        )

    elif action == "cancel_abort":
        send_message("👍 No changes made.", chat_id=chat_id)

    elif action == "confirm_cancel":
        from trade_logger import cancel_trade
        ticker  = parts[1] if len(parts) > 1 else ""
        removed = cancel_trade(ticker, chat_id)
        if not removed:
            send_message(f"⚠️ No open position found for <b>{ticker}</b>.", chat_id=chat_id)
        else:
            send_message(
                f"🗑 <b>Cancelled buy: {ticker}</b>\n"
                f"Entry <code>${removed.get('entry_price')}</code> removed — not counted in P&amp;L.",
                chat_id=chat_id,
            )

    elif action == "confirm_reopen":
        from trade_logger import reopen_trade
        ticker   = parts[1] if len(parts) > 1 else ""
        reopened = reopen_trade(ticker, chat_id)
        if not reopened:
            send_message(f"⚠️ No closed trade found for <b>{ticker}</b> to reopen.", chat_id=chat_id)
        else:
            send_message(
                f"↩️ <b>Undid sell: {ticker}</b>\n"
                f"Entry <code>${reopened.get('entry_price')}</code> moved back to open positions.",
                chat_id=chat_id,
            )

    elif action in ("cancel", "cancel_auto"):
        from trade_logger import cancel_trade, reopen_trade
        ticker = parts[1] if len(parts) > 1 else ""

        if action == "cancel_auto":
            log        = load_user_trade_log(chat_id)
            open_trade   = next((t for t in log.get("open",   []) if t["ticker"] == ticker), None)
            closed_trade = next((t for t in reversed(log.get("closed", [])) if t["ticker"] == ticker), None)

            if open_trade and closed_trade:
                # Both exist — ask which one to undo
                send_inline_keyboard(
                    f"↩️ What do you want to undo for <b>{ticker}</b>?\n\n"
                    f"Open:   bought <code>${open_trade.get('entry_price')}</code>  · {open_trade.get('opened_date')}\n"
                    f"Closed: sold <code>${closed_trade.get('closed_price')}</code>  · {closed_trade.get('closed_date')}",
                    [[
                        {"text": "❌ Undo buy",  "callback_data": f"confirm_cancel|{ticker}"},
                        {"text": "↩️ Undo sell", "callback_data": f"confirm_reopen|{ticker}"},
                    ]],
                    chat_id=chat_id,
                )
                return

            if open_trade:
                # Confirm before removing the open buy
                send_inline_keyboard(
                    f"⚠️ <b>Undo this buy?</b>\n\n"
                    f"<b>{ticker}</b>  bought at <code>${open_trade.get('entry_price')}</code>  "
                    f"· {open_trade.get('opened_date')}\n"
                    f"<i>This will remove it from your open positions.</i>",
                    [[
                        {"text": "✅ Yes, undo buy", "callback_data": f"confirm_cancel|{ticker}"},
                        {"text": "❌ No, keep it",   "callback_data": "cancel_abort"},
                    ]],
                    chat_id=chat_id,
                )
                return

            if closed_trade:
                # Confirm before reopening the closed sell
                ret  = closed_trade.get("return_pct", 0)
                sign = "+" if ret >= 0 else ""
                send_inline_keyboard(
                    f"⚠️ <b>Undo this sell?</b>\n\n"
                    f"<b>{ticker}</b>  sold at <code>${closed_trade.get('closed_price')}</code>  "
                    f"{sign}{ret}%  · {closed_trade.get('closed_date')}\n"
                    f"<i>This will reopen the position as if the sale never happened.</i>",
                    [[
                        {"text": "✅ Yes, undo sell", "callback_data": f"confirm_reopen|{ticker}"},
                        {"text": "❌ No, keep it",    "callback_data": "cancel_abort"},
                    ]],
                    chat_id=chat_id,
                )
                return

    elif action == "sell":
        ticker    = parts[1] if len(parts) > 1 else ""
        price_raw = parts[2] if len(parts) > 2 else ""

        price = float(price_raw) if price_raw else _fetch_live_price(ticker)
        if not price:
            send_message(f"⚠️ Could not fetch price for <b>{ticker}</b>. Try: <code>/sold {ticker} 197.10</code>", chat_id=chat_id)
            return

        closed = manual_close_trade(ticker, price, chat_id)
        if not closed:
            send_message(f"⚠️ No open position found for <b>{ticker}</b>. Use /positions to see open trades.", chat_id=chat_id)
            return

        ret   = closed["return_pct"]
        gain  = closed["gain_usd"]
        emoji = "✅" if ret >= 0 else "🔴"
        sign  = "+" if ret >= 0 else ""
        gsign = "+" if gain >= 0 else ""
        send_message(
            f"{emoji} <b>Closed: {ticker}</b>\n"
            f"Entry:  <code>${closed['entry_price']}</code>\n"
            f"Exit:   <code>${closed['closed_price']}</code>\n"
            f"Return: <b>{sign}{ret}%</b>  P&amp;L: <code>{gsign}${abs(gain):.2f}</code>\n"
            f"<i>Saved to trade history.</i>",
            chat_id=chat_id,
        )


# ── Prompts for param commands ────────────────────────────────────────────────

_PARAM_PROMPTS: dict[str, str] = {
    "bought":   ("🛒 <b>What did you buy?</b>\n"
                 "<i>e.g.</i>  <code>apple</code>  ·  <code>AAPL 182.50</code>  ·  <code>AAPL 182.50 5</code>"),
    "sold":     ("💸 <b>What did you sell?</b>\n"
                 "<i>e.g.</i>  <code>apple</code>  ·  <code>AAPL 197.10</code>"),
    "cancel":   ("↩️ <b>Which trade to undo?</b>\n"
                 "<i>e.g.</i>  <code>apple</code>  ·  <code>AAPL</code>"),
    "explain":  ("💬 <b>What would you like to know?</b>\n"
                 "<i>e.g.</i>  <code>why is NVDA picked?</code>  ·  <code>apple thesis</code>"),
    "watch":    ("👀 <b>Which tickers to watch?</b>\n"
                 "<i>e.g.</i>  <code>NVDA TSLA</code>  ·  <code>nvidia tesla</code>"),
    "exclude":  ("🚫 <b>Which sector to exclude?</b>\n"
                 "<i>e.g.</i>  <code>energy</code>  ·  <code>oil stocks</code>"),
    "set_risk": ("⚖️ <b>Risk level?</b>\n"
                 "<code>conservative</code>   ·   <code>moderate</code>   ·   <code>aggressive</code>"),
    "set_st":    "💰 <b>Stock short-term budget per trade?</b>  <i>e.g.</i>  <code>30</code>",
    "set_lt":    "💰 <b>Stock long-term monthly budget?</b>  <i>e.g.</i>  <code>50</code>",
    "set_cst":   "💰 <b>Crypto short-term budget per trade?</b>  <i>e.g.</i>  <code>20</code>",
    "set_clt":   "💰 <b>Crypto long-term monthly budget?</b>  <i>e.g.</i>  <code>30</code>",
    "alert":     ("🔔 <b>Set a price alert</b>\n"
                  "<i>e.g.</i>  <code>NVDA 1000</code>  ·  <code>AAPL below 175</code>  ·  <code>TSLA above 300</code>"),
    "unalert":   ("🔕 <b>Remove which alert?</b>\n"
                  "<i>e.g.</i>  <code>NVDA</code>  (removes all NVDA alerts)  ·  <code>NVDA 1000</code>"),
    "paper_buy": ("📄 <b>Paper buy — what to simulate?</b>\n"
                  "<i>e.g.</i>  <code>AAPL 10</code>  ·  <code>AAPL 182.50 10</code>"),
    "paper_sell":("📄 <b>Paper sell — which position?</b>\n"
                  "<i>e.g.</i>  <code>AAPL</code>  ·  <code>AAPL 5</code>  (partial sell)"),
}


def _prompt_for_param(command: str, chat_id: str) -> None:
    """Save pending state and send the parameter-request prompt with a Cancel button."""
    prompt = _PARAM_PROMPTS.get(command, f"What value for /{command}?")
    save_pending_state(chat_id, command)
    send_inline_keyboard(
        prompt,
        [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
        chat_id=chat_id,
    )


# ── Extracted buy/sell execution (shared by direct + conversational paths) ────

def _execute_bought(ticker: str, price_raw, shares_raw, chat_id: str) -> str:
    from trade_logger import manual_open_trade
    # Resolve per-user thresholds (fall back to global defaults)
    _user_cfg   = get_user_config(chat_id)
    _global_cfg = get_config()
    _sl_pct  = float(_user_cfg.get("stop_loss_pct")   or _global_cfg.get("stop_loss_pct",   7.0))
    _tg_pct  = float(_user_cfg.get("target_gain_pct") or _global_cfg.get("target_gain_pct", 15.0))
    price: float | None = None
    if price_raw:
        try:
            price = float(str(price_raw).strip())
        except ValueError:
            pass
    if price is None:
        price = _fetch_live_price(ticker)
    if price is None:
        return (f"⚠️ Could not fetch price for <b>{ticker}</b>. "
                f"Reply with the price, e.g. <code>182.50</code>")

    shares: float | None = None
    if shares_raw:
        try:
            shares = float(str(shares_raw).strip())
        except (ValueError, TypeError):
            pass

    target = stop = None
    picks  = load_picks()
    if picks:
        all_st = (picks.get("stocks", {}).get("short_term", []) +
                  picks.get("crypto", {}).get("short_term", []))
        for p in all_st:
            sym = p.get("ticker") or p.get("symbol", "")
            if sym.upper() == ticker:
                target = p.get("target_price")
                stop   = p.get("stop_loss")
                break

    asset_type = "crypto" if ticker in _CRYPTO_SYMBOLS else "stock"

    trade = manual_open_trade(ticker, price, chat_id, asset_type=asset_type,
                              shares=shares, target_price=target, stop_loss=stop,
                              stop_loss_pct=_sl_pct, target_gain_pct=_tg_pct)

    alloc_str  = f"  · <code>${trade['allocation']:.2f}</code> deployed" if trade.get("allocation") else ""
    shares_str = f"  · {shares} shares" if shares else ""
    return (
        f"✅ <b>Logged: bought {ticker}</b>\n"
        f"Entry:  <code>${trade['entry_price']}</code>{shares_str}{alloc_str}\n"
        f"Target: <code>${trade['target_price']}</code>  "
        f"<i>(+{((trade['target_price']/trade['entry_price'])-1)*100:.1f}%)</i>\n"
        f"Stop:   <code>${trade['stop_loss']}</code>  "
        f"<i>({((trade['stop_loss']/trade['entry_price'])-1)*100:.1f}%)</i>\n"
        f"<i>I'll check this at 10:30 AM and 3:30 PM and alert if target/stop is hit.</i>"
    )


def _execute_sold(ticker: str, price_raw, chat_id: str) -> str:
    from trade_logger import manual_close_trade
    price: float | None = None
    if price_raw:
        try:
            price = float(str(price_raw).strip())
        except ValueError:
            pass
    if price is None:
        price = _fetch_live_price(ticker)
    if price is None:
        return (f"⚠️ Could not fetch price for <b>{ticker}</b>. "
                f"Reply with the price, e.g. <code>197.10</code>")

    closed = manual_close_trade(ticker, price, chat_id)
    if not closed:
        return f"⚠️ No open position found for <b>{ticker}</b>. Use /portfolio to see open trades."

    ret   = closed["return_pct"]
    gain  = closed["gain_usd"]
    emoji = "✅" if ret >= 0 else "🔴"
    sign  = "+" if ret >= 0 else ""
    gsign = "+" if gain >= 0 else ""
    return (
        f"{emoji} <b>Closed: {ticker}</b>\n"
        f"Entry:  <code>${closed['entry_price']}</code>\n"
        f"Exit:   <code>${closed['closed_price']}</code>\n"
        f"Return: <b>{sign}{ret}%</b>  P&amp;L: <code>{gsign}${abs(gain):.2f}</code>\n"
        f"<i>Saved to trade history.</i>"
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

    # ── /bought multi-step ────────────────────────────────────────────────────
    if command == "bought":
        if step == 2:
            # User is replying with a price (or blank = live price)
            ticker     = data.get("ticker", "")
            shares_raw = data.get("shares")
            price_raw  = text.strip() or None
            return _execute_bought(ticker, price_raw, shares_raw, chat_id)

        # Step 1: parse "apple" / "AAPL 182.50" / "AAPL 182.50 5"
        parts     = text.strip().split()
        name_raw  = parts[0] if parts else ""
        price_raw = parts[1] if len(parts) >= 2 else None
        shares_raw = parts[2] if len(parts) >= 3 else None

        if not name_raw:
            return "⚠️ Please tell me which stock you bought."

        candidates = _resolve_ticker_candidates(name_raw)
        if len(candidates) > 1:
            price_enc  = price_raw  or ""
            shares_enc = shares_raw or ""
            buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                         "callback_data": f"buy|{c['ticker']}|{price_enc}|{shares_enc}"}]
                       for c in candidates]
            send_inline_keyboard(f"🔍 Which stock did you mean by <b>{_esc(name_raw)}</b>?",
                                 buttons, chat_id=chat_id)
            return ""

        ticker = candidates[0]["ticker"]
        if price_raw is None:
            # Need price — go to step 2
            save_pending_state(chat_id, "bought", step=2, data={"ticker": ticker})
            send_inline_keyboard(
                f"Got it — <b>{ticker}</b>. At what price did you buy?\n"
                f"<i>Send blank to use live price</i>",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""

        return _execute_bought(ticker, price_raw, shares_raw, chat_id)

    # ── /sold multi-step ──────────────────────────────────────────────────────
    if command == "sold":
        if step == 2:
            ticker    = data.get("ticker", "")
            price_raw = text.strip() or None
            return _execute_sold(ticker, price_raw, chat_id)

        parts     = text.strip().split()
        name_raw  = parts[0] if parts else ""
        price_raw = parts[1] if len(parts) >= 2 else None

        if not name_raw:
            return "⚠️ Please tell me which stock you sold."

        candidates = _resolve_ticker_candidates(name_raw)
        if len(candidates) > 1:
            price_enc = price_raw or ""
            buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                         "callback_data": f"sell|{c['ticker']}|{price_enc}"}]
                       for c in candidates]
            send_inline_keyboard(f"🔍 Which stock did you mean?", buttons, chat_id=chat_id)
            return ""

        ticker = candidates[0]["ticker"]
        if price_raw is None:
            save_pending_state(chat_id, "sold", step=2, data={"ticker": ticker})
            send_inline_keyboard(
                f"Got it — <b>{ticker}</b>. At what price did you sell?\n"
                f"<i>Send blank to use live price</i>",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""

        return _execute_sold(ticker, price_raw, chat_id)

    # ── Single-step param commands ────────────────────────────────────────────
    if command == "history":
        return _parse_and_execute("HISTORY", original="/history", chat_id=chat_id)

    if command == "cancel":
        return _parse_and_execute(f"CANCEL {text}", original=f"/cancel {text}", chat_id=chat_id)

    if command == "explain":
        return _explain_pick(text)

    if command == "watch":
        return _parse_and_execute(f"WATCH {text}", original=f"/watch {text}", chat_id=chat_id)

    if command == "exclude":
        return _parse_and_execute(f"EXCLUDE {text}", original=f"/exclude {text}", chat_id=chat_id)

    if command == "set_risk":
        return _parse_and_execute(f"SET RISK {text}", original=text, chat_id=chat_id)

    if command == "set_budget":
        return _parse_and_execute(f"SET BUDGET {text}".strip(), original=f"/set_budget {text}", chat_id=chat_id)

    if command == "alert":
        return _parse_and_execute(f"ALERT {text}", original=f"/alert {text}", chat_id=chat_id)

    if command == "unalert":
        return _parse_and_execute(f"UNALERT {text}", original=f"/unalert {text}", chat_id=chat_id)

    if command == "paper_buy":
        return _parse_and_execute(f"PAPER BUY {text}", original=f"/paper_buy {text}", chat_id=chat_id)

    if command == "paper_sell":
        return _parse_and_execute(f"PAPER SELL {text}", original=f"/paper_sell {text}", chat_id=chat_id)

    if command == "paper_add_cash":
        return _parse_and_execute(f"PAPER ADD CASH {text}", original=f"/paper_add_cash {text}", chat_id=chat_id)

    if command == "start":
        return _parse_and_execute("START", original="/start", chat_id=chat_id)

    if command == "share":
        return _parse_and_execute("SHARE", original="/share", chat_id=chat_id)

    if command == "adduser":
        return _parse_and_execute(f"ADDUSER {text}", original=f"/adduser {text}", chat_id=chat_id)

    if command == "removeuser":
        return _parse_and_execute(f"REMOVEUSER {text}", original=f"/removeuser {text}", chat_id=chat_id)

    if command == "users":
        return _parse_and_execute("USERS", original="/users", chat_id=chat_id)

    if command == "broadcast":
        return _parse_and_execute(f"BROADCAST {text}", original=f"/broadcast {text}", chat_id=chat_id)

    if command == "release":
        return _parse_and_execute(f"RELEASE {text}", original=f"/release {text}", chat_id=chat_id)

    return _handle_natural_language(text)


def _handle_natural_language(query: str, chat_id: str | None = None) -> str:
    """
    Parse a free-text message into a bot command using Claude Haiku, then execute it.
    Used as a fallback when no slash-command pattern matches.
    Examples:
      "make my picks more aggressive"    → set_risk aggressive
      "add nvidia and apple to watchlist" → watch NVDA AAPL
      "never show me energy stocks"       → exclude Energy
      "set stock budget to 200, crypto 50" → set_budget stocks 200 crypto 50
      "why was microsoft picked today?"   → explain query
    """
    import anthropic
    import json

    SYSTEM = """You are a command parser for a personal stock advisor Telegram bot.
Parse the user's natural language message into a JSON command. Return ONLY valid JSON — no text before or after.

Available intents and their exact JSON format:
{"intent": "set_risk",    "value": "conservative|moderate|aggressive"}
{"intent": "watch",       "tickers": ["NVDA", "TSLA"]}
{"intent": "watch_clear"}
{"intent": "exclude",     "sectors": ["Energy", "Utilities"]}
{"intent": "exclude_clear"}
{"intent": "set_budget",  "stock_budget": 200, "crypto_budget": 50}   — either key optional, null to clear
{"intent": "set_picks",       "max_stock_picks": 3, "max_crypto_picks": 1} — either key optional, null to clear
{"intent": "set_thresholds", "stop_loss_pct": 7, "target_gain_pct": 12}  — either key optional, null to clear
{"intent": "pause"}
{"intent": "resume"}
{"intent": "status"}
{"intent": "next"}
{"intent": "settings"}
{"intent": "today"}
{"intent": "prices"}
{"intent": "perf"}
{"intent": "reset"}
{"intent": "explain",     "query": "the user's question verbatim"}
{"intent": "unknown"}

Rules:
- Map "aggressive/risky/bold" → set_risk aggressive
- Map "conservative/safe/careful" → set_risk conservative
- Map "add X to watchlist/watch X" → watch with tickers in uppercase
- Map "remove/clear watchlist" → watch_clear
- Map "exclude/skip/never pick sector" → exclude with proper sector name
- Map "set/change/increase budget" → set_budget with stock_budget and/or crypto_budget numeric values
- Map "show me N stocks/picks", "limit/reduce picks", "only N crypto" → set_picks
- Map "change stop loss", "tighten/widen stop", "set target gain", "adjust thresholds" → set_thresholds with numeric values
- Map "my settings", "show all settings", "full settings", "what are my settings" → settings
- Map "status", "am I paused", "is bot running" → status
- Map "when's my next message", "when is the next pick", "next update", "what time" → next
- "stocks 200 crypto 50" → {"stock_budget": 200, "crypto_budget": 50}
- "stock budget 150" → {"stock_budget": 150}
- "clear budgets" → {"stock_budget": null, "crypto_budget": null}
- If the message is a question about picks, use explain
- If truly unclear, use unknown"""

    try:
        client  = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            system=SYSTEM,
            messages=[{"role": "user", "content": query}],
        )
        parsed = json.loads(message.content[0].text.strip())
    except Exception as exc:
        print(f"[telegram] NL parse failed ({exc}) — treating as explain query")
        return _explain_pick(query)

    chat_id = chat_id or _chat_id()

    intent = parsed.get("intent", "unknown")
    print(f"[telegram] NL intent: {intent} from: {query!r}")

    if intent == "set_risk":
        return _parse_and_execute(f"SET RISK {parsed.get('value','moderate').upper()}", original=query, chat_id=chat_id)
    if intent == "watch":
        tickers = " ".join(parsed.get("tickers", []))
        return _parse_and_execute(f"WATCH {tickers}", original=f"/watch {tickers}", chat_id=chat_id)
    if intent == "watch_clear":
        return _parse_and_execute("WATCH NONE", original="/watch none", chat_id=chat_id)
    if intent == "exclude":
        sectors = " ".join(parsed.get("sectors", []))
        return _parse_and_execute(f"EXCLUDE {sectors.upper()}", original=f"/exclude {sectors}", chat_id=chat_id)
    if intent == "exclude_clear":
        return _parse_and_execute("EXCLUDE NONE", original="/exclude none", chat_id=chat_id)
    if intent == "set_budget":
        parts = []
        if parsed.get("stock_budget") is not None:
            parts.append(f"stocks {parsed['stock_budget']}")
        if parsed.get("crypto_budget") is not None:
            parts.append(f"crypto {parsed['crypto_budget']}")
        cmd = f"SET BUDGET {' '.join(parts)}" if parts else "SET BUDGET off"
        return _parse_and_execute(cmd, original=query, chat_id=chat_id)
    if intent == "set_picks":
        parts = []
        if parsed.get("max_stock_picks") is not None:
            parts.append(f"stocks {parsed['max_stock_picks']}")
        if parsed.get("max_crypto_picks") is not None:
            parts.append(f"crypto {parsed['max_crypto_picks']}")
        cmd = f"SET PICKS {' '.join(parts)}" if parts else "SET PICKS off"
        return _parse_and_execute(cmd, original=query, chat_id=chat_id)
    if intent == "set_thresholds":
        parts = []
        if parsed.get("stop_loss_pct") is not None:
            parts.append(f"stop {parsed['stop_loss_pct']}")
        if parsed.get("target_gain_pct") is not None:
            parts.append(f"target {parsed['target_gain_pct']}")
        cmd = f"SET THRESHOLDS {' '.join(parts)}" if parts else "SET THRESHOLDS"
        return _parse_and_execute(cmd, original=query, chat_id=chat_id)
    if intent == "pause":
        return _parse_and_execute("PAUSE", original=query, chat_id=chat_id)
    if intent == "resume":
        return _parse_and_execute("RESUME", original=query, chat_id=chat_id)
    if intent == "status":
        return _parse_and_execute("STATUS", original=query, chat_id=chat_id)
    if intent == "next":
        return _parse_and_execute("NEXT", original=query, chat_id=chat_id)
    if intent == "settings":
        return _parse_and_execute("SETTINGS", original=query, chat_id=chat_id)
    if intent == "today":
        return _parse_and_execute("TODAY", original=query, chat_id=chat_id)
    if intent == "prices":
        return _parse_and_execute("PRICES", original=query, chat_id=chat_id)
    if intent == "perf":
        return _parse_and_execute("PERF", original=query, chat_id=chat_id)
    if intent == "reset":
        return _parse_and_execute("RESET", original=query, chat_id=chat_id)
    if intent == "explain":
        return _explain_pick(parsed.get("query", query))

    # True unknown — still try explain as last resort for questions
    return _explain_pick(query)





def _nl_parse_trade(command: str, raw: str) -> dict:
    """
    Use Haiku to extract structured fields from a natural-language trade/alert param string.

    command: "bought" | "sold" | "alert" | "unalert" | "paper_buy" | "paper_sell"
    raw:     everything after the slash-command, e.g. "10 apple stocks for 182.5 dollars"

    Returns a dict with extracted fields (None for missing optional ones).
    Always returns at minimum {"ticker": None} so callers can check for missing fields.

    Examples:
      bought  "10 apple stocks today for $182.50"
              → {"ticker": "AAPL", "price": 182.50, "shares": 10}
      alert   "when nvidia drops below 800"
              → {"ticker": "NVDA", "price": 800.0, "direction": "below"}
      paper_buy "buy 5 shares of tesla"
              → {"ticker": "TSLA", "shares": 5, "price": None}
    """
    import anthropic as _anthropic
    import json as _json

    schemas = {
        "bought":    '{"ticker": "AAPL or null", "price": 182.50, "shares": 10}  — shares is optional',
        "sold":      '{"ticker": "AAPL or null", "price": 197.10, "shares": null}  — price is optional',
        "alert":     '{"ticker": "NVDA or null", "price": 800.0, "direction": "above|below|auto"}',
        "unalert":   '{"ticker": "NVDA or null"}',
        "paper_buy": '{"ticker": "AAPL or null", "shares": 10, "price": null}  — price is optional',
        "paper_sell":  '{"ticker": "AAPL or null", "shares": null, "price": null}  — both optional',
        "paper_reset": '{"price": 50000.0}  — the starting cash amount (price field reused for amount)',
    }
    schema = schemas.get(command, '{"ticker": null}')

    SYSTEM = f"""You are a field extractor for a stock trading bot command.
The user sent a /{command} command with a natural-language parameter.
Extract the required fields and return ONLY valid JSON — no text before or after.

Target schema: {schema}

Rules:
- ticker: resolve company names to uppercase ticker symbols (Apple→AAPL, Nvidia→NVDA, Tesla→TSLA, etc.)
  If you cannot confidently identify the ticker, return null.
- price: extract any dollar amount mentioned. Strip "$", "dollars", "USD". Return as float or null.
- shares: extract share count or quantity. Words like "10 shares", "10 stocks", "10 units" → 10. Return as float or null.
- direction: "below"/"under"/"drops below"/"falls to" → "below". "above"/"over"/"crosses"/"hits"/"reaches" → "above". Default → "auto".
- If a field is not mentioned, return null (do not guess).
- Return ONLY the JSON object, nothing else."""

    try:
        client  = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=SYSTEM,
            messages=[{"role": "user", "content": raw}],
        )
        result = _json.loads(message.content[0].text.strip())
        print(f"[telegram] NL trade parse ({command}): {result}")
        return result
    except Exception as exc:
        print(f"[telegram] NL trade parse failed ({exc})")
        return {"ticker": None}


def _parse_and_execute(text: str, original: str = "", chat_id: str | None = None) -> str:
    """Parse command string and return reply."""
    chat_id = chat_id or _chat_id()

    # Telegram slash-commands (/help) or plain text (HELP) — normalise both
    text = text.lstrip("/").replace("_", " ")   # /set_st 30 → SET ST 30

    if text == "TODAY":
        picks = load_picks()
        if not picks:
            return "📭 No picks for today yet. Check back after 8 AM ET."
        config = {**get_config(), **get_user_config(chat_id)}
        return format_daily_message(picks, config)

    if text == "EXPLAIN":
        _prompt_for_param("explain", chat_id)
        return ""

    if text.startswith("EXPLAIN "):
        # Preserve original casing for the query — strip the command prefix
        raw = original.lstrip("/")
        query = raw.split(" ", 1)[1].strip() if " " in raw else raw
        return _explain_pick(query)

    if text == "PERF":
        from trade_logger import get_performance_stats
        stock_stats  = get_performance_stats(chat_id, "stock")
        crypto_stats = get_performance_stats(chat_id, "crypto")

        if not stock_stats and not crypto_stats:
            return "📭 No closed trades yet. Check back after your first picks are resolved."

        def _stat_block(label: str, s: dict) -> str:
            if not s:
                return f"{label}: no closed trades yet"
            sign      = "+" if s["avg_return"] >= 0 else ""
            gain_sign = "+" if s["total_gain_usd"] >= 0 else ""
            cum_sign  = "+" if s.get("cumulative_return_pct", 0) >= 0 else ""
            best_sym,  best_r  = s["best"]
            worst_sym, worst_r = s["worst"]
            streak = s.get("streak", 0)
            streak_str = f"  🔥 {streak}-win streak" if streak >= 2 else ""
            return (
                f"<b>{label}</b> — {s['count']} trades  {s['win_rate']}% wins{streak_str}\n"
                f"Avg/trade: {sign}{s['avg_return']}%  "
                f"Cumulative: <b>{cum_sign}{s['cumulative_return_pct']}%</b>\n"
                f"Best: <b>{best_sym}</b> {'+' if best_r >= 0 else ''}{best_r}%  "
                f"Worst: <b>{worst_sym}</b> {'+' if worst_r >= 0 else ''}{worst_r}%\n"
                f"✅ {s['targets_hit']} targets  🔴 {s['stops_hit']} stops  ⏱ {s['expired']} expired\n"
                f"P&L: <code>{gain_sign}${abs(s['total_gain_usd']):.2f}</code> "
                f"on <code>${s['total_deployed_usd']:.0f}</code> deployed"
            )

        open_line = ""
        if stock_stats and stock_stats["open_count"]:
            open_line = f"\n\n<i>{stock_stats['open_count']} trade(s) still open</i>"

        lines = ["<b>📊 All-Time Performance</b>", ""]
        lines.append(_stat_block("📈 Stocks", stock_stats))
        lines += ["", _stat_block("🪙 Crypto", crypto_stats)]
        if open_line:
            lines.append(open_line)
        return "\n".join(lines)

    if text == "PRICES":
        picks = load_picks()
        if not picks:
            return "📭 No picks found for today yet. Check back after 8 AM ET."
        try:
            from price_checker import get_current_prices
            current_prices = get_current_prices(picks)
            return format_confirmation_message(picks, current_prices)
        except Exception as exc:
            return f"⚠️ Could not fetch prices: {exc}"

    if text == "COMMUNITY":
        try:
            from performance_tracker import build_community_stats
            from config_manager import get_allowed_users, load_user_trade_log
            users = get_allowed_users()
            logs  = []
            for uid in users:
                try:
                    logs.append(load_user_trade_log(uid))
                except Exception:
                    pass
            stats = build_community_stats(logs)
            if not stats or stats["total_trades"] == 0:
                return (
                    "📭 <b>StockPulz Community</b>\n\n"
                    "Not enough closed trades yet to show community stats.\n"
                    "Close your first trade via /sold to see results here."
                )
            alpha_str = ""
            if stats.get("alpha") is not None:
                sign = "+" if stats["alpha"] >= 0 else ""
                alpha_str = f"\n<b>Alpha vs S&P:</b>  <b>{sign}{stats['alpha']}%</b>"
            spy_str = ""
            if stats.get("spy_return_30d") is not None:
                s = stats["spy_return_30d"]
                spy_str = f"\n<b>S&P 500 (30d):</b>  {'+' if s >= 0 else ''}{s}%"
            best_str = worst_str = ""
            if stats.get("best_pick"):
                b, br = stats["best_pick"]
                best_str = f"\n🏆 Best pick:   <b>{b}</b> {'+' if br >= 0 else ''}{br}%"
            if stats.get("worst_pick"):
                w, wr = stats["worst_pick"]
                worst_str = f"\n💔 Worst pick:  <b>{w}</b> {'+' if wr >= 0 else ''}{wr}%"
            streak_str = ""
            if stats.get("hot_streak_users", 0) > 0:
                streak_str = f"\n🔥 {stats['hot_streak_users']} user(s) on a 3+ win streak!"
            return (
                f"🌍 <b>StockPulz Community</b>\n\n"
                f"<b>Users tracked:</b>  {stats['total_users']}\n"
                f"<b>Closed trades:</b>  {stats['total_trades']}\n"
                f"<b>Win rate:</b>  {stats['win_rate']}%  "
                f"({stats['total_wins']}W / {stats['total_losses']}L)\n"
                f"<b>Avg return/trade:</b>  {'+' if stats['avg_return'] >= 0 else ''}{stats['avg_return']}%"
                f"{spy_str}{alpha_str}"
                f"{best_str}{worst_str}{streak_str}\n\n"
                f"<i>Based on actual closed trades by StockPulz users.</i>"
            )
        except Exception as exc:
            return f"⚠️ Could not load community stats: {exc}"

    # ── Market regime ─────────────────────────────────────────────────────────
    if text == "REGIME":
        try:
            from market_regime import get_market_regime, regime_emoji
            r = get_market_regime()
            emoji = regime_emoji(r["regime"])
            return (
                f"{emoji} <b>MARKET REGIME: {r['regime'].upper()}</b>\n\n"
                f"<b>VIX:</b> {r['vix'] or 'N/A'}\n"
                f"<b>SPY vs 50-day MA:</b> {'Above ✅' if r['spy_above_50ma'] else 'Below ⚠️' if r['spy_above_50ma'] is not None else 'N/A'}\n"
                f"<b>SPY vs 200-day MA:</b> {'Above ✅' if r['spy_above_200ma'] else 'Below 🔴' if r['spy_above_200ma'] is not None else 'N/A'}\n\n"
                f"<i>{r['note']}</i>"
            )
        except Exception as exc:
            return f"⚠️ Could not fetch market regime: {exc}"

    # ── Price alerts ──────────────────────────────────────────────────────────
    if text == "ALERTS":
        from price_alert_manager import list_alerts
        return list_alerts(chat_id)

    if text == "ALERT":
        _prompt_for_param("alert", chat_id)
        return ""

    if text.startswith("ALERT "):
        from price_alert_manager import add_alert
        raw   = text[6:].strip()
        parts = raw.split()
        # Try strict parse first: ALERT NVDA 1000  |  ALERT NVDA ABOVE/BELOW 1000
        ticker, price_str, direction = None, None, "auto"
        try:
            if len(parts) >= 3 and parts[1].upper() in ("ABOVE", "BELOW"):
                ticker, direction, price_str = parts[0].upper(), parts[1].lower(), parts[2]
            elif len(parts) == 2 and _is_number(parts[1]):
                ticker, price_str = parts[0].upper(), parts[1]
            else:
                raise ValueError("needs NL parse")
            return add_alert(chat_id, ticker, float(price_str.replace(",", "")), direction)
        except (ValueError, IndexError):
            # Fall back to Haiku NL parse
            parsed    = _nl_parse_trade("alert", raw)
            ticker    = parsed.get("ticker")
            price_val = parsed.get("price")
            direction = parsed.get("direction") or "auto"
            if not ticker:
                return "🤔 I couldn't identify the stock. Try: /alert NVDA below 800 or /alert Apple when it hits $200"
            if price_val is None:
                return f"🤔 Got <b>{ticker}</b> but what price should I alert you at?"
            return add_alert(chat_id, ticker, float(price_val), direction)

    if text == "UNALERT":
        _prompt_for_param("unalert", chat_id)
        return ""

    if text.startswith("UNALERT "):
        from price_alert_manager import remove_alert
        raw   = text[8:].strip()
        parts = raw.split()
        # Strict: UNALERT NVDA  |  UNALERT NVDA 1000
        if parts and len(parts[0]) <= 5 and parts[0].isalpha():
            ticker = parts[0].upper()
            price  = float(parts[1]) if len(parts) >= 2 and _is_number(parts[1]) else None
        else:
            # NL parse: "remove nvidia alert" / "stop alerting me about apple"
            parsed = _nl_parse_trade("unalert", raw)
            ticker = parsed.get("ticker")
            price  = parsed.get("price")
        if not ticker:
            return "🤔 Which stock's alert should I remove? Try: /unalert NVDA or /unalert Apple"
        return remove_alert(chat_id, ticker, price)

    # ── Paper trading ─────────────────────────────────────────────────────────
    if text in ("PAPER BUY",):
        _prompt_for_param("paper_buy", chat_id)
        return ""

    if text.startswith("PAPER BUY "):
        from paper_trader import paper_buy
        raw   = text[10:].strip()
        parts = raw.split()
        ticker, shares, price = None, None, None
        # Strict parse: AAPL 10  |  AAPL 182.50 10
        try:
            if len(parts) == 2 and _is_number(parts[1]):
                ticker, shares = parts[0].upper(), float(parts[1])
            elif len(parts) == 3 and _is_number(parts[1]) and _is_number(parts[2]):
                ticker, price, shares = parts[0].upper(), float(parts[1]), float(parts[2])
            else:
                raise ValueError("needs NL parse")
        except (ValueError, IndexError):
            parsed = _nl_parse_trade("paper_buy", raw)
            ticker = parsed.get("ticker")
            shares = parsed.get("shares")
            price  = parsed.get("price")
        if not ticker:
            return "🤔 Which stock? Try: /paper_buy Apple 10 or /paper_buy 5 shares of Tesla"
        if not shares:
            return f"🤔 How many shares of <b>{ticker}</b> to simulate buying?"
        return paper_buy(ticker, shares, chat_id, price)

    if text in ("PAPER SELL",):
        _prompt_for_param("paper_sell", chat_id)
        return ""

    if text.startswith("PAPER SELL "):
        from paper_trader import paper_sell
        raw   = text[11:].strip()
        parts = raw.split()
        ticker, shares, price = None, None, None
        # Strict parse: AAPL  |  AAPL 5  |  AAPL 197.10 5
        try:
            if parts and len(parts[0]) <= 5 and parts[0].isalpha():
                ticker = parts[0].upper()
                shares = float(parts[1]) if len(parts) >= 2 and _is_number(parts[1]) else None
                price  = float(parts[2]) if len(parts) >= 3 and _is_number(parts[2]) else None
            else:
                raise ValueError("needs NL parse")
        except (ValueError, IndexError):
            parsed = _nl_parse_trade("paper_sell", raw)
            ticker = parsed.get("ticker")
            shares = parsed.get("shares")
            price  = parsed.get("price")
        if not ticker:
            return "🤔 Which stock to simulate selling? Try: /paper_sell Apple or /paper_sell AAPL 5 shares"
        return paper_sell(ticker, chat_id, shares, price)

    if text == "PAPER PORTFOLIO":
        from paper_trader import paper_portfolio
        return paper_portfolio(chat_id)

    if text == "PAPER PERF":
        from paper_trader import paper_performance
        return paper_performance(chat_id)

    if text == "PAPER ADD CASH" or text.startswith("PAPER ADD CASH "):
        from paper_trader import paper_add_cash
        parts = text.split()
        amount = None
        if len(parts) >= 4 and _is_number(parts[3]):
            amount = float(parts[3].replace(",", ""))
        elif len(parts) >= 4:
            raw    = text[len("PAPER ADD CASH "):].strip()
            parsed = _nl_parse_trade("paper_reset", raw)   # reuse reset schema (price = amount)
            amount = parsed.get("price")
        if not amount:
            return "🤔 How much to add? Try: /paper_add_cash 5000 or /paper_add_cash 10k"
        return paper_add_cash(amount, chat_id)

    if text == "PAPER RESET" or text.startswith("PAPER RESET "):
        from paper_trader import paper_reset
        amount = None
        parts  = text.split()
        if len(parts) == 3 and _is_number(parts[2]):
            amount = float(parts[2].replace(",", ""))
        elif len(parts) >= 3:
            # NL: "paper reset 50k" / "paper reset 100000 dollars"
            raw    = text[len("PAPER RESET "):].strip()
            parsed = _nl_parse_trade("paper_reset", raw)
            amount = parsed.get("price")   # reuse price field for the cash amount
        return paper_reset(chat_id, amount)

    # ── Backtest ──────────────────────────────────────────────────────────────
    if text == "BACKTEST":
        send_message(
            "⏳ <b>Backtest running…</b>\n\n"
            "Scoring ~600 tickers across 26 weekly intervals (1-year history).\n"
            "Results will arrive in this chat in <b>1–3 minutes</b> — no need to wait here.",
            chat_id=chat_id,
        )

        def _run_and_send():
            try:
                from backtester import run_backtest, format_backtest_message
                result = run_backtest()
                send_message(format_backtest_message(result), chat_id=chat_id)
            except Exception as exc:
                send_message(f"⚠️ Backtest failed: {exc}", chat_id=chat_id)

        threading.Thread(target=_run_and_send, daemon=True).start()
        return None  # webhook already got its "200 OK" — no second message from here

    if text in ("HELP", "START"):
        return (
            "📋 <b>Commands</b>\n"
            "\n<b>Daily</b>"
            "\n/today  ·  /prices  ·  /perf"
            "\n/explain &lt;question&gt;\n"
            "\n<b>My Trades</b>"
            "\n/bought  ·  /sold  ·  /cancel"
            "\n/positions  ·  /history\n"
            "\n<b>Intelligence</b>"
            "\n/set_budget  ·  /set_risk  ·  /mode"
            "\n/set_picks  ·  /set_thresholds"
            "\n/watch  ·  /exclude"
            "\n/crypto  <i>(on/off — hide crypto from your picks)</i>\n"
            "\n<b>Market</b>"
            "\n/regime  ·  /backtest"
            "\n/community  <i>(StockPulz users vs S&P 500)</i>\n"
            "\n<b>Price Alerts</b>"
            "\n/alert  ·  /alerts  ·  /unalert\n"
            "\n<b>Paper Trading</b>"
            "\n/paper_buy  ·  /paper_sell"
            "\n/paper_portfolio  ·  /paper_perf"
            "\n/paper_reset  ·  /paper_add_cash\n"
            "\n<b>Control</b>"
            "\n/pause  ·  /resume  ·  /status  ·  /next"
            "\n/settings  ·  /reset"
            "\n/help  ·  /share  <i>(invite link)</i>\n"
            "\n<b>Admin</b>"
            "\n/users  ·  /adduser  ·  /removeuser"
            "\n/broadcast  ·  /release  <i>(send updates to all users)</i>"
            "\n/admin_perf  <i>(all-user performance)</i>"
            "\n/bot_pause  ·  /bot_resume  <i>(global kill switch)</i>"
            "\n/bot_crypto_on  ·  /bot_crypto_off  <i>(global crypto toggle)</i>"
        )

    # /set_risk conservative | moderate | aggressive  (or natural language)
    if text == "SET RISK":
        _prompt_for_param("set_risk", chat_id)
        return ""

    if text.startswith("SET RISK "):
        raw     = text[len("SET RISK "):].strip().lower()
        profile = raw if raw in ("conservative", "moderate", "aggressive") else _nl_param("risk", raw).lower()
        if profile not in ("conservative", "moderate", "aggressive"):
            profile = "moderate"
        update_user_config(chat_id, "risk_profile", profile)
        descriptions = {
            "conservative": "Fewer picks, tighter stops, low-volatility sectors, reduced crypto.",
            "moderate":     "Balanced approach — default settings.",
            "aggressive":   "More picks, wider stops, all sectors, full crypto allocation.",
        }
        return f"✅ Risk profile → <b>{profile}</b>\n<i>{descriptions[profile]}</i>\nTakes effect tomorrow."

    # /mode st | /mode lt | /mode both — choose which sections appear in daily picks
    if text == "MODE":
        config = get_user_config(chat_id)
        current = config.get("pick_mode", "both")
        mode_desc = {
            "st":   "Short Term only (stocks + crypto, fast trades)",
            "lt":   "Long Term only (stocks + crypto, DCA positions)",
            "both": "Both short-term and long-term sections",
        }
        return (
            f"📊 <b>Pick Mode</b>\n"
            f"Current: <b>{current}</b> — {mode_desc.get(current, current)}\n\n"
            f"To change:\n"
            f"  /mode st   — short term only\n"
            f"  /mode lt   — long term only\n"
            f"  /mode both — show all sections (default)"
        )

    if text.startswith("MODE "):
        raw = text[len("MODE "):].strip().lower()
        if raw not in ("st", "lt", "both"):
            return "❌ Invalid mode. Use: /mode st, /mode lt, or /mode both"
        update_user_config(chat_id, "pick_mode", raw)
        labels = {
            "st":   "📈 Short Term only — fast trades (stock + crypto ST sections)",
            "lt":   "🏦 Long Term only — DCA positions (stock + crypto LT sections)",
            "both": "📊 Both — all sections shown (default)",
        }
        return f"✅ Pick mode → <b>{raw}</b>\n{labels[raw]}\nTakes effect tomorrow."

    # /exclude energy stocks  |  /exclude oil companies  |  /exclude none
    if text == "EXCLUDE":
        _prompt_for_param("exclude", chat_id)
        return ""

    if text.startswith("EXCLUDE "):
        raw_query     = original.lstrip("/")
        sectors_input = raw_query.split(" ", 1)[1].strip() if " " in raw_query else ""
        if sectors_input.lower() in ("none", "clear", "reset", ""):
            update_user_config(chat_id, "excluded_sectors", [])
            return "✅ Sector exclusions cleared — all sectors eligible again."
        # Use Haiku to map natural language → proper sector names
        import json as _json
        try:
            excluded = _json.loads(_nl_param("exclude", sectors_input))
        except Exception:
            excluded = [sectors_input.title()]
        update_user_config(chat_id, "excluded_sectors", excluded)
        return (f"✅ Excluding sectors: <b>{', '.join(excluded)}</b>\n"
                f"These sectors will be skipped in tomorrow's picks.\n"
                f"<i>To clear: /exclude none</i>")

    # /watch tesla/microsoft  |  /watch NVDA TSLA  |  /watch none
    if text == "WATCH":
        _prompt_for_param("watch", chat_id)
        return ""

    if text.startswith("WATCH "):
        raw_query     = original.lstrip("/")
        tickers_input = raw_query.split(" ", 1)[1].strip() if " " in raw_query else ""
        if tickers_input.upper() in ("NONE", "CLEAR", "RESET", ""):
            update_user_config(chat_id, "watchlist", [])
            return "✅ Watchlist cleared."
        # Split on spaces, commas, or slashes — support "tesla/microsoft", "NVDA, TSLA"
        import re as _re, json as _json
        parts = [p.strip() for p in _re.split(r"[,/\s]+", tickers_input) if p.strip()]
        # If all parts look like tickers (short, letters/hyphens only), use directly
        looks_like_tickers = all(len(p) <= 5 and _re.match(r"^[A-Za-z.\-]+$", p) for p in parts)
        if looks_like_tickers:
            tickers = [p.upper() for p in parts]
        else:
            # Natural language — resolve via Haiku
            try:
                tickers = _json.loads(_nl_param("watch", tickers_input))
            except Exception:
                tickers = [p.upper() for p in parts]
        update_user_config(chat_id, "watchlist", tickers)
        return (f"✅ Watchlist set: <b>{', '.join(tickers)}</b>\n"
                f"These tickers will always be evaluated in tomorrow's screener.\n"
                f"<i>To clear: /watch none</i>")

    # ── /share ───────────────────────────────────────────────────────────────
    if text == "SHARE":
        try:
            resp = requests.get(
                TELEGRAM_API.format(token=_bot_token(), method="getMe"),
                timeout=5,
            ).json()
            username = resp.get("result", {}).get("username", "")
            bot_link = f"https://t.me/{username}?start=ref" if username else "https://t.me/SanilStockBot?start=ref"
        except Exception:
            bot_link = "https://t.me/SanilStockBot?start=ref"

        return (
            f"📲 <b>Share StockPulz with friends:</b>\n\n"
            f"Hey! I'm using StockPulz — a personal AI stock advisor that sends daily stock &amp; crypto picks, "
            f"price alerts, and weekly performance recaps.\n\n"
            f"🌐 Learn more: <a href=\"https://stockpulz.com\">stockpulz.com</a>\n\n"
            f"📱 Join on Telegram 👇\n"
            f"{bot_link}\n\n"
            f"<i>(Tap the Telegram link to request access)</i>"
        )

    # ── /start (also handles deep link: /start ref) ───────────────────────────
    if text == "START" or text.startswith("START "):
        # Known user — show welcome back
        if _is_admin(chat_id) or chat_id in get_allowed_users():
            return (
                "👋 <b>Welcome back to StockPulz!</b>\n\n"
                "/today — today's picks\n"
                "/positions — your open trades\n"
                "/help — all commands\n\n"
                "<i>Questions? Just type naturally.</i>"
            )
        # Unknown user — add to pending and notify admin
        pending = get_pending_users()
        if chat_id in pending:
            return (
                "⏳ <b>Your request is already pending.</b>\n"
                "You'll be notified as soon as the admin approves your access."
            )
        # Fetch their Telegram profile for the admin notification
        first_name, username = "", ""
        try:
            resp = requests.get(
                TELEGRAM_API.format(token=_bot_token(), method="getChat"),
                params={"chat_id": chat_id}, timeout=5,
            ).json().get("result", {})
            first_name = resp.get("first_name", "")
            username   = resp.get("username", "")
        except Exception:
            pass
        add_pending_user(chat_id, first_name=first_name, username=username)
        # Notify admin with a one-tap approve button
        owner = os.environ.get("TELEGRAM_CHAT_ID", "")
        display = f"@{username}" if username else first_name or chat_id
        admin_msg = (
            f"🔔 <b>New access request</b>\n\n"
            f"Name: <b>{_esc(first_name)}</b>"
            + (f"  (@{_esc(username)})" if username else "") +
            f"\nChat ID: <code>{chat_id}</code>\n\n"
            f"Tap to approve 👇"
        )
        if owner:
            send_inline_keyboard(
                admin_msg,
                [[{"text": f"✅ Approve {_esc(first_name or chat_id)}",
                   "callback_data": f"approve_user|{chat_id}"}]],
                chat_id=owner,
            )
        return (
            "👋 <b>Welcome to StockPulz!</b>\n\n"
            "Your access request has been sent to the admin.\n"
            "You'll receive a notification as soon as you're approved — usually within a few hours.\n\n"
            "<i>StockPulz sends daily AI-curated stock &amp; crypto picks, price alerts, and weekly performance recaps.</i>"
        )

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
        # Build welcome message for the new user
        picks     = load_picks()
        user_cfg  = {**get_config(), **get_user_config(new_id)}
        picks_msg = ""
        if picks:
            try:
                picks_msg = "\n\n" + format_daily_message(picks, user_cfg)
            except Exception:
                pass
        send_message(
            "✅ <b>You're in! Welcome to StockPulz.</b>\n\n"
            "Here's what happens from here:\n\n"
            "📬 <b>8:30 AM ET</b> — morning picks land in this chat, before the market opens. "
            "Each pick includes an entry price, profit target, and stop-loss.\n\n"
            "🕙 <b>10:30 AM ET</b> — a live check compares current prices to your entries — hold, watch, or exit.\n\n"
            "📅 <b>Weekends</b> — crypto picks + a weekly performance recap.\n\n"
            "<b>Commands you'll use most:</b>\n"
            "/today — today's picks (if market is open)\n"
            "/bought AAPL — log a trade &amp; track it\n"
            "/positions — open trades &amp; P&amp;L\n"
            "/alert NVDA above 1000 — price alert\n"
            "/settings — your preferences\n"
            "/help — full command list\n\n"
            "<b>Customise your picks:</b>\n"
            "/crypto off — hide crypto if you only want stocks\n"
            "/set_risk aggressive — adjust risk appetite\n"
            "/set_budget stocks 200 — set per-trade budget\n\n"
            "<i>You can also just type naturally — e.g. \"why was NVDA picked?\" or \"add Tesla to my watchlist\".</i>"
            + picks_msg,
            chat_id=new_id,
        )
        return f"✅ <code>{new_id}</code> approved and welcomed. Today's picks sent."

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

    if text == "USERS":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        from config_manager import get_allowed_users
        users = get_allowed_users()
        owner = str(os.environ.get("TELEGRAM_CHAT_ID", ""))
        lines = ["<b>👥 Allowed Users</b>\n"]
        for u in users:
            tag = "  <i>(you)</i>" if u == owner else ""
            lines.append(f"• <code>{u}</code>{tag}")
        lines.append(f"\n<i>{len(users)} user(s) total</i>")
        return "\n".join(lines)

    # ── /admin_perf — aggregate performance across all users (admin-only) ─────
    if text == "ADMIN PERF":
        if not _is_admin(chat_id):
            return "🔒 Admin only."
        from trade_logger import get_performance_stats
        from config_manager import get_allowed_users
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

    # ── /pause /resume (per-user) ─────────────────────────────────────────────
    if text == "PAUSE":
        update_user_config(chat_id, "paused", True)
        return "⏸ <b>Your picks paused.</b> You won't receive daily briefings until you send /resume.\n<i>Other users are unaffected.</i>"

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
        from config_manager import get_allowed_users
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
        if text == "RELEASE":
            _prompt_for_param("release", chat_id)
            return ""
        notes = text[len("RELEASE "):].strip()
        if not notes:
            return "Usage: /release What's new in this update"
        from datetime import date as _date
        today = _date.today().strftime("%b %d, %Y")
        msg = (
            f"🚀 <b>StockPulz — What's New</b>  <i>({today})</i>\n\n"
            f"{_esc(notes)}\n\n"
            f"<i>Questions? Just ask the bot.</i>"
        )
        from config_manager import get_allowed_users
        sent = 0
        for uid in get_allowed_users():
            if send_message(msg, chat_id=uid):
                sent += 1
        return f"✅ Release note sent to {sent} user(s)."

    # ── /set_thresholds (per-user stop loss & target gain) ────────────────────
    if text == "SET THRESHOLDS":
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
        import re
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
                import anthropic, json as _json
                _client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                _msg    = _client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=80,
                    system=(
                        'Parse threshold values. Return JSON only.\n'
                        '{"stop_loss_pct": <number or null>, "target_gain_pct": <number or null>}\n'
                        'Examples: "7% stop 12% target" → {"stop_loss_pct": 7, "target_gain_pct": 12}\n'
                        '"tighten stop to 4" → {"stop_loss_pct": 4, "target_gain_pct": null}'
                    ),
                    messages=[{"role": "user", "content": raw}],
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
            lines.append("\n<i>Weekend: crypto picks arrive Saturday ~8 AM ET.</i>")

        return "\n".join(lines)

    if text == "SETTINGS":
        global_cfg = get_config()
        user_cfg   = get_user_config(chat_id)
        wl  = user_cfg.get("watchlist", [])
        ex  = user_cfg.get("excluded_sectors", [])
        sl_pct = user_cfg.get("stop_loss_pct")   or global_cfg.get("stop_loss_pct",   7)
        tg_pct = user_cfg.get("target_gain_pct") or global_cfg.get("target_gain_pct", 15)
        sl_src = "" if user_cfg.get("stop_loss_pct")   else " ·default"
        tg_src = "" if user_cfg.get("target_gain_pct") else " ·default"

        # Risk emoji
        risk = user_cfg.get("risk_profile", "moderate")
        risk_emoji = {"conservative": "🛡", "moderate": "⚖️", "aggressive": "🔥"}.get(risk, "⚖️")

        # Pick mode
        mode = user_cfg.get("pick_mode", "both")
        mode_label = {"st": "Short term only", "lt": "Long term only", "both": "Both"}.get(mode, mode)

        # Status
        paused      = user_cfg.get("paused", False)
        show_crypto = user_cfg.get("show_crypto", True)

        # Budget
        sb = user_cfg.get("stock_budget")
        cb = user_cfg.get("crypto_budget")
        sb_str = f"${sb}/trade" if sb else "not set"
        cb_str = f"${cb}/trade" if cb else "not set"

        # Picks
        ms = user_cfg.get("max_stock_picks")
        mc = user_cfg.get("max_crypto_picks")

        return (
            f"<b>⚙️ Your Settings</b>\n\n"
            f"{'⏸' if paused else '✅'} <b>Picks:</b> {'paused — /resume to restart' if paused else 'active'}\n"
            f"{'⏸' if not show_crypto else '✅'} <b>Crypto:</b> {'hidden — /crypto on' if not show_crypto else 'shown  ·  /crypto off to hide'}\n\n"
            f"{risk_emoji} <b>Risk:</b> {risk}  ·  /set_risk\n"
            f"📊 <b>Mode:</b> {mode_label}  ·  /mode\n\n"
            f"💰 <b>Stock budget:</b> {sb_str}  ·  /set_budget\n"
            f"₿ <b>Crypto budget:</b> {cb_str}\n"
            f"📈 <b>Stock picks:</b> {ms if ms else 'all'}  ·  /set_picks\n"
            f"🪙 <b>Crypto picks:</b> {mc if mc else 'all'}\n\n"
            f"🛑 <b>Stop loss:</b> {sl_pct}%{sl_src}  ·  /set_thresholds\n"
            f"🎯 <b>Target gain:</b> {tg_pct}%{tg_src}\n\n"
            f"👀 <b>Watchlist:</b> {', '.join(wl) if wl else 'none'}  ·  /watch\n"
            f"🚫 <b>Excluded sectors:</b> {', '.join(ex) if ex else 'none'}  ·  /exclude\n\n"
            f"<i>To reset everything: /reset</i>"
        )

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
        raw = text[len("SET BUDGET "):].strip().lower()

        # "off" or "0" → clear both
        if raw in ("off", "0", "none", "clear"):
            update_user_config_multi(chat_id, {"stock_budget": None, "crypto_budget": None})
            return "✅ Budgets cleared — picks will show no allocation amounts."

        # Parse "stocks <n> crypto <n>" in any order, or just one bucket
        import re
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
                # If no bucket specified, ask which
                return (
                    "🤔 Which bucket?\n"
                    f"/set_budget stocks {int(amount)}\n"
                    f"/set_budget crypto {int(amount)}\n"
                    f"/set_budget stocks {int(amount)} crypto {int(amount)}"
                )
            return (
                "🤔 I didn't catch that. Try:\n"
                "/set_budget stocks 200 crypto 50\n"
                "/set_budget stocks 150\n"
                "/set_budget off"
            )

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
        import re
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
                import anthropic as _ant, json as _j
                prompt = (
                    f'Parse "{raw}" into pick limits for a stock bot. '
                    'Return ONLY JSON with optional keys "max_stock_picks" and "max_crypto_picks" as integers. '
                    'Examples: "3 stocks 2 crypto"→{"max_stock_picks":3,"max_crypto_picks":2}, '
                    '"show me 4 stocks"→{"max_stock_picks":4}, "just 1 crypto"→{"max_crypto_picks":1}. '
                    'If unclear return {}.'
                )
                client  = _ant.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                msg     = client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=60,
                    messages=[{"role": "user", "content": prompt}],
                )
                updates = _j.loads(msg.content[0].text.strip())
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

    # ── /bought [TICKER|name [price] [shares]] ───────────────────────────────
    if text == "BOUGHT":
        _prompt_for_param("bought", chat_id)
        return ""

    if text.startswith("BOUGHT "):
        raw    = text[len("BOUGHT "):].strip()
        parts  = raw.split()

        # Detect natural-language params: more than 3 tokens, or non-numeric second token
        is_nl = len(parts) > 3 or (len(parts) >= 2 and not _is_number(parts[1]))
        if is_nl:
            parsed = _nl_parse_trade("bought", raw)
            name_raw   = parsed.get("ticker") or (parts[0] if parts else None)
            price_raw  = str(parsed["price"])  if parsed.get("price")  is not None else None
            shares_raw = str(parsed["shares"]) if parsed.get("shares") is not None else None
        else:
            name_raw   = parts[0] if parts else None
            price_raw  = parts[1] if len(parts) >= 2 else None
            shares_raw = parts[2] if len(parts) >= 3 else None

        if not name_raw:
            return "🤔 I couldn't identify a stock. Try: /bought Apple 182.50 or /bought 10 AAPL shares at $182.50"

        candidates = _resolve_ticker_candidates(name_raw)
        if len(candidates) > 1:
            price_enc  = price_raw  or ""
            shares_enc = shares_raw or ""
            buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                         "callback_data": f"buy|{c['ticker']}|{price_enc}|{shares_enc}"}]
                       for c in candidates]
            send_inline_keyboard(f"🔍 Which stock did you mean by <b>{_esc(name_raw)}</b>?",
                                 buttons, chat_id=chat_id)
            return ""

        return _execute_bought(candidates[0]["ticker"], price_raw, shares_raw, chat_id)

    # ── /sold [TICKER|name [price]] ──────────────────────────────────────────
    if text == "SOLD":
        _prompt_for_param("sold", chat_id)
        return ""

    if text.startswith("SOLD "):
        raw   = text[len("SOLD "):].strip()
        parts = raw.split()

        is_nl = len(parts) > 2 or (len(parts) >= 2 and not _is_number(parts[1]))
        if is_nl:
            parsed    = _nl_parse_trade("sold", raw)
            name_raw  = parsed.get("ticker") or (parts[0] if parts else None)
            price_raw = str(parsed["price"]) if parsed.get("price") is not None else None
        else:
            name_raw  = parts[0] if parts else None
            price_raw = parts[1] if len(parts) >= 2 else None

        if not name_raw:
            return "🤔 I couldn't identify a stock. Try: /sold Apple 197.10 or /sold AAPL at $197"

        candidates = _resolve_ticker_candidates(name_raw)
        if len(candidates) > 1:
            price_enc = price_raw or ""
            buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                         "callback_data": f"sell|{c['ticker']}|{price_enc}"}]
                       for c in candidates]
            send_inline_keyboard(f"🔍 Which stock did you mean by <b>{_esc(name_raw)}</b>?",
                                 buttons, chat_id=chat_id)
            return ""

        return _execute_sold(candidates[0]["ticker"], price_raw, chat_id)

    # ── /history — date-wise transaction log ─────────────────────────────────
    if text == "HISTORY":
        log         = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        closed      = log.get("closed", [])

        if not open_trades and not closed:
            return "📭 No trades yet. Use /bought to log a purchase."

        # Build a unified event list: one entry per buy and one per sell
        events = []
        for t in open_trades:
            events.append({
                "date":   t.get("opened_date", ""),
                "ticker": t["ticker"],
                "action": "BUY",
                "price":  t.get("entry_price"),
                "shares": t.get("shares"),
                "status": "OPEN",
                "ret":    None,
            })
        for t in closed:
            # Buy event
            events.append({
                "date":   t.get("opened_date", ""),
                "ticker": t["ticker"],
                "action": "BUY",
                "price":  t.get("entry_price"),
                "shares": t.get("shares"),
                "status": "CLOSED",
                "ret":    None,
            })
            # Sell event
            outcome_icon = {"target": "🎯", "stop": "🛑", "trailing_stop": "🔒",
                            "manual": "✋", "expired": "⏰"}.get(t.get("outcome", ""), "✋")
            ret = t.get("return_pct", 0)
            events.append({
                "date":   t.get("closed_date", ""),
                "ticker": t["ticker"],
                "action": "SELL",
                "price":  t.get("closed_price"),
                "shares": t.get("shares"),
                "status": outcome_icon,
                "ret":    ret,
            })

        # Sort most recent first
        events.sort(key=lambda e: e["date"], reverse=True)

        # Group by date
        from itertools import groupby
        lines = ["📋 <b>Trade History</b>\n"]
        for day, group in groupby(events, key=lambda e: e["date"]):
            try:
                from datetime import date as _date
                label = _date.fromisoformat(day).strftime("%a %b %d, %Y")
            except Exception:
                label = day
            lines.append(f"\n<b>{label}</b>")
            for e in group:
                price_str  = f"${_p(e['price'])}" if e["price"] else "—"
                shares_str = f" × {_p(e['shares'])}" if e.get("shares") else ""
                ret_str    = ""
                if e["ret"] is not None:
                    sign = "+" if e["ret"] >= 0 else ""
                    ret_str = f"  <i>{sign}{e['ret']}%</i>"
                action_icon = "🟢" if e["action"] == "BUY" else "🔴"
                lines.append(
                    f"  {action_icon} <b>{e['ticker']}</b> {e['action']}  "
                    f"<code>{price_str}{shares_str}</code>  "
                    f"{e['status']}{ret_str}"
                )

        return "\n".join(lines)

    # ── /cancel — undo accidental /bought or /sold ───────────────────────────
    if text == "CANCEL":
        # Show recent transactions as tappable buttons instead of prompting for ticker
        log         = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        closed      = log.get("closed", [])

        if not open_trades and not closed:
            return "📭 No trades to cancel."

        # Build button list: all open buys + last 5 closed sells, sorted by date
        buttons = []
        for t in sorted(open_trades, key=lambda x: x.get("opened_date", ""), reverse=True):
            label = (f"🟢 BUY  {t['ticker']}  ${_p(t.get('entry_price'))}  "
                     f"· {t.get('opened_date', '')}")
            buttons.append([{"text": label, "callback_data": f"cancel_auto|{t['ticker']}"}])

        recent_closed = sorted(closed, key=lambda x: x.get("closed_date", ""), reverse=True)[:5]
        for t in recent_closed:
            ret  = t.get("return_pct", 0)
            sign = "+" if ret >= 0 else ""
            label = (f"🔴 SELL  {t['ticker']}  ${_p(t.get('closed_price'))}  "
                     f"{sign}{ret}%  · {t.get('closed_date', '')}")
            buttons.append([{"text": label, "callback_data": f"cancel_auto|{t['ticker']}"}])

        if not buttons:
            return "📭 No trades to cancel."

        send_inline_keyboard(
            "↩️ <b>Which transaction to undo?</b>\n"
            "<i>Tap a buy to remove it, or a sell to reopen the position.</i>",
            buttons,
            chat_id=chat_id,
        )
        return ""

    if text.startswith("CANCEL "):
        from trade_logger import cancel_trade, reopen_trade

        name_raw   = text.split(None, 1)[1].strip()
        candidates = _resolve_ticker_candidates(name_raw)

        # Show ticker picker if ambiguous
        if len(candidates) > 1:
            buttons = [[{
                "text": f"{c['ticker']} — {c['name']}",
                "callback_data": f"cancel_auto|{c['ticker']}",
            }] for c in candidates]
            send_inline_keyboard(
                f"🔍 Which stock did you mean?", buttons, chat_id=chat_id,
            )
            return ""

        ticker = candidates[0]["ticker"]
        log    = load_user_trade_log(chat_id)
        has_open   = any(t["ticker"] == ticker for t in log.get("open", []))
        has_closed = any(t["ticker"] == ticker for t in log.get("closed", []))

        if not has_open and not has_closed:
            return f"⚠️ No trade found for <b>{ticker}</b>. Use /positions to see open trades."

        # Build a descriptive confirmation message
        if has_open and has_closed:
            # Show what-to-undo choice first, confirmation happens after
            open_trade   = next(t for t in log["open"] if t["ticker"] == ticker)
            closed_trade = next(t for t in reversed(log["closed"]) if t["ticker"] == ticker)
            send_inline_keyboard(
                f"↩️ What do you want to undo for <b>{ticker}</b>?\n\n"
                f"Open:   entry <code>${open_trade.get('entry_price')}</code>\n"
                f"Closed: sold <code>${closed_trade.get('closed_price')}</code>  "
                f"({'+' if closed_trade.get('return_pct',0)>=0 else ''}{closed_trade.get('return_pct',0)}%)",
                [[
                    {"text": "❌ Undo buy",  "callback_data": f"confirm_cancel|{ticker}"},
                    {"text": "↩️ Undo sell", "callback_data": f"confirm_reopen|{ticker}"},
                ]],
                chat_id=chat_id,
            )
            return ""

        if has_open:
            open_trade = next(t for t in log["open"] if t["ticker"] == ticker)
            send_inline_keyboard(
                f"⚠️ Confirm: remove open position for <b>{ticker}</b>?\n"
                f"Entry <code>${open_trade.get('entry_price')}</code> · "
                f"opened {open_trade.get('opened_date')}",
                [[
                    {"text": "✅ Yes, undo buy",  "callback_data": f"confirm_cancel|{ticker}"},
                    {"text": "❌ No, keep it",    "callback_data": "cancel_abort"},
                ]],
                chat_id=chat_id,
            )
            return ""

        if has_closed:
            closed_trade = next(t for t in reversed(log["closed"]) if t["ticker"] == ticker)
            ret  = closed_trade.get('return_pct', 0)
            sign = "+" if ret >= 0 else ""
            send_inline_keyboard(
                f"⚠️ Confirm: reopen <b>{ticker}</b> as if the sale never happened?\n"
                f"Was sold at <code>${closed_trade.get('closed_price')}</code>  ({sign}{ret}%)",
                [[
                    {"text": "✅ Yes, undo sell", "callback_data": f"confirm_reopen|{ticker}"},
                    {"text": "❌ No, keep it",    "callback_data": "cancel_abort"},
                ]],
                chat_id=chat_id,
            )
            return ""

    # ── /positions / /portfolio ───────────────────────────────────────────────
    if text in ("POSITIONS", "PORTFOLIO"):
        import yfinance as yf

        log  = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        if not open_trades:
            return "📭 No open positions. Use <code>/bought AAPL 182.50</code> to log a trade."

        # Fetch current prices for all tickers
        tickers_list = [t["ticker"] for t in open_trades]
        try:
            raw = yf.download(
                " ".join(tickers_list), period="1d", interval="1m",
                progress=False, auto_adjust=True,
            )
            if hasattr(raw["Close"], "iloc"):
                prices = {t: float(raw["Close"][t].dropna().iloc[-1])
                          for t in tickers_list if t in raw["Close"].columns}
            else:
                prices = {tickers_list[0]: float(raw["Close"].dropna().iloc[-1])}
        except Exception:
            prices = {}

        # Build position summaries + get AI guidance in one Haiku call
        position_data = []
        for t in open_trades:
            ticker  = t["ticker"]
            entry   = float(t.get("entry_price") or 0)
            target  = float(t.get("target_price") or 0)
            stop    = float(t.get("stop_loss") or 0)
            current = prices.get(ticker)
            if current and entry:
                ret_pct    = (current - entry) / entry * 100
                to_target  = (target / current - 1) * 100 if target else None
                to_stop    = (stop   / current - 1) * 100 if stop   else None
                position_data.append({
                    "ticker":     ticker,
                    "entry":      entry,
                    "current":    round(current, 2),
                    "target":     target or None,
                    "stop":       stop or None,
                    "return_pct": round(ret_pct, 2),
                    "to_target":  round(to_target, 2) if to_target is not None else None,
                    "to_stop":    round(to_stop,  2) if to_stop  is not None else None,
                })

        # Ask Haiku for one-line guidance per position
        guidance: dict[str, str] = {}
        if position_data:
            try:
                import anthropic as _ant, json as _j
                prompt = (
                    "You are a brief trading advisor. For each position below give ONE short action line "
                    "(max 10 words): HOLD / ADD MORE / TAKE PROFIT / TIGHTEN STOP / CONSIDER SELLING / etc. "
                    "Be direct. Consider proximity to target/stop and current return.\n\n"
                    f"Positions: {_j.dumps(position_data)}\n\n"
                    'Return ONLY a JSON object keyed by ticker, e.g. {"AAPL": "Hold — strong, 3.2% from target"}'
                )
                client  = _ant.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                message = client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=200,
                    messages=[{"role": "user", "content": prompt}],
                )
                guidance = _j.loads(message.content[0].text.strip())
            except Exception as exc:
                print(f"[portfolio] Guidance fetch failed (non-critical): {exc}")

        lines = ["<b>📂 Portfolio</b>", ""]
        for t in open_trades:
            ticker  = t["ticker"]
            entry   = float(t.get("entry_price") or 0)
            target  = t.get("target_price")
            stop    = t.get("stop_loss")
            current = prices.get(ticker)
            manual  = "  <i>(manual)</i>" if t.get("manual") else ""

            if current and entry:
                ret_pct   = (current - entry) / entry * 100
                sign      = "+" if ret_pct >= 0 else ""
                pnl_emoji = "🟢" if ret_pct >= 0 else "🔴"
                stop_warn = "  ⚠️ <b>NEAR STOP</b>" if stop and current <= float(stop) * 1.01 else ""
                hit_target = "  🎯 <b>NEAR TARGET</b>" if target and current >= float(target) * 0.99 else ""
                to_target = f"{((float(target)/current-1)*100):+.1f}% to target" if target else ""
                to_stop   = f"{((float(stop)/current-1)*100):+.1f}% to stop"   if stop   else ""
                price_line = " · ".join(filter(None, [to_target, to_stop]))

                lines.append(
                    f"{pnl_emoji} <b>{ticker}</b>  <code>${current:.2f}</code>  "
                    f"<i>({sign}{ret_pct:.1f}%)</i>{stop_warn}{hit_target}{manual}"
                )
                lines.append(f"   entry <code>${entry:.2f}</code>  {price_line}")
                if ticker in guidance:
                    lines.append(f"   💡 <i>{guidance[ticker]}</i>")
            else:
                lines.append(f"⬜ <b>{ticker}</b>  entry <code>${entry:.2f}</code>  "
                             f"<i>(price unavailable)</i>{manual}")
                if target or stop:
                    lines.append(f"   target <code>${target}</code>  stop <code>${stop}</code>")
            lines.append("")

        # ── Portfolio totals footer ───────────────────────────────────────────
        total_invested  = 0.0
        total_current   = 0.0
        winners         = 0
        losers          = 0
        for t in open_trades:
            entry   = float(t.get("entry_price") or 0)
            alloc   = float(t.get("allocation") or 0)
            shares  = float(t.get("shares") or 0)
            current = prices.get(t["ticker"])
            if entry and current:
                # derive cost from allocation (dollars) or shares × entry
                cost_basis = alloc if alloc else (shares * entry if shares else 0)
                curr_val   = (alloc / entry * current) if alloc else (shares * current if shares else 0)
                total_invested += cost_basis
                total_current  += curr_val
                if current >= entry:
                    winners += 1
                else:
                    losers += 1

        if total_invested > 0:
            total_pnl     = total_current - total_invested
            total_pnl_pct = total_pnl / total_invested * 100
            pnl_sign      = "+" if total_pnl >= 0 else ""
            pnl_emoji     = "🟢" if total_pnl >= 0 else "🔴"
            lines.append("─────────────────")
            lines.append(
                f"{pnl_emoji} <b>Total P&L: {pnl_sign}{total_pnl_pct:.1f}%</b>  "
                f"(${total_pnl:+,.2f})"
            )
            lines.append(
                f"   Invested: <b>${total_invested:,.2f}</b>  →  "
                f"Now: <b>${total_current:,.2f}</b>"
            )
            lines.append(f"   {winners}🟢 winning  ·  {losers}🔴 losing  ·  {len(open_trades)} total")
            lines.append("")

        lines.append("<i>Use <code>/sold TICKER price</code> to close a position.</i>")
        return "\n".join(lines)

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
