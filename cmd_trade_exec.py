"""
cmd_trade_exec.py — Trade execution helpers extracted from bot_commands.py.
"""
from __future__ import annotations

import os
import threading

from datetime import date
from telegram_api import send_message, send_photo, send_inline_keyboard
from config_manager import load_picks, load_user_trade_log, save_user_trade_log
from formatters import _p, _esc
from cmd_helpers import _resolve_ticker_candidates, _fetch_live_price


def _suggest_atr_stop(ticker: str, entry_price: float | None) -> float | None:
    """
    Compute a 1.5× ATR (14-period) stop suggestion using yfinance.
    Returns the suggested stop price or None if unavailable.
    """
    try:
        import yfinance as yf
        import numpy as np
        hist = yf.Ticker(ticker).history(period="30d", interval="1d")
        if hist.empty or len(hist) < 5:
            return None
        high = hist["High"].values
        low  = hist["Low"].values
        close= hist["Close"].values
        # True Range = max(H-L, |H-prev_C|, |L-prev_C|)
        tr = [max(high[i] - low[i],
                  abs(high[i] - close[i-1]),
                  abs(low[i]  - close[i-1]))
              for i in range(1, len(close))]
        atr = float(np.mean(tr[-14:]))
        ref = float(entry_price) if entry_price else float(close[-1])
        return round(ref - 1.5 * atr, 2)
    except Exception:
        return None


