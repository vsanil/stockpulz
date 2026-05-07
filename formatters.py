"""
formatters.py — Message formatting helpers for the stock advisor bot.

Contains all pure formatting functions (no Telegram API calls, no command parsing).
Imported by telegram_notifier.py, agent.py (via telegram_notifier re-exports), and
any other module that needs to build a message string.
"""

import html
from datetime import date


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
        if vix < 15:   vix_desc = "calm"
        elif vix < 20: vix_desc = "steady"
        elif vix < 25: vix_desc = "elevated"
        else:          vix_desc = "fearful"
        parts.append(f"VIX {vix} ({vix_desc})")

    if not parts:
        return ""

    # Overall sentiment suffix
    sentiment = ""
    if spy_pct is not None and vix is not None:
        if spy_pct >= 0.5 and vix < 20:
            sentiment = "→ ✅ Bullish backdrop"
        elif spy_pct <= -0.5 and vix >= 20:
            sentiment = "→ ⚠️ Risk-off — stay cautious"
        elif vix >= 25:
            sentiment = "→ 🔴 High fear — tighten stops"
        elif spy_pct >= 0:
            sentiment = "→ 🟡 Mild tailwind"
        else:
            sentiment = "→ 🟡 Mixed signals"

    body = "  ·  ".join(parts)
    full = f"{body}  {sentiment}".strip() if sentiment else body
    return f"📊 <b>Market:</b> <i>{full}</i>"


# ── Daily picks message (8 AM morning briefing) ───────────────────────────────

