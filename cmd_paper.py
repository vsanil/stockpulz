"""
cmd_paper.py — Paper trading commands extracted from bot_commands.py.
"""

import threading

from telegram_api import send_message, send_inline_keyboard
from config_manager import load_user_paper, save_pending_state
from formatters import _p, _esc
from cmd_helpers import _is_number, _fetch_live_price, _resolve_ticker_candidates
from cmd_nlp import _nl_parse_trade, _nl_extract_tickers_list
from cmd_settings import _prompt_for_param


def _cmd_paper(text: str, original: str, chat_id: str) -> "str | None":
    """Paper trading commands."""
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

        # ── Multi-ticker: "jpm and msft" / "apple, google" / "avery dennison and microsoft"
        # Use Haiku NL extraction so multi-word company names stay intact
        raw_names = _nl_extract_tickers_list(raw)
        # Strip any noise words that snuck in as standalone tokens
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

    return None