def _offer_setstop_if_needed(ticker: str, chat_id: str) -> None:
    """
    After logging a buy, send a follow-up stop-loss prompt if no stop is set.
    If ATR can be computed, suggests a specific stop price on the button.
    Called from bought_confirm callback, _handle_pending_reply, and cmd_trades.
    """
    try:
        log   = load_user_trade_log(chat_id)
        trade = next((t for t in log.get("open", []) if t["ticker"] == ticker.upper()), None)
        if trade and not trade.get("stop_loss"):
            entry = trade.get("entry_price")
            atr_stop = _suggest_atr_stop(ticker.upper(), entry)

            if atr_stop and atr_stop > 0:
                stop_label = f"📉 Set Stop @ ${_p(atr_stop)} (1.5× ATR)"
                # Auto-set callback sends the price directly to updatestop flow
                set_cb   = f"setstop_use|{ticker.upper()}|{atr_stop}"
                other_cb = f"setstop_prompt|{ticker.upper()}"
                msg = (
                    f"💡 No stop loss set for <b>{ticker}</b>.\n"
                    f"Based on recent volatility, a stop at <b>${_p(atr_stop)}</b> "
                    f"(1.5× ATR) would give the position room to breathe while limiting downside."
                )
                buttons = [
                    [{"text": stop_label,                  "callback_data": set_cb},
                     {"text": "✏️ Set a different price",  "callback_data": other_cb},
                     {"text": "Skip",                      "callback_data": f"setstop_skip|{ticker.upper()}"}],
                ]
            else:
                msg     = f"💡 No stop loss set for <b>{ticker}</b>. Want to add one to protect your position?"
                buttons = [[{"text": "📉 Set Stop Loss", "callback_data": f"setstop_prompt|{ticker.upper()}"},
                            {"text": "Skip",             "callback_data": f"setstop_skip|{ticker.upper()}"}]]

            send_inline_keyboard(msg, buttons, chat_id=chat_id)
    except Exception:
        pass


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
                picks.get("stocks",      {}).get("short_term", []) +
                picks.get("stocks",      {}).get("long_term",  []) +
                picks.get("crypto",      {}).get("short_term", []) +
                picks.get("etfs",        {}).get("short_term", []) +
                picks.get("etfs",        {}).get("long_term",  []) +
                picks.get("commodities", {}).get("short_term", []) +
                picks.get("commodities", {}).get("long_term",  [])
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

    # Validate ticker is real before saving
    try:
        from market_data import validate_ticker
        if not validate_ticker(ticker):
            return (f"❌ <b>{ticker}</b> isn't a recognised symbol — check the ticker and try again.\n"
                    f"<i>Example: <code>/bought AAPL</code> or <code>/bought BTC</code></i>")
    except Exception:
        pass  # fail open — never block a user from logging a trade over a network error

    picks  = load_picks()
    trade, existed = add_holding(ticker, chat_id, picks=picks)

    if existed:
        return f"📌 <b>{ticker}</b> is already in your portfolio — I'm watching it."

    # If no explicit price and the ticker isn't in today's picks, add_holding
    # leaves entry_price=None — which kills every %-based alert/nudge and $ P&L
    # for this position. Fall back to the live price as the entry (a typed
    # /bought is logged near the actual buy, so live ≈ fill).
    if price is None and not trade.get("entry_price"):
        try:
            from market_data import get_live_price
            live = get_live_price(ticker)
            if live and live > 0:
                price = live
        except Exception:
            pass  # fail open — better to log with no entry than block the user

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

    # Auto-create a stop-loss price alert so the user gets notified if it's hit
    if stop and not existed:
        try:
            from price_alert_manager import add_alert
            add_alert(chat_id, ticker, float(stop), direction="below", auto=True)
        except ValueError:
            pass  # alert already exists — skip silently
        except Exception as exc:
            print(f"[bot] auto-stop alert failed (non-critical): {exc}")

    # Detect first-ever trade for this user (no prior open or closed trades)
    log          = load_user_trade_log(chat_id)
    is_first     = len(log.get("open", [])) == 1 and len(log.get("closed", [])) == 0

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

    if is_first:
        lines.append(
            "\n<b>🎉 First trade logged!</b>\n"
            "When you exit, send <code>/sold " + ticker + "</code> and I'll record your P&amp;L.\n"
            "After your first closed trade, /stats will start showing your win rate and expectancy."
        )

    # ── Auto position-sizing suggestion ──────────────────────────────────────
    # Only show if user has a portfolio_size + risk_per_trade configured and
    # they haven't already specified shares, and we have an entry + stop.
    try:
        from config_manager import get_user_config
        u_cfg      = get_user_config(chat_id)
        port_cfg   = u_cfg.get("portfolio", {}) if isinstance(u_cfg.get("portfolio"), dict) else {}
        cap        = port_cfg.get("portfolio_size")
        risk_pct   = port_cfg.get("risk_per_trade_pct", 1.0)
        t_shares   = trade.get("shares")  # already set by user?
        if cap and entry and stop and not t_shares:
            risk_usd    = float(cap) * float(risk_pct) / 100
            risk_per_sh = abs(float(entry) - float(stop))
            if risk_per_sh > 0:
                suggested   = int(risk_usd / risk_per_sh)
                if suggested > 0:
                    total_cost  = round(suggested * float(entry), 2)
                    lines.append(
                        f"\n💡 <b>Position sizing</b> (based on your {risk_pct}% risk/trade rule)\n"
                        f"Suggested: <b>{suggested} shares</b> "
                        f"→ risks <code>${risk_usd:,.0f}</code> if stop is hit "
                        f"(total cost ≈ <code>${total_cost:,.0f}</code>)\n"
                        f"<i>Use <code>/bought {ticker} {_p(entry)} {suggested}</code> to re-log with shares.</i>"
                    )
    except Exception:
        pass  # never break the bought flow over a suggestion

    # ── Sector concentration warning ─────────────────────────────────────────
    # Warn if this new position pushes a sector over 35% of open trades.
    try:
        import yfinance as _yf
        info        = _yf.Ticker(ticker).info
        new_sector  = (info.get("sector") or "").strip()
        if new_sector:
            open_trades  = log.get("open", [])
            all_tickers  = [t["ticker"] for t in open_trades if t.get("ticker") and t["ticker"] != ticker]
            sector_counts: dict = {}
            for tk in all_tickers:
                try:
                    s = (_yf.Ticker(tk).info.get("sector") or "").strip()
                    if s:
                        sector_counts[s] = sector_counts.get(s, 0) + 1
                except Exception:
                    pass
            # Count new position in
            sector_counts[new_sector] = sector_counts.get(new_sector, 0) + 1
            total_pos = len(all_tickers) + 1
            if total_pos >= 3:   # only meaningful with ≥3 positions
                for sec, cnt in sector_counts.items():
                    pct = cnt / total_pos * 100
                    if pct > 35:
                        lines.append(
                            f"\n⚠️ <b>Concentration alert:</b> adding <b>{ticker}</b> puts "
                            f"<b>{cnt}/{total_pos}</b> of your open positions in "
                            f"<b>{_esc(sec)}</b> ({pct:.0f}%).\n"
                            f"<i>High sector concentration amplifies risk if the sector pulls back.</i>"
                        )
                        break
    except Exception:
        pass  # never break the bought flow over a sector lookup

    # ── Earnings proximity warning ────────────────────────────────────────────
    # Warn if this ticker reports earnings within 7 days — buying into earnings
    # carries extra volatility risk the user should be aware of.
    try:
        from earnings_checker import get_upcoming_earnings
        earn_map = get_upcoming_earnings([ticker], days_ahead=7)
        if earn_map:
            earn_date = earn_map[ticker]
            lines.append(
                f"\n⚠️ <b>Earnings Alert:</b> <b>{ticker}</b> reports earnings on "
                f"<b>{earn_date}</b> — within the next 7 days.\n"
                f"<i>Stocks can move sharply on earnings. Consider position sizing carefully "
                f"or waiting until after the report.</i>"
            )
    except Exception:
        pass  # never break the bought flow over an earnings warning

    return "\n".join(lines)


