"""
bot_commands.py — Bot business logic: helpers, callbacks, trade handlers, command router.

Imported by telegram_notifier.py (entry point).
Telegram API calls (send_message, etc.) come from telegram_api.py.
"""

import os
import time
import threading
import hmac
import hashlib

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

def _is_number(s: str) -> bool:
    """Return True if s looks like a numeric value (int or float, optional commas)."""
    try:
        float(s.replace(",", ""))
        return True
    except (ValueError, AttributeError):
        return False


# Crypto tickers recognised by the bot (used to set asset_type on manual trades)
_CRYPTO_SYMBOLS = {
    "BTC","ETH","SOL","BNB","XRP","ADA","DOGE","AVAX","DOT","MATIC",
    "LINK","UNI","ATOM","LTC","BCH","ALGO","XLM","VET","ICP","FIL",
}

def _is_admin(chat_id: str | None = None) -> bool:
    """Return True if the given chat_id (or env TELEGRAM_CHAT_ID) is the bot owner."""
    resolved = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    return str(resolved) == str(os.environ.get("TELEGRAM_CHAT_ID", ""))



# ── Admin invite token (HMAC-signed, time-limited) ───────────────────────────

def _make_admin_invite_token() -> str:
    """
    Generate a signed, time-limited admin invite deep-link token.

    Format: adminref_<unix_timestamp>_<hmac_16hex>
    (underscores only — safe for Telegram ?start= param, max 64 chars)

    The HMAC uses TELEGRAM_BOT_TOKEN as the secret, so only the server
    that knows the bot token can produce or verify a valid token.
    Tokens expire after ADMIN_INVITE_TTL_HOURS hours.
    """
    ts     = str(int(time.time()))
    secret = os.environ.get("TELEGRAM_BOT_TOKEN", "fallback-secret").encode()
    sig    = hmac.new(secret, ts.encode(), hashlib.sha256).hexdigest()[:16]
    return f"adminref_{ts}_{sig}"


ADMIN_INVITE_TTL_HOURS = 48   # link expires after 48 hours


