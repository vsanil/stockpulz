"""
formatters.py — Message formatting helpers for the stock advisor bot.

Contains all pure formatting functions (no Telegram API calls, no command parsing).
Imported by telegram_notifier.py, agent.py (via telegram_notifier re-exports), and
any other module that needs to build a message string.
"""
from __future__ import annotations

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
    """Format a price with magnitude-appropriate decimal precision.

    Stocks / large crypto : 2 decimals  ($610.26, $104,231.50)
    Mid crypto (≥$0.01)  : 4 decimals  ($0.5821)
    Small crypto (≥$0.0001): 6 decimals ($0.001234)
    Sub-penny (SHIB etc) : 8 decimals  ($0.00001234)
    """
    if price is None:
        return "—"
    f = float(price)
    if f >= 10000:
        return f"{f:,.0f}" if f == int(f) else f"{f:,.2f}"
    if f >= 1:
        return f"{f:.2f}"
    if f >= 0.01:
        return f"{f:.4f}"
    if f >= 0.0001:
        return f"{f:.6f}"
    return f"{f:.8f}"


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
        # NEVER fabricate a whole share the budget can't afford (a $50 crypto
        # budget used to show "1 share" of BTC with a $3k risk figure).
        def _qty_label(budget_f: float) -> tuple[float, str] | None:
            """Return (quantity, display) for the budget, or None if a stock
            budget can't afford a single share."""
            if is_crypto:
                qty = budget_f / e
                return qty, f"{qty:.4g} unit{'s' if qty != 1 else ''}"
            whole = int(budget_f / e)
            if whole < 1:
                return None
            return whole, f"{whole} share{'s' if whole != 1 else ''}"

        sizing = ""
        if is_long_term:
            # LT picks have no stop — just show position size if budget set
            if budget:
                q = _qty_label(float(budget))
                sizing = (f"  ·  <code>${int(budget)}</code> → ~{q[1]}" if q else
                          f"  ·  <code>${int(budget)}</code> &lt; 1 share — use fractional or skip")
            return (
                f"⏱ <i>Patient entry — up to <code>${_p(upper)}</code>  "
                f"<b>(+{pct_label})</b>{sizing}</i>"
            )
        else:
            # Short-term: always show risk/share; add position size if budget set
            risk_str = ""
            if stop:
                risk_per_share = round(e - float(stop), 2)
                if risk_per_share > 0:
                    risk_str = f"  ·  risk <b>${_p(risk_per_share)}/share</b>"
                    if budget:
                        q = _qty_label(float(budget))
                        if q:
                            qty, qty_str = q
                            total_risk = round(qty * risk_per_share, 2)
                            risk_str = (
                                f"  ·  <code>${int(budget)}</code> → {qty_str}, "
                                f"${_p(total_risk)} risk at stop"
                            )
                        else:
                            risk_str += (f"  ·  <code>${int(budget)}</code> &lt; 1 share "
                                         f"— use fractional or skip")
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
    if not m or not isinstance(m, dict):
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
                         recent_stats: dict | None = None,
                         earnings_this_week: dict | None = None,
                         held_tickers: set | None = None,
                         watchlist_prices: dict | None = None,
                         user_alerts_map: dict | None = None,
                         market_closed: bool = False,
                         closed_reason: str = "",
                         next_open_label: str = "",
                         company_names: dict | None = None) -> str:
    """
    Concise daily Telegram message — scannable in under 10 seconds.
    Each pick is 2 lines: prices + thesis. Full detail lives in the mini app.
    """
    today        = date.today().strftime("%a %b %d")
    pick_mode    = config.get("pick_mode", "both")
    show_st      = pick_mode in ("st", "both")
    show_lt      = pick_mode in ("lt", "both")
    show_crypto  = config.get("show_crypto", True)
    first_name   = config.get("first_name", "")
    risk_profile = config.get("risk_profile", "moderate")
    watchlist    = [t.upper() for t in (config.get("watchlist") or [])]

    _RISK_LABEL = {
        "conservative": "conservative",
        "moderate":     "moderate",
        "aggressive":   "aggressive",
    }
    risk_lbl = _RISK_LABEL.get(risk_profile, "moderate")

    stocks      = picks.get("stocks", picks)
    crypto      = picks.get("crypto", {})
    etfs        = picks.get("etfs", {})
    commodities = picks.get("commodities", {})

    # ── Watchlist sort helper — float watched tickers to top of each section ──
    def _wl_sort(lst: list, key: str = "ticker") -> list:
        if not watchlist:
            return lst
        return sorted(lst, key=lambda p: (0 if p.get(key, "").upper() in watchlist else 1))

    st_picks    = _wl_sort(stocks.get("short_term",   []) if show_st else [])
    lt_picks    = _wl_sort(stocks.get("long_term",    []) if show_lt else [])
    cst_picks   = _wl_sort(crypto.get("short_term",   []) if show_crypto else [], key="symbol")
    etf_picks   = _wl_sort(etfs.get("short_term", []) if show_st else []) + \
                  _wl_sort([{**e, "_lt": True} for e in etfs.get("long_term", [])] if show_lt else [])
    comm_picks  = _wl_sort(
        (commodities.get("short_term", []) if show_st else []) +
        ([{**c, "_lt": True} for c in commodities.get("long_term", [])] if show_lt else [])
    )
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

    greeting = f"Good morning, {_esc(first_name)}! 👋\n" if first_name else ""
    lines = [f"{greeting}{mood_emoji} <b>{today}</b>"]
    if mood:
        lines.append(f"<i>{mood}</i>")

    macro_line = _macro_narrative_line(picks.get("macro_context", {}))
    if macro_line:
        lines.append(macro_line)

    # Performance bar — only when there's real data
    if recent_stats and recent_stats.get("total"):
        t  = recent_stats["total"]
        spy = recent_stats.get("spy_return")
        _s         = lambda x: "+" if x >= 0 else ""
        _ret_val   = t.get("median_return", t["avg_return"])
        _ret_label = "median" if "median_return" in t else "avg"
        perf = f"📊 <i>{t['wins']}W/{t['losses']}L · {t['win_rate']}% · {_ret_label} {_s(_ret_val)}{_ret_val}%"
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

    def _clean_thesis(thesis: str) -> str:
        """Strip mechanical signal labels, humanize field names, normalise semicolons → middots."""
        import re as _re
        if not thesis:
            return ""
        # (signal N), (LT signal N), (signal 1 + signal 2) — number after word
        t = _re.sub(r'\s*\(\s*(?:LT )?signal \d+(?:\s*\+\s*(?:LT )?signal \d+)*\s*\)', '', thesis)
        # (1 signal), (2 signals) — number before word
        t = _re.sub(r'\s*\(\s*\d+\s*signals?\s*\)', '', t)
        # Humanize known snake_case field names leaked from AI output
        _snake = {
            'bullish_engulfing': 'bullish engulfing',
            'bearish_engulfing': 'bearish engulfing',
            'bullish_flow':      'bullish options flow',
            'bearish_flow':      'bearish options flow',
            'ret_1m':            '1M return',
            'ret_3m':            '3M return',
            'vol_ratio':         'volume ratio',
            'price_vs_52w':      'vs 52W high',
            'rs_vs_spy':         'RS vs SPY',
        }
        for raw, human in _snake.items():
            t = t.replace(raw, human)
        t = _re.sub(r';\s+', ' · ', t)
        return t.strip().rstrip('.')

    def _tline(thesis, catalyst):
        parts = [_esc(_clean_thesis(thesis))] if thesis else []
        if catalyst:
            parts.append(f"🎯 {_esc(catalyst)}")
        combined = "  ·  ".join(parts)
        return f"  {combined}" if combined else ""

    def _theme_tag(s):
        """Return a styled inline theme hashtag, e.g. #AIInfrastructure"""
        import re as _re
        theme = s.get("theme", "")
        if not theme:
            return ""
        # Collapse to hashtag: remove spaces/dashes, title-case each word
        tag = "#" + "".join(w.capitalize() for w in _re.split(r'[\s\-]+', theme.strip()) if w)
        return f"  <code>{_esc(tag)}</code>"

    def _name_tag(ticker: str) -> str:
        """Return italic company name suffix if available, e.g. '  <i>· Apple</i>'"""
        if not company_names:
            return ""
        n = company_names.get(ticker.upper(), "")
        # Skip if name is same as ticker (e.g. XRP → XRP is redundant)
        if not n or n.upper() == ticker.upper():
            return ""
        return f"  <i>· {_esc(n)}</i>"

    def _row_st(s):
        t, e, tgt, stop = s.get("ticker",""), s.get("entry_price"), s.get("target_price"), s.get("stop_loss")
        stop_str = f"  ·  stop <code>${_p(stop)}</code>" if stop else ""
        held_badge = "  📌 <i>holding</i>" if held_tickers and t.upper() in held_tickers else ""
        row = (f"<b>{_esc(t)}</b>{_name_tag(t)}{_conv_tag(s.get('conviction',3))}{held_badge}  "
               f"<code>${_p(e)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(e,tgt)}</i>{stop_str}{_theme_tag(s)}")
        tl = _tline(s.get("thesis"), s.get("catalyst"))
        if tl: row += f"\n{tl}"
        edge = s.get("edge", "")
        if edge:
            row += f"\n  Edge: {_esc(edge)}"
        row += _iv_warn(s)
        ew = _entry_window(e, stop, budget=config.get("stock_budget"), is_long_term=False, is_crypto=False)
        if ew: row += f"\n{ew}"
        return row

    def _row_lt(s):
        t, e, tgt = s.get("ticker",""), s.get("entry_price"), s.get("target_price")
        hz = f"  ·  <i>{_esc(s.get('horizon',''))}</i>" if s.get("horizon") else ""
        held_badge = "  📌 <i>holding</i>" if held_tickers and t.upper() in held_tickers else ""
        row = (f"<b>{_esc(t)}</b>{_name_tag(t)}{_conv_tag(s.get('conviction',3))}{held_badge}  "
               f"<code>${_p(e)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(e,tgt)}</i>{hz}{_theme_tag(s)}")
        tl = _tline(s.get("thesis"), s.get("catalyst"))
        if tl: row += f"\n{tl}"
        edge = s.get("edge", "")
        if edge:
            row += f"\n  Edge: {_esc(edge)}"
        ew = _entry_window(e, None, budget=config.get("stock_budget"), is_long_term=True, is_crypto=False)
        if ew: row += f"\n{ew}"
        return row

    def _row_crypto(c):
        sym, e, tgt, stop = c.get("symbol",""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss")
        stop_str = f"  ·  stop <code>${_p(stop)}</code>" if stop else ""
        row = (f"<b>{_esc(sym)}</b>{_name_tag(sym)}{_conv_tag(c.get('conviction',3))}  "
               f"<code>${_p(e)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(e,tgt)}</i>{stop_str}")
        tl = _tline(c.get("thesis"), c.get("catalyst"))
        if tl: row += f"\n{tl}"
        row += _iv_warn(c)
        ew = _entry_window(e, stop, budget=config.get("crypto_budget"), is_long_term=False, is_crypto=True)
        if ew: row += f"\n{ew}"
        return row

    def _row_etf(e):
        t, ep, tgt, stop = e.get("ticker",""), e.get("entry_price"), e.get("target_price"), e.get("stop_loss")
        is_lt = e.get("_lt", False)
        stop_str = f"  ·  stop <code>${_p(stop)}</code>" if stop and not is_lt else ""
        hz = f"  ·  <i>{_esc(e.get('horizon',''))}</i>" if is_lt and e.get("horizon") else ""
        row = (f"<b>{_esc(t)}</b>{_name_tag(t)}  "
               f"<code>${_p(ep)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(ep,tgt)}</i>{stop_str}{hz}")
        tl = _tline(e.get("thesis"), e.get("catalyst"))
        if tl: row += f"\n{tl}"
        return row

    def _row_commodity(c):
        t, ep, tgt, stop = c.get("ticker",""), c.get("entry_price"), c.get("target_price"), c.get("stop_loss")
        is_lt = c.get("_lt", False)
        stop_str = f"  ·  stop <code>${_p(stop)}</code>" if stop and not is_lt else ""
        row = (f"<b>{_esc(t)}</b>{_name_tag(t)}  "
               f"<code>${_p(ep)}</code> → <code>${_p(tgt)}</code>  "
               f"<i>{_upside(ep,tgt)}</i>{stop_str}")
        tl = _tline(c.get("thesis"), c.get("catalyst"))
        if tl: row += f"\n{tl}"
        return row

    def _row_options(o):
        t, action = o.get("ticker",""), o.get("action","CALL").upper()
        icon = "📈" if action == "CALL" else "📉"
        row = f"{icon} <b>{_esc(t)}</b>  {_esc(action)}"
        strike, expiry = o.get("strike"), o.get("expiry","")
        if strike:
            row += f"  strike <code>${_p(strike)}</code>"
        if expiry:
            row += f"  exp <i>{_esc(expiry)}</i>"
        if o.get("note"): row += f"\n  <i>{_esc(o['note'])}</i>"
        return row

    # ── Market closed banner — shown once before sections ────────────────────
    if market_closed:
        _closed_base = f"🏖️ US markets closed — {closed_reason}" if closed_reason else "🏖️ US markets closed"
        _closed_banner = f"{_closed_base} · fresh picks at 7AM ET on {next_open_label}" if next_open_label else _closed_base
        lines += ["", f"<i>{_closed_banner}</i>"]
    _closed_short = "closed" if market_closed else ""

    # ── Sections — each pick is its own expandable blockquote ─────────────────
    _near_misses = (picks.get("stocks") or {}).get("near_misses") or {}

    if st_picks:
        lines += ["", "📈 <b>STOCKS — SHORT TERM</b>"]
        for s in st_picks:
            lines += [f"<blockquote expandable>{_row_st(s)}</blockquote>"]
    elif show_st:
        _st_skip = _closed_short or "no qualifying setup today"
        lines += ["", f"📈 <b>STOCKS — SHORT TERM</b>  <i>· {_st_skip}</i>"]
        _nm_st = _near_misses.get("short_term") or []
        if _nm_st and not _closed_short:
            lines += ["<i>Near misses — didn't clear the bar:</i>"]
            for nm in _nm_st[:3]:
                tk    = nm.get("ticker", "")
                price = nm.get("current_price")
                score = nm.get("score", 0)
                why   = nm.get("near_miss_reason", "close to threshold")
                p_str = f"  ${price:,.2f}" if price else ""
                lines += [f"  <code>{tk}</code>{p_str}  score {score:.0f}/100  · <i>{why}</i>"]

    if lt_picks:
        lines += ["", "🏦 <b>STOCKS — LONG TERM</b>"]
        for s in lt_picks:
            lines += [f"<blockquote expandable>{_row_lt(s)}</blockquote>"]
    elif show_lt:
        _lt_skip = _closed_short or "no stock cleared the quality bar today"
        lines += ["", f"🏦 <b>STOCKS — LONG TERM</b>  <i>· {_lt_skip}</i>"]
        _nm_lt = _near_misses.get("long_term") or []
        if _nm_lt and not _closed_short:
            lines += ["<i>Near misses — didn't clear the bar:</i>"]
            for nm in _nm_lt[:3]:
                tk    = nm.get("ticker", "")
                price = nm.get("current_price")
                score = nm.get("score", 0)
                why   = nm.get("near_miss_reason", "close to threshold")
                p_str = f"  ${price:,.2f}" if price else ""
                lines += [f"  <code>{tk}</code>{p_str}  score {score:.0f}/100  · <i>{why}</i>"]

    if cst_picks:
        lines += ["", "🪙 <b>CRYPTO</b>  <i>· high risk</i>"]
        for c in cst_picks:
            lines += [f"<blockquote expandable>{_row_crypto(c)}</blockquote>"]
    elif show_crypto:
        lines += ["", "🪙 <b>CRYPTO</b>  <i>· no setup today</i>"]

    if etf_picks:
        lines += ["", "📦 <b>ETFs</b>"]
        for e in etf_picks:
            lines += [f"<blockquote expandable>{_row_etf(e)}</blockquote>"]
    else:
        _etf_skip = _closed_short or "no alignment with current regime"
        lines += ["", f"📦 <b>ETFs</b>  <i>· {_etf_skip}</i>"]

    if comm_picks:
        lines += ["", "🛢 <b>COMMODITIES</b>"]
        for c in comm_picks:
            lines += [f"<blockquote expandable>{_row_commodity(c)}</blockquote>"]
    else:
        _comm_skip = _closed_short or "no setup today"
        lines += ["", f"🛢 <b>COMMODITIES</b>  <i>· {_comm_skip}</i>"]

    if options_plays:
        lines += ["", "🎯 <b>OPTIONS</b>  <i>· illustrative only</i>"]
        for o in options_plays:
            lines += [f"<blockquote expandable>{_row_options(o)}</blockquote>"]
    elif st_picks:
        lines += ["", "🎯 <b>OPTIONS</b>  <i>· no qualifying conviction today</i>"]
    elif show_st and not _closed_short:
        lines += ["", "🎯 <b>OPTIONS</b>  <i>· no underlying stock setups today</i>"]

    # ── Watchlist: hit callout + "On Your Radar" section ─────────────────────
    if watchlist:
        all_pick_tickers = set(
            [s.get("ticker","").upper() for s in st_picks + lt_picks] +
            [c.get("symbol","").upper() for c in cst_picks] +
            [e.get("ticker","").upper() for e in etf_picks] +
            [c.get("ticker","").upper() for c in comm_picks]
        )
        wl_in_picks     = [t for t in watchlist if t in all_pick_tickers]
        wl_not_in_picks = [t for t in watchlist if t not in all_pick_tickers]

        # Celebrate watchlist hits (ticker already appears in picks above — no duplicate)
        if wl_in_picks:
            tickers_str = ", ".join(f"<b>{t}</b>" for t in wl_in_picks)
            lines += ["", f"⭐ <i>Watchlist hit: {tickers_str} made today's picks!</i>"]

        # Show live price/change for watched-but-not-picked tickers
        if wl_not_in_picks:
            radar_rows = []
            for t in wl_not_in_picks[:8]:   # cap at 8 to keep message compact
                data     = (watchlist_prices or {}).get(t)
                alerts   = (user_alerts_map  or {}).get(t, [])
                cur_price = data.get("price") if data else None

                if cur_price:
                    price_str = f"<code>${_p(cur_price)}</code>"
                    chg = data.get("change_pct")
                    if chg is not None:
                        sign  = "+" if chg >= 0 else ""
                        color = "📈" if chg >= 0 else "📉"
                        chg_str = f"  {color} <i>{sign}{chg:.1f}%</i>"
                    else:
                        chg_str = ""

                    # Alert proximity — pick the closest active alert
                    alert_str = ""
                    if alerts and cur_price:
                        closest = min(alerts, key=lambda a: abs(a["target"] - cur_price))
                        tgt     = closest["target"]
                        pct_away = (tgt - cur_price) / cur_price * 100
                        sign_a   = "+" if pct_away >= 0 else ""
                        alert_str = f"  🔔 <i>alert ${_p(tgt)} ({sign_a}{pct_away:.1f}%)</i>"

                    radar_rows.append(f"  <b>{_esc(t)}</b>  {price_str}{chg_str}{alert_str}")
                else:
                    radar_rows.append(f"  <b>{_esc(t)}</b>  <i>· not in today's picks</i>")

            if radar_rows:
                lines += ["", "👁 <b>On Your Radar</b>  <i>(watchlist — not in today's picks)</i>"]
                lines += radar_rows

    # ── One-time sector filter discovery tip ──────────────────────────────────
    # Show only when: user has no excluded sectors AND has received 3+ alerts
    excluded_sectors = config.get("excluded_sectors") or []
    alerts_sent      = config.get("alerts_sent_count", 0)
    if not excluded_sectors and alerts_sent == 3:
        lines += ["", "💡 <i>Don't want picks from certain sectors? Tap ⚙️ Settings → Preferences → Excluded Sectors in the dashboard to filter them out.</i>"]

    # ── Earnings this week (across today's picks) ─────────────────────────────
    if earnings_this_week:
        e_lines = []
        for ticker, date_str in sorted(earnings_this_week.items(), key=lambda x: x[1])[:6]:
            e_lines.append(f"  📅 <b>{ticker}</b> — {date_str}")
        lines += ["", "🗓 <b>Earnings This Week</b>  <i>(today's picks)</i>"]
        lines += e_lines

    # ── Footer ────────────────────────────────────────────────────────────────
    _app_url = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("APP_URL") or "").rstrip("/")
    _paper_cta = (
        "tap <b>📄 Paper Trade</b> below"
        if _app_url
        else "use <code>/paper_buy TICKER SHARES</code>"
    )

    footer_lines = [
        "",
        "💡 <i>Bought one of these? Tap to log it — you'll get stop-loss &amp; target alerts, pre-market gap warnings, earnings heads-ups, and P&amp;L tracking vs SPY. All automated.</i>",
        "",
        f"📄 <i>Not ready to commit real money? {_paper_cta} to simulate risk-free. Check performance with</i> /paper_portfolio",
    ]

    # Show /missed only on weekends (when market is closed or it's Sat/Sun)
    _is_weekend = market_closed or datetime.now(pytz.timezone("America/New_York")).weekday() >= 5
    if _is_weekend:
        footer_lines.append("📊 <i>Curious what you skipped this week?</i> /missed")

    footer_lines += [
        "",
        "⚠️ <i>Not financial advice. Open the dashboard for full analysis &amp; charts.</i>",
    ]

    lines += footer_lines

    return "\n".join(lines)


