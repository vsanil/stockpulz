"""
cmd_trade_exec.py — Trade execution helpers extracted from bot_commands.py.
"""

import os
import threading

from telegram_api import send_message, send_photo
from config_manager import load_picks, load_user_trade_log, save_user_trade_log
from formatters import _p, _esc
from cmd_helpers import _resolve_ticker_candidates, _fetch_live_price


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
