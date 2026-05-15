"""
cmd_trades.py — Real-money trade commands.

Handles: BOUGHT, SOLD, PAPER CANCEL, PAPER HISTORY, HISTORY, SUMMARY,
         UPDATESTOP, UPDATETARGET, POSITIONS, PORTFOLIO
"""

import json
import threading

from telegram_api import send_message, send_inline_keyboard
from formatters import _p, _esc
from config_manager import (
    load_user_trade_log,
    load_user_paper,
    save_pending_state,
    load_pending_state,
)
from cmd_helpers import (
    _is_number,
    _fetch_live_price,
    _resolve_ticker_candidates,
    _resolve_ticker_and_price,
    _CRYPTO_SYMBOLS,
    _get_client,
)
from cmd_trade_exec import _execute_bought, _execute_sold, _execute_update_level, _send_chart
from cmd_settings import _prompt_for_param, _send_settings_panel
from cmd_nlp import _nl_parse_trade, _nl_extract_tickers_list


def _cmd_trades(text: str, original: str, chat_id: str) -> "str | None":
    """Real-money trade commands."""
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
                    _yf_sym = f"{ticker}-USD" if ticker in _CRYPTO_SYMBOLS else ticker
                    try:
                        _fi  = yf.Ticker(_yf_sym).fast_info
                        _pr  = getattr(_fi, "last_price", None) or getattr(_fi, "regular_market_price", None)
                        if _pr:
                            prices[ticker] = round(float(_pr), 2)
                            continue
                    except Exception:
                        pass
                    try:
                        _hist = yf.Ticker(_yf_sym).history(period="1d", interval="1m")
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
                # Everything except last token = ticker/name, last token = price
                new_price    = float(parts[-1].replace(",", ""))
                ticker_input = " ".join(parts[:-1])
                _upd_cands   = _resolve_ticker_candidates(ticker_input)
                ticker       = _upd_cands[0]["ticker"].upper() if _upd_cands else parts[0].upper()
                return _execute_update_level(ticker, _field, new_price, chat_id)
            elif len(parts) == 1:
                # Got ticker/name only — resolve then prompt for price
                _upd_cands = _resolve_ticker_candidates(parts[0])
                ticker     = _upd_cands[0]["ticker"].upper() if _upd_cands else parts[0].upper()
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
            ticker    = t["ticker"]
            yf_symbol = f"{ticker}-USD" if ticker in _CRYPTO_SYMBOLS else ticker
            try:
                fi    = yf.Ticker(yf_symbol).fast_info
                price = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
                if price:
                    prices[ticker] = round(float(price), 2)
                    continue
            except Exception:
                pass
            try:
                hist = yf.Ticker(yf_symbol).history(period="1d", interval="1m")
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
                prompt = (
                    "You are a brief trading advisor. For each position give ONE short action line "
                    "(max 10 words): HOLD / WATCH / TAKE PROFIT / NEAR STOP — ACT / etc. "
                    "Be direct. Consider proximity to target/stop.\n\n"
                    f"Positions: {json.dumps(position_data)}\n\n"
                    'Return ONLY a JSON object keyed by ticker, e.g. {"AAPL": "Hold — 3% from target"}'
                )
                client  = _get_client()
                message = client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=250,
                    messages=[{"role": "user", "content": prompt}],
                )
                guidance = json.loads(message.content[0].text.strip())
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

    return None
