"""
cmd_nlp.py — NLP handlers extracted from bot_commands.py.
"""

import json
import threading

from telegram_api import send_message, _chat_id
from config_manager import load_picks, load_user_trade_log, get_allowed_users
from formatters import _esc
from cmd_helpers import _get_client, _resolve_ticker_candidates, _fetch_live_price


def _explain_pick(query: str) -> str:
    """
    Use Claude Haiku to answer a plain-English question about today's picks.
    Fuzzy-matches the query to a specific pick when possible.
    """
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

        # Resolve ticker using AI-backed resolver (handles names, misspellings, tickers)
        candidates = _resolve_ticker_candidates(query)
        guessed_ticker = candidates[0]["ticker"] if candidates else None

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
        client  = _get_client()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=350,
            messages=[{"role": "user", "content": f"{system}\n\n{user_msg}"}],
        )
        answer = message.content[0].text.strip()
        return (
            f"💬 {answer}\n\n"
            f"<i>💡 Unfamiliar with a term? Try /define RSI · /define MACD · /define stop loss</i>"
        )
    except Exception as exc:
        return f"⚠️ Could not generate explanation: {exc}"


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
    # Late import to avoid circular at module load time.
    # bot_commands is fully loaded by the time any user message triggers this function.
    from bot_commands import _parse_and_execute

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
        client  = _get_client()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[{"role": "user", "content": f"{SYSTEM}\n\nInput: {query}"}],
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
        client  = _get_client()
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": f"{SYSTEM}\n\nInput: {raw}"}],
        )
        result = json.loads(message.content[0].text.strip())
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
        client  = _get_client()
        # Use multi-turn pattern: prime the assistant then give the input.
        # More reliable than embedding instructions in user message.
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=120,
            messages=[
                {"role": "user",      "content": SYSTEM},
                {"role": "assistant", "content": "Understood. I will return only valid JSON."},
                {"role": "user",      "content": f"/{command}: {raw}"},
            ],
        )
        raw_text = message.content[0].text.strip()
        print(f"[telegram] NL trade parse raw ({command}): {raw_text!r}")
        # Try direct parse first; fall back to extracting JSON object from text
        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError:
            import re as _re2
            m = _re2.search(r'\{.*\}', raw_text, _re2.DOTALL)
            if m:
                result = json.loads(m.group())
            else:
                raise
        print(f"[telegram] NL trade parse ({command}): {result}")
        return result
    except Exception as exc:
        print(f"[telegram] NL trade parse failed ({exc})")
        return {"ticker": None}