# ── Quick-buy keyboard for morning picks ─────────────────────────────────────

def build_picks_keyboard(picks: dict, config: dict | None = None) -> list[list[dict]]:
    """
    Build an inline keyboard for the morning picks message.
    Returns one '📌 Log TICKER' button per pick — tapping opens the mini app
    pre-filled to log the position and auto-create stop-loss / target alerts.
    """
    cfg         = config or {}
    show_crypto = cfg.get("show_crypto", True)

    stocks = picks.get("stocks", picks)
    crypto = picks.get("crypto", {})
    etfs   = picks.get("etfs", {})

    # Hoist here so _pair can close over it
    render_url = (os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("APP_URL") or "").rstrip("/")

    def _header(label: str) -> list[dict]:
        return [{"text": label, "callback_data": "noop"}]

    def _buy_callback(pick: dict, asset_type: str) -> str:
        """Build the buy_pick callback_data string for a pick."""
        ticker = (pick.get("ticker") or pick.get("symbol") or "").upper()
        entry  = pick.get("entry_price")
        stop   = pick.get("stop_loss")
        target = pick.get("target_price")

        # Shares from budget — crypto is fractional; never log a whole coin
        # the budget can't afford (a $50 budget must not log 1 BTC)
        budget_key = "crypto_budget" if asset_type == "crypto" else "stock_budget"
        budget = cfg.get(budget_key)
        if budget and entry:
            if asset_type == "crypto":
                shares = round(float(budget) / float(entry), 6)
            else:
                shares = max(1, int(float(budget) / float(entry)))
        else:
            shares = 1

        # Percentages — rounded to 1 decimal
        try:
            stop_pct   = round((float(entry) - float(stop))   / float(entry) * 100, 1) if (entry and stop)   else 7
            target_pct = round((float(target) - float(entry)) / float(entry) * 100, 1) if (entry and target) else 15
        except Exception:
            stop_pct, target_pct = 7, 15

        entry_str = f"{float(entry):.2f}" if entry else "0"
        return f"buy_pick|{ticker}|{entry_str}|{shares}|{asset_type}|{stop_pct}|{target_pct}"

    def _log_btn(pick: dict, asset_type: str) -> list[dict]:
        """Return [Log TICKER, 📡 Alert at $X] buttons for one pick."""
        ticker = (pick.get("ticker") or pick.get("symbol") or "").upper()
        entry  = pick.get("entry_price")  or ""
        stop   = pick.get("stop_loss")    or ""
        target = pick.get("target_price") or ""

        if render_url:
            log_btn = {"text": f"📌 Log {ticker}", "web_app": {
                "url": (f"{render_url}/miniapp?tab=portfolio&action=add"
                        f"&ticker={ticker}&asset_type={asset_type}"
                        f"&entry={entry}&stop={stop}&target={target}")
            }}
        else:
            log_btn = {"text": f"📌 Log {ticker}", "callback_data": _buy_callback(pick, asset_type)}

        row = [log_btn]
        if entry:
            try:
                price_str = f"${float(entry):,.0f}" if float(entry) >= 100 else f"${float(entry):.2f}"
                row.append({"text": f"📡 Alert at {price_str}",
                             "callback_data": f"entry_alert|{ticker}|{entry}"})
            except Exception:
                pass
        return row

    def _add_section(picks_list: list, get_sym, asset_type: str, header: str, icon: str = ""):
        """
        Append a section header + one '📌 Log TICKER' button per pick.
        Single pick: skip the header row.
        """
        if not picks_list:
            return
        if len(picks_list) == 1:
            buttons.append(_log_btn(picks_list[0], asset_type))
            return
        buttons.append(_header(header))
        for p in picks_list:
            buttons.append(_log_btn(p, asset_type))

    buttons = []

    # ── Mini App launch buttons — top row ────────────────────────────────────
    if render_url:
        buttons.append([
            {"text": "🚀 Open Dashboard  ↗", "web_app": {"url": f"{render_url}/miniapp"}},
            {"text": "📄 Paper Trade  ↗",    "web_app": {"url": f"{render_url}/miniapp?tab=portfolio&mode=paper"}},
        ])

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
    _add_section(cst_picks, lambda c: c["symbol"], "crypto", "── 🪙 Crypto ──",      "✅")
    _add_section(etf_picks, lambda e: e["ticker"], "stock",  "── 📦 ETFs ──",        "📦")
    _add_section(comm_picks, lambda c: c["ticker"], "stock", "── 🛢 Commodities ──", "🛢")

    return buttons


