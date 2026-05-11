"""
formatters.py — Message formatting helpers for the stock advisor bot.

Contains all pure formatting functions (no Telegram API calls, no command parsing).
Imported by telegram_notifier.py, agent.py (via telegram_notifier re-exports), and
any other module that needs to build a message string.
"""

import html
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

def _ordinal(n: int) -> str:
    """Return '2nd', '3rd', '4th' etc. for streak labels."""
    if 11 <= n % 100 <= 13:
        return f"{n}th"
    return f"{n}{['th', 'st', 'nd', 'rd', 'th'][min(n % 10, 4)]}"


def format_daily_message(picks: dict, config: dict,
                         personal_notes: dict | None = None,
                         pick_streaks: dict | None = None,
                         buy_counts: dict | None = None) -> str:
    """
    Build the formatted daily Telegram message from Claude picks (stocks + crypto).

    personal_notes (optional): dict mapping ticker/symbol → one-line personalised note.
      e.g. {"AAPL": "Balances your tech-heavy portfolio with value", "BTC": "Complements your ETH long"}
      When provided, each pick gets a 💡 line showing why this pick suits the user's portfolio.

    pick_streaks (optional): dict mapping ticker/symbol → consecutive-day count (≥ 2).
      e.g. {"AAPL": 3, "BTC": 2}
      When provided, picks that recur on consecutive days show a ⚡ N-day badge.
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
    # Merge ST + LT crypto — tag LT picks, deduplicate by symbol (ST wins)
    if show_crypto:
        _cst = crypto.get("short_term", [])
        _clt = [{**c, "_lt": True} for c in crypto.get("long_term", [])]
        _seen: set = set()
        cst_picks = []
        for c in _cst + _clt:
            sym = c.get("symbol", "")
            if sym not in _seen:
                _seen.add(sym)
                cst_picks.append(c)
    else:
        cst_picks = []

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
        cst_picks = cst_picks[:max_c]

    # Macro context line — conversational narrative
    m = picks.get("macro_context", {})
    macro_line = _macro_narrative_line(m)

    lines = [
        f"<u><b>📊 Daily Picks — {today}  <i>· NYSE 9:30 AM ET</i></b></u>",
        "",
        f"<i>{_esc(picks.get('daily_summary', ''))}</i>",
    ]
    if macro_line:
        lines.append(macro_line)

    def _buy_badge(ticker: str) -> str:
        """Return '👥 N members bought' badge when count ≥ 2, else empty string."""
        n = bc.get(ticker, 0)
        return f"  <i>👥 {n} bought</i>" if n >= 2 else ""

    def _pick_row_st(i, s, personal_note: str = "", streak: int = 0):
        entry, target, stop = s.get("entry_price"), s.get("target_price"), s.get("stop_loss")
        ticker        = s.get("ticker", "")
        earnings_tag  = f"  🗓 {_esc(s['earnings_date'])}" if s.get("earnings_date") else ""
        alloc         = s.get("allocation")
        alloc_str     = f"  <code>${_p(alloc)}</code>" if alloc is not None else ""
        badge         = _conviction_badge(s.get("conviction", 3))
        streak_badge  = f"  ⚡ <i>{_ordinal(streak)} day</i>" if streak >= 2 else ""
        social_badge  = _buy_badge(ticker)
        personal_line = f"\n💡 <i>{_esc(personal_note)}</i>" if personal_note else ""
        window        = _entry_window(entry, stop=stop, budget=per_stock,
                                       is_long_term=False, is_crypto=False)
        window_line   = f"\n{window}" if window else ""
        return (
            f"<b>{_esc(ticker)}</b>  {_stars(s.get('conviction', 3))}{badge}{streak_badge}{social_badge}  "
            f"<i>{_esc(_short_company(s.get('company', '')))}</i>{earnings_tag}\n"
            f"<code>${_p(entry)}</code> → <code>${_p(target)}</code>  "
            f"<i>{_upside(entry, target)}</i>  ·  stop <code>${_p(stop)}</code>{alloc_str}{window_line}\n"
            f"<i>{_esc(s.get('thesis'))}</i>{personal_line}"
        )

    def _pick_row_lt(i, s, personal_note: str = ""):
        entry, target = s.get("entry_price"), s.get("target_price")
        ticker        = s.get("ticker", "")
        alloc         = s.get("allocation")
        alloc_str     = f"  <code>${_p(alloc)}/mo</code>" if alloc is not None else ""
        badge         = _conviction_badge(s.get("conviction", 3))
        social_badge  = _buy_badge(ticker)
        personal_line = f"\n💡 <i>{_esc(personal_note)}</i>" if personal_note else ""
        window        = _entry_window(entry, budget=per_stock,
                                       is_long_term=True, is_crypto=False)
        window_line   = f"\n{window}" if window else ""
        return (
            f"<b>{_esc(ticker)}</b>  {_stars(s.get('conviction', 3))}{badge}{social_badge}  "
            f"<i>{_esc(_short_company(s.get('company', '')))}</i>\n"
            f"<code>${_p(entry)}</code> → <code>${_p(target)}</code>  "
            f"<i>{_upside(entry, target)}</i>  ·  {_esc(s.get('horizon'))}{alloc_str}{window_line}\n"
            f"<i>{_esc(s.get('thesis'))}</i>{personal_line}"
        )

    def _pick_row_cst(i, c, personal_note: str = "", streak: int = 0):
        entry, target, stop = c.get("entry_price"), c.get("target_price"), c.get("stop_loss")
        sym           = c.get("symbol", "")
        is_lt         = c.get("_lt", False)
        alloc         = c.get("allocation")
        alloc_str     = f"  <code>${_p(alloc)}</code>" if alloc is not None else ""
        badge         = _conviction_badge(c.get("conviction", 3))
        streak_badge  = f"  ⚡ <i>{_ordinal(streak)} day</i>" if streak >= 2 else ""
        social_badge  = _buy_badge(sym)
        personal_line = f"\n💡 <i>{_esc(personal_note)}</i>" if personal_note else ""
        lt_label      = "  <i>· long-term</i>" if is_lt else ""
        stop_str      = f"  ·  stop <code>${_p(stop)}</code>" if not is_lt else ""
        window        = _entry_window(entry, stop=stop, budget=per_crypto,
                                       is_long_term=is_lt, is_crypto=True)
        window_line   = f"\n{window}" if window else ""
        return (
            f"<b>{_esc(sym)}</b>  {_stars(c.get('conviction', 3))}{badge}{streak_badge}{social_badge}  "
            f"<i>{_esc(_short_company(c.get('name', '')))}</i>{lt_label}\n"
            f"<code>${_p(entry)}</code> → <code>${_p(target)}</code>  "
            f"<i>{_upside(entry, target)}</i>{stop_str}{alloc_str}{window_line}\n"
            f"<i>{_esc(c.get('thesis'))}</i>{personal_line}"
        )

    pn = personal_notes or {}   # ticker/symbol → personal note string
    ps = pick_streaks   or {}   # ticker/symbol → consecutive-day count
    bc = buy_counts     or {}   # ticker/symbol → number of members who bought today

    if st_picks:
        budget_tag = f"  <code>${per_stock}/pick</code>" if per_stock else ""
        body = "\n\n".join(
            _pick_row_st(i, s, pn.get(s.get("ticker", ""), ""), ps.get(s.get("ticker", ""), 0))
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
            _pick_row_cst(i, c, pn.get(c.get("symbol", ""), ""), ps.get(c.get("symbol", ""), 0))
            for i, c in enumerate(cst_picks, 1)
        )
        lines += ["", f"<blockquote expandable>🪙 <b>CRYPTO</b>{budget_tag}  ⚡ HIGH RISK\n\n{body}</blockquote>"]

    # Footer — sector concentration warning (only shown when picks are concentrated)
    sector_counts: dict = {}
    total_picks = len(st_picks) + len(lt_picks)
    for p in st_picks + lt_picks:
        s = p.get("sector", "")
        if s and s != "Unknown":
            sector_counts[s] = sector_counts.get(s, 0) + 1

    concentration_line = ""
    if sector_counts and total_picks >= 2:
        top_sector, top_count = max(sector_counts.items(), key=lambda x: x[1])
        pct = top_count / total_picks
        if pct >= 0.6:   # 60%+ in one sector → warn
            concentration_line = (
                f"⚠️ <i>{top_count} of {total_picks} picks are {_esc(top_sector)} "
                f"— consider sizing down to manage sector risk.</i>"
            )

    lines += [
        "",
        "⚠️ <i>Not financial advice.</i>  📋 /help  ·  📲 /share",
        "<i>💬 Have a question about any pick? Just type it.</i>",
    ]
    if concentration_line:
        lines.append(concentration_line)

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

    def _row(ticker: str, asset_type: str) -> list[dict]:
        return [
            {"text": f"✅ Bought {ticker}", "callback_data": f"quickbuy|{ticker}"},
            {"text": "📊 Chart",            "callback_data": f"chart|{ticker}|{asset_type}"},
        ]

    buttons = []
    for s in stocks.get("short_term", []):
        ticker = s.get("ticker", "")
        if ticker:
            buttons.append(_row(ticker, "stock"))
    for s in stocks.get("long_term", []):
        ticker = s.get("ticker", "")
        if ticker:
            buttons.append(_row(ticker, "stock"))
    if show_crypto:
        for c in crypto.get("short_term", []):
            sym = c.get("symbol", "")
            if sym:
                buttons.append(_row(sym, "crypto"))
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

    lines += ["", "🔴 stop hit  ✅ on track  ⚠️ watch  🟡 flat", "<i>⚠️ Not financial advice.</i>  📋 /help  ·  📲 /share"]
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

    lines += [
        "",
        "<i>Entry vs Friday close — not actual trade results.</i>",
        "<i>⚠️ Not financial advice.</i>",
    ]
    return "\n".join(lines)


# ── EOD portfolio summary (3:30 PM close check) ───────────────────────────────

def format_eod_summary(picks: dict, current_prices: dict, open_holdings: list[dict]) -> str:
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
        return (f"  {emoji} <b>{symbol}</b>  <code>${_p(current)}</code>  "
                f"{change_str}  from <code>${_p(entry)}</code>{badge_str}{lbl}")

    st  = stocks.get("short_term", [])
    lt  = stocks.get("long_term",  [])
    cst = crypto.get("short_term", [])

    if st or lt or cst:
        any_picks = True
        lines.append("<b>Today's picks:</b>")
        for s in st:
            lines.append(_row(s.get("ticker", ""), s.get("entry_price"), s.get("target_price"), s.get("stop_loss")))
        for s in lt:
            lines.append(_row(s.get("ticker", ""), s.get("entry_price"), s.get("target_price"), None, "LT"))
        for c in cst:
            lines.append(_row(c.get("symbol", ""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss"), "crypto"))

    # User's portfolio holdings not already in picks
    pick_symbols = (
        {s.get("ticker") for s in st + lt} |
        {c.get("symbol") for c in cst}
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

    lines += ["", "<i>⚠️ Not financial advice.</i>  📋 /help"]
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
        }.get(r, "")
        if regime_note:
            lines += ["", regime_note]
        if vix:
            lines.append(f"<i>VIX: {vix} — {'elevated fear' if vix > 20 else 'calm'}</i>")

    return "\n".join(lines)
