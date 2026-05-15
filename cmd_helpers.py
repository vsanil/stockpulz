"""
cmd_helpers.py — Pure shared utilities extracted from bot_commands.py.
No imports from other cmd_*.py modules.
"""

import os
import time
import hmac
import hashlib
import json
from telegram_api import send_message
from config_manager import get_allowed_users
from formatters import _esc
from llm_client import _get_client


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


def _fetch_live_price(ticker: str) -> float | None:
    """Fetch the latest price for a ticker via yfinance. Handles crypto via TICKER-USD."""
    import yfinance as _yf
    ticker    = ticker.upper()
    yf_symbol = f"{ticker}-USD" if ticker in _CRYPTO_SYMBOLS else ticker
    # Try fast_info first (faster, no download overhead)
    try:
        fi    = _yf.Ticker(yf_symbol).fast_info
        price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
        if price:
            return float(price)
    except Exception:
        pass
    # Fallback: 1-min history
    try:
        data = _yf.download(yf_symbol, period="1d", interval="1m",
                            progress=False, auto_adjust=True)
        return float(data["Close"].dropna().iloc[-1])
    except Exception:
        return None


def _resolve_ticker_candidates(name_or_ticker: str) -> list[dict]:
    """
    Resolve a company name or ticker to a list of candidate dicts:
      [{"ticker": "AAPL", "name": "Apple Inc"}, ...]
    Returns a single-item list for unambiguous matches, multiple for ambiguous ones.
    """
    import re as _re

    raw = name_or_ticker.strip()

    # Fast path: input is already an uppercase ticker symbol (AAPL, MSFT, BTC, etc.)
    # Only applies when fully uppercase so "apple" / "Tesla" go through Haiku for proper resolution.
    if _re.match(r"^[A-Z.\-]{1,5}$", raw):
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
        client  = _get_client()
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
        candidates = json.loads(raw_json)
        if isinstance(candidates, list) and candidates:
            return candidates
    except Exception as exc:
        print(f"[telegram] _resolve_ticker_candidates failed: {exc}")

    # Last-resort fallback — warn that we're not confident about this ticker
    ticker_guess = raw.upper()
    print(f"[bot] Ticker resolution fallback: storing '{ticker_guess}' as-is — may be invalid")
    return [{"ticker": ticker_guess, "name": ticker_guess}]


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


def _nl_param(command: str, raw: str) -> str:
    """
    Use Claude Haiku to normalize a natural-language parameter for a slash command.
    Only called when the parameter isn't an obvious exact match.

    command="exclude" → returns JSON array of sector names  e.g. '["Energy","Utilities"]'
    command="watch"   → returns JSON array of ticker symbols e.g. '["TSLA","MSFT","BRK-B"]'
    command="risk"    → returns one word: conservative | moderate | aggressive
    """
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
        client  = _get_client()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompts[command]}],
        )
        return message.content[0].text.strip()
    except Exception as exc:
        print(f"[telegram] _nl_param failed for {command!r}: {exc}")
        return raw


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