def format_daily_message(picks: dict, config: dict,
                         personal_notes: dict | None = None) -> str:
    """
    Build the formatted daily Telegram message from Claude picks (stocks + crypto).

    personal_notes (optional): dict mapping ticker/symbol → one-line personalised note.
      e.g. {"AAPL": "Balances your tech-heavy portfolio with value", "BTC": "Complements your ETH long"}
      When provided, each pick gets a 💡 line showing why this pick suits the user's portfolio.
    """
    today         = date.today().strftime("%a %b %d, %Y")
    stock_budget  = config.get("stock_budget")
    crypto_budget = config.get("crypto_budget")
    pick_mode     = config.get("pick_mode", "both")

    # Compute equal per-pick amounts
    max_stock_picks  = config.get("max_short_picks", 2) + config.get("max_long_picks", 3)
    max_crypto_picks = config.get("max_crypto_short_picks", 2) + config.get("max_crypto_long_picks", 2)
    per_stock  = round(float(stock_budget)  / max(max_stock_picks,  1), 2) if stock_budget  else None
    per_crypto = round(float(crypto_budget) / max(max_crypto_picks, 1), 2) if crypto_budget else None

    show_st     = pick_mode in ("st", "both")
    show_lt     = pick_mode in ("lt", "both")
    show_crypto = config.get("show_crypto", True)   # per-user crypto on/off

    stocks    = picks.get("stocks", picks)
    crypto    = picks.get("crypto", {})
    st_picks  = stocks.get("short_term", []) if show_st else []
    lt_picks  = stocks.get("long_term", [])  if show_lt else []
    cst_picks = crypto.get("short_term", []) if (show_st and show_crypto) else []
    clt_picks = crypto.get("long_term", [])  if (show_lt and show_crypto) else []

    # Apply per-user pick caps
    max_s = config.get("max_stock_picks")
    max_c = config.get("max_crypto_picks")
    if max_s is not None and max_s > 0:
        if show_st and show_lt:
            n_st = max(1, round(max_s * 0.4))
            n_lt = max(0, max_s - n_st)
        elif show_st:
            n_st, n_lt = max_s, 0
        else:
            n_st, n_lt = 0, max_s
        st_picks = st_picks[:n_st]
        lt_picks = lt_picks[:n_lt]
    if max_c is not None and max_c > 0:
        if show_st and show_lt:
            n_cst = max(1, round(max_c * 0.5))
            n_clt = max(0, max_c - n_cst)
        elif show_st:
            n_cst, n_clt = max_c, 0
        else:
            n_cst, n_clt = 0, max_c
        cst_picks = cst_picks[:n_cst]
        clt_picks = clt_picks[:n_clt]

    # Macro context line — conversational narrative
    m = picks.get("macro_context", {})
    macro_line = _macro_narrative_line(m)

    lines = [
        f"<b>📊 Daily Picks — {today}  <i>· NYSE 9:30 AM ET</i></b>",
        f"<i>{_esc(picks.get('daily_summary', ''))}</i>",
    ]
    if macro_line:
        lines.append(macro_line)

    def _pick_row_st(i, s, personal_note: str = ""):
        entry, target, stop = s.get("entry_price"), s.get("target_price"), s.get("stop_loss")
        earnings_tag  = f"  🗓 {_esc(s['earnings_date'])}" if s.get("earnings_date") else ""
        alloc         = s.get("allocation")
        alloc_str     = f"  <code>${_p(alloc)}</code>" if alloc is not None else ""
        badge         = _conviction_badge(s.get("conviction", 3))
        personal_line = f"\n💡 <i>{_esc(personal_note)}</i>" if personal_note else ""
        return (
            f"<b>{_esc(s.get('ticker'))}</b>  {_stars(s.get('conviction', 3))}{badge}  "
            f"<i>{_esc(_short_company(s.get('company', '')))}</i>{earnings_tag}\n"
            f"<code>${_p(entry)}</code> → <code>${_p(target)}</code>  "
            f"<i>{_upside(entry, target)}</i>  ·  stop <code>${_p(stop)}</code>{alloc_str}\n"
            f"<i>{_esc(s.get('thesis'))}</i>{personal_line}"
        )

    def _pick_row_lt(i, s, personal_note: str = ""):
        entry, target = s.get("entry_price"), s.get("target_price")
        alloc         = s.get("allocation")
        alloc_str     = f"  <code>${_p(alloc)}/mo</code>" if alloc is not None else ""
        badge         = _conviction_badge(s.get("conviction", 3))
        personal_line = f"\n💡 <i>{_esc(personal_note)}</i>" if personal_note else ""
        return (
            f"<b>{_esc(s.get('ticker'))}</b>  {_stars(s.get('conviction', 3))}{badge}  "
            f"<i>{_esc(_short_company(s.get('company', '')))}</i>\n"
            f"<code>${_p(entry)}</code> → <code>${_p(target)}</code>  "
            f"<i>{_upside(entry, target)}</i>  ·  {_esc(s.get('horizon'))}{alloc_str}\n"
            f"<i>{_esc(s.get('thesis'))}</i>{personal_line}"
        )

    def _pick_row_cst(i, c, personal_note: str = ""):
        entry, target, stop = c.get("entry_price"), c.get("target_price"), c.get("stop_loss")
        alloc         = c.get("allocation")
        alloc_str     = f"  <code>${_p(alloc)}</code>" if alloc is not None else ""
        badge         = _conviction_badge(c.get("conviction", 3))
        personal_line = f"\n💡 <i>{_esc(personal_note)}</i>" if personal_note else ""
        return (
            f"<b>{_esc(c.get('symbol'))}</b>  {_stars(c.get('conviction', 3))}{badge}  "
            f"<i>{_esc(_short_company(c.get('name', '')))}</i>\n"
            f"<code>${_p(entry)}</code> → <code>${_p(target)}</code>  "
            f"<i>{_upside(entry, target)}</i>  ·  stop <code>${_p(stop)}</code>{alloc_str}\n"
            f"<i>{_esc(c.get('thesis'))}</i>{personal_line}"
        )

    def _pick_row_clt(i, c, personal_note: str = ""):
        entry, target = c.get("entry_price"), c.get("target_price")
        alloc         = c.get("allocation")
        alloc_str     = f"  <code>${_p(alloc)}/mo</code>" if alloc is not None else ""
        badge         = _conviction_badge(c.get("conviction", 3))
        personal_line = f"\n💡 <i>{_esc(personal_note)}</i>" if personal_note else ""
        return (
            f"<b>{_esc(c.get('symbol'))}</b>  {_stars(c.get('conviction', 3))}{badge}  "
            f"<i>{_esc(_short_company(c.get('name', '')))}</i>\n"
            f"<code>${_p(entry)}</code> → <code>${_p(target)}</code>  "
            f"<i>{_upside(entry, target)}</i>  ·  {_esc(c.get('horizon'))}{alloc_str}\n"
            f"<i>{_esc(c.get('thesis'))}</i>{personal_line}"
        )

    pn = personal_notes or {}   # ticker/symbol → personal note string

    if st_picks:
        budget_tag = f"  <code>${per_stock}/pick</code>" if per_stock else ""
        body = "\n\n".join(
            _pick_row_st(i, s, pn.get(s.get("ticker", ""), ""))
            for i, s in enumerate(st_picks, 1)
        )
        lines += ["", f"<blockquote expandable>📈 <b>STOCK — SHORT TERM</b>{budget_tag}\n\n{body}</blockquote>"]

    if lt_picks:
        budget_tag = f"  <code>${per_stock}/pick</code>" if per_stock else ""
        body = "\n\n".join(
            _pick_row_lt(i, s, pn.get(s.get("ticker", ""), ""))
            for i, s in enumerate(lt_picks, 1)
        )
        lines += ["", f"<blockquote expandable>🏦 <b>STOCK — LONG TERM</b>{budget_tag}\n\n{body}</blockquote>"]

    if cst_picks:
        budget_tag = f"  <code>${per_crypto}/pick</code>" if per_crypto else ""
        body = "\n\n".join(
            _pick_row_cst(i, c, pn.get(c.get("symbol", ""), ""))
            for i, c in enumerate(cst_picks, 1)
        )
        lines += ["", f"<blockquote expandable>🪙 <b>CRYPTO — SHORT TERM</b>{budget_tag}  ⚡ HIGH RISK\n\n{body}</blockquote>"]

    if clt_picks:
        budget_tag = f"  <code>${per_crypto}/pick</code>" if per_crypto else ""
        body = "\n\n".join(
            _pick_row_clt(i, c, pn.get(c.get("symbol", ""), ""))
            for i, c in enumerate(clt_picks, 1)
        )
        lines += ["", f"<blockquote expandable>💎 <b>CRYPTO — LONG TERM</b>{budget_tag}\n\n{body}</blockquote>"]

    # Footer — sector diversity line
    seen_sectors: set = set()
    sector_list: list = []
    for p in st_picks + lt_picks:
        s = p.get("sector", "")
        if s and s != "Unknown" and s not in seen_sectors:
            sector_list.append(s)
            seen_sectors.add(s)
    sector_line = f"🏭 <i>Sectors: {_esc(', '.join(sector_list))}</i>" if sector_list else ""

    lines += ["", "⚠️ <i>Not financial advice.</i>  📋 /help  ·  📲 /share"]
    if sector_line:
        lines.append(sector_line)

    return "\n".join(lines)