# ── Confirmation message ──────────────────────────────────────────────────────

def format_confirmation_message(picks: dict, current_prices: dict,
                                buy_counts: dict | None = None,
                                company_names: dict | None = None) -> str:
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
        name_sfx = ""
        if company_names:
            n = company_names.get(symbol.upper(), "")
            if n:
                name_sfx = f" <i>· {_esc(n)}</i>"
        if current is None or entry is None:
            return f"   <b>{symbol}</b>{name_sfx}  price unavailable"
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
        return (f"   <b>{symbol}</b>{name_sfx}  <code>${_p(entry)}</code> → <code>${_p(current)}</code> "
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

    lines += ["", "🔴 stop hit  ✅ on track  ⚠️ watch  🟡 flat", "<i>⚠️ Not financial advice.</i>"]
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
        pick_word = "pick" if stats['count'] == 1 else "picks"
        return [
            f"<b>{label}</b> — {stats['count']} {pick_word}, {win_pct}% wins",
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
        lines += _section("💎 Crypto", crypto_stats)
    elif show_crypto:
        lines += ["💎 Crypto: no data this week"]

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
        if target and price > float(target):
            badges.append("target exceeded ✅")
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

    # ── Portfolio P&L summary ─────────────────────────────────────────────────
    # Show the user's open-position P&L using today's closing prices.
    all_holdings = list(open_holdings)  # already passed in
    port_rows = []
    for h in all_holdings:
        tk    = h.get("ticker")
        entry = h.get("entry_price")
        price = current_prices.get(tk) if tk else None
        if not (tk and entry and price):
            continue
        try:
            pct = (float(price) - float(entry)) / float(entry) * 100
            qty = float(h.get("shares") or 0)
            pnl_usd = (float(price) - float(entry)) * qty if qty else None
            port_rows.append((tk, pct, pnl_usd))
        except Exception:
            continue

    if port_rows:
        total_pnl     = sum(r[2] for r in port_rows if r[2] is not None)
        gainers       = [r for r in port_rows if r[1] > 0]
        losers        = [r for r in port_rows if r[1] < 0]
        avg_port_pct  = sum(r[1] for r in port_rows) / len(port_rows)
        sign          = "+" if avg_port_pct >= 0 else ""
        pnl_sign      = "+" if total_pnl >= 0 else "-"
        pnl_emoji     = "📈" if avg_port_pct >= 0 else "📉"

        # The math is vs ENTRY, not vs yesterday's close — label it honestly.
        # If some positions couldn't be priced, say so instead of presenting
        # a partial average as the whole portfolio.
        n_open    = len([h for h in all_holdings if h.get("ticker") and h.get("entry_price")])
        coverage  = (f"  <i>({len(port_rows)} of {n_open} positions priced)</i>"
                     if len(port_rows) < n_open else "")

        lines.append("")
        lines.append(f"<b>{pnl_emoji} Open positions vs entry</b>{coverage}")
        lines.append(
            f"Avg: <b>{sign}{avg_port_pct:.1f}%</b>  "
            f"·  {len(gainers)} up  ·  {len(losers)} down"
        )
        if total_pnl != 0:
            lines.append(
                f"Est. gain/loss: <b>{pnl_sign}${abs(total_pnl):,.2f}</b>"
            )

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
        lines.append("<i>None of today's picks have earnings this week.</i>")

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
