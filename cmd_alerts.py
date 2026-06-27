"""
cmd_alerts.py — Price alert commands extracted from bot_commands.py.
"""
from __future__ import annotations

from telegram_api import send_message, send_inline_keyboard
from config_manager import save_pending_state
from formatters import _p
from cmd_helpers import _is_number, _nl_param, _resolve_ticker_candidates
from cmd_nlp import _nl_parse_trade
from cmd_settings import _prompt_for_param


def _cmd_alerts(text: str, original: str, chat_id: str) -> "str | None":
    """Price alert commands."""
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
            price = float(price_str.replace(",", ""))   # parse error here → NL fallback
            try:
                return add_alert(chat_id, ticker, price, direction)
            except ValueError as _dup:
                return f"⚠️ {_dup}"   # e.g. "Alert already exists" — not a generic error
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
            try:
                return add_alert(chat_id, ticker, float(price_val), direction)
            except ValueError as _dup:
                return f"⚠️ {_dup}"

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
        # Confirmation step — show what we're about to remove before executing
        import urllib.parse as _urlparse
        price_enc = _urlparse.quote(str(price) if price else "", safe="")
        price_label = f" @ ${_p(price)}" if price else " (all levels)"
        send_inline_keyboard(
            f"🔕 <b>Remove alert for {ticker}?</b>{price_label}\n"
            f"<i>You won't receive price notifications for this ticker.</i>",
            [[
                {"text": f"✅ Yes, remove {ticker}", "callback_data": f"unalert_confirm|{ticker}|{price_enc}"},
                {"text": "❌ Cancel",                "callback_data": "cancel_abort"},
            ]],
            chat_id=chat_id,
        )
        return ""

    # ── /alertall — create stop-loss alerts for all open positions with a stop ─
    if text == "ALERTALL":
        from price_alert_manager import add_alert
        from config_manager import load_user_trade_log
        log = load_user_trade_log(chat_id)
        open_trades = log.get("open", [])

        # Only consider positions that have a stop_loss price set
        with_stop = [t for t in open_trades if t.get("stop_loss")]
        if not with_stop:
            return (
                "⚠️ None of your open positions have a stop-loss price set.\n"
                "Use <code>/updatestop TICKER PRICE</code> to set one, "
                "then run /alertall again."
            )

        created, skipped, errors = [], [], []
        for trade in with_stop:
            ticker     = trade["ticker"]
            stop_price = float(trade["stop_loss"])
            try:
                add_alert(chat_id, ticker, stop_price, direction="below")
                created.append((ticker, stop_price))
            except ValueError as exc:
                if "already exists" in str(exc).lower():
                    skipped.append(ticker)
                else:
                    errors.append(f"{ticker}: {exc}")

        lines = []
        if created:
            lines.append(f"🔔 <b>Created {len(created)} stop alert{'s' if len(created) != 1 else ''}:</b>")
            for t, p in created:
                lines.append(f"  📉 <b>{t}</b> → alert below <code>${_p(p)}</code>")
        if skipped:
            lines.append(f"\n⏭ <b>Already set ({len(skipped)}):</b> {', '.join(skipped)}")
        if errors:
            lines.append(f"\n⚠️ Errors: {'; '.join(errors)}")
        if not created and not errors:
            lines.append("✅ All stop-loss alerts are already active — nothing new to create.")

        return "\n".join(lines)

    return None