def _execute_add(ticker: str, chat_id: str,
                 add_price: float | None = None,
                 add_shares: float | None = None) -> str:
    """
    Add shares to an existing open position.
    Recalculates weighted-average entry price.
    If position doesn't exist, suggests /bought instead.
    """
    ticker = ticker.upper()
    log    = load_user_trade_log(chat_id)

    for trade in log.get("open", []):
        if trade.get("ticker") != ticker:
            continue

        old_entry  = trade.get("entry_price")
        old_shares = trade.get("shares")

        if add_price is None:
            # Fall back to live price
            try:
                add_price = _fetch_live_price(ticker)
            except Exception:
                pass

        if add_price is None:
            return (
                f"⚠️ Couldn't fetch a price for <b>{ticker}</b>.\n"
                f"Use: <code>/add {ticker} PRICE SHARES</code>"
            )

        add_price_f  = float(add_price)
        add_shares_f = float(add_shares) if add_shares else None

        lines = []

        if old_entry and old_shares and add_shares_f:
            old_e_f  = float(old_entry)
            old_s_f  = float(old_shares)
            new_s_f  = old_s_f + add_shares_f
            new_avg  = round((old_e_f * old_s_f + add_price_f * add_shares_f) / new_s_f, 4)
            trade["entry_price"] = new_avg
            trade["shares"]      = new_s_f
            trade["allocation"]  = round(new_avg * new_s_f, 2)
            lines.append(
                f"✅ <b>{ticker}</b> scaled in: added <b>{add_shares_f:g} shares</b> @ "
                f"<code>${_p(add_price_f)}</code>\n"
                f"New avg entry: <code>${_p(new_avg)}</code>  ·  "
                f"Total: <b>{new_s_f:g} shares</b>"
            )
        elif add_shares_f:
            # No prior shares tracked — just update entry to new price, set shares
            trade["entry_price"] = add_price_f
            trade["shares"]      = add_shares_f
            lines.append(
                f"✅ <b>{ticker}</b> updated: "
                f"<b>{add_shares_f:g} shares</b> @ <code>${_p(add_price_f)}</code>"
            )
        else:
            # No shares provided — just note the add-on price
            lines.append(
                f"✅ <b>{ticker}</b> add-on recorded @ <code>${_p(add_price_f)}</code>\n"
                f"<i>Tip: include share count to track your average entry: "
                f"<code>/add {ticker} {_p(add_price_f)} SHARES</code></i>"
            )

        # vs current stop/target
        stop   = trade.get("stop_loss")
        target = trade.get("target_price")
        new_e  = trade.get("entry_price", add_price_f)
        if stop:
            risk_pct = (float(new_e) - float(stop)) / float(new_e) * 100
            lines.append(f"Risk to stop: <code>{risk_pct:.1f}%</code>  (<code>${_p(stop)}</code>)")
        if target:
            upside = (float(target) - float(new_e)) / float(new_e) * 100
            lines.append(f"Upside to target: <code>{upside:.1f}%</code>  (<code>${_p(target)}</code>)")

        save_user_trade_log(chat_id, log)
        return "\n".join(lines)

    return (
        f"⚠️ <b>{ticker}</b> not found in your open positions.\n"
        f"To open a new position use <code>/bought {ticker}</code>."
    )


