"""
cmd_trades.py — Real-money trade commands.

Handles: BOUGHT, SOLD, PAPER CANCEL, PAPER HISTORY, HISTORY, SUMMARY,
         UPDATESTOP, UPDATETARGET, POSITIONS, PORTFOLIO
"""
from __future__ import annotations

import json
import os
import threading

from telegram_api import send_message, send_inline_keyboard, typing_until_done, send_typing_action
from formatters import _p, _esc
from config_manager import (
    load_user_trade_log,
    mutate_user_trade_log,
    NO_WRITE,
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
from cmd_trade_exec import _execute_bought, _execute_sold, _execute_update_level, _execute_add, _execute_trim, _send_chart, _offer_setstop_if_needed
from cmd_settings import _prompt_for_param, _send_settings_panel
from cmd_nlp import _nl_parse_trade, _nl_extract_tickers_list
from llm_client import strip_fences


def get_missed_picks_message(chat_id: str) -> str:
    """
    Return a formatted 'picks you skipped this week' message for chat_id.
    Used both by /missed command and the Saturday auto-push in agent.py.
    Returns "" if nothing to report (user caught all picks or no data).
    """
    try:
        from config_manager import load_weekly_picks
        weekly = load_weekly_picks()
    except Exception:
        return ""

    if not weekly:
        return ""

    log    = load_user_trade_log(chat_id)
    bought = {t["ticker"].upper() for t in log.get("open", []) + log.get("closed", [])}

    all_picks: list[dict] = []
    for day_picks in weekly.values():
        stocks = day_picks.get("stocks", day_picks)
        for bucket in ("short_term", "long_term"):
            for p in stocks.get(bucket, []):
                tk = p.get("ticker", "").upper()
                if tk and tk not in bought:
                    all_picks.append({"ticker": tk, "entry": p.get("entry_price"),
                                      "target": p.get("target_price"), "thesis": p.get("thesis", "")})

    seen: set = set()
    unique_picks = []
    for p in all_picks:
        if p["ticker"] not in seen:
            seen.add(p["ticker"])
            unique_picks.append(p)

    if not unique_picks:
        return ""

    missed_tickers = [p["ticker"] for p in unique_picks]
    prices: dict = {}
    try:
        import yfinance as _yf
        raw = _yf.download(" ".join(missed_tickers), period="1d",
                            interval="1d", progress=False, auto_adjust=True)
        if not raw.empty:
            close = raw["Close"]
            if hasattr(close, "columns"):
                for tk in missed_tickers:
                    if tk in close.columns:
                        try:
                            prices[tk] = float(close[tk].dropna().iloc[-1])
                        except Exception:
                            pass
            else:
                if missed_tickers:
                    try:
                        prices[missed_tickers[0]] = float(close.dropna().iloc[-1])
                    except Exception:
                        pass
    except Exception:
        pass

    lines = [f"👀 <b>Picks you skipped this week</b>  ({len(unique_picks)} missed)\n"]
    for p in unique_picks[:10]:
        tk    = p["ticker"]
        entry = p["entry"]
        curr  = prices.get(tk)
        if entry and curr:
            move  = (curr - float(entry)) / float(entry) * 100
            emoji = "🟢" if move > 0 else "🔴"
            move_s = f"{move:+.1f}%"
        elif entry:
            emoji  = "⬜"
            move_s = "—"
        else:
            emoji  = "⬜"
            move_s = "—"
        entry_s = f"  <i>entry ${_p(float(entry))}</i>" if entry else ""
        lines.append(f"{emoji} <b>{tk}</b>  {move_s}{entry_s}")

    lines.append("\n<i>These are picks from this week you didn't log.</i>")
    text = "\n".join(lines)
    _app_url = os.environ.get("APP_URL", "").rstrip("/")
    keyboard = []
    if _app_url:
        keyboard = [[
            {"text": "📌 Log a Position", "web_app": {"url": f"{_app_url}/miniapp?tab=portfolio&action=add"}},
            {"text": "📋 Today's Picks",  "web_app": {"url": f"{_app_url}/miniapp?tab=picks"}},
        ]]
    return text, keyboard


def _cmd_trades(text: str, original: str, chat_id: str) -> "str | None":
    """Real-money trade commands."""
    # ── /bought [TICKER|name [price] [shares]] ───────────────────────────────
    # ── /add TICKER [PRICE] [SHARES] — scale into existing position ──────────
    # ── /trim TICKER [SHARES] [PRICE] — partial close ────────────────────────
    if text.startswith("TRIM "):
        import re as _re
        parts  = text[5:].strip().split()
        ticker = parts[0].upper() if parts else ""
        if not ticker:
            return (
                "✂️ <b>Trim a position</b>\n\n"
                "Usage: <code>/trim TICKER [SHARES] [PRICE]</code>\n"
                "e.g. <code>/trim NVDA</code>  — sells half at live price\n"
                "e.g. <code>/trim NVDA 10</code>  — sells 10 shares at live price\n"
                "e.g. <code>/trim NVDA 10 145.50</code>  — sells 10 shares at $145.50\n\n"
                "<i>Logs a partial close. Use /sold to close the full position.</i>"
            )
        shares_arg = None
        price_arg  = None
        nums = [p for p in parts[1:] if _re.match(r"[\d.,]+$", p)]
        if len(nums) >= 1:
            try:
                shares_arg = float(nums[0].replace(",", ""))
            except ValueError:
                pass
        if len(nums) >= 2:
            try:
                price_arg = float(nums[1].replace(",", ""))
            except ValueError:
                pass
        return _execute_trim(ticker, chat_id, shares_arg, price_arg)

    if text == "TRIM":
        return (
            "✂️ <b>Trim a position</b>\n\n"
            "Usage: <code>/trim TICKER [SHARES] [PRICE]</code>\n"
            "e.g. <code>/trim NVDA</code>  — sells half at live price\n"
            "e.g. <code>/trim NVDA 10</code>  — sells 10 shares at live price\n\n"
            "<i>Logs a partial close. Use /sold to close the full position.</i>"
        )

    # ── /add TICKER [PRICE] [SHARES] — scale into existing position ──────────
    if text.startswith("ADD "):
        import re as _re
        parts  = text[4:].strip().split()
        ticker = parts[0].upper() if parts else ""
        if not ticker:
            return (
                "➕ <b>Scale into a position</b>\n\n"
                "Usage: <code>/add TICKER PRICE SHARES</code>\n"
                "e.g. <code>/add NVDA 138.50 10</code>\n\n"
                "<i>Recalculates your average entry across all shares.</i>"
            )
        price_arg  = None
        shares_arg = None
        nums = [p for p in parts[1:] if _re.match(r"[\d.,]+$", p)]
        if len(nums) >= 1:
            try:
                price_arg = float(nums[0].replace(",", ""))
            except ValueError:
                pass
        if len(nums) >= 2:
            try:
                shares_arg = float(nums[1].replace(",", ""))
            except ValueError:
                pass
        return _execute_add(ticker, chat_id, price_arg, shares_arg)

    if text == "ADD":
        return (
            "➕ <b>Scale into a position</b>\n\n"
            "Usage: <code>/add TICKER PRICE SHARES</code>\n"
            "e.g. <code>/add NVDA 138.50 10</code>\n\n"
            "<i>Recalculates your average entry across all shares.</i>"
        )

    # ── /size TICKER [RISK%] — standalone position sizing ────────────────────
    if text.startswith("SIZE ") or text == "SIZE":
        import re as _re
        raw = text[5:].strip() if text.startswith("SIZE ") else ""
        if not raw:
            return (
                "📐 <b>Position size calculator</b>\n\n"
                "Usage: <code>/size TICKER [RISK%]</code>\n"
                "e.g. <code>/size NVDA</code>  — uses your default risk %\n"
                "e.g. <code>/size NVDA 1.5</code>  — override to 1.5% risk\n\n"
                "<i>Calculates shares and allocation based on portfolio size, "
                "risk per trade, and live ATR-based stop.</i>"
            )
        parts  = raw.split()
        ticker = parts[0].upper()
        # Optional custom risk% override
        custom_risk = None
        for p in parts[1:]:
            try:
                custom_risk = float(p.replace("%", ""))
                break
            except ValueError:
                pass

        try:
            from config_manager import get_user_config
            from cmd_trade_exec import _suggest_atr_stop
            u_cfg    = get_user_config(chat_id)
            port_cfg = u_cfg.get("portfolio", {}) if isinstance(u_cfg.get("portfolio"), dict) else {}
            cap      = port_cfg.get("portfolio_size")
            risk_pct = custom_risk if custom_risk is not None else float(port_cfg.get("risk_per_trade_pct", 1.0))

            live = _fetch_live_price(ticker)
            if not live:
                return f"❌ Could not fetch live price for <b>{ticker}</b>. Try again in a moment."

            atr_stop = _suggest_atr_stop(ticker, live)
            stop_str = f"<code>${_p(atr_stop)}</code> (1.5× ATR)" if atr_stop else "<i>unavailable</i>"

            lines = [f"📐 <b>{ticker}</b> position sizing  ·  live <code>${_p(live)}</code>"]

            if cap and atr_stop and atr_stop > 0:
                cap_f        = float(cap)
                risk_dollars = cap_f * risk_pct / 100
                risk_per_sh  = live - atr_stop
                if risk_per_sh > 0:
                    shares    = int(risk_dollars / risk_per_sh)
                    alloc     = round(shares * live, 2)
                    alloc_pct = alloc / cap_f * 100
                    rr_str    = ""
                    lines += [
                        "",
                        f"  Portfolio size:  <code>${cap_f:,.0f}</code>",
                        f"  Risk per trade:  <code>{risk_pct:.1f}%</code>  → <b>${risk_dollars:,.2f}</b>",
                        f"  Suggested stop:  {stop_str}",
                        f"  Risk per share:  <code>${risk_per_sh:.2f}</code>",
                        "",
                        f"  <b>Recommended:</b> <b>{shares} shares</b>  ≈ <code>${alloc:,.2f}</code>  ({alloc_pct:.1f}% of portfolio)",
                        "",
                        f"  <i>Max loss if stop hit: ${risk_dollars:,.2f} ({risk_pct:.1f}%)</i>",
                    ]
                else:
                    lines.append(f"\n  Suggested stop: {stop_str}\n  <i>Stop is above live price — cannot size.</i>")
            else:
                missing = []
                if not cap:
                    missing.append("portfolio size (<code>/settings</code>)")
                if not atr_stop:
                    missing.append("ATR stop (insufficient price history)")
                lines += [
                    f"\n  Suggested stop: {stop_str}",
                    f"\n⚠️ Need {' and '.join(missing)} to calculate shares.",
                ]

            return "\n".join(lines)
        except Exception as exc:
            return f"❌ Size calculation failed: {exc}"

    # ── /pause TICKER — mute nudges for a position (toggle) ─────────────────
    if text.startswith("PAUSE ") or text == "PAUSE":
        raw = text[6:].strip().upper() if text.startswith("PAUSE ") else ""
        if not raw:
            return (
                "🔇 <b>Pause nudges for a position</b>\n\n"
                "Usage: <code>/pause TICKER</code>\n"
                "e.g. <code>/pause NVDA</code>\n\n"
                "Mutes trailing stop nudges, stale alerts, and partial profit prompts "
                "for that position until you run <code>/pause TICKER</code> again to resume."
            )
        ticker = raw.split()[0]

        # A TOGGLE must flip whatever is stored NOW — flipping a stale snapshot
        # can write back the state the user just changed elsewhere.
        def _mut(log):
            log = log or {"open": [], "closed": [], "watchlist": []}
            for trade in log.get("open", []):
                if (trade.get("ticker") or "").upper() == ticker:
                    trade["_paused"] = not trade.get("_paused", False)
                    return log, trade["_paused"]
            return NO_WRITE, None

        paused = mutate_user_trade_log(chat_id, _mut)
        if paused is None:
            return f"❌ No open position found for <b>{ticker}</b>. Check <code>/positions</code>."
        action = "🔇 <b>Nudges paused</b>" if paused else "🔔 <b>Nudges resumed</b>"
        note = (" You won't receive trailing stop, stale, or partial profit alerts for this position."
                if paused else " Nudges are active again.")
        return f"{action} for <b>{ticker}</b>.{note}"

    # ── /import — bulk import CSV trade history ───────────────────────────────
    if text == "IMPORT":
        return (
            "📥 <b>Import trade history</b>\n\n"
            "Paste your CSV text directly after the command:\n"
            "<code>/import TICKER,ENTRY,EXIT,SHARES,ENTRY_DATE,EXIT_DATE</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/import\n"
            "AAPL,150.00,162.50,10,2024-01-05,2024-01-19\n"
            "MSFT,310.00,335.00,5,2024-01-10,2024-01-24\n"
            "TSLA,225.00,210.00,8,2024-02-01,2024-02-15</code>\n\n"
            "Columns: <code>ticker, entry_price, exit_price, shares, entry_date, exit_date</code>\n"
            "Header row is optional and will be ignored.\n"
            "Dates can be <code>YYYY-MM-DD</code> or <code>MM/DD/YYYY</code>."
        )

    if text.startswith("IMPORT\n") or text.startswith("IMPORT "):
        import csv as _csv
        import io as _io

        # everything after "IMPORT" (strip leading newline or space)
        raw_csv = text[6:].lstrip("\n ")
        if not raw_csv.strip():
            return "📥 No CSV data found. Send <code>/import</code> for usage instructions."

        reader = _csv.reader(_io.StringIO(raw_csv))
        imported = 0
        skipped  = 0
        errors   = []
        records  = []   # parsed first; appended to the log in one mutate below

        for row_num, row in enumerate(reader, start=1):
            if not row or not any(c.strip() for c in row):
                continue
            # Skip header row
            first = row[0].strip().upper()
            if first in ("TICKER", "SYMBOL", "STOCK", "ASSET"):
                continue
            if len(row) < 4:
                errors.append(f"Row {row_num}: too few columns ({len(row)})")
                skipped += 1
                continue
            try:
                ticker     = row[0].strip().upper()
                entry_p    = float(row[1].strip().replace(",", ""))
                exit_p     = float(row[2].strip().replace(",", ""))
                shares_v   = float(row[3].strip().replace(",", ""))
                entry_date = row[4].strip() if len(row) > 4 else ""
                exit_date  = row[5].strip() if len(row) > 5 else ""

                # Normalise date format
                def _norm_date(d: str) -> str:
                    import re as _re2
                    if _re2.match(r"\d{4}-\d{2}-\d{2}", d):
                        return d
                    m = _re2.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", d)
                    if m:
                        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
                    return d

                entry_date = _norm_date(entry_date)
                exit_date  = _norm_date(exit_date)

                ret_pct = (exit_p - entry_p) / entry_p * 100 if entry_p else 0
                gain    = (exit_p - entry_p) * shares_v

                record = {
                    "ticker":       ticker,
                    "entry_price":  round(entry_p, 4),
                    "exit_price":   round(exit_p, 4),
                    "shares":       shares_v,
                    "return_pct":   round(ret_pct, 2),
                    "gain_usd":     round(gain, 2),
                    "bought_at":    entry_date,
                    "closed_date":  exit_date,
                    "source":       "import",
                }
                records.append(record)
                imported += 1
            except Exception as exc:
                errors.append(f"Row {row_num}: {exc}")
                skipped += 1

        if imported:
            def _mut(log):
                log = log or {"open": [], "closed": [], "watchlist": []}
                log.setdefault("closed", []).extend(records)
                return log, None
            mutate_user_trade_log(chat_id, _mut)

        msg = f"📥 <b>Import complete</b> — <b>{imported}</b> trade{'s' if imported != 1 else ''} added."
        if skipped:
            msg += f"\n⚠️ {skipped} row{'s' if skipped != 1 else ''} skipped."
        if errors:
            err_detail = "\n".join(f"  • {e}" for e in errors[:5])
            msg += f"\n\n<i>Errors:\n{err_detail}</i>"
        if imported:
            msg += "\n\nRun /stats to see your updated performance."
        return msg

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

        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        _perf_btn = [{"text": "📈 View Performance", "web_app": {"url": f"{_app_url}/miniapp?tab=performance"}}] if _app_url else None
        if buttons:
            all_buttons = buttons + ([_perf_btn] if _perf_btn else [])
            send_inline_keyboard(
                "🗑 <b>Remove a trade entry?</b>\n"
                "<i>Confirmation required before any change is made.</i>",
                all_buttons,
                chat_id=chat_id,
            )
        elif _perf_btn:
            send_inline_keyboard("📈 View your trade history:", [_perf_btn], chat_id=chat_id)
        return ""

    # ── /summary — one-shot portfolio health view ────────────────────────────
    if text == "SUMMARY":
        import yfinance as yf
        from trade_logger import get_performance_stats

        log         = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        with typing_until_done(chat_id):
            stats   = get_performance_stats(chat_id)

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
                    elif target and current > float(target):
                        badge = "  ✅ TARGET EXCEEDED"
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
            raw_upd = text[len(prefix):].strip()
            label   = "stop-loss" if _field == "stop_loss" else "target"

            # First try NL parse (handles "update my apple stop to 170", "nvda 500", etc.)
            _nl_upd = _nl_parse_trade("bought", raw_upd)  # reuse bought parser for ticker+price
            nl_ticker = (_nl_upd.get("ticker") or "").strip(".,;") or None
            nl_price  = _nl_upd.get("price")

            if nl_ticker and nl_price is not None:
                _upd_cands = _resolve_ticker_candidates(nl_ticker)
                ticker     = _upd_cands[0]["ticker"].upper() if _upd_cands else nl_ticker.upper()
                return _execute_update_level(ticker, _field, float(nl_price), chat_id)

            # Fallback: simple space-split — "AAPL 170" or "AAPL" (one token)
            parts = raw_upd.split()
            if len(parts) >= 2 and _is_number(parts[-1]):
                new_price    = float(parts[-1].replace(",", ""))
                ticker_input = " ".join(parts[:-1])
                _upd_cands   = _resolve_ticker_candidates(ticker_input)
                ticker       = _upd_cands[0]["ticker"].upper() if _upd_cands else parts[0].upper()
                return _execute_update_level(ticker, _field, new_price, chat_id)
            elif len(parts) == 1:
                # Got ticker/name only — resolve then prompt for price
                _upd_cands = _resolve_ticker_candidates(parts[0])
                ticker     = _upd_cands[0]["ticker"].upper() if _upd_cands else parts[0].upper()
                save_pending_state(chat_id, _cmd.lower(), step=2, data={"ticker": ticker})
                send_inline_keyboard(
                    f"📝 New {label} price for <b>{ticker}</b>?",
                    [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                    chat_id=chat_id,
                )
                return ""
            else:
                return f"⚠️ Usage: <code>/{_cmd.lower()} TICKER PRICE</code>  e.g. <code>/{_cmd.lower()} NVDA 118</code>"

    # ── /note TICKER text... — save a note on an open position ──────────────
    if text == "NOTE":
        log         = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        if not open_trades:
            return "📭 No open positions. Log a buy with /bought first."
        buttons = [[{"text": t["ticker"], "callback_data": f"note_pick|{t['ticker']}"}]
                   for t in open_trades]
        send_inline_keyboard("📝 <b>Add a note to which position?</b>", buttons, chat_id=chat_id)
        return ""

    if text.startswith("NOTE "):
        parts   = text[5:].strip().split(None, 1)
        if not parts:
            return "⚠️ Usage: <code>/note TICKER Your note here</code>"
        ticker_raw = parts[0].strip(".,;")
        note_text  = parts[1].strip() if len(parts) > 1 else ""
        candidates = _resolve_ticker_candidates(ticker_raw)
        ticker     = candidates[0]["ticker"].upper() if candidates else ticker_raw.upper()
        if not note_text:
            # No note text — prompt
            save_pending_state(chat_id, "note", step=2, data={"ticker": ticker})
            send_inline_keyboard(
                f"📝 What note do you want to add to <b>{ticker}</b>?",
                [[{"text": "❌ Cancel", "callback_data": f"cancel_pending|{chat_id}"}]],
                chat_id=chat_id,
            )
            return ""
        # Save note
        def _mut(log):
            log = log or {"open": [], "closed": [], "watchlist": []}
            for t in log.get("open", []):
                if t["ticker"] == ticker:
                    t["notes"] = note_text[:500]
                    return log, True
            return NO_WRITE, False

        if not mutate_user_trade_log(chat_id, _mut):
            return f"⚠️ <b>{ticker}</b> not found in your open positions."
        return f"📝 Note saved for <b>{ticker}</b>:\n<i>{note_text[:200]}</i>"

    # ── /recap — daily open-position check-in ───────────────────────────────
    if text == "RECAP":
        import yfinance as yf

        log         = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        if not open_trades:
            return "📭 No open positions.\n\nLog a buy with <code>/bought</code> first."

        # Deduplicate by ticker
        seen_r: set  = set()
        unique_r: list = []
        for t in open_trades:
            if t["ticker"] not in seen_r:
                seen_r.add(t["ticker"])
                unique_r.append(t)

        lines_r    = ["📊 <b>POSITION RECAP</b>\n"]
        warnings_r = []

        for t in unique_r:
            ticker = t["ticker"]
            entry  = t.get("entry_price")
            stop   = t.get("stop_loss")
            target = t.get("target_price")
            yf_sym = f"{ticker}-USD" if ticker in _CRYPTO_SYMBOLS else ticker

            current_r = None
            prev_r    = None
            try:
                fi_r      = yf.Ticker(yf_sym).fast_info
                current_r = getattr(fi_r, "last_price", None) or getattr(fi_r, "regular_market_price", None)
                prev_r    = getattr(fi_r, "previous_close", None)
                if current_r: current_r = float(current_r)
                if prev_r:    prev_r    = float(prev_r)
            except Exception:
                pass

            if current_r is None:
                lines_r.append(f"<b>{ticker}</b>  <i>price unavailable</i>")
                continue

            # 24h change
            day_str_r = ""
            if prev_r and prev_r > 0:
                day_pct_r  = (current_r - prev_r) / prev_r * 100
                day_sign_r = "+" if day_pct_r >= 0 else ""
                day_dot_r  = "🟢" if day_pct_r >= 0 else "🔴"
                day_str_r  = f"  {day_dot_r} {day_sign_r}{day_pct_r:.1f}% today"

            # P&L vs entry
            pnl_str_r = ""
            if entry and float(entry) > 0:
                pnl_r  = (current_r - float(entry)) / float(entry) * 100
                sign_r = "+" if pnl_r >= 0 else ""
                pnl_str_r = f"  <i>({sign_r}{pnl_r:.1f}% from entry)</i>"

            # Stop/target proximity badges
            badge_r = ""
            if stop and float(stop) > 0:
                pct_from_stop_r = (current_r - float(stop)) / float(stop) * 100
                if current_r <= float(stop):
                    badge_r = "  🔴 <b>STOP HIT</b>"
                    warnings_r.append(
                        f"🔴 <b>{ticker}</b> has hit its stop at <code>${_p(stop)}</code>! "
                        f"Send <code>/sold {ticker}</code> to exit."
                    )
                elif pct_from_stop_r <= 5:
                    badge_r = f"  ⚠️ {pct_from_stop_r:.1f}% above stop"
                    warnings_r.append(
                        f"⚠️ <b>{ticker}</b> is only {pct_from_stop_r:.1f}% above stop "
                        f"(<code>${_p(stop)}</code>)."
                    )

            if target and float(target) > 0:
                pct_from_tgt_r = (float(target) - current_r) / float(target) * 100
                if current_r >= float(target):
                    badge_r += "  🎯 <b>TARGET HIT</b>"
                elif pct_from_tgt_r <= 5:
                    badge_r += f"  🎯 {pct_from_tgt_r:.1f}% from target"

            lines_r.append(
                f"<b>{ticker}</b>  <code>${_p(current_r)}</code>{day_str_r}{pnl_str_r}{badge_r}"
            )

        if warnings_r:
            lines_r.append("")
            lines_r.append("──────────────────")
            lines_r.extend(warnings_r)

        lines_r.append("")
        lines_r.append("<i>/sold TICKER to close  ·  /updatestop to adjust levels</i>")
        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        if _app_url:
            send_inline_keyboard("\n".join(lines_r), [[{"text": "📊 View Portfolio", "web_app": {"url": f"{_app_url}/miniapp?tab=portfolio"}}]], chat_id=chat_id)
            return ""
        return "\n".join(lines_r)

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
        names_by_ticker:  dict[str, str] = {}

        stock_trades  = [t for t in unique_trades if t.get("asset_type") == "stock"]
        crypto_trades = [t for t in unique_trades if t.get("asset_type") != "stock"]
        stock_tickers = [t["ticker"] for t in stock_trades]

        # Pre-fill crypto names from static map
        try:
            from market_data import get_company_names as _gcn
            _cn = _gcn([t["ticker"] for t in crypto_trades])
            names_by_ticker.update(_cn)
        except Exception:
            pass

        send_typing_action(chat_id)   # fires while yfinance .info calls run
        for t in stock_trades:
            ticker = t["ticker"]
            try:
                info   = yf.Ticker(ticker).info
                price  = (info.get("currentPrice") or info.get("regularMarketPrice")
                          or info.get("previousClose"))
                if price:
                    prices[ticker] = round(float(price), 2)
                sector_by_ticker[ticker] = info.get("sector", "Unknown")
                raw_name = info.get("shortName") or info.get("longName") or ""
                if raw_name:
                    for suffix in (", Inc.", " Inc.", " Corp.", " Corporation", " & Co.", " Co.", " Ltd.", " plc"):
                        if raw_name.endswith(suffix):
                            raw_name = raw_name[:-len(suffix)]
                            break
                    names_by_ticker[ticker] = raw_name[:22]
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
                guidance = json.loads(strip_fences(message.content[0].text))
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
                stop_hit     = stop   and current <= float(stop)
                near_stop    = stop   and not stop_hit and current <= float(stop) * 1.03
                tgt_exceeded = target and current > float(target)
                near_tgt     = target and not tgt_exceeded and current >= float(target) * 0.97
                if stop_hit:
                    badge = "  🔴 <b>STOP HIT</b>"
                elif near_stop:
                    badge = "  ⚠️ <b>NEAR STOP</b>"
                elif tgt_exceeded:
                    badge = "  ✅ <b>TARGET EXCEEDED</b>"
                elif near_tgt:
                    badge = "  🎯 <b>NEAR TARGET</b>"
                else:
                    badge = ""

                cname = names_by_ticker.get(ticker, "")
                name_sfx = f"  <i>· {cname}</i>" if cname else ""
                lines.append(f"<b>{ticker}</b>{name_sfx}  <code>${_p(current)}</code>{badge}")

                # Pick levels line (only if we have them)
                if entry or target or stop:
                    level_parts = []
                    if entry:
                        to_entry = (current - float(entry)) / float(entry) * 100
                        sign     = "+" if to_entry >= 0 else ""
                        level_parts.append(f"entry <code>${_p(entry)}</code> ({sign}{to_entry:.1f}%)")
                    if target:
                        if current >= float(target):
                            level_parts.append(f"target <code>${_p(target)}</code> (✅ exceeded)")
                        else:
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

        lines.append("<i>Use the buttons below to act on any position.</i>")
        send_message("\n".join(lines), chat_id=chat_id)

        # ── Single "Open Dashboard" button — all actions live in the Mini App ──
        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        if _app_url:
            send_inline_keyboard(
                "Tap to edit levels, close positions, add notes, and more.",
                [[{"text": "📊 Open Dashboard", "web_app": {"url": f"{_app_url}/miniapp"}}]],
                chat_id=chat_id,
            )
        return ""

    # ── /export — send closed trades as a CSV document ───────────────────────
    if text == "EXPORT":
        import csv, io          # NB: never re-import `os` here — module-level os
        from telegram_api import _bot_token   # (line 10) is used across this fn;
        # a local `import os` makes `os` function-local → UnboundLocalError at the
        # earlier os.environ reads (/positions, /history, /dashboard, /missed).
        import requests as _req
        log    = load_user_trade_log(chat_id)
        closed = log.get("closed", [])
        if not closed:
            return "📭 No closed trades to export yet."

        # Build CSV in memory
        buf = io.StringIO()
        fields = ["ticker", "asset_type", "opened_date", "closed_date",
                  "entry_price", "closed_price", "shares", "return_pct",
                  "gain_usd", "outcome"]
        writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore",
                                lineterminator="\n")
        writer.writeheader()
        for t in sorted(closed, key=lambda x: x.get("closed_date", "")):
            writer.writerow({f: t.get(f, "") for f in fields})

        csv_bytes = buf.getvalue().encode("utf-8")
        filename  = f"stockpulz_trades_{chat_id}.csv"

        # Send via Telegram sendDocument
        try:
            resp = _req.post(
                f"https://api.telegram.org/bot{_bot_token()}/sendDocument",
                data={"chat_id": chat_id,
                      "caption": f"📊 <b>Trade history export</b> — {len(closed)} closed trades\n"
                                  "<i>Open in Excel or Google Sheets to analyse your performance.</i>",
                      "parse_mode": "HTML"},
                files={"document": (filename, csv_bytes, "text/csv")},
                timeout=15,
            )
            if resp.ok:
                return ""  # document sent — no additional text reply needed
            return f"⚠️ Could not send file: {resp.text[:120]}"
        except Exception as exc:
            return f"⚠️ Export failed: {exc}"

    # ── /week — this week's trades, P&L and win rate ──────────────────────────
    if text == "WEEK":
        import datetime as _dt
        log    = load_user_trade_log(chat_id)
        closed = log.get("closed", [])
        open_t = log.get("open",   [])

        today      = _dt.date.today()
        week_start = today - _dt.timedelta(days=today.weekday())  # Monday
        ws         = week_start.isoformat()

        # Trades opened this week
        opened_wk = [t for t in open_t   if t.get("opened_date", "") >= ws]
        opened_wk += [t for t in closed  if t.get("opened_date", "") >= ws]

        # Trades closed this week
        closed_wk = [t for t in closed if t.get("closed_date", "") >= ws]

        if not opened_wk and not closed_wk:
            return (
                f"📅 <b>This Week</b>  <i>({week_start.strftime('%b %d')} – today)</i>\n\n"
                "No trades opened or closed this week yet.\n"
                "<i>Log one with /bought.</i>"
            )

        lines = [f"📅 <b>This Week</b>  <i>({week_start.strftime('%b %d')} – {today.strftime('%b %d')})</i>\n"]

        # Opened
        if opened_wk:
            lines.append(f"🟢 <b>Opened ({len(opened_wk)})</b>")
            for t in opened_wk:
                entry = t.get("entry_price")
                lines.append(f"  • <b>{t['ticker']}</b>" + (f" @ ${_p(entry)}" if entry else ""))

        # Closed — wins/losses + P&L
        if closed_wk:
            wins     = [t for t in closed_wk if float(t.get("return_pct", 0)) > 0]
            losses   = [t for t in closed_wk if float(t.get("return_pct", 0)) <= 0]
            net_usd  = sum(float(t.get("gain_usd", 0)) for t in closed_wk)
            net_sign = "+" if net_usd >= 0 else ""
            wr       = int(len(wins) / len(closed_wk) * 100)
            best     = max(closed_wk, key=lambda t: float(t.get("return_pct", 0)))
            worst    = min(closed_wk, key=lambda t: float(t.get("return_pct", 0)))

            lines.append(f"\n🔴 <b>Closed ({len(closed_wk)})</b>  ·  {len(wins)}W / {len(losses)}L  ·  {wr}% WR")
            for t in closed_wk:
                ret  = float(t.get("return_pct", 0))
                sign = "+" if ret >= 0 else ""
                icon = "✅" if ret > 0 else "❌"
                lines.append(f"  {icon} <b>{t['ticker']}</b>  {sign}{ret:.1f}%")

            pnl_color = "green" if net_usd >= 0 else "red"
            lines.append(
                f"\n💰 <b>Net P&amp;L:</b>  "
                f"<b>{net_sign}${abs(net_usd):,.2f}</b>\n"
                f"🏆 Best: <b>{best['ticker']}</b> {'+' if float(best.get('return_pct',0))>0 else ''}{float(best.get('return_pct',0)):.1f}%  "
                f"·  Worst: <b>{worst['ticker']}</b> {'+' if float(worst.get('return_pct',0))>0 else ''}{float(worst.get('return_pct',0)):.1f}%"
            )

        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        if _app_url:
            send_inline_keyboard("\n".join(lines), [[{"text": "📈 View Performance", "web_app": {"url": f"{_app_url}/miniapp?tab=performance"}}]], chat_id=chat_id)
            return ""
        return "\n".join(lines)

    # ── /journal — trade journal: closed trades with lesson notes ────────────
    if text == "JOURNAL":
        log    = load_user_trade_log(chat_id)
        closed = log.get("closed", [])

        # Only show trades that have a journal note
        noted  = [t for t in closed if t.get("journal_note")]
        all_ct = len(closed)

        if not noted:
            if all_ct == 0:
                return (
                    "📓 <b>Trade Journal</b>\n\n"
                    "No closed trades yet.\n"
                    "<i>After closing a trade with /sold, you'll be prompted to add a note.</i>"
                )
            return (
                f"📓 <b>Trade Journal</b>\n\n"
                f"You have {all_ct} closed trade(s) but no journal notes yet.\n"
                f"<i>After your next /sold, tap 'Add a note' to start building your journal.</i>"
            )

        lines = [f"📓 <b>Trade Journal</b>  ·  {len(noted)} entr{'y' if len(noted)==1 else 'ies'}\n"]
        for t in reversed(noted):  # most recent first
            tk       = t.get("ticker", "?")
            ret      = t.get("return_pct")
            closed_d = str(t.get("closed_date") or t.get("closed_at", ""))[:10]
            note     = t.get("journal_note", "")
            sign     = "+" if ret and float(ret) >= 0 else ""
            ret_str  = f"  {sign}{float(ret):.1f}%" if ret is not None else ""
            emoji    = "📈" if ret and float(ret) >= 0 else "📉"
            lines.append(
                f"{emoji} <b>{tk}</b>{ret_str}"
                + (f"  <i>{closed_d}</i>" if closed_d else "")
            )
            lines.append(f'<i>&#8220;{_esc(note[:300])}&#8221;</i>\n')

        return "\n".join(lines)

    # ── /mymovers — today's best & worst movers among open positions ──────────
    if text == "MYMOVERS":
        log         = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])
        if not open_trades:
            return (
                "📭 You have no open positions.\n"
                "<i>Log one with /bought to start tracking.</i>"
            )

        tickers = list({t["ticker"] for t in open_trades})
        send_message("📡 <i>Fetching live prices…</i>", chat_id=chat_id)

        prices = {}
        for tk in tickers:
            p = _fetch_live_price(tk)
            if p:
                prices[tk] = p

        if not prices:
            return "⚠️ Could not fetch live prices right now. Try again shortly."

        # Build move list: compare live price vs entry
        moves = []
        for t in open_trades:
            tk    = t["ticker"]
            live  = prices.get(tk)
            entry = t.get("entry_price")
            if not live or not entry:
                continue
            try:
                pct = (float(live) - float(entry)) / float(entry) * 100
                moves.append((tk, float(live), float(entry), pct))
            except Exception:
                continue

        if not moves:
            return "⚠️ Could not calculate moves — missing entry prices for your positions."

        moves.sort(key=lambda x: x[3], reverse=True)

        lines = ["📊 <b>My Movers</b>  ·  live vs entry\n"]
        for i, (tk, live, entry, pct) in enumerate(moves):
            sign  = "+" if pct >= 0 else ""
            if pct >= 3:
                icon = "🚀"
            elif pct >= 1:
                icon = "📈"
            elif pct >= -1:
                icon = "➡️"
            elif pct >= -3:
                icon = "📉"
            else:
                icon = "🔻"
            lines.append(
                f"{icon} <b>{tk}</b>  {sign}{pct:.1f}%  "
                f"<code>${_p(live)}</code>  <i>entry ${_p(entry)}</i>"
            )

        best  = moves[0]
        worst = moves[-1]
        if len(moves) > 1:
            lines.append(
                f"\n🏆 <b>Best:</b> {best[0]} ({'+' if best[3]>=0 else ''}{best[3]:.1f}%)  "
                f"·  <b>Worst:</b> {worst[0]} ({'+' if worst[3]>=0 else ''}{worst[3]:.1f}%)"
            )

        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        if _app_url:
            send_inline_keyboard("\n".join(lines), [[{"text": "📊 View Portfolio", "web_app": {"url": f"{_app_url}/miniapp?tab=portfolio"}}]], chat_id=chat_id)
            return ""
        return "\n".join(lines)

    # ── /card — shareable performance card PNG ───────────────────────────────
    if text == "CARD":
        log    = load_user_trade_log(chat_id)
        closed = log.get("closed", [])
        if not closed:
            return (
                "📊 <b>Performance Card</b>\n\n"
                "No closed trades yet — close a position with /sold first.\n"
                "<i>After your first trade, /card will generate a shareable stats card.</i>"
            )

        send_message("🎨 <i>Generating your performance card…</i>", chat_id=chat_id)

        def _send_card():
            try:
                from card_generator import generate_performance_card
                from telegram_api import send_photo
                img = generate_performance_card(chat_id)
                if img:
                    rets    = [float(t["return_pct"]) for t in closed if t.get("return_pct") is not None]
                    wins    = [r for r in rets if r > 0]
                    wr      = len(wins) / len(rets) * 100 if rets else 0
                    caption = (
                        f"📊 <b>My StockPulz stats</b>\n"
                        f"Win rate: <b>{wr:.0f}%</b>  ·  "
                        f"{len(closed)} trades closed\n"
                        f"<i>Generated by StockPulz</i>"
                    )
                    send_photo(img, caption=caption, chat_id=chat_id)
                else:
                    send_message("⚠️ Could not generate card — try again shortly.", chat_id=chat_id)
            except Exception as exc:
                send_message(f"⚠️ Card generation failed: {exc}", chat_id=chat_id)

        import threading
        threading.Thread(target=_send_card, daemon=True).start()
        return None

    # ── /top — all-time best closed trades ───────────────────────────────────
    if text == "TOP" or text.startswith("TOP "):
        # Optionally: /top 10 — default is 5
        n = 5
        parts = text.split()
        if len(parts) > 1:
            try:
                n = max(1, min(int(parts[1]), 20))
            except ValueError:
                pass

        log    = load_user_trade_log(chat_id)
        closed = log.get("closed", [])
        if not closed:
            return (
                "📭 No closed trades yet.\n"
                "<i>Log your first position with /bought — then /sold to close it.</i>"
            )

        # Build sortable list from closed trades
        scored = []
        for t in closed:
            ret = t.get("return_pct")
            if ret is None:
                continue
            try:
                scored.append(t)
            except Exception:
                continue

        if not scored:
            return "⚠️ No trades with P&L data found."

        # Sort by return_pct descending
        scored.sort(key=lambda x: float(x.get("return_pct", 0)), reverse=True)
        top_n    = scored[:n]
        bottom_n = scored[-n:][::-1]  # worst n, reversed so biggest loss is first

        lines = [f"🏆 <b>Your Top {n} Trades</b>  (all-time)\n"]
        for i, t in enumerate(top_n, 1):
            ret   = float(t.get("return_pct", 0))
            sign  = "+" if ret >= 0 else ""
            tk    = t.get("ticker", "?")
            pnl   = t.get("gain_usd") or t.get("pnl_usd")
            pnl_str = f"  <code>${abs(float(pnl)):,.0f}</code>" if pnl else ""
            closed_d = str(t.get("closed_date") or t.get("closed_at", ""))[:10]
            lines.append(
                f"{i}. 🟢 <b>{tk}</b>  <b>{sign}{ret:.1f}%</b>{pnl_str}"
                + (f"  <i>{closed_d}</i>" if closed_d else "")
            )

        if len(scored) > n:
            lines.append(f"\n📉 <b>Toughest {min(n, len(bottom_n))} Trades</b>\n")
            for i, t in enumerate(bottom_n[:n], 1):
                ret   = float(t.get("return_pct", 0))
                sign  = "+" if ret >= 0 else ""
                tk    = t.get("ticker", "?")
                pnl   = t.get("gain_usd") or t.get("pnl_usd")
                pnl_str = f"  <code>${abs(float(pnl)):,.0f}</code>" if pnl else ""
                closed_d = str(t.get("closed_date") or t.get("closed_at", ""))[:10]
                lines.append(
                    f"{i}. 🔴 <b>{tk}</b>  <b>{sign}{ret:.1f}%</b>{pnl_str}"
                    + (f"  <i>{closed_d}</i>" if closed_d else "")
                )

        # Summary stats
        all_rets = [float(t.get("return_pct", 0)) for t in scored]
        wins     = [r for r in all_rets if r > 0]
        losses   = [r for r in all_rets if r < 0]
        win_rate = len(wins) / len(all_rets) * 100 if all_rets else 0
        avg_win  = sum(wins) / len(wins)   if wins   else 0
        avg_loss = sum(losses) / len(losses) if losses else 0
        lines.append(
            f"\n<i>{len(scored)} closed trades  ·  "
            f"{win_rate:.0f}% win rate  ·  "
            f"avg win {avg_win:+.1f}%  ·  avg loss {avg_loss:+.1f}%</i>"
        )

        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        if _app_url:
            send_inline_keyboard("\n".join(lines), [[{"text": "📈 View Performance", "web_app": {"url": f"{_app_url}/miniapp?tab=performance"}}]], chat_id=chat_id)
            return ""
        return "\n".join(lines)

    # ── /goal — annual return goal progress bar ──────────────────────────────
    if text == "GOAL" or text.startswith("GOAL "):
        from config_manager import get_user_config, update_user_config
        from datetime import date as _date
        import re as _re

        arg = text[5:].strip() if text.startswith("GOAL ") else ""

        # Setting a new goal: /goal 25 or /goal 25%
        if arg:
            num_match = _re.search(r"(\d+(?:\.\d+)?)", arg)
            if num_match:
                goal_pct = float(num_match.group(1))
                update_user_config(chat_id, "goal_return_pct", goal_pct)
                update_user_config(chat_id, "goal_year",       _date.today().year)
                return (
                    f"🎯 Goal set: <b>+{goal_pct:.0f}%</b> return in {_date.today().year}.\n"
                    f"<i>I'll track your progress here. Send /goal anytime to check in.</i>"
                )
            return "⚠️ Usage: <code>/goal 25</code> to set a 25% annual return target."

        # Show progress
        cfg      = get_user_config(chat_id)
        goal_pct = cfg.get("goal_return_pct")
        goal_yr  = cfg.get("goal_year", _date.today().year)

        if not goal_pct:
            return (
                "🎯 <b>Annual Return Goal</b>\n\n"
                "You haven't set a goal yet.\n"
                "Use <code>/goal 25</code> to set a +25% annual return target.\n\n"
                "<i>I'll track your closed P&amp;L against it throughout the year.</i>"
            )

        # Compute progress from closed trades this year
        log      = load_user_trade_log(chat_id)
        closed   = log.get("closed", [])
        cap      = float(cfg.get("portfolio", {}).get("portfolio_size") or 10_000) \
                   if isinstance(cfg.get("portfolio"), dict) else 10_000

        year_pnl = 0.0
        yr_str   = str(goal_yr)
        for t in closed:
            cd = str(t.get("closed_date") or t.get("closed_at", ""))
            if cd.startswith(yr_str):
                try:
                    year_pnl += float(t.get("gain_usd") or t.get("pnl_usd") or 0)
                except Exception:
                    pass

        goal_usd    = cap * float(goal_pct) / 100
        progress    = min(year_pnl / goal_usd, 1.0) if goal_usd else 0
        pct_done    = progress * 100
        actual_pct  = year_pnl / cap * 100 if cap else 0

        # Progress bar: 20 chars
        filled = int(progress * 20)
        bar    = "█" * filled + "░" * (20 - filled)

        # Days remaining in year
        today     = _date.today()
        year_end  = _date(goal_yr, 12, 31)
        days_left = max((year_end - today).days, 0)
        pace_note = ""
        if days_left > 0 and today.year == goal_yr:
            day_of_year  = today.timetuple().tm_yday
            expected_pct = day_of_year / 365 * 100
            if pct_done >= expected_pct:
                pace_note = "  ✅ <i>ahead of pace</i>"
            else:
                pace_note = "  ⚠️ <i>behind pace</i>"

        sign = "+" if year_pnl >= 0 else ""
        lines = [
            f"🎯 <b>Goal: +{goal_pct:.0f}% in {goal_yr}</b>\n",
            f"<code>[{bar}]</code>  {pct_done:.0f}%{pace_note}",
            f"Earned: <b>{sign}${year_pnl:,.0f}</b>  ·  Target: <b>${goal_usd:,.0f}</b>",
            f"Return so far: <b>{actual_pct:+.1f}%</b>  ·  {days_left} days left",
        ]
        if pct_done >= 100:
            lines.append("\n🏆 <b>Goal achieved!</b> Consider raising it: <code>/goal NEW%</code>")
        elif year_pnl < 0:
            lines.append(f"\n<i>Currently in the red — need ${goal_usd - year_pnl:,.0f} more to hit goal.</i>")
        else:
            lines.append(f"\n<i>Need ${goal_usd - year_pnl:,.0f} more to hit goal.</i>")

        lines.append("<i>Update goal: <code>/goal 30</code></i>")
        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        if _app_url:
            send_inline_keyboard("\n".join(lines), [[{"text": "📈 View Performance", "web_app": {"url": f"{_app_url}/miniapp?tab=performance"}}]], chat_id=chat_id)
            return ""
        return "\n".join(lines)

    # ── /missed — picks user skipped and their outcome ───────────────────────
    if text == "MISSED":
        try:
            from config_manager import load_weekly_picks
            weekly = load_weekly_picks()
        except Exception:
            weekly = {}

        if not weekly:
            return (
                "📭 No recent picks data available.\n"
                "<i>Weekly picks are stored Mon–Fri; check back after the next morning run.</i>"
            )

        log    = load_user_trade_log(chat_id)
        bought = {t["ticker"].upper() for t in log.get("open", []) + log.get("closed", [])}

        # Collect all picks across all days this week
        all_picks: list[dict] = []
        for day_picks in weekly.values():
            stocks = day_picks.get("stocks", day_picks)
            for bucket in ("short_term", "long_term"):
                for p in stocks.get(bucket, []):
                    tk = p.get("ticker", "").upper()
                    if tk and tk not in bought:
                        all_picks.append({"ticker": tk, "entry": p.get("entry_price"),
                                          "target": p.get("target_price"), "thesis": p.get("thesis", "")})

        # Deduplicate by ticker
        seen: set = set()
        unique_picks = []
        for p in all_picks:
            if p["ticker"] not in seen:
                seen.add(p["ticker"])
                unique_picks.append(p)

        if not unique_picks:
            return "✅ You didn't miss any picks this week — you caught them all!"

        # Fetch live prices for each missed pick
        missed_tickers = [p["ticker"] for p in unique_picks]
        prices: dict = {}
        try:
            import yfinance as _yf
            raw = _yf.download(" ".join(missed_tickers), period="1d",
                                interval="1d", progress=False, auto_adjust=True)
            if not raw.empty:
                close = raw["Close"]
                if hasattr(close, "columns"):
                    for tk in missed_tickers:
                        if tk in close.columns:
                            try:
                                prices[tk] = float(close[tk].dropna().iloc[-1])
                            except Exception:
                                pass
                else:
                    if missed_tickers:
                        try:
                            prices[missed_tickers[0]] = float(close.dropna().iloc[-1])
                        except Exception:
                            pass
        except Exception:
            pass

        lines = [f"👀 <b>Picks you skipped this week</b>  ({len(unique_picks)} missed)\n"]
        for p in unique_picks[:10]:
            tk     = p["ticker"]
            entry  = p["entry"]
            curr   = prices.get(tk)
            if entry and curr:
                move   = (curr - float(entry)) / float(entry) * 100
                emoji  = "🟢" if move > 0 else "🔴"
                move_s = f"{move:+.1f}%"
            elif entry:
                emoji  = "⬜"
                move_s = "—"
            else:
                emoji  = "⬜"
                move_s = "—"
            entry_s = f"  <i>entry ${_p(float(entry))}</i>" if entry else ""
            lines.append(f"{emoji} <b>{tk}</b>  {move_s}{entry_s}")

        lines.append("\n<i>These are picks from this week you didn't log.</i>")
        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        _kb = []
        if _app_url:
            _kb = [[
                {"text": "📌 Log a Position", "web_app": {"url": f"{_app_url}/miniapp?tab=portfolio&action=add"}},
                {"text": "📋 Today's Picks",  "web_app": {"url": f"{_app_url}/miniapp?tab=picks"}},
            ]]
        send_inline_keyboard("\n".join(lines), _kb, chat_id=chat_id)
        return ""

    # ── /best-setup — win rate analysis by sector / asset type / timeframe ──────
    if text == "BESTSETUP":
        log    = load_user_trade_log(chat_id)
        closed = [t for t in log.get("closed", []) if not t.get("partial")]
        if len(closed) < 5:
            return (
                "🔬 <b>Best Setup Analysis</b>\n\n"
                "You need at least 5 closed trades to find meaningful patterns.\n"
                "<i>Keep trading and check back!</i>"
            )
        # Group by asset_type
        from collections import defaultdict as _dd
        by_type: dict = _dd(list)
        by_tf:   dict = _dd(list)
        for t in closed:
            atype = (t.get("asset_type") or "stock").lower()
            tf    = (t.get("timeframe") or "short_term").lower()
            by_type[atype].append(t)
            by_tf[tf].append(t)

        def _wr(trades: list) -> tuple:
            wins = sum(1 for t in trades if float(t.get("return_pct", 0)) > 0)
            avg  = sum(float(t.get("return_pct", 0)) for t in trades) / len(trades) if trades else 0
            return wins, len(trades), round(avg, 1)

        lines = ["🔬 <b>Your Best Setup Analysis</b>", ""]

        # Asset type breakdown
        if len(by_type) > 1:
            lines.append("<b>By asset type:</b>")
            for atype, trades in sorted(by_type.items(), key=lambda x: _wr(x[1])[0] / _wr(x[1])[1], reverse=True):
                w, n, avg = _wr(trades)
                wr_pct = int(w / n * 100)
                icon = "🟢" if wr_pct >= 55 else ("🟡" if wr_pct >= 40 else "🔴")
                lines.append(f"  {icon} <b>{atype.replace('_', ' ').title()}</b>  {w}/{n}  ({wr_pct}% win rate)  avg {avg:+.1f}%")
            lines.append("")

        # Timeframe breakdown
        tf_labels = {"short_term": "Short-term (ST)", "long_term": "Long-term (LT)"}
        if len(by_tf) > 1:
            lines.append("<b>By timeframe:</b>")
            for tf, trades in sorted(by_tf.items(), key=lambda x: _wr(x[1])[0] / _wr(x[1])[1], reverse=True):
                w, n, avg = _wr(trades)
                wr_pct = int(w / n * 100)
                icon = "🟢" if wr_pct >= 55 else ("🟡" if wr_pct >= 40 else "🔴")
                label = tf_labels.get(tf, tf)
                lines.append(f"  {icon} <b>{label}</b>  {w}/{n}  ({wr_pct}% win rate)  avg {avg:+.1f}%")
            lines.append("")

        # Sector breakdown via yfinance (cached on trade records, fallback to lookup)
        try:
            import yfinance as yf
            by_sector: dict = _dd(list)
            for t in closed:
                sector = t.get("_sector")
                if not sector:
                    try:
                        sector = yf.Ticker(t["ticker"]).info.get("sector", "Unknown")
                        t["_sector"] = sector   # cache on record (not persisted, but useful in this run)
                    except Exception:
                        sector = "Unknown"
                if sector and sector != "Unknown":
                    by_sector[sector].append(t)

            if len(by_sector) >= 2:
                lines.append("<b>By sector</b> (≥3 trades):")
                ranked = sorted(
                    [(s, ts) for s, ts in by_sector.items() if len(ts) >= 3],
                    key=lambda x: _wr(x[1])[0] / _wr(x[1])[1],
                    reverse=True,
                )
                for sector, trades in ranked[:5]:
                    w, n, avg = _wr(trades)
                    wr_pct = int(w / n * 100)
                    icon = "🟢" if wr_pct >= 55 else ("🟡" if wr_pct >= 40 else "🔴")
                    lines.append(f"  {icon} <b>{sector}</b>  {w}/{n}  ({wr_pct}% win rate)  avg {avg:+.1f}%")

                # Edge call-out
                best = ranked[0] if ranked else None
                worst = ranked[-1] if ranked and len(ranked) > 1 else None
                if best and worst and best[0] != worst[0]:
                    bw, bn, _ = _wr(best[1])
                    ww, wn, _ = _wr(worst[1])
                    if int(bw/bn*100) - int(ww/wn*100) >= 15:
                        lines += [
                            "",
                            f"💡 <b>Your edge:</b> {int(bw/bn*100)}% win rate in <b>{best[0]}</b> "
                            f"vs {int(ww/wn*100)}% in {worst[0]}. "
                            f"Stick to your strengths."
                        ]
        except Exception:
            pass

        lines += ["", "<i>Based on your closed trade history · /review for deeper AI coaching</i>"]
        return "\n".join(lines)

    # ── /playbook — personal trading rules ───────────────────────────────────
    if text == "PLAYBOOK" or text.startswith("PLAYBOOK "):
        from config_manager import get_user_config, update_user_config
        u_cfg = get_user_config(chat_id)
        rules = list(u_cfg.get("playbook_rules") or [])

        sub = text[9:].strip() if text.startswith("PLAYBOOK ") else ""

        # /playbook add <rule>
        if sub.upper().startswith("ADD "):
            rule = sub[4:].strip()
            if not rule:
                return "Usage: <code>/playbook add Never hold through earnings</code>"
            if len(rules) >= 8:
                return "📋 You already have 8 rules — the max. Remove one first with <code>/playbook remove N</code>."
            rules.append(rule)
            update_user_config(chat_id, "playbook_rules", rules)
            return f"📋 Rule added: <i>{_esc(rule)}</i>\n\nYou now have {len(rules)} rule{'s' if len(rules) != 1 else ''}. It will surface during /bought and stale nudges."

        # /playbook remove N
        if sub.upper().startswith("REMOVE ") or sub.upper().startswith("DEL "):
            idx_str = sub.split()[-1]
            try:
                idx = int(idx_str) - 1
                if 0 <= idx < len(rules):
                    removed = rules.pop(idx)
                    update_user_config(chat_id, "playbook_rules", rules)
                    return f"📋 Removed: <i>{_esc(removed)}</i>"
                else:
                    return f"❌ No rule #{idx_str}. You have {len(rules)} rule{'s' if len(rules) != 1 else ''}."
            except ValueError:
                return "Usage: <code>/playbook remove 2</code> (use the rule number)"

        # /playbook clear
        if sub.upper() == "CLEAR":
            update_user_config(chat_id, "playbook_rules", [])
            return "📋 All playbook rules cleared."

        # Default: show playbook
        if not rules:
            return (
                "📋 <b>Your Trading Playbook</b>\n\n"
                "No rules yet. Add your personal trading rules — they'll be shown "
                "when you enter trades and during position check-ins.\n\n"
                "<b>Examples:</b>\n"
                "  • <i>Never hold through earnings</i>\n"
                "  • <i>Always take 50% off at 2R</i>\n"
                "  • <i>Only trade with the trend (above 50 EMA)</i>\n\n"
                "Add one: <code>/playbook add Never hold through earnings</code>"
            )
        rule_lines = "\n".join(f"  {i+1}. {_esc(r)}" for i, r in enumerate(rules))
        return (
            f"📋 <b>Your Trading Playbook</b>\n\n"
            f"{rule_lines}\n\n"
            f"<code>/playbook add &lt;rule&gt;</code> · <code>/playbook remove N</code>"
        )

    # ── /remind — ticker reminder in N days ──────────────────────────────────
    if text == "REMIND" or text.startswith("REMIND "):
        from config_manager import get_user_config, update_user_config
        from datetime import date as _date, timedelta as _timedelta

        if text == "REMIND":
            # Show current reminders
            u_cfg = get_user_config(chat_id)
            reminders = list(u_cfg.get("reminders") or [])
            today_str = _date.today().isoformat()
            # Filter expired reminders
            reminders = [r for r in reminders if r.get("date", "") >= today_str]
            update_user_config(chat_id, "reminders", reminders)
            if not reminders:
                return (
                    "⏰ <b>Trade Reminders</b>\n\n"
                    "No active reminders.\n\n"
                    "Add one: <code>/remind NVDA in 3 days</code>\n"
                    "Or by date: <code>/remind NVDA on 2026-06-01</code>"
                )
            lines = ["⏰ <b>Your Trade Reminders</b>\n"]
            for i, r in enumerate(reminders, 1):
                ticker = r.get("ticker", "?")
                d = r.get("date", "")
                note = r.get("note", "")
                note_part = f" — {_esc(note)}" if note else ""
                lines.append(f"  {i}. <b>{ticker}</b> on {d}{note_part}")
            lines.append(f"\n<code>/remind remove N</code> to cancel one")
            return "\n".join(lines)

        arg = text[7:].strip()  # everything after "REMIND "

        # /remind remove N  — cancel a reminder
        if arg.upper().startswith("REMOVE ") or arg.upper().startswith("DEL "):
            idx_str = arg.split()[-1]
            try:
                idx = int(idx_str) - 1
                u_cfg = get_user_config(chat_id)
                reminders = list(u_cfg.get("reminders") or [])
                if 0 <= idx < len(reminders):
                    removed = reminders.pop(idx)
                    update_user_config(chat_id, "reminders", reminders)
                    return f"⏰ Reminder for <b>{removed.get('ticker','?')}</b> on {removed.get('date','')} removed."
                else:
                    return f"❌ No reminder #{idx_str}."
            except ValueError:
                return "Usage: <code>/remind remove 2</code>"

        # Parse: TICKER [in N days | in N weeks | on YYYY-MM-DD] [optional note]
        import re as _re
        reminder_date = None
        ticker = ""
        note = ""

        # "TICKER in N days"
        m = _re.match(r"^([A-Z]{1,6})\s+IN\s+(\d+)\s+(DAY|DAYS|WEEK|WEEKS)", arg, _re.I)
        if m:
            ticker = m.group(1).upper()
            n = int(m.group(2))
            unit = m.group(3).lower()
            delta = n * 7 if "week" in unit else n
            reminder_date = (_date.today() + _timedelta(days=delta)).isoformat()
            note = arg[m.end():].strip().lstrip("-—").strip()

        # "TICKER on YYYY-MM-DD"
        if not reminder_date:
            m = _re.match(r"^([A-Z]{1,6})\s+ON\s+(\d{4}-\d{2}-\d{2})", arg, _re.I)
            if m:
                ticker = m.group(1).upper()
                reminder_date = m.group(2)
                note = arg[m.end():].strip().lstrip("-—").strip()

        if not (ticker and reminder_date):
            return (
                "⏰ <b>Set a Trade Reminder</b>\n\n"
                "Usage:\n"
                "  <code>/remind NVDA in 3 days</code>\n"
                "  <code>/remind TSLA in 2 weeks</code>\n"
                "  <code>/remind AAPL on 2026-06-15</code>\n"
                "  <code>/remind MSFT in 5 days earnings check</code> (with note)"
            )

        today_str = _date.today().isoformat()
        if reminder_date < today_str:
            return "❌ That date is in the past. Please pick a future date."

        u_cfg = get_user_config(chat_id)
        reminders = list(u_cfg.get("reminders") or [])
        reminders.append({"ticker": ticker, "date": reminder_date, "note": note})
        # Keep sorted by date, cap at 20
        reminders = sorted(reminders, key=lambda x: x.get("date", ""))[:20]
        update_user_config(chat_id, "reminders", reminders)

        note_part = f"\n\U0001f4dd Note: <i>{_esc(note)}</i>" if note else ""
        return (
            f"⏰ Reminder set!\n\n"
            f"<b>{ticker}</b> — {reminder_date}{note_part}\n\n"
            f"I'll send you a push notification on that day.\n"
            f"See all reminders: <code>/remind</code>"
        )

    # ── /quiethours — enable/disable quiet hours and set window ─────────────
    if text == "QUIETHOURS" or text.startswith("QUIETHOURS "):
        from config_manager import get_user_config, update_user_config
        import re as _re

        arg = text[12:].strip() if text.startswith("QUIETHOURS ") else ""

        # Show current status when no argument
        if not arg:
            cfg      = get_user_config(chat_id)
            enabled  = cfg.get("quiet_hours_enabled", False)
            q_from   = cfg.get("quiet_from", "22:00")
            q_to     = cfg.get("quiet_to",   "07:00")
            status   = "🔕 <b>ON</b>" if enabled else "🔔 <b>OFF</b>"
            return (
                f"🌙 <b>Quiet Hours</b>\n\n"
                f"Status: {status}\n"
                f"Window: <code>{q_from}</code> → <code>{q_to}</code> ET\n\n"
                f"<b>Commands:</b>\n"
                f"  <code>/quiethours on</code> — enable\n"
                f"  <code>/quiethours off</code> — disable\n"
                f"  <code>/quiethours 22:00 07:00</code> — set from/to times (ET)\n\n"
                f"<i>During quiet hours, non-urgent position alerts are suppressed.</i>"
            )

        arg_lower = arg.lower()

        if arg_lower == "on":
            update_user_config(chat_id, "quiet_hours_enabled", True)
            cfg    = get_user_config(chat_id)
            q_from = cfg.get("quiet_from", "22:00")
            q_to   = cfg.get("quiet_to",   "07:00")
            return (
                f"🔕 Quiet hours <b>enabled</b>.\n"
                f"Window: <code>{q_from}</code> → <code>{q_to}</code> ET\n\n"
                f"<i>To change the window: <code>/quiethours HH:MM HH:MM</code></i>"
            )

        if arg_lower == "off":
            update_user_config(chat_id, "quiet_hours_enabled", False)
            return "🔔 Quiet hours <b>disabled</b>. You'll receive all alerts."

        # Parse "HH:MM HH:MM"
        time_match = _re.match(r"^(\d{1,2}:\d{2})\s+(\d{1,2}:\d{2})$", arg)
        if time_match:
            def _valid_time(s: str) -> bool:
                try:
                    h, m = s.split(":")
                    return 0 <= int(h) <= 23 and 0 <= int(m) <= 59
                except Exception:
                    return False

            from_t = time_match.group(1).zfill(5)  # e.g. "9:00" → "09:00"
            to_t   = time_match.group(2).zfill(5)
            if not (_valid_time(from_t) and _valid_time(to_t)):
                return "❌ Invalid times. Use 24-hour format, e.g. <code>/quiethours 22:00 07:00</code>"

            update_user_config(chat_id, "quiet_from", from_t)
            update_user_config(chat_id, "quiet_to",   to_t)
            update_user_config(chat_id, "quiet_hours_enabled", True)
            return (
                f"🔕 Quiet hours set and <b>enabled</b>.\n"
                f"Window: <code>{from_t}</code> → <code>{to_t}</code> ET\n\n"
                f"<i>Disable anytime with <code>/quiethours off</code></i>"
            )

        return (
            "❓ Unknown quiet hours command.\n\n"
            "Usage:\n"
            "  <code>/quiethours</code> — show status\n"
            "  <code>/quiethours on</code> / <code>off</code>\n"
            "  <code>/quiethours 22:00 07:00</code> — set window (ET)"
        )

    # ── /review — AI critique of recent trades ────────────────────────────────
    if text == "REVIEW":
        log    = load_user_trade_log(chat_id)
        closed = log.get("closed", [])
        if len(closed) < 3:
            return (
                "📊 <b>Trade Review</b>\n\n"
                "You need at least 3 closed trades for a pattern analysis.\n"
                "<i>Close more positions with /sold to unlock this.</i>"
            )

        send_message("🔍 <i>Analyzing your recent trades…</i>", chat_id=chat_id)

        def _run_review():
            try:
                recent = sorted(
                    closed,
                    key=lambda t: str(t.get("closed_date") or t.get("closed_at", "")),
                    reverse=True
                )[:10]

                trade_lines = []
                for t in recent:
                    tk    = t.get("ticker", "?")
                    ret   = t.get("return_pct")
                    held  = t.get("held_days") or t.get("days_held")
                    entry = t.get("entry_price")
                    stop  = t.get("stop_loss")
                    tgt   = t.get("target_price")
                    note  = t.get("journal_note", "")
                    line  = f"{tk}: {'+' if ret and float(ret)>=0 else ''}{float(ret):.1f}%" if ret else f"{tk}: P&L unknown"
                    if held:    line += f", held {held}d"
                    if entry and stop:
                        risk_pct = abs(float(entry) - float(stop)) / float(entry) * 100
                        line += f", risk {risk_pct:.1f}%"
                    if entry and tgt:
                        rr = abs(float(tgt) - float(entry)) / max(abs(float(entry) - float(stop or entry)), 0.01)
                        line += f", R:R {rr:.1f}"
                    if note:    line += f", note: {note[:80]}"
                    trade_lines.append(line)

                prompt = (
                    "You are a candid, experienced trading coach. "
                    "Review these recent trades and give ONE specific, actionable observation "
                    "about a pattern you notice — could be holding time, risk sizing, cutting losses too early, "
                    "letting losers run, chasing entries, or anything concrete. "
                    "Be direct and honest. 2-3 sentences max. No generic advice.\n\n"
                    "Trades (most recent first):\n" + "\n".join(trade_lines)
                )

                client = _get_client()
                resp   = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=250,
                    messages=[{"role": "user", "content": prompt}],
                )
                insight = resp.content[0].text.strip()

                wins    = [t for t in recent if t.get("return_pct") and float(t["return_pct"]) > 0]
                losses  = [t for t in recent if t.get("return_pct") and float(t["return_pct"]) <= 0]
                wr      = len(wins) / len(recent) * 100 if recent else 0

                msg = (
                    f"🧠 <b>Your Trade Review</b>  <i>(last {len(recent)} trades)</i>\n\n"
                    f"Win rate: <b>{wr:.0f}%</b>  ·  {len(wins)}W / {len(losses)}L\n\n"
                    f"<b>Coach's take:</b>\n{_esc(insight)}\n\n"
                    f"<i>Use /journal to add notes to individual trades, "
                    f"or /stats for full stats.</i>"
                )
                _app_url = os.environ.get("APP_URL", "").rstrip("/")
                if _app_url:
                    send_inline_keyboard(msg, [[{"text": "📈 View Performance", "web_app": {"url": f"{_app_url}/miniapp?tab=performance"}}]], chat_id=chat_id)
                else:
                    send_message(msg, chat_id=chat_id)
            except Exception as exc:
                send_message(f"⚠️ Review failed: {exc}", chat_id=chat_id)

        import threading as _threading
        _threading.Thread(target=_run_review, daemon=True).start()
        return None

    # ── /since DATE — date-range P&L ─────────────────────────────────────────
    if text == "SINCE" or text.startswith("SINCE "):
        from datetime import datetime, date as _date
        import re as _re

        raw_arg = text[6:].strip() if text.startswith("SINCE ") else ""
        if not raw_arg:
            return (
                "📅 <b>Date-range P&amp;L</b>\n\n"
                "Usage: <code>/since jan</code>  or  <code>/since 2025-01-01</code>\n"
                "Shows your win rate and P&amp;L for trades closed on or after that date."
            )

        # Parse flexible date: "jan", "jan 2025", "2025-01-01", "01/01/2025"
        since_date = None
        raw_lower  = raw_arg.lower().strip()
        month_map  = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        try:
            for abbr, mn in month_map.items():
                if raw_lower.startswith(abbr):
                    yr_match = _re.search(r"\d{4}", raw_lower)
                    yr = int(yr_match.group()) if yr_match else _date.today().year
                    since_date = _date(yr, mn, 1)
                    break
            if not since_date:
                for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d"):
                    try:
                        since_date = datetime.strptime(raw_arg, fmt).date()
                        break
                    except ValueError:
                        pass
        except Exception:
            pass

        if not since_date:
            return f"⚠️ Couldn't parse date: <code>{_esc(raw_arg)}</code>\nTry: <code>/since jan</code> or <code>/since 2025-01-01</code>"

        log    = load_user_trade_log(chat_id)
        closed = log.get("closed", [])

        filtered = []
        for t in closed:
            cd = t.get("closed_date") or t.get("closed_at", "")
            try:
                t_date = datetime.strptime(str(cd)[:10], "%Y-%m-%d").date()
                if t_date >= since_date:
                    filtered.append(t)
            except Exception:
                pass

        if not filtered:
            return (
                f"📅 No closed trades found since <b>{since_date.strftime('%b %d, %Y')}</b>.\n"
                f"<i>Total closed trades in log: {len(closed)}</i>"
            )

        rets      = [float(t["return_pct"]) for t in filtered if t.get("return_pct") is not None]
        pnl_vals  = [float(t.get("gain_usd") or t.get("pnl_usd") or 0) for t in filtered if t.get("gain_usd") or t.get("pnl_usd")]
        wins      = [r for r in rets if r > 0]
        losses    = [r for r in rets if r < 0]
        win_rate  = len(wins) / len(rets) * 100 if rets else 0
        net_pnl   = sum(pnl_vals)
        avg_win   = sum(wins)   / len(wins)   if wins   else 0
        avg_loss  = sum(losses) / len(losses) if losses else 0
        best      = max(rets)  if rets else 0
        worst     = min(rets)  if rets else 0

        pnl_str   = f"  ·  est. <b>{'+'if net_pnl>=0 else ''}${net_pnl:,.0f}</b>" if pnl_vals else ""
        lines = [
            f"📅 <b>Since {since_date.strftime('%b %d, %Y')}</b>\n",
            f"Trades: <b>{len(filtered)}</b>  ·  Win rate: <b>{win_rate:.0f}%</b>{pnl_str}",
        ]
        if rets:
            lines.append(f"Avg win: <b>{avg_win:+.1f}%</b>  ·  Avg loss: <b>{avg_loss:+.1f}%</b>")
            lines.append(f"Best: <b>{best:+.1f}%</b>  ·  Worst: <b>{worst:+.1f}%</b>")

        # List the trades
        lines.append("")
        for t in sorted(filtered, key=lambda x: str(x.get("closed_date") or x.get("closed_at","")), reverse=True)[:15]:
            ret  = float(t["return_pct"]) if t.get("return_pct") is not None else None
            tk   = t.get("ticker", "?")
            cd   = str(t.get("closed_date") or t.get("closed_at", ""))[:10]
            ret_str = f"<b>{ret:+.1f}%</b>" if ret is not None else "—"
            emoji   = "🟢" if ret and ret > 0 else "🔴" if ret and ret < 0 else "⬜"
            lines.append(f"{emoji} <b>{tk}</b>  {ret_str}  <i>{cd}</i>")

        if len(filtered) > 15:
            lines.append(f"<i>…and {len(filtered)-15} more</i>")

        _app_url = os.environ.get("APP_URL", "").rstrip("/")
        if _app_url:
            send_inline_keyboard("\n".join(lines), [[{"text": "📈 View Performance", "web_app": {"url": f"{_app_url}/miniapp?tab=performance"}}]], chat_id=chat_id)
            return ""
        return "\n".join(lines)

    # ── /share [TICKER] — single trade brag card ─────────────────────────────
    if text == "TRADESHARE" or text.startswith("TRADESHARE "):
        arg    = text[11:].strip().upper() if text.startswith("TRADESHARE ") else ""
        log    = load_user_trade_log(chat_id)
        closed = log.get("closed", [])

        if not closed:
            return "📭 No closed trades yet. Close a position with /sold first."

        trade = None
        if arg:
            for t in reversed(closed):
                if t.get("ticker", "").upper() == arg:
                    trade = t
                    break
            if not trade:
                return f"⚠️ No closed trade found for <b>{_esc(arg)}</b>."
        else:
            # Default to best trade (highest return_pct)
            scored = [t for t in closed if t.get("return_pct") is not None]
            if not scored:
                return "⚠️ No trades with P&L data to share."
            trade = max(scored, key=lambda t: float(t.get("return_pct", 0)))

        tk      = trade.get("ticker", "?")
        ret     = trade.get("return_pct")
        ret_str = f"{float(ret):+.1f}%" if ret is not None else ""
        caption = f"📊 <b>{tk}</b>  {ret_str}  via StockPulz"

        send_message("🎨 <i>Generating trade card…</i>", chat_id=chat_id)

        def _send_share(t=trade, cap=caption):
            try:
                from card_generator import generate_trade_card
                from telegram_api import send_photo
                img = generate_trade_card(t)
                if img:
                    send_photo(img, caption=cap, chat_id=chat_id)
                else:
                    send_message("⚠️ Could not generate card.", chat_id=chat_id)
            except Exception as exc:
                send_message(f"⚠️ Card error: {exc}", chat_id=chat_id)

        import threading as _threading
        _threading.Thread(target=_send_share, daemon=True).start()
        return None

    return None
