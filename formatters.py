"""
formatters.py — Message formatting helpers for the stock advisor bot.

Contains all pure formatting functions (no Telegram API calls, no command parsing).
Imported by telegram_notifier.py, agent.py (via telegram_notifier re-exports), and
any other module that needs to build a message string.
"""

import html
import os
from datetime import date, datetime
import pytz


# ── Shared text helpers ───────────────────────────────────────────────────────

def _esc(text) -> str:
    """HTML-escape dynamic content so <, >, & don't break Telegram's parser."""
    return html.escape(str(text)) if text else ""


def _stars(conviction: int) -> str:
    c = max(1, min(5, int(conviction)))
    return "★" * c + "☆" * (5 - c)


def _p(price) -> str:
    """Format a price cleanly: strip .00 only, commas for thousands."""
    if price is None:
        return "—"
    f = float(price)
    if f >= 1000:
        return f"{f:,.0f}" if f == int(f) else f"{f:,.2f}"
    s = f"{f:.2f}"
    return s[:-3] if s.endswith(".00") else s


def _upside(entry, target) -> str:
    """Return (+X.X%) or (-X.X%) string."""
    try:
        pct  = (float(target) - float(entry)) / float(entry) * 100
        sign = "+" if pct >= 0 else ""
        return f"{sign}{pct:.1f}%"
    except Exception:
        return ""


def _entry_window(entry, stop=None, budget=None,
                  is_long_term: bool = False, is_crypto: bool = False) -> str:
    """
    Return a one-line entry window + position sizing hint for a pick.

    Short-term stock / crypto:
      "⏱ Enter within 2% — skip if above $X  ·  risk $Y/share"
      With budget: "⏱ … → N shares, $Z risk at stop"
    Long-term stock:
      "⏱ Patient entry — up to $X (+3%)"
      With budget: "⏱ … → ~N shares at entry"
    Returns empty string if entry price is missing or invalid.
    """
    if not entry:
        return ""
    try:
        e   = float(entry)
        pct = 0.03 if (is_long_term or is_crypto) else 0.02
        upper     = e * (1 + pct)
        pct_label = "3%" if pct == 0.03 else "2%"

        # ── Position sizing suffix ────────────────────────────────────────────
        sizing = ""
        if is_long_term:
            # LT picks have no stop — just show share count if budget set
            if budget:
                shares = max(1, int(float(budget) / e))
                sizing = f"  ·  <code>${int(budget)}</code> → ~{shares} share{'s' if shares != 1 else ''}"
            return (
                f"⏱ <i>Patient entry — up to <code>${_p(upper)}</code>  "
                f"<b>(+{pct_label})</b>{sizing}</i>"
            )
        else:
            # Short-term: always show risk/share; add share count if budget set
            risk_str = ""
            if stop:
                risk_per_share = round(e - float(stop), 2)
                if risk_per_share > 0:
                    risk_str = f"  ·  risk <b>${_p(risk_per_share)}/share</b>"
                    if budget:
                        shares    = max(1, int(float(budget) / e))
                        total_risk = round(shares * risk_per_share, 2)
                        sizing = (
                            f"  ·  <code>${int(budget)}</code> → {shares} share{'s' if shares != 1 else ''}, "
                            f"${_p(total_risk)} risk at stop"
                        )
                        risk_str = sizing   # replace plain risk_str with full sizing line
            return (
                f"⏱ <i>Enter within {pct_label} — skip if above "
                f"<code>${_p(upper)}</code>{risk_str}</i>"
            )
    except Exception:
        return ""


def _conviction_badge(conviction: int) -> str:
    """Return a subtle signal-strength label for high/low conviction picks."""
    c = max(1, min(5, int(conviction or 3)))
    if c == 5: return " <i>· Highest conviction</i>"
    if c == 4: return " <i>· Strong setup</i>"
    if c <= 2: return " <i>· Low conviction — small position</i>"
    return ""   # 3 stars = no badge (default, not noteworthy)