def _execute_trim(ticker: str, chat_id: str,
                  trim_shares: float | None = None,
                  trim_price: float | None = None) -> str:
    """
    Partial close — sell trim_shares of an open position.
    Records partial P&L, reduces remaining shares, keeps position open.
    """
    ticker = ticker.upper()
    log    = load_user_trade_log(chat_id)

    for trade in log.get("open", []):
        if trade.get("ticker") != ticker:
            continue

        entry      = trade.get("entry_price")
        cur_shares = trade.get("shares")

        if trim_price is None:
            try:
                trim_price = _fetch_live_price(ticker)
            except Exception:
                pass
        if trim_price is None:
            return (
                f"⚠️ Couldn't fetch a price for <b>{ticker}</b>.\n"
                f"Use: <code>/trim {ticker} SHARES PRICE</code>"
            )

        trim_p = float(trim_price)

        if trim_shares is None:
            if cur_shares:
                trim_shares = round(float(cur_shares) / 2, 0)  # default: sell half
            else:
                return (
                    f"⚠️ Specify how many shares to sell: "
                    f"<code>/trim {ticker} SHARES</code>"
                )

        trim_s = float(trim_shares)

        if cur_shares and trim_s >= float(cur_shares):
            return (
                f"⚠️ That would close the entire position.\n"
                f"Use <code>/sold {ticker}</code> to fully close, or specify fewer shares."
            )

        # Compute partial P&L
        lines = []
        if entry:
            entry_f  = float(entry)
            ret_pct  = (trim_p - entry_f) / entry_f * 100
            pnl_usd  = (trim_p - entry_f) * trim_s
            sign     = "+" if ret_pct >= 0 else ""
            emoji    = "📈" if ret_pct >= 0 else "📉"
            lines.append(
                f"{emoji} <b>{ticker}</b> partial close: sold <b>{trim_s:g} shares</b> "
                f"@ <code>${_p(trim_p)}</code>\n"
                f"P&amp;L on trim: <b>{sign}{ret_pct:.1f}%</b>  ·  "
                f"<b>{sign}${abs(pnl_usd):,.2f}</b>"
            )
            # Log partial P&L into closed trades
            from datetime import date as _date
            partial_record = {
                "ticker":       ticker,
                "entry_price":  entry_f,
                "exit_price":   trim_p,
                "shares":       trim_s,
                "return_pct":   round(ret_pct, 2),
                "gain_usd":     round(pnl_usd, 2),
                "closed_date":  str(_date.today()),
                "partial":      True,
            }
            log.setdefault("closed", []).append(partial_record)
        else:
            lines.append(
                f"✅ <b>{ticker}</b> trimmed: sold <b>{trim_s:g} shares</b> "
                f"@ <code>${_p(trim_p)}</code>"
            )

        # Update remaining position
        if cur_shares:
            remaining = float(cur_shares) - trim_s
            trade["shares"]     = remaining
            trade["allocation"] = round(remaining * float(entry or trim_p), 2) if entry else None
            lines.append(f"Remaining: <b>{remaining:g} shares</b> still open")
        else:
            lines.append(f"<i>Update shares manually if needed: <code>/add {ticker}</code></i>")

        save_user_trade_log(chat_id, log)
        return "\n".join(lines)

    return (
        f"⚠️ <b>{ticker}</b> not found in your open positions.\n"
        f"Use <code>/positions</code> to see your open trades."
    )


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
    _ret_pct  = None  # tracked for post-close debrief
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
                        _ret_pct = ret_pct
                        sign     = "+" if ret_pct >= 0 else ""
                        emoji    = "📈" if ret_pct >= 0 else "📉"
                        qty      = float(t.get("shares") or shares_sold or 0)
                        pnl_usd  = (exit_price - entry) * qty if qty else None
                        pnl_str  = f"  ·  P&amp;L: <b>{sign}${abs(pnl_usd):,.2f}</b>" if pnl_usd else ""
                        pnl_line = f"\n{emoji} Return: <b>{sign}{ret_pct:.1f}%</b>{pnl_str}"
                    break
        except Exception:
            pass

    # ── Tax-awareness note ───────────────────────────────────────────────────
    tax_note = ""
    if price is not None:
        try:
            exit_price_tax = float(str(price).replace(",", ""))
            log_tax = load_user_trade_log(chat_id)
            for t_tax in log_tax.get("open", []):
                if t_tax["ticker"] == ticker:
                    opened_str = t_tax.get("opened_date") or t_tax.get("opened_at", "")
                    if opened_str:
                        try:
                            opened_d = date.fromisoformat(str(opened_str)[:10])
                            days_held = (date.today() - opened_d).days
                            # Determine pnl direction
                            entry_tax = t_tax.get("entry_price")
                            pnl_positive = entry_tax and exit_price_tax > float(entry_tax)
                            pnl_negative = entry_tax and exit_price_tax < float(entry_tax)
                            if pnl_positive:
                                if days_held < 330:
                                    tax_note = (
                                        f"\n\n⚠️ <b>Tax Note:</b> This is a <b>short-term gain</b> "
                                        f"(held {days_held} days). Short-term gains are taxed at your "
                                        f"income rate — typically higher than the 15-20% long-term rate."
                                    )
                                elif days_held < 365:
                                    days_left = 365 - days_held
                                    tax_note = (
                                        f"\n\n💡 <b>Tax Tip:</b> You've held this {days_held} days — "
                                        f"just {days_left} more days would qualify for the <b>lower "
                                        f"long-term capital gains rate</b> (15-20% vs your income rate). "
                                        f"Worth considering if you're not in a rush to sell."
                                    )
                                else:
                                    tax_note = (
                                        f"\n\n✅ <b>Long-term gain</b> — held {days_held} days. "
                                        f"You qualify for the preferential long-term capital gains rate "
                                        f"(0%, 15%, or 20% depending on your income). Nice work holding the course."
                                    )
                            elif pnl_negative:
                                tax_note = (
                                    "\n\n📋 <b>Tax Note:</b> This loss may be deductible against "
                                    "your capital gains. Keep records for your tax filing."
                                )
                        except Exception:
                            pass
                    break
        except Exception:
            pass

    removed = remove_holding(ticker, chat_id)
    if not removed:
        return f"⚠️ <b>{ticker}</b> is not in your portfolio."

    # ── Consecutive loss circuit breaker ────────────────────────────────────
    # After logging a loss, check if the last 3 closed trades are all losses
    # AND the trade before those 3 was a win (fresh streak start, not a re-fire
    # on the 4th or 5th consecutive loss).
    try:
        if _ret_pct is not None and _ret_pct < 0:
            log_after = load_user_trade_log(chat_id)
            recent = [
                t for t in log_after.get("closed", [])
                if not t.get("partial")
            ][-4:]   # last 4 non-partial closed trades

            def _is_loss(t: dict) -> bool:
                g = t.get("gain_usd")
                r = t.get("return_pct")
                if g is not None:
                    return float(g) < 0
                if r is not None:
                    return float(r) < 0
                return False

            if len(recent) >= 3:
                last3 = recent[-3:]
                fourth = recent[-4] if len(recent) >= 4 else None
                all_losses = all(_is_loss(t) for t in last3)
                # Only fire if this is the START of the streak:
                # the 4th-most-recent was a win (or we only have 3 trades total)
                fresh_streak = (fourth is None) or (not _is_loss(fourth))

                if all_losses and fresh_streak:
                    from telegram_api import send_message as _send_msg
                    tickers_str = ", ".join(t.get("ticker", "?") for t in last3)
                    _send_msg(
                        f"⚠️ <b>3 consecutive losses — consider reducing size</b>\n\n"
                        f"Your last 3 closed trades ({tickers_str}) were all losses.\n\n"
                        f"This can happen to any trader — but when it does, it's a signal to:\n"
                        f"  • <b>Cut position size by 25-50%</b> until you find your footing\n"
                        f"  • <b>Review your entry criteria</b> — are you trading with the market regime?\n"
                        f"  • <b>Take a day off</b> if you feel frustrated — revenge trading amplifies losses\n\n"
                        f"<code>/review</code> for an AI analysis of your recent trade patterns · "
                        f"<code>/heatmap</code> to check market conditions",
                        chat_id=chat_id,
                    )
    except Exception:
        pass   # circuit breaker is non-critical — never block the main sold response

    # Build debrief note based on P&L direction
    debrief_note = ""
    if _ret_pct is not None:
        if _ret_pct > 0.5:
            debrief_note = "\n\n💬 <i>Good discipline taking profits. Letting winners run AND knowing when to exit is what separates good traders from great ones.</i>"
        elif _ret_pct < -0.5:
            debrief_note = "\n\n💬 <i>Cutting a losing position before the stop is sometimes the right call — especially if your thesis has changed. Protecting capital is always the priority.</i>"
        else:
            debrief_note = "\n\n💬 <i>Exiting at breakeven is perfectly valid risk management — you protected your capital and kept your options open.</i>"
    return f"✅ <b>{ticker}</b> removed from your portfolio.{pnl_line}{tax_note}{debrief_note}"


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
            trade.pop("_notified_breakeven",  None)
            trade.pop("_notified_trail_15",   None)
            trade.pop("_notified_trail_90",   None)

        save_user_trade_log(chat_id, log)

        label    = "stop-loss" if field == "stop_loss" else "target"
        old_str  = f"${_p(old_val)}" if old_val else "none"
        entry    = trade.get("entry_price")
        vs_entry = ""
        if entry:
            pct = (new_price - float(entry)) / float(entry) * 100
            vs_entry = f"  <i>({'+' if pct >= 0 else ''}{pct:.1f}% vs entry)</i>"

        result = (
            f"✅ <b>{ticker}</b> {label} updated: "
            f"<code>{old_str}</code> → <code>${_p(new_price)}</code>{vs_entry}"
        )

        # ── Risk check when stop is moved ─────────────────────────────────────
        # Warn if the new stop now risks more than the user's risk_per_trade rule.
        if field == "stop_loss":
            try:
                from config_manager import get_user_config
                u_cfg    = get_user_config(chat_id)
                port_cfg = u_cfg.get("portfolio", {}) if isinstance(u_cfg.get("portfolio"), dict) else {}
                cap      = port_cfg.get("portfolio_size")
                risk_pct = float(port_cfg.get("risk_per_trade_pct", 1.0))
                shares   = trade.get("shares")
                if cap and shares and entry:
                    allowed_risk = float(cap) * risk_pct / 100
                    actual_risk  = abs(float(entry) - new_price) * float(shares)
                    if actual_risk > allowed_risk * 1.05:   # 5% grace band
                        over_pct = (actual_risk / allowed_risk - 1) * 100
                        result += (
                            f"\n\n⚠️ <b>Risk alert:</b> this stop risks "
                            f"<code>${actual_risk:,.0f}</code> "
                            f"({over_pct:.0f}% over your {risk_pct:.1f}% rule "
                            f"of <code>${allowed_risk:,.0f}</code>).\n"
                            f"<i>Consider reducing to {int(allowed_risk / max(abs(float(entry) - new_price), 0.01))} shares "
                            f"or tightening your stop.</i>"
                        )
            except Exception:
                pass

        return result

    return f"⚠️ <b>{ticker}</b> not found in your open positions."