def _verify_admin_invite_token(token: str) -> bool:
    """
    Return True if token is a valid, unexpired admin invite token.
    Any tampering (wrong signature or expired timestamp) returns False.
    """
    if not token.startswith("adminref_"):
        return False
    parts = token.split("_")
    # Expected: ["adminref", "<ts>", "<sig>"]
    if len(parts) != 3:
        return False
    _, ts_str, received_sig = parts
    try:
        ts = int(ts_str)
    except ValueError:
        return False
    # Check expiry
    if time.time() - ts > ADMIN_INVITE_TTL_HOURS * 3600:
        print(f"[telegram] Admin invite token expired (age {int(time.time()-ts)}s).")
        return False
    # Verify HMAC — constant-time comparison prevents timing attacks
    secret       = os.environ.get("TELEGRAM_BOT_TOKEN", "fallback-secret").encode()
    expected_sig = hmac.new(secret, ts_str.encode(), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(expected_sig, received_sig):
        print(f"[telegram] Admin invite token HMAC mismatch — possible forgery attempt.")
        return False
    return True


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
        user_msg = f"{context}\n\nUser question: {query}"
    else:
        # Not in today's picks — fetch live info via yfinance and answer generally
        import yfinance as yf
        import re

        # Try to extract a ticker from the query
        ticker_match = re.search(r'\b([A-Z]{1,5})\b', query.upper())
        # Also try common name→ticker mappings
        name_map = {
            "NVIDIA": "NVDA", "NVDIA": "NVDA", "NVIDEA": "NVDA",
            "APPLE": "AAPL", "MICROSOFT": "MSFT", "AMAZON": "AMZN",
            "GOOGLE": "GOOGL", "ALPHABET": "GOOGL", "META": "META",
            "TESLA": "TSLA", "NETFLIX": "NFLX", "BITCOIN": "BTC-USD",
            "ETHEREUM": "ETH-USD", "SOLANA": "SOL-USD",
        }
        guessed_ticker = None
        for name, sym in name_map.items():
            if name in query.upper():
                guessed_ticker = sym
                break
        if not guessed_ticker and ticker_match:
            guessed_ticker = ticker_match.group(1)

        live_context = ""
        if guessed_ticker:
            try:
                t    = yf.Ticker(guessed_ticker)
                info = t.info or {}
                fi   = t.fast_info
                hist = t.history(period="5d")
                price     = getattr(fi, "last_price", None) or info.get("regularMarketPrice")
                prev_close= getattr(fi, "previous_close", None) or info.get("previousClose")
                mkt_cap   = info.get("marketCap")
                pe        = info.get("trailingPE")
                summary   = info.get("longBusinessSummary", "")[:400]
                chg_pct   = ((price - prev_close) / prev_close * 100) if price and prev_close else None
                chg_str   = f"{chg_pct:+.2f}% today" if chg_pct is not None else ""
                cap_str   = f"${mkt_cap/1e9:.0f}B market cap" if mkt_cap else ""
                pe_str    = f"P/E {pe:.1f}" if pe else ""
                live_context = (
                    f"Ticker: {guessed_ticker}\n"
                    f"Price: ${price:.2f} {chg_str}\n"
                    f"{cap_str}  {pe_str}\n"
                    f"About: {summary}\n"
                    f"5-day close prices: {[round(float(x),2) for x in hist['Close'].tail(5).tolist()]}\n"
                )
            except Exception as exc:
                live_context = f"Could not fetch live data for {guessed_ticker}: {exc}\n"

        picks_summary = ", ".join(
            (p.get("ticker") or p.get("symbol", "")) for _, p in all_picks
        )
        context = (
            f"Today's picks: {picks_summary}\n\n"
            f"Live market data for the queried stock:\n{live_context}"
        )
        system  = (
            "You are a friendly financial analyst assistant. Answer the user's question "
            "using the live market data provided. Be concise (3-5 sentences), use plain "
            "English, mention the current price and what's driving the move if relevant. "
            "Do NOT give disclaimers. Do NOT suggest they ask about today's picks."
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

    # Fast path: looks unambiguously like a ticker symbol (1-5 chars, letters/dots/hyphens).
    # ≤5 chars covers AAPL, MSFT, GOOGL, META, ETH, BTC, SOL etc.
    # Anything longer (COSTCO=6, ACCENTURE=9) must go through Haiku for proper resolution.
    if _re.match(r"^[A-Za-z.\-]{1,5}$", raw):
        return [{"ticker": raw.upper(), "name": raw.upper()}]

    # Ask Haiku for up to 4 candidates with full names
    prompt = (
        f'The user typed "{raw}" as a stock or crypto to trade. '
        'Return up to 4 matching US stock/crypto candidates as a JSON array of objects. '
        'Each object must have "ticker" (official symbol, max 5 chars) and "name" (short company name). '
        'Order by most likely match first. '
        'Examples: "apple" → [{"ticker":"AAPL","name":"Apple Inc"}], '
        '"costco" → [{"ticker":"COST","name":"Costco"}], '
        '"accenture" → [{"ticker":"ACN","name":"Accenture"}], '
        '"bank" → [{"ticker":"JPM","name":"JPMorgan"},{"ticker":"BAC","name":"Bank of America"},'
        '{"ticker":"WFC","name":"Wells Fargo"},{"ticker":"C","name":"Citigroup"}]. '
        'Return ONLY the JSON array, no other text.'
    )
    try:
        client  = _ant.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw_json = message.content[0].text.strip()
        if raw_json.startswith("```"):
            raw_json = raw_json.split("```")[1]
            if raw_json.startswith("json"):
                raw_json = raw_json[4:]
        candidates = _j.loads(raw_json)
        if isinstance(candidates, list) and candidates:
            return candidates
    except Exception as exc:
        print(f"[telegram] _resolve_ticker_candidates failed: {exc}")

    # Last-resort fallback — warn that we're not confident about this ticker
    ticker_guess = raw.upper()
    print(f"[bot] Ticker resolution fallback: storing '{ticker_guess}' as-is — may be invalid")
    return [{"ticker": ticker_guess, "name": ticker_guess}]


def _send_release_broadcast(notes: str, admin_chat_id: str) -> str:
    """Broadcast a release note to all users and return a confirmation string."""
    from datetime import date as _date
    today = _date.today().strftime("%b %d, %Y")
    msg = (
        f"🚀 <b>StockPulz — What's New</b>  <i>({today})</i>\n\n"
        f"{_esc(notes)}\n\n"
        f"<i>Questions? Just ask the bot.</i>"
    )
    sent = 0
    for uid in get_allowed_users():
        if send_message(msg, chat_id=uid):
            sent += 1
    return f"✅ Release note sent to {sent} user(s)."


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
    cq_id   = callback_query.get("id", "")
    data    = callback_query.get("data", "")
    chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id", ""))

    parts = data.split("|")
    action = parts[0] if parts else ""

    # Show an instant toast so users know the tap registered (prevents double-tapping)
    _TOASTS = {
        "quickbuy":           "⏳ Logging position…",
        "chart":              "⏳ Generating chart…",
        "confirm_sell":       "⏳ Removing position…",
        "sold_pick":          "⏳ Loading…",
        "sold_review":        "⏳ Loading…",
        "sold_confirm":       "⏳ Closing position…",
        "bought_confirm":     "⏳ Logging position…",
        "updatelevel_pick":   "⏳ Loading…",
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

    if action == "quickbuy":
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
            print(f"[bot] quickbuy failed for {ticker}: {exc}")
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
            result = _execute_sold(ticker, chat_id, price=price_raw or None, shares_sold=shares_raw or None)
            send_message(result, chat_id=chat_id)
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
            "budget_stock":  ("settings_budget_stock",  "💰 <b>Stock budget per trade?</b>\n<i>e.g. <code>200</code> or <code>off</code> to clear</i>"),
            "budget_crypto": ("settings_budget_crypto", "₿ <b>Crypto budget per trade?</b>\n<i>e.g. <code>50</code> or <code>off</code> to clear</i>"),
            "stop":          ("settings_stop",          "🛑 <b>Stop loss %?</b>\n<i>e.g. <code>7</code> for 7%</i>"),
            "target":        ("settings_target",        "🎯 <b>Target gain %?</b>\n<i>e.g. <code>15</code> for 15%</i>"),
            "watchlist":     ("watch",                  "👀 <b>Watchlist tickers?</b>\n<i>e.g. <code>TSLA MSFT NVDA</code>  ·  blank to clear</i>"),
            "exclude":       ("exclude",                "🚫 <b>Sectors to exclude?</b>\n<i>e.g. <code>Energy Utilities</code>  ·  blank to clear</i>"),
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
    "broadcast": ("📢 <b>Type your message to broadcast to all users:</b>\n"
                  "<i>e.g.</i>  <code>Picks will be delayed today — back tomorrow at 8:30 AM ET</code>"),
    "release":   ("🚀 <b>Type your release note to send to all users:</b>\n"
                  "<i>e.g.</i>  <code>New feature: price alerts now support crypto!</code>"),
    "chart":     ("📊 <b>Which ticker?</b>\n"
                  "<i>e.g.</i>  <code>AAPL</code>  ·  <code>NVDA</code>  ·  <code>BTC</code>"),
    "feedback":  ("💬 <b>What's on your mind?</b>\n"
                  "<i>Share any thoughts, suggestions, or issues — your feedback goes straight to the team.</i>"),
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


def _send_settings_panel(chat_id: str) -> None:
    """Send the full interactive /settings panel with buttons for every preference."""
    cfg        = get_user_config(chat_id)
    global_cfg = get_config()

    # Values
    paused      = bool(cfg.get("paused", False))
    show_crypto = bool(cfg.get("show_crypto", True))
    risk        = cfg.get("risk_profile", "moderate")
    mode        = cfg.get("pick_mode", "both")
    sb          = cfg.get("stock_budget")
    cb          = cfg.get("crypto_budget")
    ms          = cfg.get("max_stock_picks")
    mc          = cfg.get("max_crypto_picks")
    sl_pct      = cfg.get("stop_loss_pct")   or global_cfg.get("stop_loss_pct",   7)
    tg_pct      = cfg.get("target_gain_pct") or global_cfg.get("target_gain_pct", 15)
    wl          = cfg.get("watchlist", [])
    ex          = cfg.get("excluded_sectors", [])
    skip_conf   = bool(cfg.get("skip_confirmation",     False))
    skip_eod    = bool(cfg.get("skip_eod",              False))
    skip_wl     = bool(cfg.get("skip_watchlist_alerts", False))
    skip_pre    = bool(cfg.get("skip_premarket",        False))

    # Labels
    risk_emoji  = {"conservative": "🛡", "moderate": "⚖️", "aggressive": "🔥"}.get(risk, "⚖️")
    mode_label  = {"st": "ST only", "lt": "LT only", "both": "Both"}.get(mode, mode)
    sb_label    = f"${int(sb)}" if sb else "not set"
    cb_label    = f"${int(cb)}" if cb else "not set"
    ms_label    = str(ms) if ms else "all"
    mc_label    = str(mc) if mc else "all"
    wl_label    = ", ".join(wl[:3]) + ("…" if len(wl) > 3 else "") if wl else "none"
    ex_label    = ", ".join(ex[:2]) + ("…" if len(ex) > 2 else "") if ex else "none"

    text = (
        "<b>⚙️ Settings</b>  —  tap any button to change\n\n"
        f"{'⏸' if paused else '✅'} Picks {'paused' if paused else 'active'}   "
        f"{'🔕' if not show_crypto else '🔔'} Crypto {'hidden' if not show_crypto else 'shown'}\n"
        f"{risk_emoji} Risk: <b>{risk}</b>   📊 Mode: <b>{mode_label}</b>\n"
        f"💰 Stock budget: <b>{sb_label}</b>   ₿ Crypto: <b>{cb_label}</b>\n"
        f"📈 Stock picks: <b>{ms_label}</b>   🪙 Crypto picks: <b>{mc_label}</b>\n"
        f"🛑 Stop loss: <b>{sl_pct}%</b>   🎯 Target: <b>{tg_pct}%</b>\n"
        f"👀 Watchlist: <b>{wl_label}</b>   🚫 Excluded: <b>{ex_label}</b>\n"
        f"📨 10:30 confirm: <b>{'off' if skip_conf else 'on'}</b>   "
        f"🌅 EOD summary: <b>{'off' if skip_eod else 'on'}</b>   "
        f"👁 WL alerts: <b>{'off' if skip_wl else 'on'}</b>   "
        f"🌅 Pre-market: <b>{'off' if skip_pre else 'on'}</b>"
    )

    buttons = [
        # Row 1: toggles
        [
            {"text": "⏸ Pause" if not paused else "▶️ Resume",
             "callback_data": "settings_toggle|paused"},
            {"text": "🔕 Hide crypto" if show_crypto else "🔔 Show crypto",
             "callback_data": "settings_toggle|show_crypto"},
        ],
        # Row 2: risk + mode (open pickers)
        [
            {"text": f"{risk_emoji} Risk: {risk}",     "callback_data": "settings_open|risk"},
            {"text": f"📊 Mode: {mode_label}",          "callback_data": "settings_open|mode"},
        ],
        # Row 3: budgets (prompt)
        [
            {"text": f"💰 Stock budget: {sb_label}",   "callback_data": "settings_prompt|budget_stock"},
            {"text": f"₿ Crypto budget: {cb_label}",   "callback_data": "settings_prompt|budget_crypto"},
        ],
        # Row 4: pick counts (open pickers)
        [
            {"text": f"📈 Stock picks: {ms_label}",    "callback_data": "settings_open|picks_stock"},
            {"text": f"🪙 Crypto picks: {mc_label}",   "callback_data": "settings_open|picks_crypto"},
        ],
        # Row 5: thresholds (prompt)
        [
            {"text": f"🛑 Stop loss: {sl_pct}%",       "callback_data": "settings_prompt|stop"},
            {"text": f"🎯 Target gain: {tg_pct}%",     "callback_data": "settings_prompt|target"},
        ],
        # Row 6: watchlist + exclude (prompt)
        [
            {"text": f"👀 Watchlist: {wl_label}",      "callback_data": "settings_prompt|watchlist"},
            {"text": f"🚫 Exclude: {ex_label}",        "callback_data": "settings_prompt|exclude"},
        ],
        # Row 7: notification opt-outs (row a)
        [
            {"text": f"📨 10:30 AM {'✅' if not skip_conf else '🔕 off'}",
             "callback_data": "settings_toggle|skip_confirmation"},
            {"text": f"🌅 EOD {'✅' if not skip_eod else '🔕 off'}",
             "callback_data": "settings_toggle|skip_eod"},
        ],
        # Row 7b: more notification opt-outs
        [
            {"text": f"👁 WL alerts {'✅' if not skip_wl else '🔕 off'}",
             "callback_data": "settings_toggle|skip_watchlist_alerts"},
            {"text": f"🌅 Pre-market {'✅' if not skip_pre else '🔕 off'}",
             "callback_data": "settings_toggle|skip_premarket"},
        ],
        # Row 8: reset
        [
            {"text": "🔄 Reset all settings", "callback_data": "settings_reset_ask"},
        ],
    ]

    send_inline_keyboard(text, buttons, chat_id=chat_id)


# ── Extracted buy/sell execution (shared by direct + conversational paths) ────

def _send_chart(ticker: str, asset_type: str, chat_id: str) -> None:
    """
    Generate and send a candlestick chart for a ticker.
    Annotates with entry/target/stop lines if the ticker is in today's picks.
    Runs synchronously — call from a background thread for non-blocking UX.
    """
    from chart_generator import generate_chart
    from telegram_api import send_photo

    ticker = ticker.upper()

    # Look up pick levels from today's picks
    entry = target = stop = None
    try:
        picks = load_picks()
        if picks:
            all_p = (
                picks.get("stocks", {}).get("short_term", []) +
                picks.get("stocks", {}).get("long_term",  []) +
                picks.get("crypto", {}).get("short_term", [])
            )
            for p in all_p:
                sym = (p.get("ticker") or p.get("symbol", "")).upper()
                if sym == ticker:
                    entry  = p.get("entry_price")
                    target = p.get("target_price")
                    stop   = p.get("stop_loss")
                    break
    except Exception as exc:
        print(f"[bot] Chart level lookup failed (non-critical): {exc}")

    img = generate_chart(ticker, entry=entry, target=target, stop=stop, asset_type=asset_type)

    if img is None:
        send_message(f"⚠️ Could not generate chart for <b>{ticker}</b>. Try again shortly.", chat_id=chat_id)
        return

    # Build caption with level legend
    parts = [f"<b>{ticker}</b>  ·  30-day chart"]
    if entry:  parts.append(f"🟢 Entry  <code>${_p(entry)}</code>")
    if target: parts.append(f"🔵 Target <code>${_p(target)}</code>")
    if stop:   parts.append(f"🔴 Stop   <code>${_p(stop)}</code>")
    caption = "\n".join(parts)

    send_photo(img, caption=caption, chat_id=chat_id)


def _execute_bought(ticker: str, chat_id: str,
                    price=None, shares=None) -> str:
    """
    Add ticker to user's portfolio.
    price / shares are optional — used when the user confirms a manual /bought entry.
    Without price, pick levels from today's picks are used as-is.
    """
    from trade_logger import add_holding
    ticker = ticker.upper()

    # Resolve to canonical ticker symbol (e.g. "COSTCO" → "COST") before storing.
    # This prevents duplicates when the user types a company name instead of a symbol.
    try:
        candidates = _resolve_ticker_candidates(ticker)
        if candidates:
            canonical = candidates[0]["ticker"].upper()
            if canonical != ticker:
                print(f"[bot] _execute_bought: resolved {ticker} → {canonical}")
                ticker = canonical
    except Exception:
        pass  # keep original ticker if resolution fails
    picks  = load_picks()
    trade, existed = add_holding(ticker, chat_id, picks=picks)

    if existed:
        return f"📌 <b>{ticker}</b> is already in your portfolio — I'm watching it."

    # Override entry price if user provided one explicitly
    if price is not None:
        try:
            entry_val = float(str(price).replace(",", ""))
            log = load_user_trade_log(chat_id)
            for t in log.get("open", []):
                if t["ticker"] == ticker:
                    t["entry_price"] = entry_val
                    if shares:
                        try:
                            t["shares"] = float(str(shares).replace(",", ""))
                            t["allocation"] = round(entry_val * t["shares"], 2)
                        except Exception:
                            pass
                    break
            save_user_trade_log(chat_id, log)
            trade["entry_price"] = entry_val
        except Exception:
            pass

    entry  = trade.get("entry_price")
    target = trade.get("target_price")
    stop   = trade.get("stop_loss")

    lines = [f"✅ <b>{ticker}</b> added to your portfolio."]
    if entry and target and stop:
        lines.append(
            f"Pick levels — entry <code>${_p(entry)}</code>  "
            f"· target <code>${_p(target)}</code>  "
            f"· stop <code>${_p(stop)}</code>"
        )
        lines.append("<i>I'll alert you if the price hits the target or stop.</i>")
    elif entry:
        lines.append(f"Entry logged at <code>${_p(entry)}</code>.")
    else:
        lines.append("<i>Not in today's picks — I'll watch the price for you.</i>")
    return "\n".join(lines)


def _execute_sold(ticker: str, chat_id: str,
                  price=None, shares_sold=None) -> str:
    """
    Remove ticker from user's portfolio.
    If price is provided, calculates and shows realized P&L before removing.
    """
    from trade_logger import remove_holding
    ticker = ticker.upper()

    # Calculate P&L if exit price provided
    pnl_line = ""
    if price is not None:
        try:
            exit_price = float(str(price).replace(",", ""))
            log        = load_user_trade_log(chat_id)
            for t in log.get("open", []):
                if t["ticker"] == ticker:
                    entry = t.get("entry_price")
                    if entry:
                        entry    = float(entry)
                        ret_pct  = (exit_price - entry) / entry * 100
                        sign     = "+" if ret_pct >= 0 else ""
                        emoji    = "📈" if ret_pct >= 0 else "📉"
                        qty      = float(t.get("shares") or shares_sold or 0)
                        pnl_usd  = (exit_price - entry) * qty if qty else None
                        pnl_str  = f"  ·  P&amp;L: <b>{sign}${abs(pnl_usd):,.2f}</b>" if pnl_usd else ""
                        pnl_line = f"\n{emoji} Return: <b>{sign}{ret_pct:.1f}%</b>{pnl_str}"
                    break
        except Exception:
            pass

    removed = remove_holding(ticker, chat_id)
    if not removed:
        return f"⚠️ <b>{ticker}</b> is not in your portfolio."
    return f"✅ <b>{ticker}</b> removed from your portfolio.{pnl_line}"


def _execute_update_level(ticker: str, field: str, new_price: float, chat_id: str) -> str:
    """
    Update stop_loss or target_price on an open trade.
    field: "stop_loss" | "target_price"
    Resets trailing-stop notification flags when stop is updated so nudges
    can re-fire if the position continues to move.
    """
    ticker = ticker.upper()
    log    = load_user_trade_log(chat_id)

    for trade in log.get("open", []):
        if trade["ticker"] != ticker:
            continue

        old_val   = trade.get(field)
        trade[field] = round(new_price, 2)

        # Reset trailing nudge flags so they can re-fire from the new level
        if field == "stop_loss":
            trade.pop("_notified_breakeven", None)
            trade.pop("_notified_trail_90",  None)

        save_user_trade_log(chat_id, log)

        label    = "stop-loss" if field == "stop_loss" else "target"
        old_str  = f"${_p(old_val)}" if old_val else "none"
        entry    = trade.get("entry_price")
        vs_entry = ""
        if entry:
            pct = (new_price - float(entry)) / float(entry) * 100
            vs_entry = f"  <i>({'+' if pct >= 0 else ''}{pct:.1f}% vs entry)</i>"

        return (
            f"✅ <b>{ticker}</b> {label} updated: "
            f"<code>{old_str}</code> → <code>${_p(new_price)}</code>{vs_entry}"
        )

    return f"⚠️ <b>{ticker}</b> not found in your open positions."



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

    if command == "share":
        return _parse_and_execute("SHARE", original="/share", chat_id=chat_id)

    if command == "adduser":
        return _parse_and_execute(f"ADDUSER {text}", original=f"/adduser {text}", chat_id=chat_id)

    if command == "removeuser":
        return _parse_and_execute(f"REMOVEUSER {text}", original=f"/removeuser {text}", chat_id=chat_id)

    if command == "dividends":
        return _parse_and_execute("DIVIDENDS", original="/dividends", chat_id=chat_id)

    if command == "feedback":
        return _parse_and_execute(f"FEEDBACK {text}" if text else "FEEDBACK", original=original, chat_id=chat_id)

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
{"intent": "bought",     "ticker": "AAPL", "price": 182.50, "shares": 10}  — price/shares optional
{"intent": "sold",       "ticker": "AAPL", "price": 197.10, "shares": null} — shares optional
{"intent": "paper_buy",  "ticker": "AAPL", "price": 182.50, "shares": 10}  — price/shares optional
{"intent": "paper_sell", "ticker": "AAPL", "price": 197.10, "shares": null} — shares optional
{"intent": "alert",      "ticker": "NVDA", "price": 800.0, "direction": "above|below|auto"}
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
{"intent": "reset"}
{"intent": "explain",     "query": "the user's question verbatim"}
{"intent": "unknown"}

Rules:
- "bought/buy/purchased/got X shares/stocks" → bought intent with ticker resolved to uppercase symbol
- "sold/sell/sold off X" → sold intent
- "paper buy/simulate buying/paper trade" → paper_buy intent
- "paper sell/simulate selling" → paper_sell intent
- "alert me when/notify when/set alert" → alert intent
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
- Always resolve company names to uppercase ticker symbols (Apple→AAPL, Nvidia→NVDA, Evergy→EVRG)
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

    if intent == "bought":
        ticker = parsed.get("ticker") or ""
        price  = parsed.get("price")
        shares = parsed.get("shares")
        if not ticker:
            return _parse_and_execute("BOUGHT", original="/bought", chat_id=chat_id)
        price_str  = str(price)  if price  is not None else ""
        shares_str = str(shares) if shares is not None else ""
        cmd = f"BOUGHT {ticker} {price_str} {shares_str}".strip()
        return _parse_and_execute(cmd, original=query, chat_id=chat_id)

    if intent == "sold":
        ticker = parsed.get("ticker") or ""
        price  = parsed.get("price")
        if not ticker:
            return _parse_and_execute("SOLD", original="/sold", chat_id=chat_id)
        price_str = str(price) if price is not None else ""
        cmd = f"SOLD {ticker} {price_str}".strip()
        return _parse_and_execute(cmd, original=query, chat_id=chat_id)

    if intent == "paper_buy":
        ticker = parsed.get("ticker") or ""
        price  = parsed.get("price")
        shares = parsed.get("shares")
        if not ticker:
            return _parse_and_execute("PAPER BUY", original="/paper_buy", chat_id=chat_id)
        price_str  = str(price)  if price  is not None else ""
        shares_str = str(shares) if shares is not None else ""
        cmd = f"PAPER BUY {ticker} {price_str} {shares_str}".strip()
        return _parse_and_execute(cmd, original=query, chat_id=chat_id)

    if intent == "paper_sell":
        ticker = parsed.get("ticker") or ""
        price  = parsed.get("price")
        if not ticker:
            return _parse_and_execute("PAPER SELL", original="/paper_sell", chat_id=chat_id)
        price_str = str(price) if price is not None else ""
        cmd = f"PAPER SELL {ticker} {price_str}".strip()
        return _parse_and_execute(cmd, original=query, chat_id=chat_id)

    if intent == "alert":
        ticker    = parsed.get("ticker") or ""
        price     = parsed.get("price")
        direction = parsed.get("direction") or "auto"
        if not ticker or price is None:
            return _parse_and_execute("ALERT", original="/alert", chat_id=chat_id)
        return _parse_and_execute(f"ALERT {ticker} {direction} {price}", original=query, chat_id=chat_id)

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
    if intent == "reset":
        return _parse_and_execute("RESET", original=query, chat_id=chat_id)
    if intent == "explain":
        return _explain_pick(parsed.get("query", query))

    # True unknown — still try explain as last resort for questions
    return _explain_pick(query)





def _nl_extract_tickers_list(raw: str) -> list[str]:
    """
    Use Haiku to extract a list of stock/crypto names or tickers from a natural-language string.

    Handles inputs like:
      "avery dennison, microsoft, CRM, solana and EEM"
      "I bought apple, tesla and a bit of solana"
      "NVDA"

    Returns a list of name/ticker strings (e.g. ["avery dennison", "MSFT", "CRM", "solana", "EEM"]).
    Falls back to [raw] (treat entire input as one item) on any error.
    """
    import anthropic as _anthropic
    import json as _json

    SYSTEM = """You are a ticker extractor for a stock/crypto trading bot.
The user's message names one or more stocks, ETFs, mutual funds, or cryptocurrencies they bought.
Extract every asset mentioned and return ONLY a JSON array of strings — one entry per asset.
Each entry should be the ticker symbol if obvious (e.g. "NVDA", "CRM", "EEM"),
or the company/crypto name if a ticker isn't clear (e.g. "avery dennison", "solana").
Do NOT include quantities, prices, or time references.
Return ONLY the JSON array, nothing else.
Examples:
  "avery dennison, microsoft, CRM, solana and EEM" → ["avery dennison", "microsoft", "CRM", "solana", "EEM"]
  "I picked up some apple and a bit of tesla today" → ["apple", "tesla"]
  "NVDA" → ["NVDA"]
  "bought 10 shares of amazon and 2 bitcoin" → ["amazon", "bitcoin"]"""

    try:
        client  = _anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=SYSTEM,
            messages=[{"role": "user", "content": raw}],
        )
        result = _json.loads(message.content[0].text.strip())
        if isinstance(result, list) and result:
            print(f"[telegram] NL ticker list extracted: {result}")
            return result
    except Exception as exc:
        print(f"[telegram] _nl_extract_tickers_list failed ({exc})")
    return [raw]


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
- ticker: resolve company names and misspellings to uppercase ticker symbols
  (Apple→AAPL, Nvidia/Nvidea/NVDA→NVDA, Tesla→TSLA, Costco→COST, Microsoft→MSFT, etc.)
  Make your best guess for close misspellings and phonetic matches.
  Only return null if you have absolutely no idea what stock/crypto is being referenced.
- shares: a number followed by "stock", "stocks", "share", "shares", "unit", "units", "coin", "coins" → that number is shares.
  Examples: "2 stocks" → shares=2, "10 shares of apple" → shares=10, "bought 5 units" → shares=5.
  NEVER put a shares count into the price field.
- price: a dollar amount (has "$", "dollars", "USD", "at", "for", "@", or is clearly a market price like 182.50).
  Examples: "$182.50" → price=182.50, "at 65000" → price=65000, "for 3200 dollars" → price=3200.
  If the only number in the input is paired with "stocks/shares/units", it is shares, NOT price.
- direction: "below"/"under"/"drops below"/"falls to" → "below". "above"/"over"/"crosses"/"hits"/"reaches" → "above". Default → "auto".
- Words like "today", "yesterday", "this morning" are time references — ignore them.
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
        # Re-generate personal notes on demand (same Haiku call as morning send)
        personal_notes: dict = {}
        try:
            from ai_analyzer import personalize_picks
            log            = load_user_trade_log(chat_id)
            open_positions = log.get("open", [])
            risk_profile   = config.get("risk_profile", "moderate")
            personal_notes = personalize_picks(picks, open_positions, risk_profile)
        except Exception as pn_exc:
            print(f"[telegram] /today personal notes failed (non-critical): {pn_exc}")
        buy_counts: dict = {}
        if config.get("show_buy_counts"):
            try:
                from config_manager import load_buy_counts
                buy_counts = load_buy_counts()
            except Exception:
                pass
        return format_daily_message(picks, config, personal_notes=personal_notes,
                                    buy_counts=buy_counts)

    if text == "EXPLAIN":
        _prompt_for_param("explain", chat_id)
        return ""

    if text.startswith("EXPLAIN "):
        # Preserve original casing for the query — strip the command prefix
        raw   = original.lstrip("/")
        query = raw.split(" ", 1)[1].strip() if " " in raw else raw
        # Fire in background — Haiku can take 2-4s; returning "" lets webhook
        # respond instantly (200 OK) while the answer arrives a moment later.
        send_message("💬 <i>Thinking…</i>", chat_id=chat_id)
        def _explain_async():
            reply = _explain_pick(query)
            send_message(reply, chat_id=chat_id)
        threading.Thread(target=_explain_async, daemon=True).start()
        return None   # already sent "Thinking…", don't send a second message

    if text == "CHART":
        _prompt_for_param("chart", chat_id)
        return ""

    if text.startswith("CHART "):
        ticker = text.split(" ", 1)[1].strip().upper()
        if not ticker:
            return "⚠️ Please provide a ticker, e.g. /chart AAPL"
        from chart_generator import is_crypto
        asset_type = "crypto" if is_crypto(ticker) else "stock"
        send_message("📊 <i>Generating chart…</i>", chat_id=chat_id)
        threading.Thread(
            target=_send_chart, args=(ticker, asset_type, chat_id), daemon=True
        ).start()
        return None   # already sent "Generating…"

    if text == "PRICES":
        picks = load_picks()
        if not picks:
            return "📭 No picks found for today yet. Check back after 8 AM ET."
        try:
            from price_checker import get_current_prices
            current_prices = get_current_prices(picks)
            buy_counts: dict = {}
            if get_config().get("show_buy_counts"):
                try:
                    from config_manager import load_buy_counts
                    buy_counts = load_buy_counts()
                except Exception:
                    pass
            return format_confirmation_message(picks, current_prices, buy_counts=buy_counts)
        except Exception as exc:
            return f"⚠️ Could not fetch prices: {exc}"

    if text == "COMMUNITY":
        try:
            from performance_tracker import build_community_stats
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

    # ── /dividends ────────────────────────────────────────────────────────────
    if text == "DIVIDENDS":
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
                save_pending_state(chat_id, "alert", step=1)
                send_inline_keyboard(
                    "🔔 Couldn't find that ticker — please use the symbol directly\n"
                    "<i>e.g. <code>NVDA</code>, <code>AAPL</code>, <code>BTC</code></i>",
                    [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""
            if price_val is None:
                save_pending_state(chat_id, "alert", step=2,
                                   data={"ticker": ticker, "direction": direction})
                send_inline_keyboard(
                    f"🔔 Got <b>{ticker}</b>. Alert me when it goes above or below what price?\n"
                    f"<i>e.g. <code>850</code> or <code>above 900</code></i>",
                    [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""
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
            save_pending_state(chat_id, "unalert", step=1)
            send_inline_keyboard(
                "🔕 Which stock's alert should I remove?\n<i>e.g. NVDA, Apple, BTC</i>",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""
        return remove_alert(chat_id, ticker, price)

    # ── Paper trading ─────────────────────────────────────────────────────────
    if text in ("PAPER BUY",):
        _prompt_for_param("paper_buy", chat_id)
        return ""

    if text.startswith("PAPER BUY "):
        import re as _re
        raw = text[10:].strip()

        _MULTI_NOISE = {"STOCKS", "SHARES", "UNITS", "COINS", "TOKENS", "OF", "AT",
                        "DOLLARS", "DOLLAR", "USD", "EACH", "FOR", "BUY", "PAPER",
                        "SOME", "ALL", "MY", "A", "AN", "THE", "I", "WE"}

        # ── Multi-ticker: "jpm and msft" or "apple, google" ─────────────────
        raw_names = [t.strip().strip(".,;") for t in _re.split(r",|\band\b", raw, flags=_re.IGNORECASE)]
        raw_names = [n for n in raw_names if n and not _is_number(n) and n.upper() not in _MULTI_NOISE]
        if len(raw_names) >= 2:
            resolved = []
            for name in raw_names:
                cands = _resolve_ticker_candidates(name)
                if cands:
                    resolved.append(cands[0])
            if resolved:
                lines = ["📄 <b>Paper buy — live prices:</b>\n"]
                tickers = []
                for r in resolved:
                    live = _fetch_live_price(r["ticker"])
                    price_str = f"<code>${_p(live)}</code>" if live else "<i>price unavailable</i>"
                    lines.append(f"  • <b>{r['ticker']}</b> {price_str}")
                    tickers.append(r["ticker"])
                save_pending_state(chat_id, "paper_buy", step=1, data={"tickers": tickers})
                send_inline_keyboard(
                    "\n".join(lines) + f"\n\n<i>How many shares of each? e.g. <code>2 5</code></i>",
                    [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""

        # ── Single ticker — NL parse ─────────────────────────────────────────
        parsed = _nl_parse_trade("paper_buy", raw)
        ticker = parsed.get("ticker")
        shares = parsed.get("shares")
        price  = parsed.get("price")

        # Fallback: first non-numeric, non-noise word
        if not ticker:
            tokens = [p.strip(".,;") for p in raw.split() if p.strip(".,;").upper() not in _MULTI_NOISE]
            ticker_raw = next((p for p in tokens if not _is_number(p)), None)
            if ticker_raw:
                cands = _resolve_ticker_candidates(ticker_raw)
                if cands:
                    ticker = cands[0]["ticker"]

        if not ticker:
            save_pending_state(chat_id, "paper_buy")
            return "🤔 Which stock? Try: <code>Apple 10</code> or <code>AVY 2</code>"

        # Disambiguate if needed
        candidates = _resolve_ticker_candidates(ticker)
        if len(candidates) > 1:
            shares_enc = str(shares) if shares is not None else ""
            buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                         "callback_data": f"pbuy|{c['ticker']}|{shares_enc}"}]
                       for c in candidates]
            send_inline_keyboard(f"🔍 Which stock did you mean by <b>{_esc(ticker)}</b>?",
                                 buttons, chat_id=chat_id)
            return ""
        if candidates:
            ticker = candidates[0]["ticker"]

        # Ask for shares if missing
        if shares is None:
            live = _fetch_live_price(ticker)
            live_hint = f"  <i>(live: <code>${_p(live)}</code>)</i>" if live else ""
            save_pending_state(chat_id, "paper_buy", step=1, data={"ticker": ticker})
            send_inline_keyboard(
                f"📄 How many shares of <b>{ticker}</b> to simulate buying?{live_hint}",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""

        # Have ticker + shares — fetch price if needed and show confirmation
        if price is None:
            price = _fetch_live_price(ticker)

        shares_str = f"  ·  <b>{shares} shares</b>"
        total_str  = f"  ·  total <code>${float(price or 0) * float(shares):,.2f}</code>" if price else ""
        price_str  = f"<code>${_p(price)}</code>" if price else "<i>live price</i>"
        send_inline_keyboard(
            f"📄 <b>Confirm paper buy?</b>\n"
            f"<b>{ticker}</b>{shares_str}  @  {price_str}{total_str}\n"
            f"<i>Tap ✅ to simulate, or type correct details to adjust.</i>",
            [[{"text": "✅ Confirm", "callback_data": f"pbuy_confirm|{ticker}|{price or ''}|{shares}"},
              {"text": "❌ Cancel",  "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return ""

    if text in ("PAPER SELL",):
        from config_manager import load_user_paper
        positions = load_user_paper(chat_id).get("positions", [])
        if not positions:
            return "📭 No open paper positions to sell."
        buttons = []
        for p in positions:
            tk    = p["ticker"]
            entry = p.get("entry_price")
            sh    = p.get("shares")
            label = tk
            if sh:    label += f"  ·  {sh} shares"
            if entry: label += f"  @  ${_p(entry)}"
            buttons.append([{"text": f"📄 {label}", "callback_data": f"psell|{tk}|"}])
        buttons.append([{"text": "✏️ Type a different ticker", "callback_data": "sold_manual"}])
        send_inline_keyboard("📄 <b>Paper sell — which position?</b>", buttons, chat_id=chat_id)
        return ""

    if text.startswith("PAPER SELL "):
        import re as _re
        raw = text[11:].strip()

        _MULTI_NOISE = {"STOCKS", "SHARES", "UNITS", "COINS", "TOKENS", "OF", "AT",
                        "DOLLARS", "DOLLAR", "USD", "EACH", "FOR", "SELL", "PAPER",
                        "SOME", "ALL", "MY", "A", "AN", "THE", "I", "WE"}

        # ── Multi-ticker: "microsoft and btc" ───────────────────────────────
        raw_names = [t.strip().strip(".,;") for t in _re.split(r",|\band\b", raw, flags=_re.IGNORECASE)]
        raw_names = [n for n in raw_names if n and not _is_number(n) and n.upper() not in _MULTI_NOISE]
        if len(raw_names) >= 2:
            resolved = []
            for name in raw_names:
                cands = _resolve_ticker_candidates(name)
                if cands:
                    resolved.append(cands[0])
            if resolved:
                lines = ["📄 <b>Simulate selling full positions at live price?</b>\n"]
                tickers_enc = []
                for r in resolved:
                    live = _fetch_live_price(r["ticker"])
                    price_str = f"<code>${_p(live)}</code>" if live else "<i>price unavailable</i>"
                    lines.append(f"  • <b>{r['ticker']}</b> {price_str}")
                    tickers_enc.append(r["ticker"])
                send_inline_keyboard(
                    "\n".join(lines),
                    [[{"text": "✅ Sell all", "callback_data": f"psell_bulk|{','.join(tickers_enc)}"},
                      {"text": "❌ Cancel",  "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""

        # ── Single ticker — NL parse ─────────────────────────────────────────
        parsed = _nl_parse_trade("paper_sell", raw)
        ticker = parsed.get("ticker")
        shares = parsed.get("shares")
        price  = parsed.get("price")

        # Fallback: first non-numeric, non-noise word
        if not ticker:
            tokens = [p.strip(".,;") for p in raw.split() if p.strip(".,;").upper() not in _MULTI_NOISE]
            ticker_raw = next((p for p in tokens if not _is_number(p)), None)
            if ticker_raw:
                cands = _resolve_ticker_candidates(ticker_raw)
                if cands:
                    ticker = cands[0]["ticker"]

        if not ticker:
            save_pending_state(chat_id, "paper_sell", step=1)
            send_inline_keyboard(
                "📄 <b>Paper sell — which position?</b>\n"
                "<i>e.g.</i> <code>Apple</code> or <code>AVY 5</code>",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""

        # Disambiguate if needed
        candidates = _resolve_ticker_candidates(ticker)
        if len(candidates) > 1:
            shares_enc = str(shares) if shares is not None else ""
            buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                         "callback_data": f"psell|{c['ticker']}|{shares_enc}"}]
                       for c in candidates]
            send_inline_keyboard(f"🔍 Which stock did you mean by <b>{_esc(ticker)}</b>?",
                                 buttons, chat_id=chat_id)
            return ""
        if candidates:
            ticker = candidates[0]["ticker"]

        # Show confirmation — shares optional (blank = sell full position)
        if price is None:
            price = _fetch_live_price(ticker)
        shares_str = f"  ·  <b>{shares} shares</b>" if shares else "  ·  full position"
        price_str  = f"<code>${_p(price)}</code>" if price else "<i>live price</i>"
        shares_enc = str(shares) if shares else ""
        send_inline_keyboard(
            f"📄 <b>Confirm paper sell?</b>\n"
            f"<b>{ticker}</b>{shares_str}  @  {price_str}\n"
            f"<i>Tap ✅ to simulate, or type correct details to adjust.</i>",
            [[{"text": "✅ Confirm", "callback_data": f"psell_confirm|{ticker}|{price or ''}|{shares_enc}"},
              {"text": "❌ Cancel",  "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return ""

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
            save_pending_state(chat_id, "paper_add_cash")
            send_inline_keyboard(
                "💵 <b>How much cash to add to your paper account?</b>\n"
                "<i>e.g. <code>5000</code> or <code>10k</code></i>",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""
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
        # Always confirm before wiping — show current portfolio value
        from config_manager import load_user_paper
        current   = load_user_paper(chat_id)
        cash      = current.get("cash", 0)
        positions = current.get("positions", [])
        amount_enc = str(amount) if amount is not None else ""
        cash_str   = f"${amount:,.2f}" if amount is not None else f"${current.get('starting_cash', 10_000):,.2f}"
        detail     = f"  ·  {len(positions)} open position(s)  ·  ${cash:,.2f} cash" if positions else f"  ·  ${cash:,.2f} cash"
        send_inline_keyboard(
            f"⚠️ <b>Reset paper portfolio?</b>{detail}\n"
            f"<i>This wipes all positions and history. Starting cash: <b>{cash_str}</b></i>",
            [[{"text": "✅ Yes, reset everything", "callback_data": f"paper_reset_confirm|{amount_enc}"},
              {"text": "❌ Cancel",                "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return ""

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
        is_admin = _is_admin(chat_id)
        msg = (
            "📋 <b>StockPulz Commands</b>\n"
            "\n<b>📈 Daily Picks</b>"
            "\n/today — Today's stock &amp; crypto picks"
            "\n/prices — Live prices vs entry for today's picks"
            "\n/explain — Ask any market question or why a stock was picked"
            "\n/chart — Candlestick chart for any ticker\n"
            "\n<b>💼 My Portfolio</b>"
            "\n/bought — Log a position you entered"
            "\n/sold — Close a position &amp; record P&amp;L"
            "\n/positions — Open positions with live P&amp;L &amp; exposure"
            "\n/summary — Portfolio health: win rate, open P&amp;L, risk warnings"
            "\n/history — Full trade history (open &amp; closed)"
            "\n/updatestop — Move a stop-loss (e.g. to breakeven)"
            "\n/updatetarget — Adjust a price target"
            "\n/fixticker — Rename a wrongly stored ticker (e.g. COSTCO → COST)\n"
            "\n<b>🧪 Paper Trading</b>"
            "\n/paper_buy — Simulate a buy (no real money)"
            "\n/paper_sell — Simulate a sell"
            "\n/paper_cancel — Remove a paper position without recording a sale"
            "\n/paper_portfolio — Virtual portfolio, cash &amp; unrealised P&amp;L"
            "\n/paper_perf — Paper trading performance vs SPY"
            "\n/paper_history — All past paper trades"
            "\n/paper_add_cash — Add cash to your paper account"
            "\n/paper_reset — Wipe paper portfolio and start fresh\n"
            "\n<b>🔔 Price Alerts</b>"
            "\n/alert — Set a price alert (e.g. NVDA above 1000)"
            "\n/alerts — View all your active alerts"
            "\n/unalert — Remove an alert\n"
            "\n<b>🌍 Market</b>"
            "\n/regime — Current market conditions (Bull / Bear / Caution)"
            "\n/community — All StockPulz users' performance vs S&amp;P 500"
            "\n/backtest — How the strategy performed historically\n"
            "\n<b>⚙️ Settings</b>"
            "\n/settings — All preferences in one interactive panel"
            "\n/set_risk — Risk profile: conservative / moderate / aggressive"
            "\n/set_budget — Per-trade budget (e.g. stocks 200 crypto 50)"
            "\n/set_thresholds — Stop loss &amp; target gain % (e.g. stop 7 target 15)"
            "\n/watch — Always include specific tickers in your picks"
            "\n/exclude — Exclude a sector (e.g. Energy, Utilities)"
            "\n/pause — Pause daily briefings"
            "\n/resume — Resume daily briefings"
            "\n/status — Check if picks are active and when the next one runs"
            "\n/next — When's the next scheduled pick"
            "\n/reset — Reset all settings to defaults"
            "\n/dividends — Dividend yield &amp; next ex-date for your positions"
            "\n/share — Get your invite link"
            "\n/feedback — Send feedback to the team\n"
            "\n<i>💬 You can also just type naturally — e.g. \"why was NVDA picked?\", "
            "\"set my risk to aggressive\", or \"alert me when BTC hits 100k\".</i>\n"
        )
        if is_admin:
            msg += (
                "\n<b>🔑 Admin</b>"
                "\n/dashboard — Overview: users, positions, top performer, last run"
                "\n/feedback — View all user feedback"
                "\n/users — List all allowed users"
                "\n/pending — Users awaiting approval"
                "\n/broadcast — Send a message to all users"
                "\n/release — Send a release note to all users"
                "\n/admin_perf — Performance across all users"
                "\n/bot_pause · /bot_resume — Global kill switch"
                "\n/bot_crypto_on · /bot_crypto_off — Toggle crypto globally\n"
            )
        msg += (
            "\n📖 <a href=\"https://stockpulz.com/commands\">Full guide with examples →</a>"
        )
        return msg

    # /set_risk conservative | moderate | aggressive  (or natural language)
    if text == "SET RISK":
        _prompt_for_param("set_risk", chat_id)
        return ""

    if text.startswith("SET RISK "):
        raw     = text[len("SET RISK "):].strip().lower()
        profile = raw if raw in ("conservative", "moderate", "aggressive") else _nl_param("risk", raw).lower()
        if profile not in ("conservative", "moderate", "aggressive"):
            send_inline_keyboard(
                "⚖️ Choose your risk profile:",
                [[
                    {"text": "🛡 Conservative", "callback_data": "set_risk|conservative"},
                    {"text": "⚖️ Moderate",     "callback_data": "set_risk|moderate"},
                    {"text": "🔥 Aggressive",   "callback_data": "set_risk|aggressive"},
                ]],
                chat_id=chat_id,
            )
            return ""
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
        bot_username = _get_bot_username()
        if not bot_username:
            return "⚠️ Could not fetch bot username — try again in a moment."

        # Admin share → cryptographically signed token, auto-approves the clicker.
        # Regular user share → still requires admin approval (normal pending flow).
        if _is_admin(chat_id):
            deep_link = _make_admin_invite_token()   # signed + time-limited (48h)
            footer    = "<i>(Anyone who taps this link is automatically approved ✅ — link valid 48 hours)</i>"
        else:
            deep_link = f"ref_{chat_id}"
            footer    = "<i>(Your friend will need admin approval — usually a few hours)</i>"

        bot_link = f"https://t.me/{bot_username}?start={deep_link}"

        return (
            f"📲 <b>Share StockPulz with friends:</b>\n\n"
            f"Hey! I'm using StockPulz — a personal AI stock advisor that sends daily stock &amp; crypto picks, "
            f"price alerts, and weekly performance recaps.\n\n"
            f'🌐 Learn more: <a href="https://stockpulz.com/">stockpulz.com</a>\n\n'
            f"📱 Join on Telegram 👇\n"
            f'<a href="{bot_link}">{bot_link}</a>\n\n'
            f"{footer}"
        )

    # ── /start (also handles deep links: /start admin_ref | /start ref_<id>) ───
    if text == "START" or text.startswith("START "):
        # Parse deep link parameter (everything after "START ")
        deep_param = text[6:].strip() if text.startswith("START ") else ""
        is_admin_invite = _verify_admin_invite_token(deep_param)

        # Known user — show welcome back
        if _is_admin(chat_id) or chat_id in get_allowed_users():
            return (
                "👋 <b>Welcome back to StockPulz!</b>\n\n"
                "/today — Today's stock &amp; crypto picks\n"
                "/positions — open trades with live P&amp;L\n"
                "/settings — your preferences\n"
                "/help — all commands with descriptions\n\n"
                "📖 <a href=\"https://stockpulz.com/commands\">Full guide with examples →</a>"
            )

        # ── Admin invite link → auto-approve immediately ──────────────────────
        if is_admin_invite:
            reply = _parse_and_execute(f"ADDUSER {chat_id}", original=f"/adduser {chat_id}", chat_id=chat_id)
            # ADDUSER sends a full welcome message to the new user — just confirm here
            return (
                "✅ <b>You're in! Welcome to StockPulz.</b>\n\n"
                "You were auto-approved via an admin invite link.\n\n"
                "Here's how to get started:\n"
                "/today — Today's stock &amp; crypto picks\n"
                "/settings — set your risk level, budget &amp; preferences\n"
                "/help — all commands with descriptions\n\n"
                "📖 <a href=\"https://stockpulz.com/commands\">Full guide with examples →</a>"
            )

        # ── Normal flow — add to pending, notify admin ────────────────────────
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
        # If referred by a user, mention who referred them
        referrer_str = ""
        if deep_param.startswith("ref_"):
            referrer_id = deep_param[4:]
            referrer_str = f"\nReferred by user: <code>{referrer_id}</code>"
        admin_msg = (
            f"🔔 <b>New access request</b>\n\n"
            f"Name: <b>{_esc(first_name)}</b>"
            + (f"  (@{_esc(username)})" if username else "") +
            f"\nChat ID: <code>{chat_id}</code>"
            f"{referrer_str}\n\n"
            f"Tap to approve 👇"
        )
        if owner:
            send_inline_keyboard(
                admin_msg,
                [[
                    {"text": f"✅ Approve {_esc(first_name or chat_id)}",
                     "callback_data": f"approve_user|{chat_id}"},
                    {"text": "❌ Reject",
                     "callback_data": f"reject_user|{chat_id}"},
                ]],
                chat_id=owner,
            )
        return (
            "👋 <b>Welcome to StockPulz!</b>\n\n"
            "Your access request has been sent to the admin.\n"
            "You'll be notified as soon as you're approved — usually within a few hours.\n\n"
            "<i>StockPulz delivers daily AI-curated stock &amp; crypto picks, price alerts, and weekly performance recaps.</i>\n\n"
            "📖 <a href=\"https://stockpulz.com\">Learn more at stockpulz.com →</a>"
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
            "Every morning before the market opens, you'll get 2–5 AI-curated stock and crypto picks — "
            "each with an entry price, profit target, stop-loss, and a one-line reason why.\n\n"
            "<b>Your daily schedule:</b>\n"
            "📬 <b>8 AM ET</b> — morning picks with conviction scores &amp; personal notes\n"
            "🕙 <b>10:30 AM</b> — live price check: hold, watch, or exit\n"
            "📊 <b>3:30 PM</b> — end-of-day snapshot of how picks moved\n"
            "📅 <b>Saturday</b> — crypto picks + weekly recap\n\n"
            "<b>Step 1 — Set your preferences:</b>\n"
            "Just tap /settings to set your risk level, budget, and whether you want crypto.\n\n"
            "<b>Step 2 — Track your portfolio:</b>\n"
            "Tell me what you hold with /bought AAPL and I'll watch it for you.\n\n"
            "<b>Step 3 — Ask anything:</b>\n"
            "You can type naturally — \"why was NVDA picked?\", \"set my risk to aggressive\", "
            "\"alert me when BTC hits 100k\". No slash commands needed.\n\n"
            "📋 /help — full command list  ·  /today — today's picks"
            + picks_msg,
            chat_id=new_id,
        )
        # Send settings panel immediately after welcome so user can configure on the spot
        _send_settings_panel(new_id)
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

    # ── /feedback — submit feedback (users) or view all (admin) ─────────────
    if text == "FEEDBACK":
        if _is_admin(chat_id):
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
        else:
            _prompt_for_param("feedback", chat_id)
            return ""

    if text.startswith("FEEDBACK "):
        raw_feedback = original.split(" ", 1)[1].strip() if " " in original else ""
        if not raw_feedback:
            return "⚠️ Please include your feedback text. e.g. <code>/feedback I love this bot!</code>"
        # Get user profile for context
        try:
            import requests as _req
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
        now_utc = _dt.utcnow()

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
        parts_ft = text[len("FIXTICKER "):].strip().upper().split()
        if len(parts_ft) != 2:
            return "⚠️ Usage: <code>/fixticker OLDTICKER NEWTICKER</code>  e.g. <code>/fixticker COSTCO COST</code>"
        old_tk, new_tk = parts_ft
        log     = load_user_trade_log(chat_id)
        changed = 0
        for t in log.get("open", []):
            if t["ticker"] == old_tk:
                t["ticker"] = new_tk
                changed += 1
        for t in log.get("closed", []):
            if t["ticker"] == old_tk:
                t["ticker"] = new_tk
                changed += 1
        if changed == 0:
            return f"⚠️ <b>{old_tk}</b> not found in your portfolio."
        save_user_trade_log(chat_id, log)
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
        import re as _re
        raw = text[len("BOUGHT "):].strip()

        # ── Multi-ticker: "microsoft, bnb and btc" ────────────────────────────
        _MULTI_NOISE = {"STOCKS", "SHARES", "UNITS", "COINS", "TOKENS", "OF", "AT",
                        "DOLLARS", "DOLLAR", "USD", "EACH", "FOR", "BOUGHT", "BUY",
                        "SOME", "ALL", "MY", "A", "AN", "THE", "I", "WE"}
        raw_names = [t.strip().strip(".,;") for t in _re.split(r",|\band\b", raw, flags=_re.IGNORECASE)]
        raw_names = [n for n in raw_names if n and not _is_number(n) and n.upper() not in _MULTI_NOISE]
        if len(raw_names) >= 2:
            resolved = []
            for name in raw_names:
                cands = _resolve_ticker_candidates(name)
                if cands:
                    resolved.append(cands[0])
            if resolved:
                lines = ["🛒 <b>Log these positions at live price?</b>\n"]
                confirm_parts = []
                for r in resolved:
                    live = _fetch_live_price(r["ticker"])
                    price_str = f"<code>${_p(live)}</code>" if live else "<i>price unavailable</i>"
                    lines.append(f"  • <b>{r['ticker']}</b> {price_str}")
                    if live:
                        confirm_parts.append(f"{r['ticker']}|{live}")
                send_inline_keyboard(
                    "\n".join(lines),
                    [[{"text": "✅ Log all at live price", "callback_data": f"bought_bulk|{','.join(confirm_parts)}"},
                      {"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""

        # ── Single ticker — always use NL parser ─────────────────────────────
        parsed     = _nl_parse_trade("bought", raw)
        name_raw   = (parsed.get("ticker") or "").strip(".,;") or None
        price_raw  = str(parsed["price"])  if parsed.get("price")  is not None else None
        shares_raw = str(parsed["shares"]) if parsed.get("shares") is not None else None

        # Last-resort fallback: first non-numeric, non-noise word
        if not name_raw:
            tokens = [p.strip(".,;") for p in raw.split() if p.strip(".,;").upper() not in _MULTI_NOISE]
            name_raw = next((p for p in tokens if not _is_number(p)), None)

        if not name_raw:
            return "🤔 I couldn't identify a stock. Try: <code>/bought Apple</code> or <code>/bought AAPL 182.50 5</code>"

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

        # ── Price sanity check ────────────────────────────────────────────────
        # If the parsed price is way below the live price (>80% off), the number
        # is almost certainly a share count that the NL parser misidentified.
        # Swap it: treat parsed price as shares, use live price instead.
        live = _fetch_live_price(ticker)
        if price_raw is not None and live:
            try:
                price_f_check = float(price_raw)
                if price_f_check > 0 and price_f_check < live * 0.20:
                    # Looks like a share count, not a price
                    if shares_raw is None:
                        shares_raw = price_raw
                    price_raw = str(live)
            except (ValueError, TypeError):
                pass

        # If still no price, use live price
        if price_raw is None:
            if live:
                price_raw = str(live)
            else:
                save_pending_state(chat_id, "bought", step=2, data={"ticker": ticker})
                send_inline_keyboard(
                    f"💰 At what price did you buy <b>{ticker}</b>?\n<i>Send blank to use live price</i>",
                    [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""

        # Always confirm so user can verify what was understood
        price_f    = float(price_raw)
        shares_enc = shares_raw or ""
        total_str  = f"  ·  total <code>${price_f * float(shares_raw):,.2f}</code>" if shares_raw else ""
        shares_str = f"  ·  <b>{shares_raw} shares</b>" if shares_raw else ""
        send_inline_keyboard(
            f"🛒 <b>Confirm buy?</b>\n"
            f"<b>{ticker}</b>{shares_str}  @  <code>${_p(price_raw)}</code>/share{total_str}\n"
            f"<i>Tap ✅ to log, or type the correct details to adjust.</i>",
            [[{"text": "✅ Confirm", "callback_data": f"bought_confirm|{ticker}|{price_raw}|{shares_enc}"},
              {"text": "❌ Cancel",  "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return ""

    # ── /sold [TICKER|name [price]] ──────────────────────────────────────────
    if text == "SOLD":
        # Show open positions as tappable buttons — no typing needed
        log         = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        seen: set   = set()
        unique      = []
        for t in open_trades:
            if t["ticker"] not in seen:
                seen.add(t["ticker"])
                unique.append(t)

        if unique:
            buttons = []
            for t in unique:
                ticker = t["ticker"]
                entry  = t.get("entry_price")
                entry_str = f"  (entry ${_p(entry)})" if entry else ""
                buttons.append([{
                    "text":          f"💸 {ticker}{entry_str}",
                    "callback_data": f"sold_pick|{ticker}",
                }])
            buttons.append([{"text": "✏️ Type a different ticker", "callback_data": "sold_manual"}])
            send_inline_keyboard(
                "💸 <b>Which position did you close?</b>",
                buttons,
                chat_id=chat_id,
            )
        else:
            _prompt_for_param("sold", chat_id)
        return ""

    if text.startswith("SOLD "):
        import re as _re
        raw = text[len("SOLD "):].strip()

        # ── Multi-ticker: "microsoft and btc" / "apple, tesla" ───────────────
        _MULTI_NOISE = {"STOCKS", "SHARES", "UNITS", "COINS", "TOKENS", "OF", "AT",
                        "DOLLARS", "DOLLAR", "USD", "EACH", "FOR", "SOLD", "SELL",
                        "SOME", "ALL", "MY", "A", "AN", "THE", "I", "WE"}
        raw_names = [t.strip().strip(".,;") for t in _re.split(r",|\band\b", raw, flags=_re.IGNORECASE)]
        raw_names = [n for n in raw_names if n and not _is_number(n) and n.upper() not in _MULTI_NOISE]
        if len(raw_names) >= 2:
            resolved = []
            for name in raw_names:
                cands = _resolve_ticker_candidates(name)
                if cands:
                    resolved.append(cands[0])
            if resolved:
                lines = ["💸 <b>Close these positions at live price?</b>\n"]
                confirm_parts = []
                for r in resolved:
                    live = _fetch_live_price(r["ticker"])
                    price_str = f"<code>${_p(live)}</code>" if live else "<i>price unavailable</i>"
                    lines.append(f"  • <b>{r['ticker']}</b> {price_str}")
                    if live:
                        confirm_parts.append(f"{r['ticker']}|{live}")
                send_inline_keyboard(
                    "\n".join(lines),
                    [[{"text": "✅ Close all at live price", "callback_data": f"sold_bulk|{','.join(confirm_parts)}"},
                      {"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""

        # ── Single ticker — always use NL parser ─────────────────────────────
        parsed     = _nl_parse_trade("sold", raw)
        name_raw   = (parsed.get("ticker") or "").strip(".,;") or None
        price_raw  = str(parsed["price"])  if parsed.get("price")  is not None else None
        shares_raw = str(parsed["shares"]) if parsed.get("shares") is not None else None

        if not name_raw:
            tokens = [p.strip(".,;") for p in raw.split() if p.strip(".,;").upper() not in _MULTI_NOISE]
            name_raw = next((p for p in tokens if not _is_number(p)), None)

        if not name_raw:
            return "🤔 I couldn't identify a stock. Try: /sold Apple 197.10 or /sold AAPL at $197"

        candidates = _resolve_ticker_candidates(name_raw)
        if len(candidates) > 1:
            price_enc  = price_raw  or ""
            shares_enc = shares_raw or ""
            buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                         "callback_data": f"sell|{c['ticker']}|{price_enc}|{shares_enc}"}]
                       for c in candidates]
            send_inline_keyboard(f"🔍 Which stock did you mean by <b>{_esc(name_raw)}</b>?",
                                 buttons, chat_id=chat_id)
            return ""

        ticker = candidates[0]["ticker"]

        # ── Price sanity check ────────────────────────────────────────────────
        live = _fetch_live_price(ticker)
        if price_raw is not None and live:
            try:
                price_f_check = float(price_raw)
                if price_f_check > 0 and price_f_check < live * 0.20:
                    if shares_raw is None:
                        shares_raw = price_raw
                    price_raw = str(live)
            except (ValueError, TypeError):
                pass

        # Use live price if not specified
        if price_raw is None:
            if live:
                price_raw = str(live)
            else:
                save_pending_state(chat_id, "sold", step=2, data={"ticker": ticker})
                send_inline_keyboard(
                    f"💰 At what price did you sell <b>{ticker}</b>?\n<i>Send blank to use live price</i>",
                    [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""

        # Always confirm
        shares_enc = shares_raw or ""
        shares_str = f"  ·  <b>{shares_raw} shares</b>" if shares_raw else "  ·  full position"
        send_inline_keyboard(
            f"💸 <b>Confirm sell?</b>\n"
            f"<b>{ticker}</b>{shares_str}  @  <code>${_p(price_raw)}</code>\n"
            f"<i>Tap ✅ to log, or type the correct details to adjust.</i>",
            [[{"text": "✅ Confirm", "callback_data": f"sold_confirm|{ticker}|{price_raw}|{shares_enc}"},
              {"text": "❌ Cancel",  "callback_data": f"cancel_pending|{chat_id}"}]],
            chat_id=chat_id,
        )
        return ""

    # ── /paper_cancel — remove a paper position without recording a sale ──────
    if text == "PAPER CANCEL":
        from paper_trader import paper_cancel
        data      = load_user_paper(chat_id)
        positions = data.get("positions", [])
        if not positions:
            return "📭 No paper positions to remove."
        buttons = [[{"text": f"🗑 {p['ticker']}  ${_p(p.get('entry_price'))}  · {p.get('shares')} shares",
                     "callback_data": f"paper_cancel_pos|{p['ticker']}"}]
                   for p in positions]
        send_inline_keyboard(
            "🗑 <b>Remove which paper position?</b>\n"
            "<i>Cash will be refunded. Not counted as a sale.</i>",
            buttons, chat_id=chat_id,
        )
        return ""

    if text.startswith("PAPER CANCEL "):
        from paper_trader import paper_cancel
        raw  = text[13:].strip()
        candidates = _resolve_ticker_candidates(raw)
        if len(candidates) > 1:
            buttons = [[{"text": f"{c['ticker']} — {c['name']}",
                         "callback_data": f"paper_cancel_pos|{c['ticker']}"}]
                       for c in candidates]
            send_inline_keyboard(f"🔍 Which stock?", buttons, chat_id=chat_id)
            return ""
        ticker = candidates[0]["ticker"]
        return paper_cancel(ticker, chat_id)

    # ── /paper_history — closed paper trades with remove buttons ────────────
    if text == "PAPER HISTORY":
        from paper_trader import paper_history as get_paper_history
        history = get_paper_history(chat_id)

        if not history:
            return (
                "📄 <b>Paper Trade History</b>\n\n"
                "No closed paper trades yet.\n"
                "Use /paper_sell to close a position."
            )

        lines = ["📄 <b>PAPER TRADE HISTORY</b>\n"]
        for t in history:
            gain_pct = t.get("gain_pct", 0)
            gain     = t.get("gain", 0)
            sign     = "+" if gain >= 0 else ""
            emoji    = "✅" if gain >= 0 else "❌"
            shares   = t.get("shares", "")
            lines.append(
                f"{emoji} <b>{t['ticker']}</b>  {shares} shares\n"
                f"   Buy <code>${_p(t.get('buy_price'))}</code> → "
                f"Sell <code>${_p(t.get('sell_price'))}</code>  "
                f"<b>{sign}{gain_pct:.1f}%</b>  (${gain:+.2f})\n"
                f"   📅 {t.get('closed_date', '')}"
            )

        send_message("\n\n".join([lines[0]] + lines[1:]), chat_id=chat_id)

        # 🗑 one button per history entry
        buttons = []
        for idx, t in enumerate(history):
            gain_pct = t.get("gain_pct", 0)
            sign     = "+" if gain_pct >= 0 else ""
            buttons.append([{
                "text":          f"🗑 {t['ticker']}  {sign}{gain_pct:.1f}%  · {t.get('closed_date', '')}",
                "callback_data": f"paper_hist_rm_confirm|{idx}",
            }])

        send_inline_keyboard(
            "🗑 <b>Remove a paper trade?</b>\n"
            "<i>Proceeds will be refunded and shares restored.</i>",
            buttons,
            chat_id=chat_id,
        )
        return ""

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
                "etype":  "open",
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
                "etype":  "closed_buy",
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
                "etype":  "closed_sell",
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

        send_message("\n".join(lines), chat_id=chat_id)

        # Build 🗑 remove buttons — one per unique removable entry
        buttons = []
        seen_open   = set()
        seen_closed = set()
        for e in events:
            ticker = e["ticker"]
            if e["etype"] == "open" and ticker not in seen_open:
                seen_open.add(ticker)
                price_label = f"${_p(e['price'])}" if e["price"] else "—"
                buttons.append([{
                    "text":          f"🗑 {ticker}  buy @ {price_label}",
                    "callback_data": f"cancel_auto|{ticker}",
                }])
            elif e["etype"] == "closed_sell" and ticker not in seen_closed:
                seen_closed.add(ticker)
                price_label = f"${_p(e['price'])}" if e["price"] else "—"
                ret_val     = e["ret"] or 0
                sign        = "+" if ret_val >= 0 else ""
                buttons.append([{
                    "text":          f"🗑 {ticker}  sold @ {price_label}  {sign}{ret_val}%",
                    "callback_data": f"cancel_auto|{ticker}",
                }])

        if buttons:
            send_inline_keyboard(
                "🗑 <b>Remove a trade entry?</b>\n"
                "<i>Confirmation required before any change is made.</i>",
                buttons,
                chat_id=chat_id,
            )
        return ""

    # ── /summary — one-shot portfolio health view ────────────────────────────
    if text == "SUMMARY":
        import yfinance as yf
        from trade_logger import get_performance_stats

        log         = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        stats       = get_performance_stats(chat_id)

        lines = ["📊 <b>Portfolio Health</b>", ""]

        # ── All-time stats block ──────────────────────────────────────────────
        if stats and stats["count"] > 0:
            wins   = stats["wins"]
            losses = stats["count"] - wins
            sign   = "+" if stats["total_gain_usd"] >= 0 else ""
            pnl_sign = "+" if stats["avg_return"] >= 0 else ""
            lines.append(
                f"<b>All-time</b>  ·  {stats['count']} trades  ·  "
                f"<b>{stats['win_rate']}%</b> win rate  ({wins}W / {losses}L)"
            )
            lines.append(
                f"P&amp;L: <b>{sign}${abs(stats['total_gain_usd']):.2f}</b>  ·  "
                f"avg <b>{pnl_sign}{stats['avg_return']}%</b>/trade"
            )
            # Best / worst
            if stats.get("best"):
                bt, br = stats["best"]
                wt, wr = stats["worst"]
                lines.append(
                    f"🏆 Best: <b>{bt}</b> {'+' if br >= 0 else ''}{br:.1f}%  ·  "
                    f"💔 Worst: <b>{wt}</b> {'+' if wr >= 0 else ''}{wr:.1f}%"
                )
            # Streak
            if stats.get("streak", 0) >= 2:
                lines.append(f"🔥 <b>{stats['streak']} win streak</b>")
            elif stats["count"] > 0 and wins == 0:
                lines.append(f"<i>No wins yet — hang in there.</i>")
            # Outcomes breakdown
            t_hits  = stats.get("targets_hit", 0)
            s_hits  = stats.get("stops_hit", 0)
            expired = stats.get("expired", 0)
            if t_hits or s_hits or expired:
                outcome_parts = []
                if t_hits:  outcome_parts.append(f"🎯 {t_hits} targets hit")
                if s_hits:  outcome_parts.append(f"🛑 {s_hits} stops hit")
                if expired: outcome_parts.append(f"⏱ {expired} expired")
                lines.append("  ".join(outcome_parts))
        else:
            lines.append("<i>No closed trades yet.</i>")
            lines.append("<i>Log a buy with /bought — I'll track target &amp; stop for you.</i>")

        # ── Open positions block ──────────────────────────────────────────────
        lines.append("")

        if open_trades:
            # Deduplicate by ticker
            seen: set = set()
            unique: list = []
            for t in open_trades:
                if t["ticker"] not in seen:
                    seen.add(t["ticker"])
                    unique.append(t)

            lines.append(f"<b>Open</b>  ·  {len(unique)} holding{'s' if len(unique) != 1 else ''}")

            # Fetch live prices + sector in one pass (stocks: .info; crypto: fast_info)
            from earnings_checker import get_upcoming_earnings as _get_earnings_s
            prices: dict = {}
            _sum_sector_map: dict[str, str] = {}
            _sum_stock_tickers = [t["ticker"] for t in unique if t.get("asset_type") == "stock"]

            for t in unique:
                ticker = t["ticker"]
                if t.get("asset_type") == "stock":
                    try:
                        _inf  = yf.Ticker(ticker).info
                        _pr   = (_inf.get("currentPrice") or _inf.get("regularMarketPrice")
                                 or _inf.get("previousClose"))
                        if _pr:
                            prices[ticker] = round(float(_pr), 2)
                        _sum_sector_map[ticker] = _inf.get("sector", "Unknown")
                    except Exception:
                        _sum_sector_map[ticker] = "Unknown"
                else:
                    try:
                        _fi  = yf.Ticker(ticker).fast_info
                        _pr  = getattr(_fi, "last_price", None) or getattr(_fi, "regular_market_price", None)
                        if _pr:
                            prices[ticker] = round(float(_pr), 2)
                            continue
                    except Exception:
                        pass
                    try:
                        _hist = yf.Ticker(ticker).history(period="1d", interval="1m")
                        if not _hist.empty:
                            prices[ticker] = round(float(_hist["Close"].iloc[-1]), 2)
                    except Exception:
                        pass
            _sum_earnings_map: dict[str, str] = _get_earnings_s(_sum_stock_tickers, days_ahead=2) if _sum_stock_tickers else {}

            # Concentration warning
            _sum_conc_warn = ""
            if len(_sum_stock_tickers) >= 2:
                _sc: dict[str, int] = {}
                for _s in _sum_sector_map.values():
                    _sc[_s] = _sc.get(_s, 0) + 1
                _top_s, _top_n = max(_sc.items(), key=lambda x: x[1])
                if _top_s and _top_s != "Unknown" and _top_n / len(_sum_stock_tickers) >= 0.5:
                    _pct = int(_top_n / len(_sum_stock_tickers) * 100)
                    _sum_conc_warn = f"⚡ <i>{_pct}% {_top_s} — consider diversifying</i>"

            if _sum_conc_warn:
                lines.append(_sum_conc_warn)

            for t in unique:
                ticker  = t["ticker"]
                current = prices.get(ticker)
                entry   = t.get("entry_price")
                target  = t.get("target_price")
                stop    = t.get("stop_loss")

                if current and entry:
                    pct     = (current - float(entry)) / float(entry) * 100
                    sign    = "+" if pct >= 0 else ""
                    badge   = ""
                    if stop   and current <= float(stop):
                        badge = "  🔴 STOP HIT"
                    elif stop and current <= float(stop) * 1.03:
                        badge = "  ⚠️ NEAR STOP"
                    elif target and current >= float(target) * 0.97:
                        badge = "  🎯 NEAR TARGET"
                    lines.append(
                        f"<b>{ticker}</b>  <code>${_p(current)}</code>  "
                        f"<b>{sign}{pct:.1f}%</b> vs entry{badge}"
                    )
                elif current:
                    lines.append(f"<b>{ticker}</b>  <code>${_p(current)}</code>  <i>no entry price</i>")
                else:
                    lines.append(f"<b>{ticker}</b>  <i>price unavailable</i>")
                if ticker in _sum_earnings_map:
                    lines.append(f"   📅 <i>Earnings {_sum_earnings_map[ticker]} — consider position size</i>")
        else:
            lines.append("<b>Open</b>  ·  no holdings")
            lines.append("<i>Use /bought to log a position.</i>")

        lines.append("")
        lines.append("<i>/positions for full detail  ·  /history for trade log</i>")
        return "\n".join(lines)

    # ── /updatestop / /updatetarget — adjust levels on open trades ──────────
    for _cmd, _field in (("UPDATESTOP", "stop_loss"), ("UPDATETARGET", "target_price")):
        prefix = _cmd + " "
        if text == _cmd:
            label     = "stop-loss" if _field == "stop_loss" else "target"
            emoji     = "🛑" if _field == "stop_loss" else "🎯"
            log       = load_user_trade_log(chat_id)
            open_trades = log.get("open", [])
            if not open_trades:
                return f"📭 No open positions to update."
            # Build one button per open position showing current level
            buttons = []
            for t in open_trades:
                tk      = t["ticker"]
                current = t.get(_field)
                level   = f"  ·  current {emoji} <code>${_p(current)}</code>" if current else ""
                buttons.append([{
                    "text":          f"{tk}{('  · ' + emoji + ' $' + _p(current)) if current else ''}",
                    "callback_data": f"updatelevel_pick|{_cmd.lower()}|{tk}",
                }])
            buttons.append([{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}])
            send_inline_keyboard(
                f"📝 <b>Update {label}</b> — pick a position:",
                buttons,
                chat_id=chat_id,
            )
            return ""

        if text.startswith(prefix):
            parts = text[len(prefix):].strip().split()
            if len(parts) >= 2 and _is_number(parts[-1]):
                ticker    = parts[0].upper()
                new_price = float(parts[-1].replace(",", ""))
                return _execute_update_level(ticker, _field, new_price, chat_id)
            elif len(parts) == 1:
                # Got ticker only — prompt for price
                ticker = parts[0].upper()
                label  = "stop-loss" if _field == "stop_loss" else "target"
                save_pending_state(chat_id, _cmd.lower(), step=2, data={"ticker": ticker})
                send_inline_keyboard(
                    f"📝 New {label} price for <b>{ticker}</b>?",
                    [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""
            else:
                return f"⚠️ Usage: <code>/{_cmd.lower()} TICKER PRICE</code>  e.g. <code>/{_cmd.lower()} NVDA 118</code>"

    # ── /positions / /portfolio ───────────────────────────────────────────────
    if text in ("POSITIONS", "PORTFOLIO"):
        import yfinance as yf

        log         = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        if not open_trades:
            return "📭 No holdings yet. Tap /bought and tell me which stocks or crypto you're holding."

        # Deduplicate by ticker (keep first occurrence)
        seen: set = set()
        unique_trades = []
        for t in open_trades:
            if t["ticker"] not in seen:
                seen.add(t["ticker"])
                unique_trades.append(t)

        # Fetch current prices + sector in one pass.
        # Stocks: single .info call gives price AND sector (no double-fetch).
        # Crypto: fast_info is sufficient (no sector needed).
        from earnings_checker import get_upcoming_earnings as _get_earnings
        prices: dict          = {}
        sector_by_ticker: dict[str, str] = {}

        stock_trades  = [t for t in unique_trades if t.get("asset_type") == "stock"]
        crypto_trades = [t for t in unique_trades if t.get("asset_type") != "stock"]
        stock_tickers = [t["ticker"] for t in stock_trades]

        for t in stock_trades:
            ticker = t["ticker"]
            try:
                info   = yf.Ticker(ticker).info
                price  = (info.get("currentPrice") or info.get("regularMarketPrice")
                          or info.get("previousClose"))
                if price:
                    prices[ticker] = round(float(price), 2)
                sector_by_ticker[ticker] = info.get("sector", "Unknown")
            except Exception:
                sector_by_ticker[ticker] = "Unknown"

        for t in crypto_trades:
            ticker = t["ticker"]
            try:
                fi    = yf.Ticker(ticker).fast_info
                price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
                if price:
                    prices[ticker] = round(float(price), 2)
                    continue
            except Exception:
                pass
            try:
                hist = yf.Ticker(ticker).history(period="1d", interval="1m")
                if not hist.empty:
                    prices[ticker] = round(float(hist["Close"].iloc[-1]), 2)
            except Exception:
                pass

        # Earnings in next 2 days
        earnings_map: dict[str, str] = _get_earnings(stock_tickers, days_ahead=2) if stock_tickers else {}

        # Concentration warning string
        concentration_warn = ""
        if len(stock_tickers) >= 2:
            sector_counts: dict[str, int] = {}
            for s in sector_by_ticker.values():
                sector_counts[s] = sector_counts.get(s, 0) + 1
            top_sector, top_n = max(sector_counts.items(), key=lambda x: x[1])
            if top_sector and top_sector != "Unknown" and top_n / len(stock_tickers) >= 0.5:
                pct = int(top_n / len(stock_tickers) * 100)
                concentration_warn = f"⚡ <i>{pct}% {top_sector} — consider diversifying</i>"

        # Build position data for Haiku guidance
        position_data = []
        for t in unique_trades:
            ticker  = t["ticker"]
            current = prices.get(ticker)
            entry   = float(t.get("entry_price") or 0)
            target  = float(t.get("target_price") or 0)
            stop    = float(t.get("stop_loss") or 0)
            if current:
                position_data.append({
                    "ticker":    ticker,
                    "current":   round(current, 2),
                    "pick_entry": entry or None,
                    "target":    target or None,
                    "stop":      stop   or None,
                    "to_target": round((target / current - 1) * 100, 1) if target and current else None,
                    "to_stop":   round((stop   / current - 1) * 100, 1) if stop   and current else None,
                })

        # Ask Haiku for one-line guidance per position
        guidance: dict[str, str] = {}
        if position_data:
            try:
                import anthropic as _ant, json as _j
                prompt = (
                    "You are a brief trading advisor. For each position give ONE short action line "
                    "(max 10 words): HOLD / WATCH / TAKE PROFIT / NEAR STOP — ACT / etc. "
                    "Be direct. Consider proximity to target/stop.\n\n"
                    f"Positions: {_j.dumps(position_data)}\n\n"
                    'Return ONLY a JSON object keyed by ticker, e.g. {"AAPL": "Hold — 3% from target"}'
                )
                client  = _ant.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
                message = client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=250,
                    messages=[{"role": "user", "content": prompt}],
                )
                guidance = _j.loads(message.content[0].text.strip())
            except Exception as exc:
                print(f"[portfolio] Guidance fetch failed (non-critical): {exc}")

        # ── Exposure summary ──────────────────────────────────────────────────
        total_alloc  = sum(float(t.get("allocation") or 0) for t in unique_trades)
        stock_alloc  = sum(float(t.get("allocation") or 0) for t in unique_trades
                          if t.get("asset_type") == "stock")
        crypto_alloc = sum(float(t.get("allocation") or 0) for t in unique_trades
                          if t.get("asset_type") == "crypto")

        total_pnl    = 0.0
        priced_count = 0
        for t in unique_trades:
            current = prices.get(t["ticker"])
            entry   = t.get("entry_price")
            alloc   = t.get("allocation")
            if current and entry and alloc:
                total_pnl += (float(current) - float(entry)) / float(entry) * float(alloc)
                priced_count += 1

        n = len(unique_trades)
        lines = [f"<b>📂 Portfolio</b>  <i>· {n} holding{'s' if n != 1 else ''}</i>"]

        # Deployment line — only show if any allocation data exists
        if total_alloc > 0:
            deploy_parts = []
            if stock_alloc  > 0: deploy_parts.append(f"stocks ${stock_alloc:,.0f}")
            if crypto_alloc > 0: deploy_parts.append(f"crypto ${crypto_alloc:,.0f}")
            breakdown = "  ·  " + " / ".join(deploy_parts) if deploy_parts else ""
            lines.append(f"💼 <b>${total_alloc:,.0f}</b> deployed{breakdown}")

        # P&L line — only if we have live prices for at least one position
        if priced_count > 0:
            pnl_sign  = "+" if total_pnl >= 0 else ""
            pnl_emoji = "📈" if total_pnl >= 0 else "📉"
            pnl_pct   = f"  ({pnl_sign}{total_pnl / total_alloc * 100:.1f}%)" if total_alloc > 0 else ""
            lines.append(f"{pnl_emoji} Unrealized P&amp;L: <b>{pnl_sign}${abs(total_pnl):,.2f}</b>{pnl_pct}")

        if concentration_warn:
            lines.append(concentration_warn)

        lines.append("")

        for t in unique_trades:
            ticker  = t["ticker"]
            current = prices.get(ticker)
            entry   = t.get("entry_price")
            target  = t.get("target_price")
            stop    = t.get("stop_loss")

            if current:
                # Alert badges
                stop_hit   = stop   and current <= float(stop)
                near_stop  = stop   and not stop_hit and current <= float(stop) * 1.03
                near_tgt   = target and current >= float(target) * 0.97
                if stop_hit:
                    badge = "  🔴 <b>STOP HIT</b>"
                elif near_stop:
                    badge = "  ⚠️ <b>NEAR STOP</b>"
                elif near_tgt:
                    badge = "  🎯 <b>NEAR TARGET</b>"
                else:
                    badge = ""

                lines.append(f"<b>{ticker}</b>  <code>${_p(current)}</code>{badge}")

                # Pick levels line (only if we have them)
                if entry or target or stop:
                    level_parts = []
                    if entry:
                        to_entry = (current - float(entry)) / float(entry) * 100
                        sign     = "+" if to_entry >= 0 else ""
                        level_parts.append(f"entry <code>${_p(entry)}</code> ({sign}{to_entry:.1f}%)")
                    if target:
                        to_tgt = (float(target) / current - 1) * 100
                        level_parts.append(f"target <code>${_p(target)}</code> ({to_tgt:+.1f}%)")
                    if stop:
                        to_stp = (float(stop) / current - 1) * 100
                        level_parts.append(f"stop <code>${_p(stop)}</code> ({to_stp:+.1f}%)")
                    lines.append("   " + "  ·  ".join(level_parts))
                else:
                    lines.append("   <i>No pick levels — add via today's picks</i>")

                if ticker in guidance:
                    lines.append(f"   💡 <i>{_esc(guidance[ticker])}</i>")
                if ticker in earnings_map:
                    lines.append(f"   📅 <i>Earnings {earnings_map[ticker]} — consider position size</i>")
            else:
                lines.append(f"<b>{ticker}</b>  <i>price unavailable</i>")
                if ticker in earnings_map:
                    lines.append(f"   📅 <i>Earnings {earnings_map[ticker]} — consider position size</i>")

            lines.append("")

        lines.append("<i>Tap a button to remove a holding, or /bought to add one</i>")
        send_message("\n".join(lines), chat_id=chat_id)

        # ── Quick-remove buttons — one per holding ────────────────────────────
        buttons = []
        for t in unique_trades:
            ticker = t["ticker"]
            buttons.append([{
                "text":          f"🗑 Remove {ticker}",
                "callback_data": f"confirm_sell|{ticker}",
            }])
        if buttons:
            send_inline_keyboard(
                "Remove a holding from your portfolio:",
                buttons,
                chat_id=chat_id,
            )
        return ""

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