# ── Confirmation message (10:30 AM run) ──────────────────────────────────────

def format_confirmation_message(picks: dict, current_prices: dict) -> str:
    """
    Build the 10:30 AM check-in message.
    Compares entry prices from morning picks to current live prices.
    """
    now    = date.today().strftime("%a %b %d")
    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})

    def price_line(symbol: str, entry, target, stop) -> str:
        current = current_prices.get(symbol)
        if current is None or entry is None:
            return f"   <b>{symbol}</b>  price unavailable"
        pct   = (current - float(entry)) / float(entry) * 100
        arrow = "▲" if pct >= 0 else "▼"
        if stop and current <= float(stop):
            badge = "🔴 STOP HIT"
        elif target and pct >= (float(target) - float(entry)) / float(entry) * 100 * 0.5:
            badge = "✅ On track"
        elif pct < -2:
            badge = "⚠️ Watch"
        else:
            badge = "🟡 Neutral"
        return (f"   <b>{symbol}</b>  <code>${_p(entry)}</code> → <code>${_p(current)}</code> "
                f"{arrow}{abs(pct):.1f}%  {badge}")

    st  = stocks.get("short_term", [])
    lt  = stocks.get("long_term", [])
    cst = crypto.get("short_term", [])
    clt = crypto.get("long_term", [])

    lines = [f"<b>🕙 10:30 AM ET Check — {now}</b>"]

    if st:
        lines += ["", "<b>📈 Short Term</b>"]
        for s in st:
            lines.append(price_line(s.get("ticker", ""), s.get("entry_price"), s.get("target_price"), s.get("stop_loss")))
    if lt:
        lines += ["", "<b>🏦 Long Term</b>"]
        for s in lt:
            lines.append(price_line(s.get("ticker", ""), s.get("entry_price"), s.get("target_price"), None))
    if cst:
        lines += ["", "<b>🪙 Crypto Short Term</b>"]
        for c in cst:
            lines.append(price_line(c.get("symbol", ""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss")))
    if clt:
        lines += ["", "<b>💎 Crypto Long Term</b>"]
        for c in clt:
            lines.append(price_line(c.get("symbol", ""), c.get("entry_price"), c.get("target_price"), None))

    lines += ["", "🔴 exit  ✅ hold  ⚠️ watch  🟡 wait", "<i>⚠️ Not financial advice.</i>  📋 /help  ·  📲 /share"]
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
            vs       = round(avg - spy, 1)
            vs_sign  = "+" if vs  >= 0 else ""
            spy_sign = "+" if spy >= 0 else ""
            bench = f" vs S&P {spy_sign}{spy}% ({vs_sign}{vs}%)"
        return [
            f"<b>{label}</b> — {stats['count']} picks, {win_pct}% wins",
            f"Best: <b>{best_sym}</b> {best_sign}{best_r}%  Worst: <b>{worst_sym}</b> {worst_sign}{worst_r}%",
            f"Avg: {sign}{avg}%{bench} {emoji}",
        ]

    pick_mode    = (config or {}).get("pick_mode", "both")
    show_stocks  = pick_mode in ("st", "lt", "both")   # always show stocks unless explicitly off
    show_crypto  = pick_mode in ("st", "lt", "both")

    lines = [f"<b>📅 Week of {week_end} — Recap</b>", ""]

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

    lines += [
        "",
        "<i>Entry vs Friday close — not actual trade results.</i>",
        "<i>⚠️ Not financial advice.</i>",
    ]
    return "\n".join(lines)