def _short_company(name: str, max_len: int = 22) -> str:
    """Trim long company names at a word boundary so lines stay compact."""
    if not name:
        return ""
    for suffix in (", Inc.", " Inc.", " Corp.", " Corporation", " & Co.", " Co.", " Ltd.", " plc"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name if len(name) <= max_len else name[:max_len].rsplit(" ", 1)[0] + "…"


# ── Macro narrative helper ────────────────────────────────────────────────────

def _macro_narrative_line(m: dict) -> str:
    """
    Convert raw macro numbers into a brief conversational narrative line for Telegram.
    Returns an empty string if no macro data is available.

    Examples:
      "📊 Market: SPY +1.2% · VIX 17 (calm) · 10Y 4.3%  →  ✅ Bullish backdrop"
      "📊 Market: SPY -0.8% · VIX 23 (elevated) · 10Y 4.5%  →  ⚠️ Stay cautious"
    """
    if not m:
        return ""

    spy_pct = m.get("spy_pct")
    vix     = m.get("vix")
    tnx     = m.get("tnx_yield")

    parts = []
    if spy_pct is not None:
        sign = "+" if spy_pct >= 0 else ""
        parts.append(f"SPY {sign}{spy_pct}%")
    if tnx is not None:
        parts.append(f"10Y {tnx}%")
    if vix is not None:
        # Match market_regime.py thresholds: LOW=15, CAUTION=20, ELEVATED_RISK=22, HIGH=28
        if vix < 15:   vix_desc = "calm"
        elif vix < 20: vix_desc = "steady"
        elif vix < 22: vix_desc = "caution"
        elif vix < 28: vix_desc = "elevated"
        else:          vix_desc = "fearful"
        parts.append(f"VIX {vix} ({vix_desc})")

    if not parts:
        return ""

    # Overall sentiment suffix — aligned with market_regime.py thresholds
    sentiment = ""
    if spy_pct is not None and vix is not None:
        if vix >= 28:
            sentiment = "→ 🔴 High fear — tighten stops"
        elif vix >= 22 and spy_pct >= 0:
            sentiment = "→ ⚠️ VIX elevated — reduce size"
        elif spy_pct >= 0.5 and vix < 20:
            sentiment = "→ ✅ Bullish backdrop"
        elif spy_pct <= -0.5 and vix >= 20:
            sentiment = "→ ⚠️ Risk-off — stay cautious"
        elif spy_pct >= 0:
            sentiment = "→ 🟡 Mild tailwind"
        else:
            sentiment = "→ 🟡 Mixed signals"

    body = "  ·  ".join(parts)
    full = f"{body}  {sentiment}".strip() if sentiment else body
    return f"📊 <b>Market:</b> <i>{full}</i>"


# ── Daily picks message (8 AM morning briefing) ───────────────────────────────

def _ordinal(n: int) -> str:
    """Return '2nd', '3rd', '4th' etc. for streak labels."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd', 'th'][min(n % 10, 4)]}"


def format_daily_message(picks: dict, config: dict,
                         personal_notes: dict | None = None,
                         pick_streaks: dict | None = None,
                         buy_counts: dict | None = None,
                         recent_stats: dict | None = None) -> str:
    """
    Concise daily Telegram message — scannable in under 10 seconds.
    Each pick is 2 lines: prices + thesis. Full detail lives in the mini app.
    """
    today       = date.today().strftime("%a %b %d")
    pick_mode   = config.get("pick_mode", "both")
    show_st     = pick_mode in ("st", "both")
    show_lt     = pick_mode in ("lt", "both")
    show_crypto = config.get("show_crypto", True)

    stocks      = picks.get("stocks", picks)
    crypto      = picks.get("crypto", {})
    etfs        = picks.get("etfs", {})
    commodities = picks.get("commodities", {})

    st_picks    = stocks.get("short_term",   []) if show_st else []
    lt_picks    = stocks.get("long_term",    []) if show_lt else []
    cst_picks   = crypto.get("short_term",   []) if show_crypto else []
    etf_st      = etfs.get("short_term",     []) if show_st else []
    etf_lt      = [{**e, "_lt": True} for e in etfs.get("long_term", [])] if show_lt else []
    etf_picks   = etf_st + etf_lt
    comm_st     = commodities.get("short_term", []) if show_st else []
    comm_lt     = [{**c, "_lt": True} for c in commodities.get("long_term", [])] if show_lt else []
    comm_picks  = comm_st + comm_lt
    options_plays = picks.get("options_plays", [])

    # ── Header ────────────────────────────────────────────────────────────────
    mood = _esc(picks.get("daily_summary", ""))
    mood_emoji = "📈"
    if mood:
        lo = mood.lower()
        if any(w in lo for w in ("bear", "sell", "weak", "crash", "plunge")):
            mood_emoji = "📉"
        elif any(w in lo for w in ("cautious", "volatile", "uncertain", "fear", "risk-off", "elevated")):
            mood_emoji = "⚠️"

    lines = [f"{mood_emoji} <b>{today}</b>"]
    if mood:
        lines.append(f"<i>{mood}</i>")

    macro_line = _macro_narrative_line(picks.get("macro_context", {}))
    if macro_line:
        lines.append(macro_line)

    # Performance bar — only when there's real data
    if recent_stats and recent_stats.get("total"):
        t  = recent_stats["total"]
        spy = recent_stats.get("spy_return")
        _s = lambda x: "+" if x >= 0 else ""
        perf = f"📊 <i>{t['wins']}W/{t['losses']}L · {t['win_rate']}% · avg {_s(t['avg_return'])}{t['avg_return']}%"
        if spy is not None:
            perf += f" · vs SPY {_s(spy)}{spy}%"
        perf += f"  ({recent_stats['days']}d)</i>"
        lines.append(perf)

    # ── Pick row helpers ──────────────────────────────────────────────────────
    def _conv_tag(conviction):
        c = max(1, min(5, int(conviction or 3)))
        if c == 5: return "  <i>· ⭐ highest conviction</i>"
        if c <= 2: return "  <i>· low conviction</i>"
        return ""

    def _iv_warn(pick):
        of = (pick.get("options_flow") or {})
        return "\n  🔴 <i>IV extreme — high crush risk on options</i>" \
               if of.get("iv_label") == "EXTREME" else ""

    def _tline(thesis, catalyst):
        parts = [_esc(thesis)] if thesis else []
        if catalyst:
            parts.append(f"🎯 {_esc(catalyst)}")
        return "  <i>" + "  ·  ".join(parts) + "</i>" if parts else ""

    def _row_st(s):
        t, e, tgt, stop = s.get("ticker",""), s.get("entry_price"), s.get("target_price"), s.get("stop_loss")
        stop_str = f"  ·  stop <code>${_p(stop)}</code>" if stop else ""
        row = (f"<b>{_esc(t)}</b>{_conv_tag(s.get('conviction',3))}  "
               f"<code>${_p(e)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(e,tgt)}</i>{stop_str}")
        tl = _tline(s.get("thesis"), s.get("catalyst"))
        if tl: row += f"\n{tl}"
        row += _iv_warn(s)
        return row

    def _row_lt(s):
        t, e, tgt = s.get("ticker",""), s.get("entry_price"), s.get("target_price")
        hz = f"  ·  <i>{_esc(s.get('horizon',''))}</i>" if s.get("horizon") else ""
        row = (f"<b>{_esc(t)}</b>{_conv_tag(s.get('conviction',3))}  "
               f"<code>${_p(e)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(e,tgt)}</i>{hz}")
        tl = _tline(s.get("thesis"), s.get("catalyst"))
        if tl: row += f"\n{tl}"
        return row

    def _row_crypto(c):
        sym, e, tgt, stop = c.get("symbol",""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss")
        stop_str = f"  ·  stop <code>${_p(stop)}</code>" if stop else ""
        row = (f"<b>{_esc(sym)}</b>{_conv_tag(c.get('conviction',3))}  "
               f"<code>${_p(e)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(e,tgt)}</i>{stop_str}")
        tl = _tline(c.get("thesis"), c.get("catalyst"))
        if tl: row += f"\n{tl}"
        row += _iv_warn(c)
        return row

    def _row_etf(e):
        t, ep, tgt, stop = e.get("ticker",""), e.get("entry_price"), e.get("target_price"), e.get("stop_loss")
        is_lt = e.get("_lt", False)
        stop_str = f"  ·  stop <code>${_p(stop)}</code>" if stop and not is_lt else ""
        hz = f"  ·  <i>{_esc(e.get('horizon',''))}</i>" if is_lt and e.get("horizon") else ""
        row = (f"<b>{_esc(t)}</b>  "
               f"<code>${_p(ep)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(ep,tgt)}</i>{stop_str}{hz}")
        tl = _tline(e.get("thesis"), e.get("catalyst"))
        if tl: row += f"\n{tl}"
        return row

    def _row_commodity(c):
        t, ep, tgt, stop = c.get("ticker",""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss")
        is_lt = c.get("_lt", False)
        stop_str = f"  ·  stop <code>${_p(stop)}</code>" if stop and not is_lt else ""
        row = (f"<b>{_esc(t)}</b>  "
               f"<code>${_p(ep)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(ep,tgt)}</i>{stop_str}")
        tl = _tline(c.get("thesis"), c.get("catalyst"))
        if tl: row += f"\n{tl}"
        return row

    def _row_options(o):
        t, action = o.get("ticker",""), o.get("action","CALL").upper()
        icon = "📈" if action == "CALL" else "📉"
        row = (f"{icon} <b>{_esc(t)}</b>  {_esc(action)}  "
               f"strike <code>${_p(o.get('strike'))}</code>  "
               f"exp <i>{_esc(o.get('expiry',''))}</i>")
        if o.get("note"): row += f"\n  <i>{_esc(o['note'])}</i>"
        return row

    # ── Sections ──────────────────────────────────────────────────────────────
    if st_picks:
        body = "\n\n".join(_row_st(s) for s in st_picks)
        lines += ["", f"<blockquote expandable>📈 <b>STOCKS — SHORT TERM</b>\n\n{body}</blockquote>"]

    if lt_picks:
        body = "\n\n".join(_row_lt(s) for s in lt_picks)
        lines += ["", f"<blockquote expandable>🏦 <b>STOCKS — LONG TERM</b>\n\n{body}</blockquote>"]

    if cst_picks:
        body = "\n\n".join(_row_crypto(c) for c in cst_picks)
        lines += ["", f"<blockquote expandable>🪙 <b>CRYPTO</b>  <i>· high risk</i>\n\n{body}</blockquote>"]

    if etf_picks:
        body = "\n\n".join(_row_etf(e) for e in etf_picks)
        lines += ["", f"<blockquote expandable>📦 <b>ETFs</b>\n\n{body}</blockquote>"]

    if comm_picks:
        body = "\n\n".join(_row_commodity(c) for c in comm_picks)
        lines += ["", f"<blockquote expandable>🛢 <b>COMMODITIES</b>\n\n{body}</blockquote>"]

    if options_plays:
        body = "\n\n".join(_row_options(o) for o in options_plays)
        lines += ["", f"<blockquote expandable>🎯 <b>OPTIONS</b>  <i>· illustrative only</i>\n\n{body}</blockquote>"]

    # ── Footer ────────────────────────────────────────────────────────────────
    lines += [
        "",
        "⚠️ <i>Not financial advice. Open the dashboard for full analysis & charts.</i>",
    ]

    return "\n".join(lines)


# ── Quick-buy keyboard for morning picks ─────────────────────────────────────

def build_picks_keyboard(picks: dict, config: dict | None = None) -> list[list[dict]]:
    """
    Build an inline keyboard for the morning picks message.
    Returns one '✅ Bought TICKER' button per pick (ST stocks, LT stocks, crypto).
    Tapping a button fires the quickbuy callback — no typing needed.
    """
    cfg         = config or {}
    show_crypto = cfg.get("show_crypto", True)

    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})
    etfs   = picks.get("etfs", {})

    def _header(label: str) -> list[dict]:
        return [{"text": label, "callback_data": "noop"}]

    def _pair(ticker: str, asset_type: str) -> list[dict]:
        """Return [Bought, 📊 Chart] for one pick — used to build 2-per-row layouts."""
        return [
            {"text": f"✅ Bought {ticker}", "callback_data": f"quickbuy|{ticker}"},
            {"text": "📊 Chart",            "callback_data": f"chart|{ticker}|{asset_type}"},
        ]

    def _add_section(picks_list: list, get_sym, asset_type: str, header: str, icon: str = ""):
        """
        Append a section header + picks paired 2-per-row.
        Single pick: skip the header row, fold the icon into the button text.
        Odd pick at end: place its Bought + Chart on one row (no empty columns).
        """
        if not picks_list:
            return
        if len(picks_list) == 1:
            # Only 1 pick — no header row, embed icon in button to save vertical space
            ticker = get_sym(picks_list[0])
            pair = _pair(ticker, asset_type)
            if icon:
                pair[0] = {**pair[0], "text": f"{icon} Bought {ticker}"}
            buttons.append(pair)
            return
        buttons.append(_header(header))
        it = iter(picks_list)
        for p in it:
            right = next(it, None)
            if right is None:
                # Last odd pick — buy + chart on one row, no empty columns
                ticker = get_sym(p)
                buttons.append(_pair(ticker, asset_type))
            else:
                buttons.append(_pair(get_sym(p), asset_type) + _pair(get_sym(right), asset_type))

    buttons = []

    # ── Mini App launch button — top, full-width, prominent ───────────────────
    render_url = (os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
    if render_url:
        buttons.append([{
            "text":    "🚀 Open Dashboard  ↗",
            "web_app": {"url": f"{render_url}/miniapp"},
        }])

    st_picks  = [s for s in stocks.get("short_term", []) if s.get("ticker")]
    lt_picks  = [s for s in stocks.get("long_term",  []) if s.get("ticker")]
    cst_picks = [c for c in crypto.get("short_term", []) if c.get("symbol")] if show_crypto else []
    etf_picks = (
        [e for e in etfs.get("short_term", []) if e.get("ticker")] +
        [e for e in etfs.get("long_term",  []) if e.get("ticker")]
    )
    commodities = picks.get("commodities", {})
    comm_picks  = (
        [c for c in commodities.get("short_term", []) if c.get("ticker")] +
        [c for c in commodities.get("long_term",  []) if c.get("ticker")]
    )

    _add_section(st_picks,  lambda s: s["ticker"], "stock",  "── 📈 Short Term ──",  "📈")
    _add_section(lt_picks,  lambda s: s["ticker"], "stock",  "── 🏛 Long Term ──",   "🏛")
    _add_section(cst_picks, lambda c: c["symbol"], "crypto", "── 🪙 Crypto ──",      "🪙")
    _add_section(etf_picks, lambda e: e["ticker"], "stock",  "── 📦 ETFs ──",        "📦")
    _add_section(comm_picks, lambda c: c["ticker"], "stock", "── 🛢 Commodities ──", "🛢")

    return buttons


# ── Confirmation message ──────────────────────────────────────────────────────

def format_confirmation_message(picks: dict, current_prices: dict,
                                buy_counts: dict | None = None) -> str:
    """
    Build the live prices check message.
    Compares entry prices from morning picks to current live prices.
    """
    et     = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    now    = now_et.strftime("%a %b %d · %I:%M %p ET").replace(" 0", " ")
    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})

    bc = buy_counts or {}

    def price_line(symbol: str, entry, target, stop) -> str:
        current = current_prices.get(symbol)
        if current is None or entry is None:
            return f"   <b>{symbol}</b>  price unavailable"
        pct   = (current - float(entry)) / float(entry) * 100
        if abs(pct) < 0.05:
            change_str = "≈0%"
        else:
            arrow      = "▲" if pct >= 0 else "▼"
            change_str = f"{arrow}{abs(pct):.1f}%"
        if stop and current <= float(stop):
            badge = "🔴 Stop hit — consider exit"
        elif pct <= -2:
            badge = "⚠️ Watch — pulling back"
        elif pct < -1:
            badge = "⚠️ Watch"
        elif pct >= 0.5:
            badge = "✅ On track"
        else:
            badge = "🟡 Flat — hold"
        n = bc.get(symbol, 0)
        social = f"  <i>👥 {n}</i>" if n >= 2 else ""
        return (f"   <b>{symbol}</b>  <code>${_p(entry)}</code> → <code>${_p(current)}</code> "
                f"{change_str}  {badge}{social}")

    st  = stocks.get("short_term", [])
    lt  = stocks.get("long_term", [])
    cst = crypto.get("short_term", [])

    lines = [f"<u><b>📊 Live Prices — {now}</b></u>"]

    if st:
        lines += ["", "<b>📈 Short Term</b>"]
        for s in st:
            lines.append(price_line(s.get("ticker", ""), s.get("entry_price"), s.get("target_price"), s.get("stop_loss")))
    if lt:
        lines += ["", "<b>🏦 Long Term</b>"]
        for s in lt:
            lines.append(price_line(s.get("ticker", ""), s.get("entry_price"), s.get("target_price"), None))
    if cst:
        lines += ["", "<b>🪙 Crypto</b>"]
        for c in cst:
            lines.append(price_line(c.get("symbol", ""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss")))

    etfs_d   = picks.get("etfs", {})
    etf_st   = etfs_d.get("short_term", [])
    etf_lt   = etfs_d.get("long_term",  [])
    if etf_st or etf_lt:
        lines += ["", "<b>📦 ETFs</b>"]
        for e in etf_st:
            lines.append(price_line(e.get("ticker", ""), e.get("entry_price"), e.get("target_price"), e.get("stop_loss")))
        for e in etf_lt:
            lines.append(price_line(e.get("ticker", ""), e.get("entry_price"), e.get("target_price"), None))

    comm     = picks.get("commodities", {})
    comm_st  = comm.get("short_term", [])
    comm_lt  = comm.get("long_term",  [])
    if comm_st or comm_lt:
        lines += ["", "<b>🛢 Commodities</b>"]
        for c in comm_st:
            lines.append(price_line(c.get("ticker", ""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss")))
        for c in comm_lt:
            lines.append(price_line(c.get("ticker", ""), c.get("entry_price"), c.get("target_price"), None))

    lines += ["", "🔴 stop hit  ✅ on track  ⚠️ watch  🟡 flat", "<i>⚠️ Not financial advice.</i>  📋 /help  ·  📲 /share  ·  💬 /feedback"]
    return "\n".join(lines)


# ── Weekly recap (Saturday morning) ──────────────────────────────────────────

def format_weekly_recap_message(recap: dict, config: dict | None = None) -> str:
    """
    Compact Saturday recap. recap comes from performance_tracker.build_weekly_recap().
    Keeps it to ~12 lines — wins, avg return vs S&P, best/worst pick.
    Pass config to personalise: respects pick_mode (st/lt/both) so users only see
    sections they've opted into.
    """
    week_end = date.today().strftime("%b %d")

    def _section(label: str, stats: dict | None, spy: float | None = None) -> list[str]:
        if not stats:
            return [f"{label}: no data this week"]
        win_pct    = int(stats["wins"] / stats["count"] * 100)
        avg        = stats["avg_return"]
        sign       = "+" if avg >= 0 else ""
        emoji      = "🟢" if avg > 0 else ("🔴" if avg < -1 else "🟡")
        best_sym,  best_r  = stats["best"]
        worst_sym, worst_r = stats["worst"]
        best_sign  = "+" if best_r  >= 0 else ""
        worst_sign = "+" if worst_r >= 0 else ""
        bench = ""
        if spy is not None:
            vs          = round(avg - spy, 1)
            vs_sign     = "+" if vs  >= 0 else ""
            spy_display = 0.0 if spy == 0 else spy   # collapse -0.0 → 0.0
            spy_sign    = "+" if spy_display >= 0 else ""
            bench = f" vs S&P {spy_sign}{spy_display}% ({vs_sign}{vs}%)"
        return [
            f"<b>{label}</b> — {stats['count']} picks, {win_pct}% wins",
            f"Best: <b>{best_sym}</b> {best_sign}{best_r}%  Worst: <b>{worst_sym}</b> {worst_sign}{worst_r}%",
            f"Avg: {sign}{avg}%{bench} {emoji}",
        ]

    pick_mode    = (config or {}).get("pick_mode", "both")
    show_stocks  = True                                           # stocks always shown in recap
    show_crypto  = (config or {}).get("show_crypto", True)       # respect per-user setting

    lines = [f"<u><b>📅 Week of {week_end} — Recap</b></u>", ""]

    stocks_stats = recap.get("stocks")
    crypto_stats = recap.get("crypto")

    if show_stocks and stocks_stats:
        lines += _section("📈 Stocks", stocks_stats, recap.get("spy_return"))
    elif show_stocks:
        lines += ["📈 Stocks: no data this week"]

    lines += [""]

    if show_crypto and crypto_stats:
        lines += _section("🪙 Crypto", crypto_stats)
    elif show_crypto:
        lines += ["🪙 Crypto: no data this week"]

    # ── Individual pick outcomes ───────────────────────────────────────────────
    pick_outcomes = recap.get("pick_outcomes", [])
    if pick_outcomes:
        lines += ["", "📋 <b>This week's picks:</b>"]
        # Group into rows of 3-4 items
        row_items = []
        for po in pick_outcomes:
            ticker = po.get("ticker", "")
            pct    = po.get("pct", 0)
            icon   = "🟢" if pct > 0 else ("🟡" if pct == 0 else "🔴")
            sign   = "+" if pct >= 0 else ""
            row_items.append(f"{icon} {ticker} {sign}{pct}%")
        # Output 3 per line
        chunk_size = 3
        for i in range(0, len(row_items), chunk_size):
            lines.append("  " + "  ".join(row_items[i:i + chunk_size]))

    lines += [
        "",
        "<i>Entry vs Friday close — not actual trade results.</i>",
        "<i>⚠️ Not financial advice.</i>",
    ]
    return "\n".join(lines)


# ── EOD portfolio summary (3:30 PM close check) ───────────────────────────────

def format_eod_summary(picks: dict, current_prices: dict, open_holdings: list[dict],
                       watchlist: list | None = None) -> str:
    """
    End-of-day snapshot sent at 3:30 PM.
    Shows today's picks with move since entry, plus any user holdings with alerts.
    Always sent — not just when a target/stop hits.
    """
    et     = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    now    = now_et.strftime("%I:%M %p ET").lstrip("0")

    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})

    lines = [f"<u><b>📊 Close Check — {now}</b></u>", ""]

    any_picks = False

    _watchlist = set(watchlist) if watchlist else set()

    def _row(symbol: str, entry, target, stop, label: str = "") -> str:
        current = current_prices.get(symbol)
        if current is None or entry is None:
            return f"  <b>{symbol}</b>  <i>price unavailable</i>"
        pct   = (current - float(entry)) / float(entry) * 100
        emoji = "🟢" if pct >= 1 else ("🔴" if pct <= -1 else "🟡")
        if abs(pct) < 0.05:
            change_str = "≈0%"
        elif pct > 0:
            change_str = f"▲{pct:.1f}%"
        else:
            change_str = f"▼{abs(pct):.1f}%"
        badges = []
        if stop and current <= float(stop):
            badges.append("🔴 STOP HIT")
        elif stop and current <= float(stop) * 1.03:
            badges.append("⚠️ near stop")
        if target and current >= float(target) * 0.97:
            badges.append("🎯 near target")
        badge_str = f"  <i>{' · '.join(badges)}</i>" if badges else ""
        lbl = f"  <i>{label}</i>" if label else ""
        star = "⭐" if symbol in _watchlist else ""
        return (f"  {emoji} {star}<b>{symbol}</b>  <code>${_p(current)}</code>  "
                f"{change_str}  from <code>${_p(entry)}</code>{badge_str}{lbl}")

    etfs = picks.get("etfs", {})
    comm = picks.get("commodities", {})
    st      = stocks.get("short_term", [])
    lt      = stocks.get("long_term",  [])
    cst     = crypto.get("short_term", [])
    clt     = crypto.get("long_term",  [])
    est     = etfs.get("short_term",   [])
    elt     = etfs.get("long_term",    [])
    comm_st = comm.get("short_term",   [])
    comm_lt = comm.get("long_term",    [])

    if st or lt or cst or clt or est or elt or comm_st or comm_lt:
        any_picks = True
        lines.append("<b>Today's picks:</b>")
        for s in st:
            lines.append(_row(s.get("ticker", ""), s.get("entry_price"), s.get("target_price"), s.get("stop_loss")))
        for s in lt:
            lines.append(_row(s.get("ticker", ""), s.get("entry_price"), s.get("target_price"), None, "LT"))
        for c in cst:
            lines.append(_row(c.get("symbol", ""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss"), "🪙"))
        for c in clt:
            lines.append(_row(c.get("symbol", ""), c.get("entry_price"), c.get("target_price"), None, "🪙 LT"))
        for e in est:
            lines.append(_row(e.get("ticker", ""), e.get("entry_price"), e.get("target_price"), e.get("stop_loss"), "ETF"))
        for e in elt:
            lines.append(_row(e.get("ticker", ""), e.get("entry_price"), e.get("target_price"), None, "ETF LT"))
        for c in comm_st:
            lines.append(_row(c.get("ticker", ""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss"), "🛢"))
        for c in comm_lt:
            lines.append(_row(c.get("ticker", ""), c.get("entry_price"), c.get("target_price"), None, "🛢 LT"))

    # User's portfolio holdings not already in picks
    pick_symbols = (
        {s.get("ticker") for s in st + lt} |
        {c.get("symbol") for c in cst + clt} |
        {e.get("ticker") for e in est + elt} |
        {c.get("ticker") for c in comm_st + comm_lt}
    )
    extra = [h for h in open_holdings if h.get("ticker") not in pick_symbols
             and h.get("entry_price") and current_prices.get(h["ticker"])]

    if extra:
        lines.append("")
        lines.append("<b>Your portfolio:</b>")
        for h in extra:
            lines.append(_row(h["ticker"], h.get("entry_price"), h.get("target_price"), h.get("stop_loss")))

    if not any_picks and not extra:
        return ""   # nothing to show

    lines += [
        "",
        "<i>🟢 +1%+  🟡 flat  🔴 −1%+  ·  ⚠️ Not financial advice.</i>  📋 /help  ·  📲 /share  ·  💬 /feedback",
    ]
    return "\n".join(lines)


def format_eod_full_summary(
    picks: dict,
    current_prices: dict,
    open_holdings: list[dict],
    watchlist: list | None = None,
) -> str:
    """
    Rich end-of-day wrap-up sent ~4:15 PM ET after market close.
    Shows final close prices, per-category P&L averages, and an optional
    one-line Haiku-generated commentary on the day.
    """
    import os
    et     = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    date_str = now_et.strftime("%a %b %-d")

    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})
    etfs   = picks.get("etfs",   {})
    comm   = picks.get("commodities", {})
    st      = stocks.get("short_term", [])
    lt      = stocks.get("long_term",  [])
    cst     = crypto.get("short_term", [])
    clt     = crypto.get("long_term",  [])
    est     = etfs.get("short_term",   [])
    elt     = etfs.get("long_term",    [])
    comm_st = comm.get("short_term",   [])
    comm_lt = comm.get("long_term",    [])

    if not any([st, lt, cst, clt, est, elt, comm_st, comm_lt]):
        return ""

    lines = [f"<u><b>📊 End of Day — {date_str}</b></u>", ""]

    _watchlist = set(watchlist) if watchlist else set()

    def _row(symbol, entry, target, stop, label=""):
        price = current_prices.get(symbol)
        if price is None or entry is None:
            return None, f"  <b>{symbol}</b>  <i>price unavailable</i>"
        pct   = (price - float(entry)) / float(entry) * 100
        emoji = "🟢" if pct >= 1 else ("🔴" if pct <= -1 else "🟡")
        chg   = (f"▲{pct:.1f}%" if pct > 0 else (f"▼{abs(pct):.1f}%" if pct < 0 else "≈0%"))
        badges = []
        if stop and price <= float(stop):
            badges.append("stop hit")
        elif target and price >= float(target) * 0.97:
            badges.append("near target 🎯")
        badge_str = f"  <i>({', '.join(badges)})</i>" if badges else ""
        lbl = f"  <i>{label}</i>" if label else ""
        star = "⭐" if symbol in _watchlist else ""
        txt = f"  {emoji} {star}<b>{symbol}</b>  <code>${_p(price)}</code>  {chg}{badge_str}{lbl}"
        return pct, txt

    # ── Short-term stocks ─────────────────────────────────────────────────────
    st_pcts = []
    if st:
        lines.append("<b>📈 Short Term</b>")
        for s in st:
            pct, row = _row(s.get("ticker", ""), s.get("entry_price"),
                            s.get("target_price"), s.get("stop_loss"))
            lines.append(row)
            if pct is not None:
                st_pcts.append(pct)

    # ── Long-term stocks ──────────────────────────────────────────────────────
    lt_pcts = []
    if lt:
        lines.append("<b>🏦 Long Term</b>")
        for s in lt:
            pct, row = _row(s.get("ticker", ""), s.get("entry_price"),
                            s.get("target_price"), None, "hold")
            lines.append(row)
            if pct is not None:
                lt_pcts.append(pct)

    # ── Crypto ────────────────────────────────────────────────────────────────
    if cst or clt:
        lines.append("<b>🪙 Crypto</b>")
        for c in cst:
            _, row = _row(c.get("symbol", ""), c.get("entry_price"),
                          c.get("target_price"), c.get("stop_loss"))
            lines.append(row)
        for c in clt:
            _, row = _row(c.get("symbol", ""), c.get("entry_price"),
                          c.get("target_price"), None, "LT")
            lines.append(row)

    # ── ETFs ─────────────────────────────────────────────────────────────────
    if est or elt:
        lines.append("<b>📦 ETFs</b>")
        for e in est:
            _, row = _row(e.get("ticker", ""), e.get("entry_price"),
                          e.get("target_price"), e.get("stop_loss"))
            lines.append(row)
        for e in elt:
            _, row = _row(e.get("ticker", ""), e.get("entry_price"),
                          e.get("target_price"), None, "LT")
            lines.append(row)

    # ── Commodities ───────────────────────────────────────────────────────────
    if comm_st or comm_lt:
        lines.append("<b>🛢 Commodities</b>")
        for c in comm_st:
            _, row = _row(c.get("ticker", ""), c.get("entry_price"),
                          c.get("target_price"), c.get("stop_loss"))
            lines.append(row)
        for c in comm_lt:
            _, row = _row(c.get("ticker", ""), c.get("entry_price"),
                          c.get("target_price"), None, "LT")
            lines.append(row)

    # ── User holdings not in today's picks ───────────────────────────────────
    pick_syms = (
        {s.get("ticker") for s in st + lt} |
        {c.get("symbol") for c in cst + clt} |
        {e.get("ticker") for e in est + elt} |
        {c.get("ticker") for c in comm_st + comm_lt}
    )
    extra = [h for h in open_holdings if h.get("ticker") not in pick_syms
             and h.get("entry_price") and current_prices.get(h["ticker"])]
    if extra:
        lines.append("")
        lines.append("<b>📂 Your other positions</b>")
        for h in extra:
            _, row = _row(h["ticker"], h.get("entry_price"),
                          h.get("target_price"), h.get("stop_loss"))
            lines.append(row)

    # ── Day averages ──────────────────────────────────────────────────────────
    lines.append("")
    avg_parts = []
    if st_pcts:
        avg = sum(st_pcts) / len(st_pcts)
        avg_parts.append(f"ST avg {'+' if avg >= 0 else ''}{avg:.1f}%")
    if lt_pcts:
        avg = sum(lt_pcts) / len(lt_pcts)
        avg_parts.append(f"LT avg {'+' if avg >= 0 else ''}{avg:.1f}%")
    if avg_parts:
        lines.append(f"<i>{' · '.join(avg_parts)}</i>")

    # ── Haiku one-liner (non-critical) ────────────────────────────────────────
    try:
        import anthropic as _ant
        all_rows = []
        for s in st:
            sym = s.get("ticker", "")
            p = current_prices.get(sym)
            e = s.get("entry_price")
            if p and e:
                all_rows.append(f"{sym}: {(p-float(e))/float(e)*100:+.1f}%")
        for s in lt:
            sym = s.get("ticker", "")
            p = current_prices.get(sym)
            e = s.get("entry_price")
            if p and e:
                all_rows.append(f"{sym}: {(p-float(e))/float(e)*100:+.1f}% (LT)")
        if all_rows:
            prompt = (
                "You are a concise stock market commentator. "
                "Write ONE sentence (max 15 words) summing up today's picks performance. "
                "Be direct, no disclaimers.\n\n"
                f"Today's results: {', '.join(all_rows)}"
            )
            client = _ant.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=60,
                messages=[{"role": "user", "content": prompt}],
            )
            commentary = msg.content[0].text.strip().rstrip(".")
            lines.append(f"💬 <i>{commentary}</i>")
    except Exception:
        pass  # non-critical

    lines += [
        "",
        "<i>Market closed. See you tomorrow.</i>",
        "📋 /help  ·  📲 /share  ·  💬 /feedback  ·  📊 /positions  ·  📜 /history",
    ]
    return "\n".join(lines)


# ── Monday "Week Ahead" block ────────────────────────────────────────────────

def format_week_ahead(earnings_this_week: dict, regime: dict | None = None) -> str:
    """
    Monday-only block appended to the morning message.
    Shows upcoming earnings for held/watchlisted tickers + macro context.

    earnings_this_week: {ticker: date_str, ...}  e.g. {"MSFT": "Wed May 14", "GOOGL": "Thu May 15"}
    regime: output of get_market_regime()
    """
    lines = ["", "─" * 20, "🗓 <b>Week Ahead</b>"]

    if earnings_this_week:
        lines.append("")
        lines.append("<b>Earnings this week:</b>")
        for ticker, date_str in sorted(earnings_this_week.items(), key=lambda x: x[1]):
            lines.append(f"  📢 <b>{ticker}</b> — {date_str}")
        lines.append("<i>Earnings can cause sharp moves — size positions accordingly.</i>")
    else:
        lines.append("<i>No major earnings this week for today's picks.</i>")

    if regime:
        r = regime.get("regime", "neutral")
        vix = regime.get("vix")
        regime_note = {
            "bull":     "📈 Trend is bullish — momentum setups favoured.",
            "bear":     "🐻 Bear regime — stay defensive, smaller sizes.",
            "volatile": "⚡ High volatility — tighten stops, reduce exposure.",
            "neutral":  "🟡 Mixed signals — follow entries, respect stops.",
            "elevated": "⚠️ VIX elevated but uptrend intact — reduce size, favour quality.",
        }.get(r, "")
        if regime_note:
            lines += ["", regime_note]
        if vix:
            if vix >= 28:    _vix_label = "high fear"
            elif vix >= 22:  _vix_label = "elevated risk"
            elif vix >= 20:  _vix_label = "caution"
            else:            _vix_label = "calm"
            lines.append(f"<i>VIX: {vix} — {_vix_label}</i>")

    return "\n".join(lines)
